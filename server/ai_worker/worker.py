# -*- coding: utf-8 -*-
"""开标汇总 AI · 独立常驻 Worker（Python 3.10+ venv）。

启动：
    python -m server.ai_worker.worker          # Linux（supervisor）
    python server/ai_worker/worker.py          # Windows 本地联调

铁律：
- 任务状态、进度、结果全在 PG；Redis 只传可丢的瞬时事件。
- 原子领取用 FOR UPDATE SKIP LOCKED，天然支持多 Worker。
- 每阶段/每文件短事务提交，进度实时可见，崩溃不丢已完成部分。
- 请求路径（3.8 后端）永不 import 本模块，主进程零阻塞。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 让 vendor 的 bidsummary 包可被 import（其父目录需在 sys.path）。
# 必须 append 到末尾，绝不能 insert(0)：包内 config.py / db.py / ai_signal.py 这类
# 通用名若排在最前会遮蔽标准库与第三方模块。历史 bug：signal.py 让 redis / openai
# 内部的 `import signal` 命中 Worker 模块，抛
# "attempted relative import with no known parent package"，三处功能同时失效。
BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.append(str(BASE))

# 兼容两种启动方式：-m server.ai_worker.worker（推荐）/ python server/ai_worker/worker.py
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # 项目根
    __package__ = 'server.ai_worker'

from . import (config, db, ai_signal, materialize, db_writer, schema,        # noqa: E402
               ai_prompt_store, format_sig, feedback)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('taizhang.ai.worker')

# 当前任务的版式指纹，供提示词 hook 检索 few-shot
_CURRENT = {'sig': '', 'versions': set()}

# 进度写入串行化：解析线程池会调用页级回调写进度，而 ai_task_event 上有
# (task_id, seq) 唯一索引 —— 两个线程同时算 MAX(seq)+1 必然撞键。
_STAGE_LOCK = threading.Lock()


# ------------------------------------------------------------------ 进度双写
def _next_seq(task_id):
    r = db.one('SELECT COALESCE(MAX(seq),0)+1 AS n FROM ai_task_event WHERE task_id=%s',
               (task_id,))
    return int(r['n'] or 1)


def set_stage(task_id, stage, progress=None, label=None, **kw):
    """先写 PG（事实源），再 publish Redis（快通知）。"""
    label = label or (config.STAGES.get(stage, ('', 0))[0] or stage)
    if progress is None:
        progress = config.STAGES.get(stage, ('', 0))[1]
    now = time.time()
    cols = ['stage=%s', 'stage_label=%s', 'progress=%s', 'updated_at=%s']
    args = [stage, label, int(progress), now]
    allowed = {'total_files', 'parsed_files', 'failed_files', 'skipped_files',
               'current_file', 'mode', 'prompt_version'}
    for k, v in kw.items():
        if k in allowed:
            cols.append('%s=%%s' % k)
            args.append(v)
    args.append(task_id)
    with _STAGE_LOCK:
        db.x('UPDATE ai_summary_task SET ' + ', '.join(cols) + ' WHERE id=%s', tuple(args))
        seq = _next_seq(task_id)
        payload = {k: v for k, v in kw.items() if k in allowed}
        db.x('INSERT INTO ai_task_event(task_id, seq, stage, progress, payload, created_at) '
             'VALUES(%s,%s,%s,%s,%s,%s)',
             (task_id, seq, stage, int(progress),
              json.dumps(payload, ensure_ascii=False, default=str), now))
        db.commit()
    ai_signal.publish(task_id, {'seq': seq, 'stage': stage, 'progress': int(progress),
                                'stage_label': label, **payload})


def _cancelled(task_id):
    r = db.one('SELECT cancel_requested FROM ai_summary_task WHERE id=%s', (task_id,))
    return bool(r and r['cancel_requested'])


def _fail(task_id, msg, attempts=None):
    safe = _safe_error(msg)
    db.x("UPDATE ai_summary_task SET status='FAILED', error_msg=%s, finished_at=%s, "
         'updated_at=%s WHERE id=%s', (safe[:1000], time.time(), time.time(), task_id))
    db.commit()
    ai_signal.publish(task_id, {'stage': 'FAILED', 'error': safe[:300]})


def _safe_error(msg):
    """异常堆栈脱敏：不打印 API Key、Redis 密码、投标文件内容。"""
    s = str(msg or '')[:2000]
    for secret in (os.environ.get('DASHSCOPE_API_KEY', ''),
                   os.environ.get('DEEPSEEK_API_KEY', ''),
                   os.environ.get('WORKBUDDY_API_KEY', ''),
                   os.environ.get('LLM_API_KEY', ''),
                   os.environ.get('LLM_VISION_API_KEY', ''),
                   os.environ.get('REDIS_PASSWORD', ''),
                   os.environ.get('PG_PASSWORD', '')):
        if secret and len(secret) > 4:
            s = s.replace(secret, '***')
    return '\n'.join(s.splitlines()[:8])


# ------------------------------------------------------------------ 领取 / 回收
def claim_one():
    now = time.time()
    cur = db.x(
        "UPDATE ai_summary_task SET status='RUNNING', claimed_by=%s, started_at=%s, "
        'updated_at=%s, stage=%s, stage_label=%s, progress=2 '
        "WHERE id=(SELECT id FROM ai_summary_task WHERE status='PENDING' "
        'ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *',
        (config.WORKER_ID, now, now, 'SCANNING', 'Worker 已领取'))
    r = cur.fetchone()
    if not r:
        db.commit()
        return None
    seq = _next_seq(r['id'])
    db.x('INSERT INTO ai_task_event(task_id, seq, stage, progress, payload, created_at) '
         "VALUES(%s,%s,'CLAIMED',2,%s,%s)",
         (r['id'], seq, json.dumps({'worker': config.WORKER_ID}), now))
    db.commit()
    return dict(r)


def recover_stale():
    """Worker 启动时回收僵死任务：超过 5 分钟无心跳重置 PENDING，3 次后 FAILED。"""
    cutoff = time.time() - config.HEARTBEAT_STALE
    cur = db.x(
        "UPDATE ai_summary_task SET status='PENDING', attempts=attempts+1, "
        'claimed_by=NULL, updated_at=%s '
        "WHERE status='RUNNING' AND updated_at<%s AND attempts<%s RETURNING id",
        (time.time(), cutoff, config.MAX_ATTEMPTS))
    n = len(cur.fetchall())
    db.x("UPDATE ai_summary_task SET status='FAILED', error_msg=%s, finished_at=%s "
         "WHERE status='RUNNING' AND updated_at<%s AND attempts>=%s",
         ('重试次数超限', time.time(), cutoff, config.MAX_ATTEMPTS))
    db.commit()
    if n:
        log.info('[ai] 回收僵死任务 %d 条', n)
    return n


# ------------------------------------------------------------------ 提示词 hook
def _prompt_hook(scene, base_system, user_prompt):
    """§14 四层拼装：DB active system + 字段契约 + 按 format_sig 检索的 few-shot。

    vision_transcribe 是「忠实转写」场景，不注入契约与样例，避免污染转写内容。
    """
    if scene == 'vision_transcribe':
        return ai_prompt_store.resolve_system(scene, base_system), user_prompt
    system, prompt, version = ai_prompt_store.build(
        scene, base_system, user_prompt, sig=_CURRENT['sig'],
        with_contract=(scene == 'extractor'))
    _CURRENT['versions'].add(version)
    return system, prompt


# ------------------------------------------------------------------ 主流水线
def run_task(task):
    task_id = int(task['id'])
    t0 = time.time()
    _CURRENT['sig'] = ''
    _CURRENT['versions'] = set()
    work = None
    try:
        nodes = task.get('source_nodes') or []
        if isinstance(nodes, str):
            nodes = json.loads(nodes)
        if not nodes:
            raise RuntimeError('任务没有文件快照（source_nodes 为空）')

        # ---- 1 物化 ----
        set_stage(task_id, 'SCANNING', 10, total_files=len(nodes))
        work, prepared, skipped = materialize.materialize(task_id, nodes)
        if not prepared:
            raise RuntimeError('招标文件 blob 全部缺失或超限，无可解析文件')

        from bidsummary import scanner, parsers, extractor, llm
        from bidsummary.config import CONFIG
        CONFIG.ensure_dirs()
        CONFIG.concurrency = max(1, min(int(os.environ.get('AI_CONCURRENCY', '6')), 12))

        # ---- 2 扫描 ----
        if _cancelled(task_id):
            raise Canceled()
        items = scanner.scan_folder(work, CONFIG)
        log.info('[ai] task %s 扫描到 %d 个文件', task_id, len(items))
        set_stage(task_id, 'SCANNING', 20, total_files=len(items), skipped_files=len(skipped))
        if not items:
            raise RuntimeError('目录中没有找到可解析的文件（支持 pdf/doc/docx/xls/xlsx/csv/txt/图片）')

        # ---- 3 解析（并发，单文件失败隔离） ----
        # 进度必须「逐文件 + 逐扫描页」可见：旧实现只在 done%3==0 时写库，
        # 3 个文件的任务整个阶段只有 0 和 100% 两个点，前端永远显示「解析文件 35%」，
        # 用户看到的就是假死。页级回调再叠一层节流，避免把 PG 刷爆。
        set_stage(task_id, 'PARSING', 35)
        total = max(1, len(items))
        frac = {id(it): 0.0 for it in items}
        pct_box = {'v': 35}
        last_write = {'t': 0.0}
        emit_lock = threading.Lock()

        def _emit(cur_name='', force=False):
            # 进度只是观感：写库失败绝不能让解析线程把文件判成 FAILED
            try:
                with emit_lock:
                    now = time.time()
                    if not force and now - last_write['t'] < 1.2:
                        return
                    last_write['t'] = now
                    pct = 35 + int(18 * sum(frac.values()) / total)
                    pct_box['v'] = max(pct_box['v'], pct)
                    set_stage(task_id, 'PARSING', pct_box['v'],
                              parsed_files=sum(1 for x in items if x.status == 'PARSED'),
                              failed_files=sum(1 for x in items if x.status == 'FAILED'),
                              current_file=str(cur_name or '')[:120])
            except Exception as e:
                log.warning('[ai] 写解析进度失败（忽略）：%s', str(e)[:120])

        def _mk_progress(it):
            def cb(done, tot, note=''):
                frac[id(it)] = min(1.0, done / max(1, tot))
                _emit(it.name)
            return cb

        def _mk_stop(it):
            t_start = time.time()
            state = {'t': time.time(), 'cancel': False}

            def cb():
                now = time.time()
                if now - t_start > config.PARSE_FILE_TIMEOUT:
                    log.warning('[ai] %s 解析超过 %d 秒，中止该文件（其余文件继续）',
                                it.name, config.PARSE_FILE_TIMEOUT)
                    return True
                if now - state['t'] >= 2:          # 取消检查也节流，别每页打一次库
                    state['t'] = now
                    try:
                        state['cancel'] = _cancelled(task_id)
                    except Exception:
                        state['cancel'] = False    # 查询失败不该导致中止
                return state['cancel']
            return cb

        done = 0
        with ThreadPoolExecutor(max_workers=CONFIG.concurrency) as pool:
            futs = {pool.submit(parsers.parse_file, it, CONFIG, lambda m: log.info('[ai] %s', m),
                                _mk_progress(it), _mk_stop(it)): it
                    for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    fut.result()
                except Exception as e:
                    it.status = 'FAILED'
                    it.error = _safe_error(e)[:200]
                frac[id(it)] = 1.0
                done += 1
                _emit(it.name, force=True)
                if it.status == 'PARSED':
                    log.info('[ai] 解析完成 %d/%d %s（%d 字）',
                             done, len(items), it.name, len(it.markdown or ''))
                elif it.status == 'SKIPPED':
                    log.warning('[ai] 解析中止 %d/%d %s：%s', done, len(items), it.name, it.error)
                else:
                    log.warning('[ai] 解析失败 %d/%d %s：%s', done, len(items), it.name, it.error)
        if _cancelled(task_id):
            raise Canceled()
        parsed = [i for i in items if i.status == 'PARSED']
        if not parsed:
            tail = '；'.join(f'{i.name}: {i.error}' for i in items if i.error)[:300]
            raise RuntimeError('所有文件都解析失败或超时，无法生成汇总表' + (f'（{tail}）' if tail else ''))

        # ---- 版式族指纹（few-shot 检索路由，非内容指纹） ----
        _CURRENT['sig'] = format_sig.compute(items, prepared=prepared)
        set_stage(task_id, 'GROUPING', 55)

        # ---- 4 抽取 + 校验 ----
        if _cancelled(task_id):
            raise Canceled()
        set_stage(task_id, 'EXTRACTING', 70)
        result = extractor.run_extraction(items, CONFIG, log=lambda m: log.info('[ai] %s', m))
        try:
            result.tokens = llm.usage()
        except Exception:
            pass
        set_stage(task_id, 'VALIDATING', 88,
                  mode=getattr(result, 'mode', ''),
                  prompt_version=','.join(sorted(_CURRENT['versions']))[:200])

        # ---- 5 写库（替代 exporter.save(Excel)） ----
        if _cancelled(task_id):
            raise Canceled()
        set_stage(task_id, 'SAVING', 96)
        summary_id, status = db_writer.write(
            task, result, prepared, skipped,
            prompt_version=','.join(sorted(_CURRENT['versions']))[:200])
        set_stage(task_id, 'DONE', 100)
        try:
            feedback.mine_aliases(summary_id)
        except Exception as e:
            log.warning('[ai] 别名挖掘失败（忽略）：%s', str(e)[:120])
        log.info('[ai] task %s 完成，耗时 %.1fs，summary=%s，状态=%s',
                 task_id, time.time() - t0, summary_id, status)
        return True

    except Canceled:
        db.x("UPDATE ai_summary_task SET status='CANCELED', stage='CANCELED', "
             "stage_label='已取消', finished_at=%s, updated_at=%s WHERE id=%s",
             (time.time(), time.time(), task_id))
        db.commit()
        ai_signal.publish(task_id, {'stage': 'CANCELED'})
        log.info('[ai] task %s 已取消', task_id)
        return False
    except Exception as e:
        log.error('[ai] task %s 失败：%s', task_id, _safe_error(e))
        log.debug(traceback.format_exc())
        _fail(task_id, e)
        return False
    finally:
        if work:
            materialize.clean(work)


class Canceled(Exception):
    pass


# ------------------------------------------------------------------ 入口
def main():
    log.info('[ai] Worker 启动：id=%s PG=%s:%s/%s Redis=%s',
             config.WORKER_ID, config.PG_CONF['host'], config.PG_CONF['port'],
             config.PG_CONF['dbname'], config.REDIS_URL.split('@')[-1])
    config.AI_TMP.mkdir(parents=True, exist_ok=True)

    # ---- 模型连接自检：只报「是否配置」，绝不打印密钥 ----
    # 历史坑：密钥填进了 .env.example，进程读的是 .env，任务跑完才发现全程未调模型。
    try:
        from bidsummary.config import CONFIG as _llm
        log.info('[ai] 模型配置：provider=%s text_model=%s key=%s base_url=%s vision=%s',
                 _llm.text_provider, _llm.text_model,
                 '已配置' if _llm.text_api_key else '未配置',
                 _llm.text_base_url or '未配置',
                 _llm.vision_model or '未配置')
        if not _llm.text_api_key:
            log.warning('[ai] 文本模型密钥缺失：请检查 server/ai_worker/.env（不是 .env.example）'
                        '或 supervisor environment，改完需重启 Worker')
    except Exception as e:
        log.warning('[ai] 模型配置自检失败：%s', _safe_error(e))

    # ---- 0) 数据库自检：冷启动自动建表，不必先重启后端 ----
    # 与 3.8 后端共用 server/ai_schema.sql（唯一事实源）；整段 DDL 幂等。
    # 失败必须 fail-fast 退出，否则主循环会每 3 秒刷一条「表不存在」。
    try:
        info = schema.ensure_schema()
        log.info('[ai] 数据库就绪：%d 张 ai_* 表、%d 套字段模板（%s）',
                 info['tables'], info['templates'], Path(info['file']).name)
    except Exception as e:
        log.error('[ai] 数据库自检失败，Worker 退出：%s', _safe_error(e))
        log.error('[ai] 请检查 PG_HOST/PG_PORT/PG_USER/PG_PASSWORD/PG_DB 配置，'
                  '以及 server/ai_schema.sql 是否随代码部署')
        return 1

    try:
        n = materialize.clean_old()
        if n:
            log.info('[ai] 清理过期物化目录 %d 个', n)
    except Exception as e:
        log.warning('[ai] 清理临时目录失败（忽略）：%s', str(e)[:120])

    try:
        ai_prompt_store.ensure_seed()
        log.info('[ai] 提示词版本表已就绪（7 个 scene）')
    except Exception as e:
        log.warning('[ai] 提示词 seed 失败，回退代码常量：%s', str(e)[:120])

    try:
        from bidsummary import llm
        llm.set_prompt_hook(_prompt_hook)
        log.info('[ai] 提示词外置钩子已装载')
    except Exception as e:
        log.warning('[ai] 提示词钩子装载失败（不影响主链路）：%s', str(e)[:120])

    try:
        mi = config.CONFIG.model_info()
        log.info('[ai] 模型供应商：文本=%s/%s 视觉=%s/%s',
                 mi['text_provider'], mi['text_model'],
                 mi['vision_provider'], mi['vision_model'] or '未配置')
    except Exception:
        pass

    try:
        recover_stale()
    except Exception as e:
        log.warning('[ai] 僵死任务回收失败：%s', str(e)[:120])

    idle = 0
    err_streak = 0
    while True:
        try:
            ai_signal.wait_wakeup(timeout=3)
            task = claim_one()
            if not task:
                idle += 1
                if idle % 100 == 0:
                    recover_stale()
                continue
            idle = 0
            err_streak = 0
            run_task(task)
        except KeyboardInterrupt:
            log.info('[ai] Worker 收到退出信号')
            break
        except Exception as e:
            err_streak += 1
            log.error('[ai] 主循环异常（连续 %d 次）：%s', err_streak, _safe_error(e))
            if err_streak >= 10:
                log.error('[ai] 连续 10 次异常，Worker 退出，交由 supervisor 重启')
                break
            time.sleep(3)
        finally:
            try:
                db.rollback()
            except Exception:
                pass
    db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
