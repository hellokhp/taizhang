# -*- coding: utf-8 -*-
"""日常采集执行：关键词 × 最大页宽翻页 × 过滤 × 详情 × upsert。

默认确定性：只读配方与配置。仅当配方声明 detail.ai.enabled 时，
新公告正文会调用一次 LLM，结果按正文 hash 缓存，重复命中不再调用模型。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import config, jobs, recipe as recipe_mod
from .extract import extract_detail
from .filter import RuleEngine, recipe_filters
from .normalize import normalize_item, url_hash
from .paginate import CancelledError, iter_pages
from .transport import FetchError, FetchResult, Fetcher

DETAIL_TTL_DAYS = 7
COMPARE_FIELDS = ('title', 'buyer', 'budget_yuan', 'fee_note', 'scope',
                  'doc_get_start', 'doc_get_end', 'open_at', 'publish_at',
                  'region', 'source_name', 'status')
# 这些字段对应 bid_item 的列；其余详情字段一律进 detail jsonb
_STANDARD_DETAIL_KEYS = frozenset((
    'title', 'url', 'external_id', 'buyer', 'budget', 'fee_note', 'scope',
    'doc_get_start', 'doc_get_end', 'open_at', 'publish_at', 'region',
    'source_name', 'status', '_raw'))


class NoPlatform(RuntimeError):
    pass


def _targets(params: dict) -> list[dict]:
    pid = (params.get('platform') or '').strip()
    # 前端可以一次勾选多个平台：params.platforms = ['cfcpn','ctpch',…]
    plist = params.get('platforms')
    if isinstance(plist, str):
        plist = [x for x in re.split(r'[,\s]+', plist) if x]
    if isinstance(plist, (list, tuple)) and plist:
        wanted = {str(x).strip() for x in plist if str(x).strip()}
        rows = [p for p in recipe_mod.list_platforms() if p['id'] in wanted]
        if not rows:
            raise NoPlatform('指定的平台都不存在：%s' % '、'.join(sorted(wanted)))
        return rows
    if pid:
        rows = [p for p in recipe_mod.list_platforms() if p['id'] == pid]
        if not rows:
            raise NoPlatform('平台不存在：%s' % pid)
        return rows
    return recipe_mod.list_platforms(statuses=['active', 'degraded'])


def _run_start(rec, snap, keyword: str, mode: str) -> int:
    size = rec.effective_size()
    cur = jobs.db.x(
        'INSERT INTO bid_crawl_run(platform_id, recipe_ver, config_ver, keyword, mode, '
        "status, page_size) VALUES (%s,%s,%s,%s,%s,'running',%s) RETURNING id",
        (rec.platform_id, rec.version, snap.version, keyword, mode, size))
    run_id = cur.fetchone()['id']
    jobs.db.commit()
    return int(run_id)


def _run_finish(run_id: int, status: str, stats: dict, error: str = ''):
    jobs.db.x(
        'UPDATE bid_crawl_run SET status=%s, requests=%s, page_size=%s, avg_ms=%s, '
        'fetched=%s, inserted=%s, updated=%s, dropped=%s, error=%s, finished_at=now() '
        'WHERE id=%s',
        (status, stats.get('requests', 0), stats.get('page_size'),
         stats.get('avg_ms', 0), stats.get('fetched', 0), stats.get('inserted', 0),
         stats.get('updated', 0), stats.get('dropped', 0),
         (error or '')[:1000] or None, run_id))
    jobs.db.commit()


def _cache_get(h: str) -> FetchResult | None:
    row = jobs.db.one(
        "SELECT url, body, content_type FROM bid_fetch_cache "
        "WHERE url_hash=%s AND fetched_at > now() - (%s * interval '1 day')",
        (h, DETAIL_TTL_DAYS))
    if not row:
        return None
    body = row.get('body') or ''
    # 历史缓存里可能是反爬中间页（acw_sc__v2 挑战脚本）：当没缓存，重新取
    from .browser_probe import is_waf_page
    if is_waf_page(body):
        return None
    data = None
    if 'json' in (row.get('content_type') or '').lower():
        try:
            data = json.loads(row['body'])
        except Exception:
            data = None
    return FetchResult(ok=True, status=200, url=row['url'], text=row.get('body') or '',
                       data=data, content_type=row.get('content_type') or '')


def _cache_put(url: str, res: FetchResult):
    from .browser_probe import is_waf_page
    if is_waf_page(res.text or ''):
        # 反爬中间页没有价值，写进缓存会让这一小时都读到空正文
        return
    try:
        jobs.db.x('INSERT INTO bid_fetch_cache(url_hash, url, body, content_type, fetched_at) '
                  'VALUES (%s,%s,%s,%s,now()) ON CONFLICT (url_hash) DO UPDATE '
                  'SET body=EXCLUDED.body, content_type=EXCLUDED.content_type, fetched_at=now()',
                  (url_hash(url), url, (res.text or '')[:2_000_000], res.content_type))
        jobs.db.commit()
    except Exception:
        # 缓存只是加速手段：多个平台/任务并行时撞上同一 URL 可能写冲突，
        # 不能因为写缓存失败就把整条采集判死
        try:
            jobs.db.rollback()
        except Exception:
            pass


def _detail_ctx(rec, item: dict, url: str = '') -> dict:
    """给详情请求模板准备 {id} / {url} 等占位符的值。

    列表条目由 build_item 保留原始行到 _raw；配方也可声明 detail.id_field
    从哪个已抽取字段取 id（默认 external_id，回退 _raw.id）。
    url 传绝对地址：列表里常是站内相对路径，模板 {url} 直接拼会拿不到。
    """
    d = rec.detail or {}
    raw = item.get('_raw') if isinstance(item.get('_raw'), dict) else {}
    id_field = str(d.get('id_field') or 'external_id')
    did = item.get(id_field) or item.get('external_id') or raw.get('id') or ''
    ctx = {'url': url or item.get('url') or '', 'id': str(did), 'external_id': str(did)}
    # 原始行里的标量也放进 ctx，便于配方模板引用任意字段
    for k, v in raw.items():
        if isinstance(v, (str, int, float)) and k not in ctx:
            ctx[k] = str(v)
    return ctx


def _enrich_detail(rec, fetcher: Fetcher, item: dict, *, ai: bool = True) -> tuple[dict, dict]:
    """返回 (富化后的条目, AI 统计)。任何详情失败都不影响该条目入库。"""
    stat = {'requested': 0, 'cached': 0, 'ai_llm': 0, 'ai_cache': 0,
            'ai_fallback': 0, 'ai_empty': 0}
    if not rec.detail_enabled() or not item.get('url'):
        return item, stat
    # 列表里的链接可能是 /zgczb/897997.jhtml 这种站内相对路径：
    # 直接拿去请求会抛「URL missing protocol」，详情永远为空。
    page_url = fetcher.abs_url(item['url'])
    h = url_hash(page_url)
    res = _cache_get(h)
    if res is None:
        try:
            res = fetcher.fetch_detail(page_url, ctx=_detail_ctx(rec, item, page_url))
            stat['requested'] += 1
            if res.ok:
                _cache_put(page_url, res)
        except Exception:
            return item, stat
    else:
        stat['cached'] += 1
    if not res or not res.ok:
        return item, stat
    try:
        extra = extract_detail(rec, res, item, ai=ai)
    except Exception:
        extra = {}
    meta = (extra or {}).get('_ai_meta') or {}
    src = meta.get('source') if isinstance(meta, dict) else ''
    if src == 'llm':
        stat['ai_llm'] += 1
    elif src == 'cache':
        stat['ai_cache'] += 1
    elif src == 'fallback':
        stat['ai_fallback'] += 1
    else:
        stat['ai_empty'] += 1
    if extra:
        merged = dict(item)
        detail = dict(extra.get('detail') or {})
        for k, v in extra.items():
            if k.startswith('_') or k == 'detail' or not v:
                continue
            merged[k] = v
            if k not in _STANDARD_DETAIL_KEYS:
                detail.setdefault(k, v)   # 非标准列字段统一收进 detail jsonb
        if meta:
            detail['ai'] = meta
        if detail:
            merged['detail'] = detail
        return merged, stat
    return item, stat


def _similar(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, datetime) or isinstance(b, datetime):
        try:
            return a.replace(microsecond=0) == b.replace(microsecond=0)
        except Exception:
            return str(a) == str(b)
    return str(a) == str(b)


def _converge_external_id(norm: dict, rec=None):
    """配方 URL 模板升级后，同一项目可能残留多条不同 url_hash 的旧记录。

    配方可用 normalize.legacy_url_templates 声明历史 URL 模板（支持 {id}），
    这里按规范化 hash 找出同一项目的全部旧行，保留字段最全的一条并补齐
    external_id / url / url_hash，其余删除。纯幂等，代码里无站点分支。
    """
    ext = norm.get('external_id')
    if not ext:
        return 0
    hashes = [(norm.get('url_hash') or '').strip()]
    for tpl in (((rec.normalize if rec else None) or {}).get('legacy_url_templates') or []):
        try:
            legacy = str(tpl).format(id=ext, external_id=ext, url=norm.get('url') or '')
        except Exception:
            continue
        if legacy:
            hashes.append(url_hash(legacy))
    hashes = list(dict.fromkeys(h for h in hashes if h))
    rows = jobs.db.q('SELECT id, url_hash, detail FROM bid_item '
                     'WHERE platform_id=%s AND url_hash = ANY(%s) ORDER BY id',
                     (norm['platform_id'], hashes))
    if len(rows) <= 1:
        return 0
    target = hashes[0]
    keep = next((r for r in rows if (r.get('url_hash') or '').strip() == target), None)
    if keep is None:
        keep = max(rows, key=lambda r: (1 if r.get('detail') else 0, int(r['id'])))
    jobs.db.x('UPDATE bid_item SET url=%s, url_hash=%s, external_id=%s WHERE id=%s',
              (norm['url'], target, ext, keep['id']))
    drop = [int(r['id']) for r in rows if int(r['id']) != int(keep['id'])]
    if drop:
        jobs.db.x('DELETE FROM bid_item_change WHERE item_id = ANY(%s)', (drop,))
        jobs.db.x('DELETE FROM bid_item WHERE id = ANY(%s)', (drop,))
    jobs.db.commit()
    return len(drop)


def _retryable_db_error(e) -> bool:
    """并发写同一条时的可重试错误：死锁 40P01 / 序列化 40001 / 唯一冲突 23505。"""
    return str(getattr(e, 'pgcode', '') or '') in ('40P01', '40001', '23505')


def _upsert(norm: dict, rec=None) -> tuple[str, int, dict]:
    """写入 / 更新一条标讯；并发撞车时回滚重试一次。

    不同平台并行时 platform_id 不同、互不影响；但「全平台任务」与「单平台任务」
    可能同时命中同一条公告，INSERT … ON CONFLICT 之间仍会短暂互锁。
    """
    try:
        return _upsert_once(norm, rec)
    except Exception as e:                       # noqa: BLE001
        if not _retryable_db_error(e):
            raise
        try:
            jobs.db.rollback()
        except Exception:
            pass
        time.sleep(0.2)
        return _upsert_once(norm, rec)


def _upsert_once(norm: dict, rec=None) -> tuple[str, int, dict]:
    """返回 ('new'|'updated'|'same', item_id, diff)。"""
    _converge_external_id(norm, rec)
    cols = ('platform_id', 'external_id', 'url', 'url_hash', 'title', 'buyer',
            'budget_yuan', 'fee_note', 'scope', 'doc_get_start', 'doc_get_end',
            'open_at', 'publish_at', 'region', 'source_name', 'status', 'raw', 'detail')
    vals = (norm.get('platform_id'), norm.get('external_id'), norm['url'],
            norm['url_hash'], norm['title'], norm.get('buyer'), norm.get('budget_yuan'),
            norm.get('fee_note'), norm.get('scope'), norm.get('doc_get_start'),
            norm.get('doc_get_end'), norm.get('open_at'), norm.get('publish_at'),
            norm.get('region'), norm.get('source_name'), norm.get('status'),
            json.dumps(norm.get('raw') or {}, ensure_ascii=False, default=str),
            json.dumps(norm.get('detail') or {}, ensure_ascii=False, default=str))
    holders = ['%s'] * len(cols)
    holders[-1] = '%s::jsonb'          # detail 显式转 jsonb，避免 text→jsonb 隐式转换差异
    cur = jobs.db.x(
        'INSERT INTO bid_item(%s) VALUES (%s) ON CONFLICT (platform_id, url_hash) '
        'DO NOTHING RETURNING id' % (', '.join(cols), ', '.join(holders)), vals)
    row = cur.fetchone()
    jobs.db.commit()
    if row:
        item_id = int(row['id'])
        jobs.db.x('INSERT INTO bid_item_change(item_id, change_type, diff) '
                  "VALUES (%s,'new',%s::jsonb)",
                  (item_id, json.dumps({'title': norm['title']}, ensure_ascii=False)))
        jobs.db.commit()
        return 'new', item_id, {}

    old = jobs.db.one('SELECT * FROM bid_item WHERE platform_id=%s AND url_hash=%s',
                      (norm['platform_id'], norm['url_hash']))
    if not old:
        return 'same', 0, {}
    diff = {}
    for key in COMPARE_FIELDS:
        if not _similar(old.get(key), norm.get(key)):
            diff[key] = {'old': str(old.get(key) if old.get(key) is not None else ''),
                         'new': str(norm.get(key) if norm.get(key) is not None else '')}
    sets = ', '.join('%s=%%s' % k for k in COMPARE_FIELDS)
    args = ([norm.get(k) for k in COMPARE_FIELDS] +
            [json.dumps(norm.get('detail') or {}, ensure_ascii=False, default=str),
             old['id']])
    jobs.db.x('UPDATE bid_item SET ' + sets +
              ', detail=%s::jsonb, last_seen_at=now() WHERE id=%s', tuple(args))
    if diff:
        jobs.db.x('INSERT INTO bid_item_change(item_id, change_type, diff) '
                  "VALUES (%s,'field_changed',%s::jsonb)",
                  (old['id'], json.dumps(diff, ensure_ascii=False)))
    jobs.db.commit()
    return ('updated' if diff else 'same'), int(old['id']), diff


def crawl_keyword(rec, snap, keyword: str, fetcher: Fetcher, engine: RuleEngine,
                  *, mode: str = 'incremental', dry_run: bool = False,
                  cancel=None, progress=None, with_ai: bool = True) -> dict:
    stats = {'keyword': keyword, 'fetched': 0, 'inserted': 0, 'updated': 0,
             'dropped': 0, 'skipped': 0, 'requests': 0, 'pages': 0,
             'detail_requests': 0, 'detail_cached': 0,
             'ai_llm': 0, 'ai_cache': 0, 'ai_fallback': 0, 'ai_empty': 0,
             'page_size': rec.effective_size(), 'avg_ms': 0, 'error': ''}
    t0 = time.time()
    run_id = None if dry_run else _run_start(rec, snap, keyword, mode)
    req0 = fetcher.requests
    keep_url = bool((rec.normalize or {}).get('keep_url'))
    base_url = rec.raw.get('meta', {}).get('base_url', '')
    try:
        for pg in iter_pages(rec, fetcher, keyword, cancel=cancel,
                             key_of=lambda it: it.get('url')):
            stats['pages'] += 1
            stats['fetched'] += len(pg.items)
            if progress:
                progress(pg.page)
            for raw_item in pg.items:
                if cancel and cancel():
                    raise CancelledError('用户取消')
                # 先用列表字段做确定性过滤：被剔除的条目不必消耗限速与模型额度
                head = normalize_item(raw_item, base_url=base_url,
                                      source_default=rec.meta.get('name') or rec.platform_id,
                                      keep_url=keep_url)
                head['platform_id'] = rec.platform_id
                if not head.get('url'):
                    stats['skipped'] += 1
                    continue
                decision = engine.evaluate(head)
                if not decision.keep:
                    stats['dropped'] += 1
                    continue
                # 仅对保留条目拉详情 / 跑 AI
                item, dst = _enrich_detail(rec, fetcher, raw_item, ai=with_ai)
                for k in ('ai_llm', 'ai_cache', 'ai_fallback', 'ai_empty'):
                    stats[k] += dst.get(k, 0)
                stats['detail_requests'] += dst.get('requested', 0)
                stats['detail_cached'] += dst.get('cached', 0)
                norm = normalize_item(item, base_url=base_url,
                                      source_default=rec.meta.get('name') or rec.platform_id,
                                      keep_url=keep_url)
                norm['platform_id'] = rec.platform_id
                if dry_run:
                    stats['inserted'] += 1
                    continue
                kind, _item_id, _diff = _upsert(norm, rec)
                if kind == 'new':
                    stats['inserted'] += 1
                elif kind == 'updated':
                    stats['updated'] += 1
                else:
                    stats['skipped'] += 1
    except CancelledError:
        raise
    except FetchError as e:
        stats['error'] = str(e)[:500]

    stats['requests'] = fetcher.requests - req0
    stats['avg_ms'] = int(fetcher.total_ms / max(1, fetcher.requests))
    if not dry_run and run_id:
        _run_finish(run_id, 'failed' if stats['error'] else 'ok', stats, stats['error'])
    stats['elapsed_s'] = round(time.time() - t0, 1)
    return stats


# 平台级并行度：一个任务里的不同站点互不相干（各自的限速、Cookie、翻页规则），
# 串行跑 9 个平台等于把 9 个站点的往返时间相加。并行后总耗时 ≈ 最慢的那个平台。
# 注意它与 worker 的任务级并发（BID_WORKER_SLOTS）相乘，默认 2×4=8 个平台线程。
PLATFORM_WORKERS = max(1, int(os.environ.get('BID_PLATFORM_WORKERS', '4')))
_SUM_KEYS = ('fetched', 'inserted', 'updated', 'dropped', 'requests',
             'detail_requests', 'detail_cached',
             'ai_llm', 'ai_cache', 'ai_fallback', 'ai_empty')


def _crawl_one_platform(p: dict, *, params: dict, mode: str, dry_run: bool,
                        with_ai: bool, cancel, report) -> dict:
    """跑完一个平台的全部关键词。

    异常一律收敛到 pstat['error']——一个站点改版/超时不能拖垮同批的其他平台。
    唯一向上抛的是 JobCancelled：用户取消时整批一起停。
    """
    pid = p['id']
    name = p.get('name') or pid
    pstat = {'platform_id': pid, 'name': p.get('name'), 'keywords': [], '_hits': {}}
    try:
        rec = recipe_mod.get_active(pid)
        if not rec:
            pstat['error'] = '没有启用的配方'
            return pstat
        snap = config.load_snapshot(pid)
        engine = RuleEngine(snap, pid, filters=recipe_filters(rec))
        words = params.get('keywords') or snap.keyword_words()
        if not words:
            pstat['error'] = '关键词配置为空'
            return pstat

        fetcher = Fetcher(rec)
        try:
            for j, word in enumerate(words):
                if cancel():
                    raise jobs.JobCancelled('用户取消')
                wbase = j * 100.0 / len(words)
                wspan = 100.0 / len(words)
                report(pid, wbase, '%s · 关键词「%s」请求中…' % (name, word), True)

                def _pg(page, _wb=wbase, _ws=wspan, _w=word, _n=name):
                    maxp = int(rec.pagination.get('max_pages') or 10)
                    report(pid, _wb + _ws * min(1.0, (page + 1) / max(1, maxp)),
                           '%s · 关键词「%s」第 %d 页' % (_n, _w, page))

                st = crawl_keyword(rec, snap, word, fetcher, engine, mode=mode,
                                   dry_run=dry_run, cancel=cancel, progress=_pg,
                                   with_ai=with_ai)
                # 命中数先记在本平台账上，任务结束时合并写库：
                # 多个平台同时跑同一个关键词，各自 UPDATE 会互相覆盖
                if not dry_run:
                    kw = next((k for k in snap.keywords
                               if str(k['word']).lower() == str(word).lower()), None)
                    if kw:
                        pstat['_hits'][kw['id']] = (pstat['_hits'].get(kw['id'], 0)
                                                    + st['inserted'] + st['updated'])
                pstat['keywords'].append(st)
                report(pid, wbase + wspan,
                       '%s · 「%s」完成：新增 %d / 更新 %d / 剔除 %d'
                       % (name, word, st['inserted'], st['updated'], st['dropped']), True)
                # 站点不可达快速失败：第一个关键词请求失败且 0 条数据，
                # 跳过该平台其余关键词（逐个超时会把任务拖到几十分钟）
                if st.get('error') and st.get('fetched', 0) == 0 and j == 0:
                    pstat['error'] = ('站点不可达，已跳过其余 %d 个关键词：%s'
                                      % (len(words) - 1, st['error'][:200]))
                    report(pid, 100, '%s · 站点不可达，跳过其余关键词' % name, True)
                    break
        finally:
            fetcher.close()

        for k in _SUM_KEYS:
            pstat[k] = sum(x.get(k, 0) for x in pstat['keywords'])
        if not pstat.get('error'):
            pstat['error'] = next((x['error'] for x in pstat['keywords'] if x.get('error')), '')
        report(pid, 100, '%s · 完成（%d 个关键词）' % (name, len(pstat['keywords'])), True)
    except jobs.JobCancelled:
        raise
    except Exception as e:                       # noqa: BLE001
        pstat['error'] = str(e)[:300]
        # 平台已结束（失败），总进度也要往前推，否则整批会卡在 99%
        report(pid, 100, '%s · 失败：%s' % (name, pstat['error'][:80]), True)

    # 平台健康状态：成功置 last_ok_at，失败只记最近一次错误，不影响同批其他平台
    if not dry_run:
        try:
            if not pstat.get('error'):
                jobs.db.x('UPDATE bid_platform SET last_ok_at=now(), last_err=NULL '
                          'WHERE id=%s', (pid,))
            else:
                jobs.db.x('UPDATE bid_platform SET last_err=%s WHERE id=%s',
                          (pstat['error'][:1000], pid))
            jobs.db.commit()
        except Exception:
            try:
                jobs.db.rollback()
            except Exception:
                pass
    return pstat


def run_job(job: dict, progress, cancel) -> dict:
    """worker 调用的入口：按参数跑一个或多个平台。

    平台之间并行（ThreadPoolExecutor），关键词与翻页在平台内部仍按配方顺序执行——
    同一站点的限速、Cookie、翻页状态都绑在一起，拆开只会互相踩。
    """
    params = job.get('params') or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    dry_run = bool(params.get('dry_run'))
    mode = 'full' if params.get('full') else 'incremental'
    targets = _targets(params)
    if not targets:
        raise NoPlatform('没有状态为 active/degraded 的平台，请先导入并启用配方')
    t_job = time.time()

    with_ai = params.get('with_ai', True)
    with_ai = not (isinstance(with_ai, str) and with_ai.lower() in ('0', 'false', 'off', 'no')) \
        and bool(with_ai)
    n = len(targets)
    lock = threading.Lock()
    pct_of = {p['id']: 0.0 for p in targets}

    def report(pid, local, message='', force=False):
        """平台线程报「本平台进度」，总进度取各平台的平均值。"""
        with lock:
            if local is not None:
                pct_of[pid] = max(pct_of.get(pid, 0.0), min(100.0, float(local)))
            pct = sum(pct_of.values()) / n
        try:
            progress('crawl', pct, message, force=force)
        except Exception:
            pass

    workers = max(1, min(PLATFORM_WORKERS, n))
    stats: list[dict] = []
    if workers == 1:
        for p in targets:
            if cancel():
                raise jobs.JobCancelled('用户取消')
            stats.append(_crawl_one_platform(
                p, params=params, mode=mode, dry_run=dry_run,
                with_ai=with_ai, cancel=cancel, report=report))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bid-plat') as pool:
            futs = {pool.submit(_crawl_one_platform, p, params=params, mode=mode,
                                dry_run=dry_run, with_ai=with_ai,
                                cancel=cancel, report=report): p for p in targets}
            try:
                for f in as_completed(futs):
                    stats.append(f.result())
            except jobs.JobCancelled:
                for f in futs:
                    f.cancel()
                raise

    order = {p['id']: i for i, p in enumerate(targets)}
    stats.sort(key=lambda s: order.get(s['platform_id'], 10 ** 6))

    result = {'platforms': stats, 'dry_run': dry_run, 'parallel': workers,
              'elapsed_s': 0.0}
    for k in _SUM_KEYS:
        result[k] = sum(s.get(k, 0) for s in stats)
    # 关键词命中数合并后一次写库（并发平台不会互相覆盖）
    hit_sum: dict = {}
    for s in stats:
        for kw_id, cnt in (s.pop('_hits', {}) or {}).items():
            hit_sum[kw_id] = hit_sum.get(kw_id, 0) + int(cnt or 0)
    if hit_sum and not dry_run:
        try:
            for kw_id, cnt in hit_sum.items():
                jobs.db.x('UPDATE bid_keyword SET hits_last=%s WHERE id=%s', (cnt, kw_id))
            jobs.db.commit()
        except Exception:
            try:
                jobs.db.rollback()
            except Exception:
                pass

    errs = [p.get('error') for p in stats if p.get('error')]
    if stats and len(errs) == len(stats) and result['fetched'] == 0:
        raise FetchError('全部平台采集失败：%s' % (errs[0] or '未知错误'))
    result['elapsed_s'] = round(time.time() - t_job, 1)
    return result


def preview_job(job: dict, progress, cancel) -> dict:
    """用最近入库的 raw 数据离线重跑当前配置，返回过滤影响。"""
    from .filter import preview

    params = job.get('params') or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    pid = (params.get('platform') or '').strip()
    if not pid:
        raise NoPlatform('缺少平台 id')
    snap = config.load_snapshot(pid, force=True)
    rows = jobs.db.q('SELECT title, buyer, url, publish_at, raw FROM bid_item '
                     'WHERE platform_id=%s ORDER BY last_seen_at DESC LIMIT 500', (pid,))
    items = []
    for r in rows:
        raw = r.get('raw')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        items.append({'title': r.get('title'), 'buyer': r.get('buyer'),
                      'url': r.get('url'), 'publish_at': r.get('publish_at'),
                      'raw': raw or {}})
    progress('preview', 50, '正在用最近 %d 条数据重跑配置…' % len(items))
    rec = recipe_mod.get_active(pid)
    out = preview(items, snap, pid, filters=recipe_filters(rec))
    out['platform_id'] = pid
    progress('preview', 100, '预览完成')
    return out
