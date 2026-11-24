# -*- coding: utf-8 -*-
"""管理中心域路由（/api/admin/*）—— 仅管理部可进的全局管理视图。

框架约束（见 .workbuddy/skills/taizhang-arch-guardian/SKILL.md）：
- 用相对 path（不带 /api），最终路径由 main.py 的 include_router prefix 决定
- 从 common.* 取 db / 鉴权，禁止自建基建
- 准入：main.py 挂载时加 dependencies=[Depends(require_module('admin'))]，
  部门层只允许管理部（DEPARTMENTS 中 admin 只在 management 的模块元组里）
- 用户/角色的全量 CRUD 复用 common_router 的 /api/users、/api/roles（system=all），
  这里只提供看板聚合数据，避免重复实现。
"""
from fastapi import APIRouter, Depends

from .common.database import db
from .common.guard import unlocked
from .common.security import current_user

admin_router = APIRouter(prefix='')


@admin_router.get('/overview')
def overview(username: str = Depends(current_user)):
    """管理中心看板：全局用户/角色统计与按部门、按系统分布。"""
    with unlocked():
        total_users = db.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
        total_roles = db.execute('SELECT COUNT(*) c FROM roles').fetchone()['c']
        by_dept = db.execute(
            "SELECT COALESCE(NULLIF(dept,''),'bid') k, COUNT(*) c FROM users "
            "GROUP BY COALESCE(NULLIF(dept,''),'bid') ORDER BY c DESC").fetchall()
        by_role_sys = db.execute(
            "SELECT COALESCE(NULLIF(system,''),'taizhang') k, COUNT(*) c FROM roles "
            "GROUP BY COALESCE(NULLIF(system,''),'taizhang') ORDER BY c DESC").fetchall()
        admins = db.execute('SELECT COUNT(*) c FROM users WHERE is_admin=1').fetchone()['c']
    return {
        'totalUsers': int(total_users),
        'totalRoles': int(total_roles),
        'adminCount': int(admins),
        'usersByDept': [{'key': r['k'], 'count': int(r['c'])} for r in by_dept],
        'rolesBySystem': [{'key': r['k'], 'count': int(r['c'])} for r in by_role_sys],
    }
