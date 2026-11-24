# -*- coding: utf-8 -*-
"""鉴权与权限计算（从 main.py 原样抽出，函数签名与函数体逐字不变）。

依赖：db / catalog / guard —— 全部来自 common 内部，无循环依赖。
"""
import hashlib
import json

from fastapi import Depends, Header, HTTPException

from . import guard
from .catalog import (DEPARTMENTS, DEPT_KEYS, FIELD_KEYS, LEVEL_KEYS,
                      MASK_KEYS, MODULES, PERM_KEYS)
from .database import db


def hash_pw(pw):
    return hashlib.sha256(('yadecai|' + pw).encode('utf-8')).hexdigest()


# ---------------- 鉴权 ----------------
def current_user(authorization: str = Header(default='')):
    token = authorization[7:].strip() if authorization.startswith('Bearer ') else ''
    if not token:
        raise HTTPException(401, '未登录')
    with guard.unlocked():
        row = db.execute('SELECT username FROM sessions WHERE token=?', (token,)).fetchone()
    if not row:
        raise HTTPException(401, '登录已过期，请重新登录')
    return row['username']


def require_admin(username: str = Depends(current_user)):
    with guard.unlocked():
        row = db.execute('SELECT is_admin FROM users WHERE username=?', (username,)).fetchone()
    if not row or not row['is_admin']:
        raise HTTPException(403, '需要管理员权限')
    return username


# ---------------- 权限计算 ----------------
def norm_field_perms(raw):
    """统一字段权限结构为 {字段: {'level','mask'}}，只保留非「可写」项。
    兼容历史数据：none→hidden、read→readonly、write→writable。"""
    out = {}
    for k, v in (raw or {}).items():
        if k not in FIELD_KEYS:
            continue
        if isinstance(v, dict):
            lv = str(v.get('level') or 'writable')
            mk = str(v.get('mask') or 'full')
        else:
            lv = {'none': 'hidden', 'read': 'readonly', 'write': 'writable'}.get(str(v), str(v))
            mk = 'full'
        if lv not in LEVEL_KEYS:
            continue
        if lv == 'writable':
            continue
        out[k] = {'level': lv, 'mask': mk if mk in MASK_KEYS else 'full'}
    return out


def mask_value(v, pattern):
    """按脱敏规则生成展示值（后端返回脱敏值，前端不再二次处理）。"""
    s = '' if v is None else str(v)
    if not s:
        return s
    if pattern == 'phone':
        return (s[:3] + '****' + s[-4:]) if len(s) >= 8 else '***'
    if pattern == 'name':
        return s[0] + '*' * max(1, len(s) - 1)
    if pattern == 'amount':
        return (s[0] + '***') if len(s) > 1 else '***'
    if pattern == 'text':
        return (s[:2] + '***') if len(s) > 2 else '***'
    return '***'


def field_rule(field_perms, k):
    return (field_perms or {}).get(k) or {'level': 'writable', 'mask': 'full'}


def field_level(field_perms, k):
    return field_rule(field_perms, k)['level']


def perm_of(username):
    """返回当前用户的有效权限：{'roleKey','roleName','perms','fieldPerms','dataScope'}。
    管理员全放行、全字段可写；普通用户取所属角色配置，未配置字段默认可写。"""
    with guard.unlocked():
        u = db.execute('SELECT username, is_admin, role_key FROM users WHERE username=?',
                       (username,)).fetchone()
        if not u:
            raise HTTPException(401, '账号不存在')
        if u['is_admin']:
            return {'roleKey': 'admin', 'roleName': '管理员', 'dataScope': 'ALL',
                    'perms': {k: True for k in PERM_KEYS}, 'fieldPerms': {}}
        r = db.execute('SELECT * FROM roles WHERE key=?', (u['role_key'] or 'staff',)).fetchone()
    if not r:
        r = {'key': 'staff', 'name': '投标专员', 'perms': '{}', 'field_perms': '{}', 'data_scope': 'ALL'}
    try:
        saved = json.loads(r['perms'] or '{}')
    except Exception:
        saved = {}
    try:
        fp = json.loads(r['field_perms'] or '{}')
    except Exception:
        fp = {}
    perms = {k: bool(saved.get(k)) for k in PERM_KEYS}
    return {'roleKey': r['key'], 'roleName': r['name'], 'perms': perms,
            'fieldPerms': norm_field_perms(fp), 'dataScope': (r['data_scope'] or 'ALL')}


