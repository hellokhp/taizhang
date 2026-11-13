# -*- coding: utf-8 -*-
"""Worker 侧 Redis 信号（同步客户端即可，Worker 不是 async）。

唤醒队列用 BLPOP，超时 3 秒即天然轮询兜底；Redis 挂掉直接 sleep 轮询。
进度 publish 失败可忽略 —— PG 的 ai_task_event 才是事实源。
"""
from __future__ import annotations

import json
import logging
import time

from . import config

log = logging.getLogger('taizhang.ai.worker.signal')
_client = None
_down_until = 0.0


def _c():
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(
            config.REDIS_URL, socket_connect_timeout=1, socket_timeout=8,
            decode_responses=True, health_check_interval=30, protocol=2)
        # 两处硬约束：
        # 1) protocol=2：Redis 3.x 不支持 RESP3 的 HELLO，redis-py 6/8 默认会握手失败。
        # 2) socket_timeout 必须 > BLPOP 的阻塞 timeout（wait_wakeup 默认 3s）。
        #    若设成 1s，每次 BLPOP 都会读超时，被误判成「Redis 挂了」而永久降级轮询。
    return _client


def wait_wakeup(timeout=3):
    """返回 task_id 字符串或 None；Redis 不可用时 sleep 后返回 None（轮询兜底）。"""
    global _down_until
    if time.time() < _down_until:
        time.sleep(timeout)
        return None
    try:
        r = _c().blpop(config.WAKE_KEY, timeout=timeout)
        return r[1] if r else None
    except Exception as e:
        log.warning('[ai] Redis 不可用，BLPOP 降级为轮询：%s', str(e)[:120])
        _down_until = time.time() + 30
        time.sleep(timeout)
        return None


def publish(task_id, event):
    global _down_until
    if time.time() < _down_until:
        return False
    try:
        payload = dict(event or {})
        payload['task_id'] = int(task_id)
        payload.setdefault('at', time.time())
        _c().publish(config.TASK_CHANNEL + str(task_id),
                     json.dumps(payload, ensure_ascii=False, default=str))
        return True
    except Exception as e:
        log.warning('[ai] Redis publish 失败（已忽略）：%s', str(e)[:120])
        _down_until = time.time() + 30
        return False
