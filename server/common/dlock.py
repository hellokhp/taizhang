# -*- coding: utf-8 -*-
"""可切换的锁后端：local（进程内可重入）/ redis（跨进程可重入分布式锁）。

设计目标：接口与 threading.RLock 完全兼容（acquire/release/上下文管理），
使 guard.TimedRLock 只需把内部实现从 threading.RLock 换成一个 DLock 实例，
上层 main/fs/ai 数千行 `with lock:` 代码一行不改。

阶段 2 语义保持不变：默认 local；只有 LOCK_BACKEND=redis 时才走分布式实现。

Redis 可重入锁原理：
  - key = 'taizhang:lock:<name>'，值为 Redis HASH {owner_token: reentrant_count}
  - owner_token 每线程一个（threading.local），存于 thread local，避免线程间误删
  - 加锁 Lua：key 不存在 → HSET token=1 + PEXPIRE ttl；key 存在且 token 是自己的
    → HINCRBY + 续期（可重入）；否则返回 0 表示未抢到
  - 解锁 Lua：HINCRBY -1；<=0 则 DEL key；否则仅续期
  - TTL 兜底：持锁进程崩溃后锁自动过期，绝不永久死锁（代价是极端情况下
    业务超时的锁可能被他人抢占，故 ttl 要显著大于最长临界区耗时）
"""
import os
import threading
import time
import uuid

from fastapi import HTTPException

from .config import lock_backend, redis_conf

# ---------------------------------------------------------------------------
# Lua 脚本：全部做成原子操作，避免"判断-设置"之间的竞态
# ---------------------------------------------------------------------------
# 加锁：keys[1]=锁名，argv[1]=token，argv[2]=ttl(ms)
# 返回 1=获得(含可重入)，0=未获得
_LUA_ACQUIRE = """
local key = KEYS[1]
local token = ARGV[1]
local ttl = tonumber(ARGV[2])
if redis.call('EXISTS', key) == 0 then
  redis.call('HSET', key, token, 1)
  redis.call('PEXPIRE', key, ttl)
  return 1
end
if redis.call('HEXISTS', key, token) == 1 then
  redis.call('HINCRBY', key, token, 1)
  redis.call('PEXPIRE', key, ttl)
  return 1
end
return 0
"""

# 解锁：keys[1]=锁名，argv[1]=token
# 返回 1=已释放(删key)，2=递归计数减一(仍持有)，0=token 不匹配(非持有者，忽略)
_LUA_RELEASE = """
local key = KEYS[1]
local token = ARGV[1]
if redis.call('HEXISTS', key, token) == 0 then
  return 0
end
local n = redis.call('HINCRBY', key, token, -1)
if n <= 0 then
  redis.call('DEL', key)
  return 1
end
return 2
"""

# 续期：keys[1]=锁名，argv[1]=token，argv[2]=ttl(ms)。仅持有者可续，返回1成功
_LUA_RENEW = """
local key = KEYS[1]
local token = ARGV[1]
local ttl = tonumber(ARGV[2])
if redis.call('HEXISTS', key, token) == 1 then
  redis.call('PEXPIRE', key, ttl)
  return 1
end
return 0
"""

_redis_client = None
_redis_lock = threading.Lock()


def _get_redis():
    """惰性创建全局 Redis 客户端（连接池由 redis-py 内部维护，线程安全）。"""
    global _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                import redis
                conf = redis_conf()
                _redis_client = redis.Redis(
                    host=conf['host'], port=conf['port'], db=conf['db'],
                    password=conf['password'] or None,
                    decode_responses=True, socket_connect_timeout=2,
                    socket_timeout=2, health_check_interval=30,
                    # 现场 Redis 5.0 不支持 HELLO/RESP3，redis-py 8.x 默认 RESP3 会报
                    # ResponseError；强制 RESP2 兼容老服务端（Lua 脚本命令均为 2.x 即有）。
                    protocol=2)
    return _redis_client


class LocalRLock:
    """进程内可重入锁（阶段 1 行为），保留以支撑 local 后端。"""
    def __init__(self, timeout=None):
        self._r = threading.RLock()
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get('LOCK_WAIT_SECONDS', '3'))

    def acquire(self, blocking=True, timeout=-1):
        if not blocking:
            return self._r.acquire(blocking=False)
        t = self.timeout if timeout is None or timeout < 0 else timeout
        ok = self._r.acquire(timeout=t)
        if not ok:
            raise HTTPException(503, '系统繁忙：等待资源超过 %gs，请稍后重试' % self.timeout)
        return ok

    def release(self):
        return self._r.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class RedisRLock:
    """跨进程可重入锁。接口与 LocalRLock 一致，可原地替换。

    owner token 存 threading.local：同一线程内的嵌套 acquire 视为可重入。
    FastAPI 同步端点在线程池执行，同一请求始终落在同一线程，语义正确。
    """

    def __init__(self, name, timeout=None, ttl_ms=None):
        self.key = 'taizhang:lock:' + name
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get('LOCK_WAIT_SECONDS', '3'))
        # ttl 要显著大于最长临界区；台账写接口多在 statement_timeout=10s 内完成，给 30s
        self.ttl_ms = int(ttl_ms or os.environ.get('LOCK_TTL_MS', '30000'))
        self._local = threading.local()

    def _token(self):
        tok = getattr(self._local, 'token', None)
        if tok is None:
            tok = uuid.uuid4().hex
            self._local.token = tok
        return tok

    def _depth(self):
        return getattr(self._local, 'depth', 0)

    def acquire(self, blocking=True, timeout=None):
        r = _get_redis()
        token = self._token()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        acquired = bool(r.eval(_LUA_ACQUIRE, 1, self.key, token, self.ttl_ms))
        if acquired:
            self._local.depth = self._depth() + 1
            return True
        if not blocking:
            return False
        # 轮询抢锁；间隔 20ms，避免惊群
        while time.monotonic() < deadline:
            time.sleep(0.02)
            acquired = bool(r.eval(_LUA_ACQUIRE, 1, self.key, token, self.ttl_ms))
            if acquired:
                self._local.depth = self._depth() + 1
                return True
        raise HTTPException(503, '系统繁忙：等待分布式锁超过 %.1fs（%s）'
                            % (self.timeout, self.key))

    def release(self):
        depth = self._depth()
        if depth <= 0:
            return
        r = _get_redis()
        try:
            r.eval(_LUA_RELEASE, 1, self.key, self._token())
        finally:
            self._local.depth = depth - 1

    def renew(self):
        """供长临界区手动续期；普通 `with lock:` 无需调用（TTL 已够长）。"""
        return bool(_get_redis().eval(_LUA_RENEW, 1, self.key, self._token(), self.ttl_ms))

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def make_lock(name='global', timeout=None):
    """按 LOCK_BACKEND 返回对应锁实例。guard.TimedRLock 内部用它替换 threading.RLock。"""
    if lock_backend() == 'redis':
        return RedisRLock(name, timeout=timeout)
    return LocalRLock(timeout=timeout)
