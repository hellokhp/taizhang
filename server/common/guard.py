# -*- coding: utf-8 -*-
"""并发/超时/健康组件 —— 转发层（阶段 1 不迁移实现，保证全进程单一实例）。

为什么是转发而不是复制：guard 内部持有进程级状态（_sems 并发闸门、_keyed_locks
细粒度锁、_inflight 计数）。若在 common 与 server 各放一份实现，main.py 与拆出的
新 app 会各自持有一套锁，互斥语义立刻失效 —— 这是比"代码整齐"严重得多的问题。

阶段 2 把锁换成一个共享的 Redis 实现后，各进程天然共享状态，届时再把实现整体
迁进本文件、让 server/guard.py 反过来转发即可，调用方代码一行不用改。

所有名字来自同一个 server.guard 模块对象；server.main 继续 from . import guard，
common 内的模块 from . import guard，两者拿到的是同一个模块，状态唯一。
"""
from ..guard import (  # noqa: F401
    SLOW_MS,
    HEAVY_CAP,
    TimingMiddleware,
    TimedRLock,
    heavy,
    heavy_slot,
    health_payload,
    keyed,
    tx,
    unlocked,
)

# 全局锁实例。重构前它在 server.main 里创建（lock = guard.TimedRLock()），
# 现在收进本模块，使 main 与拆出的各 app 共享同一把锁实例。
# 阶段 2 会把 TimedRLock 的底层换成 Redis 分布式锁，这里就是唯一切换点。
lock = TimedRLock()
