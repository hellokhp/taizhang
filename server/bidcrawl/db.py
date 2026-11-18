# -*- coding: utf-8 -*-
"""标讯采集 · 独立 PG 访问层（CLI / Worker 使用，`%s` 占位符）。

后端 FastAPI 的 PgDB 用 `?`，本包用 `%s`，两套 SQL 不混用。
后端接口层（server/bidcrawl_api.py）继续用 PgDB，不走这里。
"""
from __future__ import annotations

import threading
from pathlib import Path

from ..shared_config import pg_conf

BASE = Path(__file__).resolve().parent
SCHEMA_PATH = BASE / 'schema.sql'

PG_CONF = pg_conf()

_local = threading.local()


def conn():
    c = getattr(_local, 'conn', None)
    if c is None or c.closed:
        import psycopg2
        c = psycopg2.connect(**PG_CONF)
        c.autocommit = False
        cur = c.cursor()
        for sql in ("SET lock_timeout = '8s'",
                    "SET statement_timeout = '300s'",
                    "SET idle_in_transaction_session_timeout = '60s'"):
            try:
                cur.execute(sql)
                c.commit()
            except Exception:
                c.rollback()
        cur.close()
        _local.conn = c
    return c


def q(sql, args=()):
    """查询，返回 list[dict]；读完立即结束只读事务。"""
    from psycopg2.extras import RealDictCursor
    c = conn()
    cur = c.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return [dict(r) for r in rows]
    except Exception:
        c.rollback()
        raise
    finally:
        cur.close()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    """执行写语句，返回 cursor（支持 RETURNING）；调用方负责 commit()。"""
    from psycopg2.extras import RealDictCursor
    c = conn()
    cur = c.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(sql, args)
        return cur
    except Exception:
        c.rollback()
        cur.close()
        raise


def commit():
    conn().commit()


def rollback():
    conn().rollback()


def close():
    c = getattr(_local, 'conn', None)
    if c and not c.closed:
        try:
            c.close()
        except Exception:
            pass
    _local.conn = None


def init_schema():
    """幂等建表 + 种子数据（schema.sql 全部语句幂等）。"""
    sql = SCHEMA_PATH.read_text(encoding='utf-8')
    c = conn()
    cur = c.cursor()
    try:
        cur.execute(sql)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        cur.close()
