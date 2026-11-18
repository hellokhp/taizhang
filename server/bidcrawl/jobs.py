# -*- coding: utf-8 -*-
"""异步任务：提交（幂等去重）/ 领取 / 心跳 / 进度 / 取消 / 僵死回收。

四道闸门：
  前端置灰 + client_token 幂等 + uq_job_token + uq_job_active(同平台同类只跑一个)
再加全局并发 / 队列上限，避免连点把系统压垮。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import db

MAX_CONCURRENCY = int(os.environ.get('BID_MAX_CONCURRENCY', '2'))
MAX_QUEUE = int(os.environ.get('BID_MAX_QUEUE', '20'))
MAX_ATTEMPTS = int(os.environ.get('BID_MAX_ATTEMPTS', '3'))
STALE_SECONDS = int(os.environ.get('BID_STALE_SECONDS', '90'))
PROGRESS_INTERVAL = 2.0

JOB_FIELDS = ('id', 'job_type', 'platform_id', 'status', 'stage', 'progress',
              'done_steps', 'total_steps', 'message', 'result', 'error',
              'attempts', 'claimed_by', 'cancel_requested', 'created_by',
              'created_at', 'started_at', 'heartbeat_at', 'finished_at', 'params')

_progress_lock = threading.Lock()
_last_progress: dict[int, tuple[float, str, int]] = {}


class CapacityError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


def steps_of(job_type: str) -> int:
    return {'run': 100, 'rules_preview': 3, 'analyze': 6}.get(job_type, 1)


def _json(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def _params_hash(params: dict) -> str:
    return hashlib.sha1(_json(params or {}).encode('utf-8')).hexdigest()[:16]


def normalize_params(platform_id: str | None, params: dict | None) -> dict:
    """以 platform_id 为准清洗任务参数。

    全平台任务（platform_id 为空）若残留 params.platform，runner 只会跑那一个
    站点——界面上写着「全部启用平台」，实际只采了一个。指定了 platform_id 时
    则反过来，用 platform_id 覆盖 params 里的单平台 / 多平台列表，避免
    「任务列表显示 A、实际采 B」。
    """
    out = dict(params or {})
    if platform_id:
        out['platform'] = platform_id
        out.pop('platforms', None)
    else:
        out.pop('platform', None)
    return out


def active_counts() -> dict:
    r = db.one("SELECT count(*) FILTER (WHERE status='running') AS running, "
               "count(*) FILTER (WHERE status='queued') AS queued FROM bid_crawl_job")
    return {'running': int(r['running'] or 0), 'queued': int(r['queued'] or 0)}


def assert_capacity():
    c = active_counts()
    if c['running'] >= MAX_CONCURRENCY:
        raise CapacityError('当前已有 %d 个采集任务在运行，请稍后再试' % c['running'])
    if c['queued'] >= MAX_QUEUE:
        raise CapacityError('任务队列已满（%d），请稍后再试' % c['queued'])


def submit(job_type: str, platform_id: str | None, params: dict, user: str,
           client_token: str | None = None) -> tuple[dict, bool]:
    """先插入，冲突即查回；绝不「先查再插」（并发下会双写）。"""
    key = '%s:%s:%s' % (job_type, platform_id or '-', _params_hash(params))
    try:
        cur = db.x(
            'INSERT INTO bid_crawl_job(job_type, platform_id, dedupe_key, params, '
            'client_token, created_by, total_steps, message) '
            "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,'排队中') RETURNING *",
            (job_type, platform_id, key, _json(params or {}), client_token, user,
             steps_of(job_type)))
        row = dict(cur.fetchone())
        db.commit()
        return _view(row), False
    except Exception as e:
        import psycopg2
        if not isinstance(e, psycopg2.errors.UniqueViolation):
            raise
        db.rollback()
        row = db.one(
            "SELECT * FROM bid_crawl_job WHERE job_type=%s "
            "AND coalesce(platform_id,'-')=coalesce(%s,'-') "
            "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
            (job_type, platform_id))
        if not row and client_token:
            row = db.one('SELECT * FROM bid_crawl_job WHERE client_token=%s', (client_token,))
        if not row:
            raise
        return _view(row), True


def _view(row: dict) -> dict:
    out = {k: row.get(k) for k in JOB_FIELDS}
    for k in ('created_at', 'started_at', 'heartbeat_at', 'finished_at'):
        if out.get(k) is not None:
            out[k] = out[k].isoformat() if hasattr(out[k], 'isoformat') else out[k]
    return out


def claim_one(worker_id: str) -> dict | None:
    cur = db.x(
        "UPDATE bid_crawl_job SET status='running', claimed_by=%s, started_at=now(), "
        "heartbeat_at=now(), attempts=attempts+1, stage='claimed', message='已领取' "
        'WHERE id=('
        '  SELECT id FROM bid_crawl_job WHERE status=%s ORDER BY id '
        '  FOR UPDATE SKIP LOCKED LIMIT 1'
        ') AND (SELECT count(*) FROM bid_crawl_job WHERE status=%s) < %s '
        'RETURNING *',
        (worker_id, 'queued', 'running', MAX_CONCURRENCY))
    row = cur.fetchone()
    db.commit()
    return _view(dict(row)) if row else None


def heartbeat(job_id: int):
    db.x("UPDATE bid_crawl_job SET heartbeat_at=now() WHERE id=%s AND status='running'",
         (job_id,))
    db.commit()


def set_progress(job_id: int, stage: str, progress: int, message: str = '',
                 done_steps: int | None = None, force: bool = False):
    """进度节流：同一 stage 最多 2s 写一次，避免高频 UPDATE 拖慢数据库。"""
    progress = max(0, min(100, int(progress)))
    now = time.time()
    with _progress_lock:
        last = _last_progress.get(job_id)
        if not force and last and last[1] == stage and \
                now - last[0] < PROGRESS_INTERVAL and progress < 100:
            return
        _last_progress[job_id] = (now, stage, progress)

    cols = ['stage=%s', 'progress=%s', 'message=%s']
    args = [stage, progress, (message or '')[:300]]
    if done_steps is not None:
        cols.append('done_steps=%s')
        args.append(int(done_steps))
    args.append(job_id)
    db.x('UPDATE bid_crawl_job SET ' + ', '.join(cols) + ' WHERE id=%s', tuple(args))
    _event(job_id, stage, progress, message)
    db.commit()


def _event(job_id: int, stage: str, progress: int, message: str):
    db.x('INSERT INTO bid_job_event(job_id, seq, stage, progress, message) '
         'SELECT %s, COALESCE(MAX(seq),0)+1, %s, %s, %s FROM bid_job_event WHERE job_id=%s',
         ((job_id), (stage), (progress), ((message or '')[:300]), (job_id)))


def finish(job_id: int, status: str, result: dict | None = None, error: str = ''):
    terminal = status in ('ok', 'failed', 'cancelled')
    db.x('UPDATE bid_crawl_job SET status=%s, result=%s::jsonb, error=%s, '
         'finished_at=CASE WHEN %s THEN now() ELSE finished_at END, '
         "progress=CASE WHEN %s='ok' THEN 100 ELSE progress END, message=%s "
         "WHERE id=%s AND status NOT IN ('ok','failed','cancelled')",
         (status,
          _json(result) if terminal and result is not None else None,
          (error or '')[:1000] or None,
          terminal, status,
          {'ok': '完成', 'failed': '失败', 'cancelled': '已取消'}.get(status, status),
          job_id))
    db.commit()
    with _progress_lock:
        _last_progress.pop(job_id, None)


def request_cancel(job_id: int) -> bool:
    cur = db.x("UPDATE bid_crawl_job SET cancel_requested=true, message='正在取消…' "
               "WHERE id=%s AND status IN ('queued','running')", (job_id,))
    db.commit()
    return cur.rowcount > 0


def is_cancelled(job_id: int) -> bool:
    r = db.one('SELECT cancel_requested, status FROM bid_crawl_job WHERE id=%s', (job_id,))
    if not r:
        return True
    return bool(r['cancel_requested']) or r['status'] in ('cancelled', 'failed', 'ok')


def recover_stale() -> list[dict]:
    cur = db.x(
        'UPDATE bid_crawl_job SET '
        "status=CASE WHEN attempts>=%s THEN 'failed' ELSE 'queued' END, "
        "error=CASE WHEN attempts>=%s THEN '心跳超时且重试次数已用尽' ELSE error END, "
        'finished_at=CASE WHEN attempts>=%s THEN now() ELSE NULL END, '
        "claimed_by=NULL, heartbeat_at=NULL, message='心跳超时，已回收' "
        "WHERE status='running' AND heartbeat_at < now() - (%s * interval '1 second') "
        'RETURNING id, status, attempts',
        (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, STALE_SECONDS))
    rows = [dict(r) for r in cur.fetchall()]
    db.commit()
    return rows


def get_job(job_id: int) -> dict | None:
    r = db.one('SELECT * FROM bid_crawl_job WHERE id=%s', (job_id,))
    return _view(dict(r)) if r else None


def list_jobs(status: str = '', limit: int = 50) -> list[dict]:
    limit = max(1, min(200, int(limit or 50)))
    if status == 'active':
        rows = db.q("SELECT * FROM bid_crawl_job WHERE status IN ('queued','running') "
                    'ORDER BY id DESC LIMIT %s', (limit,))
    elif status:
        rows = db.q('SELECT * FROM bid_crawl_job WHERE status=%s ORDER BY id DESC LIMIT %s',
                    (status, limit))
    else:
        rows = db.q('SELECT * FROM bid_crawl_job ORDER BY id DESC LIMIT %s', (limit,))
    return [_view(dict(r)) for r in rows]


def events_after(job_id: int, after_seq: int = 0, limit: int = 200) -> list[dict]:
    rows = db.q('SELECT seq, stage, progress, message, created_at FROM bid_job_event '
                'WHERE job_id=%s AND seq>%s ORDER BY seq LIMIT %s',
                (job_id, int(after_seq or 0), max(1, min(500, int(limit or 200)))))
    for r in rows:
        if r.get('created_at') is not None and hasattr(r['created_at'], 'isoformat'):
            r['created_at'] = r['created_at'].isoformat()
    return rows


def fail_if_orphan(job_id: int, error: str):
    """worker 内部异常兜底：确保任务不会永远停在 running。"""
    try:
        finish(job_id, 'failed', error=error)
    except Exception:
        pass
