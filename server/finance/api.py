# -*- coding: utf-8 -*-
"""财务域路由（/api/finance/*）。

框架约束（见 .workbuddy/skills/taizhang-arch-guardian/SKILL.md）：
- 用相对 path（不带 /api），最终路径由 main.py 的 include_router prefix 决定
- 从 common.* 取 db / lock / 鉴权 / 审计，禁止自建
- 权限点先登记到 common/catalog.py 的 PERM_CATALOG
- 纯读用 guard.unlocked()，慢写用 guard.keyed() + guard.tx()，重活用 guard.heavy()
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..common.database import db
from ..common.guard import heavy, keyed, lock, tx, unlocked
from ..common.security import current_user, need

# 域路由组：最终挂载由 main.py 决定，path 一律相对形式
finance_router = APIRouter(prefix='')


def init_tables(dbobj=None):
    """幂等建表。由 main.py 启动时调用（初始化副作用留在入口，框架硬约束 #7）。"""
    target = dbobj or db
    target.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))


@finance_router.get('/items')
def list_items(username: str = Depends(current_user)):
    need(username, 'view.finance', '无财务模块查看权限')
    with unlocked():
        rows = db.execute('SELECT * FROM fin_item ORDER BY id DESC LIMIT 100').fetchall()
    return {'items': [dict(r) for r in rows]}


#  Finance 写操作模板（取消注释并按业务改造）：
# @finance_router.post('/items')
# @heavy('财务写入', cap=1)                    # 重活闸门；装饰器在路由装饰器下方
# def create_item(body: dict, username: str = Depends(current_user)):
#     need(username, 'fin.edit', '无财务编辑权限')
#     with lock:                               # 全进程唯一锁（common.guard.lock）
#         with tx(db):                         # 异常必回滚
#             ...
#     _audit(...)
