# -*- coding: utf-8 -*-
"""标讯采集 · 每日定时任务调度。

运行位置：worker 主循环每 30 秒调用一次 `tick()`。调度不在 FastAPI 进程里做，
因为采集本来就依赖 worker 在线；worker 不在时定时任务自然不触发，页面会显示
「上次执行」为空。

三条硬约束：
- 同一任务同一天最多提交一次：`last_run_date` + 条件 UPDATE 原子抢占，
  多个 worker 同时 tick 也只有一个能抢到。
- 提交失败（如队列满）回退 `last_run_date`，5 分钟后重试，不丢当天的采集。
- 全平台任务 platform_id 为空，与手动「全部启用平台」共用同一个活动任务槽
  （uq_job_active），撞车时合并而不是排队堆叠。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from . import jobs

# 提交失败后的重试间隔（秒），避免队列满时每 30 秒刷一次错误
RETRY_AFTER_S = int(os.environ.get('BID_SCHED_RETRY_S', '300'))


def hhmm_to_min(value) -> int | None:
    """'09:30' / '9：30' → 570；非法返回 None。"""
    m = re.fullmatch(r'\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*', str(value or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def norm_hhmm(value) -> str:
    m = hhmm_to_min(value)
    if m is None:
        raise ValueError('执行时间格式应为 HH:MM（24 小时制）')
    return '%02d:%02d' % (m // 60, m % 60)


def now_minutes(now: datetime | None = None) -> int:
    now = now or datetime.now()
    return now.hour * 60 + now.minute


def initial_last_run_date(run_at: str, now: datetime | None = None):
    """新建/改时间时用：今天这个点已经过了就标记「今天已跑」，避免保存后立刻触发。"""
    m = hhmm_to_min(run_at)
    now = now or datetime.now()
    return now.date() if (m is not None and m <= now_minutes(now)) else None


def next_run_at(sch: dict, now: datetime | None = None):
    """下一次执行时间（仅供展示，不参与调度判断）。"""
    m = hhmm_to_min(sch.get('run_at'))
    if m is None or not sch.get('enabled'):
        return None
    now = now or datetime.now()
    today = now.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
    last = sch.get('last_run_date')
    if last == now.date() or today <= now:
        return today + timedelta(days=1)
    return today


def _claim(sid: int) -> dict | None:
    """原子抢占今天的执行权。抢不到说明别的 worker 已抢，或今天已跑过。"""
    cur = jobs.db.x(
        'UPDATE bid_crawl_schedule SET last_run_date=CURRENT_DATE, '
        'last_attempt_at=now() '
        'WHERE id=%s AND enabled '
        '  AND (last_run_date IS NULL OR last_run_date < CURRENT_DATE) '
        'RETURNING *', (sid,))
    row = cur.fetchone()
    jobs.db.commit()
    return dict(row) if row else None


def build_params(sch: dict, platforms: list[str] | None = None) -> dict:
    """空数组 = 全部：不传 platforms / keywords 时 runner 自动取全部启用平台与
    各平台自己的完整关键词集（含平台专属词）。"""
    params: dict = {'dry_run': False, 'schedule_id': int(sch['id'])}
    plats = [str(x).strip() for x in (sch.get('platforms') or []) if str(x).strip()]
    if platforms is not None:
        plats = [str(x).strip() for x in platforms if str(x).strip()]
    if plats:
        params['platforms'] = plats
    words = [str(x).strip() for x in (sch.get('keywords') or []) if str(x).strip()]
    if words:
        params['keywords'] = words
    return params


def _mark(sid: int, **fields):
    keys = list(fields)
    sets = ', '.join('%s=%%s' % k for k in keys)
    jobs.db.x('UPDATE bid_crawl_schedule SET ' + sets + ' WHERE id=%s',
              tuple(fields[k] for k in keys) + (sid,))
    jobs.db.commit()


def fire(sch: dict, *, user: str = '', platforms: list[str] | None = None) -> dict:
    """提交一次采集 job 并把结果记回任务行。调用方负责已抢占（或手动触发）。"""
    params = build_params(sch, platforms)
    who = user or str(sch.get('created_by') or '') or 'system'
    try:
        job, dedupe = jobs.submit('run', None, params, '定时·' + who)
    except Exception as e:                       # noqa: BLE001
        # 队列满等：回退当天的执行标记，5 分钟后重试，别把今天这次丢干净
        _mark(sch['id'], last_run_date=None, last_status='error',
              last_error=str(e)[:500], last_attempt_at=datetime.now())
        return {'ok': False, 'error': str(e)[:300]}
    _mark(sch['id'], last_job_id=int(job['id']),
          last_status='merged' if dedupe else 'queued',
          last_error='', last_attempt_at=datetime.now())
    return {'ok': True, 'job_id': int(job['id']), 'dedupe': dedupe}


def tick(now: datetime | None = None) -> list[dict]:
    """worker 每 30 秒调一次：找出到点且今天没跑过的任务并提交。"""
    now = now or datetime.now()
    cur = now_minutes(now)
    rows = jobs.db.q(
        'SELECT * FROM bid_crawl_schedule WHERE enabled '
        '  AND (last_run_date IS NULL OR last_run_date < CURRENT_DATE) '
        '  AND (last_attempt_at IS NULL '
        "       OR last_attempt_at < now() - (%s * interval '1 second')) "
        'ORDER BY id', (RETRY_AFTER_S,))
    out = []
    for r in rows:
        m = hhmm_to_min(r.get('run_at'))
        if m is None or cur < m:
            continue
        got = _claim(int(r['id']))
        if not got:
            continue
        res = fire(got)
        out.append(dict(res, schedule_id=int(r['id']), name=r.get('name')))
    return out


def run_now(sid: int, user: str = '', platforms: list[str] | None = None) -> dict:
    """手动「立即执行」：不占用当天的自动执行名额，到点仍会照常触发。

    重复点击由 uq_job_active 兜底（同平台活动任务合并），不会堆出一串全平台任务。
    """
    row = jobs.db.one('SELECT * FROM bid_crawl_schedule WHERE id=%s', (sid,))
    if not row:
        return {'ok': False, 'error': '定时任务不存在'}
    return fire(dict(row), user=user, platforms=platforms)
