# -*- coding: utf-8 -*-
"""开标汇总 AI · Redis 实时信号层（可丢、可降级）。

设计铁律：PG 是任务状态与结果的唯一事实源，Redis 只传瞬时事件。
- 后端入队后 `wake(task_id)` 发唤醒信号；失败静默忽略（Worker 有 3s 轮询兜底）。
- Worker 每阶段 `publish_progress(task_id, event)`；失败静默忽略。
- SSE 端点订阅 `taizhang:ai:task:<id>`；Redis 不可用时返回 None，
  由 API 层推 `degraded` 事件并退化为服务端 3 秒轮询读 PG。

Redis 里绝不出现：投标文件原文、报价、API Key。只含状态/计数/文件名/时间戳。
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger('taizhang.ai.signal')

PREFIX = 'taizhang:ai:'
WAKE_KEY = PREFIX + 'wakeup'
TASK_CHANNEL = PREFIX + 'task:'
TASK_TTL = 86400          # 唤醒队列最多积压一天，避免 Redis 内存无限增长

_client = None
_aclient = None
_disabled_until = 0.0
_DISABLE_GAP = 30.0       # 连接失败后 30 秒内不再重试，避免每个请求都卡超时


def _url():
    return os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')


def _enabled():
    return os.environ.get('AI_SIGNAL', '1') != '0'


def _now():
    return time.time()


def _mark_down(why):
    global _disabled_until, _client, _aclient
    _disabled_until = _now() + _DISABLE_GAP
    _client = None
    _aclient = None
    log.warning('[ai] Redis 信号层不可用，自动降级为轮询：%s', why)


def is_available():
    """SSE 端点用来判断是否走实时订阅。不会阻塞超过连接超时。"""
    if not _enabled() or _now() < _disabled_until:
        return False
    try:
        get_sync().ping()
        return True
    except Exception as e:
        _mark_down(str(e)[:120])
        return False


def get_sync():
    """同步客户端（普通同步端点 + Worker 用）。"""
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(
            _url(), socket_connect_timeout=1, socket_timeout=3,
            decode_responses=True, health_check_interval=30, protocol=2)
        # protocol=2 是硬要求：Redis 3.x（含 Windows 3.0.504）不支持 RESP3 的
        # HELLO 握手，redis-py 6/8 默认协商 RESP3 会报 "unknown command 'HELLO'"。
        # socket_timeout 保持短（请求路径要快速失败），但仍 > 1s 的 pubsub 读超时。
    return _client


def get_async():
    """异步客户端（仅 SSE 端点用，必须 asyncio，否则会阻塞 event loop）。"""
    global _aclient
    if _aclient is None:
        import redis.asyncio as aredis
        _aclient = aredis.from_url(
            _url(), socket_connect_timeout=1, socket_timeout=3,
            decode_responses=True, health_check_interval=30, protocol=2)
        # next_message() 的 get_message(timeout=1.0) 需要 socket_timeout > 1.0，
        # 否则空闲 1 秒就会读超时，SSE 被误判为 Redis 故障而推 degraded。
    return _aclient


def wake(task_id):
    """后端建任务后唤醒 Worker；失败可忽略，Worker 有 BLPOP 超时轮询兜底。"""
    if not _enabled() or _now() < _disabled_until:
        return False
    try:
        c = get_sync()
        c.lpush(WAKE_KEY, str(task_id))
        c.ltrim(WAKE_KEY, 0, 999)
        c.expire(WAKE_KEY, TASK_TTL)
        return True
    except Exception as e:
        _mark_down(str(e)[:120])
        return False


def publish_progress(task_id, event):
    """Worker 阶段进度广播；失败可忽略，PG 里的 ai_task_event 才是事实源。"""
    if not _enabled() or _now() < _disabled_until:
        return False
    try:
        payload = dict(event or {})
        payload['task_id'] = int(task_id)
        payload.setdefault('at', _now())
        get_sync().publish(TASK_CHANNEL + str(task_id), json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as e:
        _mark_down(str(e)[:120])
        return False


def channel(task_id):
    return TASK_CHANNEL + str(task_id)


async def subscribe(task_id):
    """返回 (pubsub, client) 或 None。调用方负责 finally 里 close。"""
    if not _enabled() or _now() < _disabled_until:
        return None
    try:
        c = get_async()
        ps = c.pubsub(ignore_subscribe_messages=True)
        await ps.subscribe(channel(task_id))
        return ps, c
    except Exception as e:
        _mark_down(str(e)[:120])
        return None


async def unsubscribe(ps):
    try:
        await ps.unsubscribe()
        await ps.close()
    except Exception:
        pass


async def next_message(ps, timeout=1.0):
    """非阻塞式取一条消息；无消息返回 None（让出 event loop 给 ping/取消检查）。"""
    try:
        m = await ps.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if not m or m.get('type') != 'message':
            return None
        return json.loads(m['data'])
    except Exception:
        return None