def need(username, key, msg='无权执行该操作'):
    if not perm_of(username)['perms'].get(key):
        raise HTTPException(403, msg)


# ---------------- 部门 → 模块准入 ----------------
# 部门是「能不能进这个模块」的硬门槛，角色权限是「进去后能做什么」的细粒度，
# 两者独立叠加。is_admin 恒为 management（全模块），与角色超管旁路保持一致。
def dept_of(username):
    """返回用户的部门 key；管理员恒为 management。未知/空值归 bid（存量兼容）。"""
    with guard.unlocked():
        r = db.execute('SELECT dept, is_admin FROM users WHERE username=?', (username,)).fetchone()
    if not r:
        raise HTTPException(401, '账号不存在')
    if r['is_admin']:
        return 'management'
    dept = r['dept']
    return dept if dept in DEPT_KEYS else 'bid'


def modules_of(username):
    """返回该用户可访问的模块 key 列表（供前端菜单与登录响应使用）。"""
    dept = dept_of(username)
    for d in DEPARTMENTS:
        if d[0] == dept:
            return list(d[2])
    return []


def can_module(username, module):
    return module in modules_of(username)


def require_module(module):
    """router 级准入依赖工厂：当前用户部门无权访问该模块时返回 403。
    用法：app.include_router(router, dependencies=[Depends(require_module('finance'))])
    这样不污染 router 定义本身，同一 router 挂到不同前缀时各有独立准入。"""
    def dep(username: str = Depends(current_user)):
        if not can_module(username, module):
            dept = dept_of(username)
            label = next((d[1] for d in DEPARTMENTS if d[0] == dept), dept)
            mod_label = next((m[1] for m in MODULES if m[0] == module), module)
            raise HTTPException(403, '部门「%s」无权访问「%s」模块' % (label, mod_label))
        return username
    return dep


def filter_out(payload, field_perms):
    """出站：hidden 字段剔除；masked 字段返回脱敏值。"""
    if not field_perms:
        return payload
    out = {}
    for k, v in payload.items():
        r = field_rule(field_perms, k)
        if r['level'] == 'hidden':
            continue
        out[k] = mask_value(v, r['mask']) if r['level'] == 'masked' else v
    return out


def old_payload(rid):
    """读取库中现有 payload；不存在返回 None（用于区分新增/更新）。"""
    if not rid:
        return None
    with guard.unlocked():
        r = db.execute('SELECT payload FROM rows WHERE rid=?', (rid,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r['payload'])
    except Exception:
        return {}


def guard_in(rid, payload, field_perms):
    """入站：hidden / readonly / masked 字段不落库，保留旧值。
    返回 (新payload, 越权明细[(字段, 旧值, 提交值, 档位)])。"""
    old = old_payload(rid)
    is_new = old is None
    # 从旧值出发：前端只提交「可写」字段时，未提交的字段必须保持原值，不能被覆盖丢失
    out = {} if is_new else dict(old)
    if not field_perms:
        out.update(payload)
        return out, []
    rejects = []
    for k, v in payload.items():
        r = field_rule(field_perms, k)
        if r['level'] == 'writable':
            out[k] = v
            continue
        sv = '' if v is None else str(v).strip()
        if is_new:
            # 新建：隐藏字段有值直接拒绝；只读/脱敏字段静默丢弃，避免整单新增被拦死
            if r['level'] == 'hidden' and sv:
                rejects.append((k, '', v, r['level']))
            continue
        ov = (old or {}).get(k, '')
        ovs = '' if ov is None else str(ov).strip()
        # 脱敏字段回传的是脱敏值，与旧值脱敏后相同视为「未变更」
        same = sv == ovs or (r['level'] == 'masked' and sv == str(mask_value(ov, r['mask'])).strip())
        if not same:
            rejects.append((k, ov, v, r['level']))
    return out, rejects
