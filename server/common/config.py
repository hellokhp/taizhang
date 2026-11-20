# -*- coding: utf-8 -*-
"""PG 配置统一入口（薄封装）。

阶段 1 刻意不复制 shared_config.py 的实现：ai_worker、bidcrawl 等独立进程仍从
shared_config 读取，保持"唯一事实源"。这里只做转发，让新拆出的 app 将来可以
统一写成 from common.config import pg_conf，而底层仍是同一个模块、同一份缓存。

阶段 2/3 若要彻底合并配置实现，只需改 shared_config 为转发到本模块即可，
调用方无需改动。
"""
from ..shared_config import (  # noqa: F401
    config_path,
    lock_backend,
    pg_conf,
    pg_password,
    pg_summary,
    redis_conf,
)
