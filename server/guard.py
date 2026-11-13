# -*- coding: utf-8 -*-
"""平台卡死治理 · 第 1 步（安全止血）共用组件。

三个互不相干的小东西，业务模块只管用、不反向依赖本模块之外的东西：
  1. TimingMiddleware —— 纯 ASGI 慢请求日志（只记到响应头的 TTFB，不缓冲响应体）
  2. heavy / heavy_slot —— 重活接口并发闸门，防止 N 个慢请求占满 40 线程池
  3. health_payload    —— /healthz 数据源，任何单项异常都不让健康检查变 500

对应《平台卡死治理与故障隔离方案 v2.0》：§6.4 超时分级、§8 并发与限流、
§9 可观测性、§7.4 健康检查。

注意：并发闸门是**进程内**的，当前部署为单进程（--workers 1），语义正确；
将来按域拆进程后必须换成 Redis / PG 实现（方案 §8、§11）。
"""
from __future__ import annotations

import functools
import inspect
import logging
import os
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException

log = logging.getLogger('taizhang.guard')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s'))
    log.addHandler(_h)
    log.propagate = False          # 避免与 uvicorn/root logger 重复输出
log.setLevel(logging.INFO)

SLOW_MS = int(os.environ.get('SLOW_REQUEST_MS', '1000'))
HEAVY_CAP = int(os.environ.get('HEAVY_CONCURRENCY', '2'))


# ---------------- 1. 慢请求日志 ----------------
def _tracked(path: str) -> bool:
    if not path.startswith('/api/'):
        return False
    # SSE 进度流是长连接：TTFB 很短但结束时间以分钟计，不参与慢请求统计
    if path.startswith('/api/ai-summary/tasks/') and path.endswith('/stream'):
        return False
    return True


