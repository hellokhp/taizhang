# -*- coding: utf-8 -*-
"""标讯采集 · 独立 Worker（claim / 心跳 / 进度 / 取消 / 僵死回收）。

启动：
    python -m server.bidcrawl.cli worker
    python -m server.bidcrawl.worker          # 等价

铁律：
- 任务状态、进度、结果全在 PG；进程被杀后靠心跳回收。
- 请求路径（FastAPI）只提交任务，不执行采集。
- 两层并发：任务级 SLOTS（默认 2），任务内**平台级并行**（runner.PLATFORM_WORKERS，
  默认 4）。不同站点各有自己的限速与 Cookie，并行不会互相拖慢；总线程上限
  是两者的乘积，可用环境变量收紧。
- 队列上限 20，超限由接口返回 429。
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import jobs, runner, scheduler
from ..shared_config import pg_password

log = logging.getLogger('taizhang.bidcrawl.worker')

WORKER_ID = os.environ.get('BID_WORKER_ID', 'bid-%d' % os.getpid())
SLOTS = int(os.environ.get('BID_WORKER_SLOTS', '2'))
JOB_TIMEOUT = int(os.environ.get('BID_JOB_TIMEOUT', '3600'))
STALE_CHECK = int(os.environ.get('BID_STALE_CHECK', '30'))
# 定时任务轮询间隔：到点后最多晚这么久开始采集
SCHED_TICK = int(os.environ.get('BID_SCHED_TICK', '30'))


def _safe_error(msg: str) -> str:
    s = str(msg or '')[:1500]
    for secret in (pg_password(),
                   os.environ.get('LLM_API_KEY', ''),
                   os.environ.get('DEEPSEEK_API_KEY', ''),
                   os.environ.get('DASHSCOPE_API_KEY', '')):
        if secret and len(secret) > 4:
            s = s.replace(secret, '***')
    return '\n'.join(s.splitlines()[:8])


def _stop_requested() -> bool:
    return getattr(_stop_requested, 'flag', False)


def _mark_stop(signum=None, frame=None):
    _stop_requested.flag = True
    log.info('收到停止信号，等待当前任务在安全点退出…')


def _heartbeat_loop(stop: threading.Event):
    while not stop.wait(15):
        try:
            ids = jobs.db.q("SELECT id FROM bid_crawl_job WHERE claimed_by=%s "
                            "AND status='running'", (WORKER_ID,))
            for r in ids:
                jobs.heartbeat(int(r['id']))
        except Exception:
            log.warning('心跳写入失败：%s', traceback.format_exc(limit=1))


def _execute(job: dict):
    job_id = int(job['id'])
    jtype = job['job_type']
    deadline = time.monotonic() + JOB_TIMEOUT

    def progress(stage, pct, message='', force=False):
        try:
            jobs.set_progress(job_id, stage, pct, message, force=force)
        except Exception:
            log.warning('进度写入失败 job=%s', job_id)

    def cancel():
        return _stop_requested() or time.monotonic() > deadline or jobs.is_cancelled(job_id)

    log.info('开始任务 #%s %s platform=%s', job_id, jtype, job.get('platform_id'))
    try:
        progress('start', 1, '开始执行')
        if jtype == 'run':
            result = runner.run_job(job, progress, cancel)
        elif jtype == 'rules_preview':
            result = runner.preview_job(job, progress, cancel)
        else:
            raise ValueError('P0 暂不支持任务类型：%s' % jtype)
        if _stop_requested() or jobs.is_cancelled(job_id):
            jobs.finish(job_id, 'cancelled', result=result)
        else:
            jobs.finish(job_id, 'ok', result=result)
        log.info('任务 #%s 完成', job_id)
    except jobs.JobCancelled:
        if time.monotonic() > deadline:
            jobs.finish(job_id, 'failed', error='任务超时（%d 秒）' % JOB_TIMEOUT)
        else:
            jobs.finish(job_id, 'cancelled')
        log.info('任务 #%s 取消', job_id)
    except Exception as e:
        log.error('任务 #%s 失败：%s\n%s', job_id, e, traceback.format_exc(limit=6))
        jobs.finish(job_id, 'failed', error=_safe_error(e))


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    jobs.db.init_schema()
    log.info('Worker %s 启动，任务并发=%d，任务内平台并行=%d，队列上限=%d',
             WORKER_ID, min(SLOTS, jobs.MAX_CONCURRENCY), runner.PLATFORM_WORKERS,
             jobs.MAX_QUEUE)
    _stop_requested.flag = False
    try:
        signal.signal(signal.SIGINT, _mark_stop)
        signal.signal(signal.SIGTERM, _mark_stop)
    except Exception:
        pass

    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True)
    hb.start()

    pool = ThreadPoolExecutor(max_workers=SLOTS, thread_name_prefix='bid-job')
    last_stale = 0.0
    last_sched = 0.0
    try:
        while not _stop_requested():
            now = time.monotonic()
            if now - last_stale > STALE_CHECK:
                try:
                    recovered = jobs.recover_stale()
                    if recovered:
                        log.warning('回收僵死任务：%s', recovered)
                except Exception:
                    log.warning('僵死回收失败：%s', traceback.format_exc(limit=1))
                last_stale = now

            # 定时任务：到点即提交，多 worker 靠条件 UPDATE 抢占，不会重复触发
            if now - last_sched > SCHED_TICK:
                try:
                    fired = scheduler.tick()
                    for x in fired:
                        if x.get('ok'):
                            log.info('定时任务「%s」已提交 job #%s%s',
                                     x.get('name'), x.get('job_id'),
                                     '（合并到活动任务）' if x.get('dedupe') else '')
                        else:
                            log.warning('定时任务「%s」提交失败：%s',
                                        x.get('name'), x.get('error'))
                except Exception:
                    log.warning('定时任务检查失败：%s', traceback.format_exc(limit=1))
                last_sched = now

            running = jobs.active_counts()['running']
            if running >= min(SLOTS, jobs.MAX_CONCURRENCY):
                time.sleep(1)
                continue
            try:
                job = jobs.claim_one(WORKER_ID)
            except Exception:
                log.warning('领取任务失败：%s', traceback.format_exc(limit=1))
                time.sleep(3)
                continue
            if not job:
                time.sleep(2)
                continue
            pool.submit(_execute, job)
    finally:
        stop.set()
        pool.shutdown(wait=True)
        log.info('Worker %s 退出', WORKER_ID)


if __name__ == '__main__':
    main()
