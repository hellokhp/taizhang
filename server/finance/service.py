# -*- coding: utf-8 -*-
"""财务业务逻辑层。

api.py 只做参数校验与权限，业务规则写这里，保持路由轻薄。
依赖注入：从 api.py 传入 db / username，本模块不直接 import common.database 的全局 db
（便于单元测试时替换连接）。
"""
import json
import time

from ..common.database import db


def list_items_api(limit: int = 100, offset: int = 0):
    """示例：分页列出条目。

    注意（见 references/database.md）：
    - 列表查询只碰瘦主表，大字段拆到详情表
    - 深分页用游标 (sort_col, id) < (:last, :id) 替代 LIMIT offset
    """
    return db.execute(
        'SELECT id, title, amount, created_at FROM fin_item '
        'ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()


def create_item(payload: dict, username: str):
    """示例：新建条目。调用方须已持有 lock（见 api.py）。"""
    cur = db.execute(
        'INSERT INTO fin_item(title, amount, payload, created_by, created_at) '
        'VALUES(?,?,?,?,?)',
        (payload.get('title', ''), payload.get('amount', 0),
         json.dumps(payload, ensure_ascii=False), username, time.time()))
    db.commit()
    return cur.lastrowid
