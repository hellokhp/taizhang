# -*- coding: utf-8 -*-
"""Worker 独立 PG 连接（psycopg2，`%s` 占位符）。

后端 PgDB 用 `?`，这里用 `%s` —— 两套 SQL 不能混用，这是最容易出错的地方。
连接不跨线程共享；Worker 主线程独占一条，文件解析线程池不碰 DB。
"""
from __future__ import annotations

import threading

import psycopg2
from psycopg2.extras import RealDictCursor

from . import config

_local = threading.local()


def conn():
    c = getattr(_local, 'conn', None)
    if c is None or c.closed:
        c = psycopg2.connect(**config.PG_CONF)
        c.autocommit = False
        # 逐条 SET + 逐条提交：老版本 PG 若不支持某一条，不能让一次 rollback
        # 把前面已生效的设置也撤销。Worker 允许长事务，与前台 10s 严格分离。
        cur = c.cursor()
        for sql in ("SET lock_timeout = '8s'",
                    "SET statement_timeout = '300s'",
                    "SET idle_in_transaction_session_timeout = '30s'"):
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
    """执行写语句，返回 cursor（含 RETURNING 结果）；调用方负责 commit()。"""
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
