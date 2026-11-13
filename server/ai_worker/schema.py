# -*- coding: utf-8 -*-
"""Worker 冷启动自检：确保 ai_* 表存在（与后端共用 server/ai_schema.sql）。

为什么需要这个文件：
  建表原本只在后端 3.8 的 ai_summary_api.init_tables() 里做，于是
  「后端没重启 / Worker 先启动」时 Worker 会无限报 relation ... does not exist。
  两个进程 Python 版本与依赖不同、不能共享 Python 模块，但 DDL 是纯 SQL，
  共用一份 ai_schema.sql 即可让 Worker 独立冷启动。

幂等：整段 SQL 全是 IF NOT EXISTS / ON CONFLICT DO NOTHING，重复执行安全。
"""
from __future__ import annotations

from pathlib import Path

from . import db

SCHEMA_FILE = Path(__file__).resolve().parents[1] / 'ai_schema.sql'

# PG 无 ADD COLUMN IF NOT EXISTS，老库升级逐条 try（与后端 AI_ALTER_COLS 一致）
ALTER_COLS = (
    ('ai_summary_task', 'prompt_version', "TEXT DEFAULT ''"),
    ('ai_summary_task', 'summary_id', 'BIGINT'),
    ('ai_summary', 'raw_result', "JSONB DEFAULT '{}'"),
    ('ai_summary', 'prompt_version', "TEXT DEFAULT ''"),
    ('ai_prompt_fewshot', 'is_regression', 'SMALLINT DEFAULT 0'),
)

EXPECTED_TABLES = 11


def ensure_schema():
    """执行共享 DDL + 补列，返回自检信息；失败直接抛给 main() 做 fail-fast。"""
    if not SCHEMA_FILE.is_file():
        raise RuntimeError('共享 DDL 缺失：%s' % SCHEMA_FILE)
    sql = SCHEMA_FILE.read_text(encoding='utf-8')

    c = db.conn()
    cur = c.cursor()
    try:
        # psycopg2 支持一次执行多语句；不传 vars，避免 SQL 里的 % 被当占位符
        cur.execute(sql)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        cur.close()

    for table, col, typ in ALTER_COLS:
        try:
            db.x('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, typ))
            db.commit()
        except Exception:
            db.rollback()

    n = db.one("SELECT COUNT(*) AS n FROM pg_tables "
               "WHERE schemaname='public' AND tablename ~ '^ai_'")
    t = db.one('SELECT COUNT(*) AS n FROM ai_field_template')
    tables = int(n['n'] or 0)
    if tables < EXPECTED_TABLES:
        raise RuntimeError('ai_* 表只建成 %d/%d 张，请检查 DDL 执行权限' %
                           (tables, EXPECTED_TABLES))
    return {'tables': tables, 'templates': int(t['n'] or 0),
            'file': str(SCHEMA_FILE)}