class TimingMiddleware:
    """纯 ASGI 中间件，只包 http.response.start，响应体原样透传。

    刻意不用 @app.middleware('http') / BaseHTTPMiddleware —— 那会包装
    StreamingResponse，开标汇总 SSE 的事件流可能被缓冲或截断。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not _tracked(scope.get('path', '')):
            await self.app(scope, receive, send)
            return

        t0 = time.perf_counter()
        path = scope.get('path', '')
        method = scope.get('method', '')

        async def send_wrapper(message):
            if message['type'] == 'http.response.start':
                ms = (time.perf_counter() - t0) * 1000
                if ms >= SLOW_MS:
                    log.warning('SLOW %s %s %.0fms status=%s',
                                method, path, ms, message.get('status'))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception('UNHANDLED %s %s (%.0fms)',
                          method, path, (time.perf_counter() - t0) * 1000)
            raise


# ---------------- 2. 重活并发闸门 ----------------
_sems = {}
_inflight = {}
_state_lock = threading.Lock()


def _sem(name, cap):
    with _state_lock:
        s = _sems.get(name)
        if s is None:
            s = threading.BoundedSemaphore(cap)
            _sems[name] = s
        return s


@contextmanager
def heavy_slot(name, cap=None):
    """占用一个重活名额；满了立即返回 429，不排队、不占线程池线程。

    这是「40 个慢请求打满线程池」的直接止血点：抢不到名额的请求在 1ms 内
    被拒，而不是排队等全局 RLock 把 40 个线程全部占住。
    """
    limit = int(cap or HEAVY_CAP)
    sem = _sem(name, limit)
    if not sem.acquire(blocking=False):
        raise HTTPException(429, '「%s」并发已达上限 %d，请等待当前任务完成后重试'
                            % (name, limit))
    with _state_lock:
        _inflight[name] = _inflight.get(name, 0) + 1
    try:
        yield
    finally:
        with _state_lock:
            _inflight[name] = max(0, _inflight.get(name, 1) - 1)
        sem.release()


def heavy(name, cap=None):
    """端点装饰器版本；同步 / async 端点都支持。

    functools.wraps 保留 __wrapped__ 链，FastAPI 仍能正确解析原端点参数签名
    （项目里 fs_api._wrap 已用同样方式验证过）。
    """
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **kw):
                with heavy_slot(name, cap):
                    return await fn(*a, **kw)
            return aw

        @functools.wraps(fn)
        def w(*a, **kw):
            with heavy_slot(name, cap):
                return fn(*a, **kw)
        return w
    return deco


# ---------------- 3. 健康检查数据 ----------------
def health_payload(db):
    """返回进程 / PG / 任务积压状态；单项异常只体现在字段里，不抛给调用方。"""
    with _state_lock:
        out = {'ok': True, 'pid': os.getpid(), 'ts': time.time(),
               'db': 'unknown', 'ai_queue': None, 'heavy': dict(_inflight)}
    try:
        r = db.execute('SELECT 1 AS ok').fetchone()
        out['db'] = 'ok' if r else 'unknown'
    except Exception as e:
        out['ok'] = False
        out['db'] = 'error: %s' % str(e)[:160]
    try:
        r = db.execute("SELECT count(*) AS c FROM ai_summary_task "
                       "WHERE status IN ('PENDING','RUNNING')").fetchone()
        out['ai_queue'] = int(r['c']) if r else 0
    except Exception:
        # AI 表尚未建好也视为进程健康，冷启动由 ai_schema.sql 自检负责
        out['ai_queue'] = None
    return out


# ---------------- 4. 去卡死：带超时的全局锁 / 细粒度锁 / 事务边界 ----------------
class TimedRLock:
    """可重入锁，但获取带上限：等待超时直接 503，绝不无限排队。

    替换 main.py 原来的 threading.RLock。可重入性必须保留 —— 锁内会嵌套调用
    perm_of() / need() / audit_write() / fs.lock()，换成普通 Lock 会自死锁。
    仍保持单一把全局锁：拆成多个域锁会形成「台账锁 -> 文件锁」与
    「文件锁 -> 台账锁」的 ABBA 死锁（见方案 §15.2）。
    """

    def __init__(self, timeout=None):
        self._r = threading.RLock()
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get('LOCK_WAIT_SECONDS', '3'))

    def __enter__(self):
        if not self._r.acquire(timeout=self.timeout):
            raise HTTPException(503, '系统繁忙：等待资源超过 %gs，请稍后重试' % self.timeout)
        return self

    def __exit__(self, *exc):
        self._r.release()
        return False

    def acquire(self, *a, **kw):
        return self._r.acquire(*a, **kw)

    def release(self):
        return self._r.release()


_keyed_locks = {}
_keyed_guard = threading.Lock()


@contextmanager
def keyed(name):
    """按业务键的细粒度互斥，替代「用全局锁保护某一类慢写操作」。

    慢写（导入 / 清空）用它而不是全局锁，就不会在持锁期间挡住登录、看板、
    文件与 AI 的所有请求；同类慢写之间仍然串行。
    """
    with _keyed_guard:
        lk = _keyed_locks.get(name)
        if lk is None:
            lk = threading.RLock()
            _keyed_locks[name] = lk
    lk.acquire()
    try:
        yield
    finally:
        lk.release()


@contextmanager
def tx(db):
    """显式事务边界：异常必 rollback，避免连接停在 idle in transaction
    持有行锁（fs.py:292 记录过这类事故把全站拖死）。

    注意 PgDB 对 SELECT 会自动 commit，因此这里不是严格的 ACID 事务边界，
    主要职责是「异常时不留下未提交事务」。彻底的事务化属于方案 §6.1 后续项。
    """
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


@contextmanager
def unlocked():
    """显式标记「纯读 / 已由 PG MVCC 保证一致性，不需要全局互斥」。

    保留 with 结构是为了不改动既有代码块缩进，同时让审查时能一眼看出
    哪些路径已解锁。当前端点是每线程独立 PG 连接，读读、读写都不需要进程内互斥。
    """
    yield
