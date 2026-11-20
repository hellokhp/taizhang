# -*- coding: utf-8 -*-
"""数据访问层：每线程 PG 连接 + PgDB（从 main.py 原样抽出，行为零变化）。

同时收纳原 main.py 顶部的一组路径常量（BASE/ROOT/DB_PATH/USERS_PATH）。
注意路径基准点：本文件位于 server/common/ 下，__file__.parent 是 common 目录，
因此用 .parent.parent 回到 server/ 目录，保证 DB_PATH/USERS_PATH 指向与重构前
完全相同的位置。
"""
import threading
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from .config import pg_conf

BASE = Path(__file__).resolve().parent.parent   # server/（common 的上一级）
ROOT = BASE.parent                              # 项目根
DB_PATH = BASE / 'data.db'                      # 历史 SQLite（仅一次性迁移/回滚备份）
USERS_PATH = BASE / 'users.json'

PG_CONF = pg_conf()

# FastAPI 同步接口在线程池中执行，psycopg2 连接不可跨线程共享：每线程一个连接。
_local = threading.local()


def _conn():
    """每线程一个连接（FastAPI 同步接口在线程池中执行，psycopg2 连接不可跨线程共享）。"""
    conn = getattr(_local, 'conn', None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(**PG_CONF)
        conn.autocommit = False
        # 平台卡死治理 第 1 步：前台连接一律设上限，绝不允许无限等待 / 无限挂起。
        #   lock_timeout                      3s   行锁等待，超时立即报错而非拖住全站
        #   statement_timeout                 10s  单条慢 SQL 自动终止（PG 是逐语句计时，
        #                                        逐行写的大导入不受影响）
        #   idle_in_transaction_session...   30s  开事务不干活的连接自动断开
        # 逐条 SET + 逐条提交：老版本 PG 不支持某一条时，一次 rollback 不会把
        # 前面已生效的设置一起撤销。Worker 连接独立设 300s，见 ai_worker/db.py。
        cur = conn.cursor()
        for sql in ("SET lock_timeout = '3s'",
                    "SET statement_timeout = '10s'",
                    "SET idle_in_transaction_session_timeout = '30s'"):
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()
        cur.close()
        _local.conn = conn
    return conn


class PgDB:
    """沿用原 SQLite 调用习惯（? 占位符 / executemany / commit / executescript）访问 PostgreSQL。
    读语句执行完立即结束只读事务；写语句由调用方 commit() 提交，异常时自动回滚。"""

    @staticmethod
    def _sql(sql):
        return sql.replace('?', '%s')

    def execute(self, sql, params=()):
        conn = _conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(self._sql(sql), params)
            if sql.lstrip()[:6].upper() == 'SELECT':
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        return cur

    def executemany(self, sql, seq):
        conn = _conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.executemany(self._sql(sql), seq)
        except Exception:
            conn.rollback()
            raise
        return cur

    def executescript(self, script):
        conn = _conn()
        cur = conn.cursor()
        try:
            cur.execute(script)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def commit(self):
        _conn().commit()

    def rollback(self):
        _conn().rollback()


db = PgDB()
