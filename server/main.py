# -*- coding: utf-8 -*-
"""亚德彩投标台账 · 内网协作后端（FastAPI + PostgreSQL）

启动：
    python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
（或直接双击 start_api.bat）

账号管理：编辑本目录 users.json 后重启服务即可，启动时自动同步进数据库。
数据库：PostgreSQL，连接参数统一在 server/config.json 的 postgres 段；
        可用 TAIZHANG_CONFIG 指定配置文件，或临时用 PG_* 环境变量覆盖。
首次启动自动建表；若 PG 为空且存在历史 SQLite 文件 server/data.db，会自动迁移其中全部数据。
"""
import json
import os
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- 阶段 1 重构：公共基础设施已抽到 server/common/，这里统一再导入同名符号，
# 使下方 1000+ 行路由代码零改动。详见 server/common/__init__.py 说明。 ----
from . import guard                      # 保留：路由里直接用 guard.keyed/unlocked/heavy 等
from .common import guard as _cg         # 与 server.guard 同一转发层，仅取 lock 实例
from .common.audit import FIELD_LABELS, audit_diff, audit_write
from .common.auth_tables import (
    backfill_role_perms,
    init_db,
    migrate_from_sqlite,
    seed_roles,
    sync_users,
    upsert_file_user,
)
from .common.catalog import (
    ALL_SYSTEM,
    DEPARTMENTS,
    DEPT_KEYS,
    DEPT_SYSTEMS,
    FIELD_CATALOG,
    FIELD_KEYS,
    LEVEL_KEYS,
    LEVELS,
    MASK_CATALOG,
    MASK_KEYS,
    MODULES,
    PERM_CATALOG,
    PERM_KEYS,
    ROLE_SEEDS,
    SCOPES,
    perm_systems,
    perms_for_system,
    system_for_dept,
)
from .common.database import BASE, DB_PATH, ROOT, USERS_PATH, PgDB, _conn, db
from .common.security import (
    can_module,
    current_user,
    dept_of,
    filter_out,
    field_level,
    field_rule,
    guard_in,
    hash_pw,
    mask_value,
    modules_of,
    need,
    norm_field_perms,
    old_payload,
    perm_of,
    require_admin,
    require_module,
)

# 全局锁实例现在由 common.guard 持有（全进程唯一）；下方 `with lock:` 与
# auth_tables 里的 `with guard.lock:` 用的是同一把锁。
lock = _cg.lock






init_db()

# ---------------- 权限模型 ----------------
# 台账 51 个字段（与前端 lib/fields.js 的 F 保持一致，用于权限矩阵与字段屏蔽）

# 模块/操作权限（新增功能模块时在这里追加一项即可，前端自动出现勾选项）

# 字段权限四档（对应流程文档：隐藏 / 脱敏 / 只读 / 可写）
# 脱敏规则（仅用于 masked 档）
# 行级数据范围（台账无部门字段，DEPT 暂未启用；新增部门字段后在此扩展）






seed_roles()




backfill_role_perms()






sync_users()



app = FastAPI(title='亚德彩投标台账')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

# 平台卡死治理 第 1 步：慢请求日志。后 add 的中间件在外层，可覆盖 CORS 处理耗时。
app.add_middleware(guard.TimingMiddleware)

# ---- 阶段 3：按域拆分的路由组（逻辑边界；将来拆进程时整组移出即可）----
# 通用域：认证 / 用户 / 角色 / 权限目录 / 日报 —— 公司系统共享能力
common_router = APIRouter()
# 台账域：投标台账业务 —— 本模块专属
taizhang_router = APIRouter()

STRIP_KEYS = {'_updatedBy', '_updatedAt'}


def pack(row, field_perms=None):
    """数据库行 -> 前端行（payload 展开 + id + 更新人元信息）；按字段权限剔除不可见字段。"""
    o = filter_out(json.loads(row['payload']), field_perms)
    o['id'] = row['rid']
    o['_updatedBy'] = row['updated_by']
    o['_updatedAt'] = row['updated_at']
    return o


def clean_payload(d):
    """入库前剔除前端内部字段。"""
    return {k: v for k, v in d.items()
            if not str(k).startswith('__') and k not in STRIP_KEYS and k != 'id'}


def row_key(r):
    """与前端 rowKey 一致的去重键：招标编号+标段；无编号用 项目名+开标时间。"""
    no = str(r.get('bidNo') or '').strip().lower()
    pg = str(r.get('pkg') or '').strip()
    if no:
        return no + '|' + pg
    return 'p|' + str(r.get('projectName') or '').strip() + '|' + str(r.get('openTime') or '')


# ---------------- 人员字段自动建号 ----------------
PERSON_FIELDS = ('lead', 'copilot', 'reviewer')
DEFAULT_PERSON_PW = '123456'


def ensure_person(name):
    """根据字符串匹配 users：先 username 精确，再 display_name 精确；都没命中就自动创建普通账号。
    冲突时 username 追加数字后缀（张伟→张伟2）。返回 (username, created_bool)。
    调用方须已持有全局 lock。"""
    name = str(name or '').strip()
    if not name:
        return None, False
    u = db.execute('SELECT username FROM users WHERE username=?', (name,)).fetchone()
    if u:
        return u['username'], False
    u = db.execute('SELECT username FROM users WHERE display_name=?', (name,)).fetchone()
    if u:
        return u['username'], False
    username = name
    n = 2
    while db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
        username = name + str(n)
        n += 1
    db.execute('INSERT INTO users(username, pass_hash, display_name, is_admin) VALUES(?,?,?,0)',
               (username, hash_pw(DEFAULT_PERSON_PW), name))
    db.commit()
    upsert_file_user(username, password=DEFAULT_PERSON_PW, display_name=name, is_admin=False)
    return username, True


def collect_person_names(rows):
    """从多行里收集三个人员字段的所有非空值（去重保序）。"""
    seen = []
    for raw in rows:
        r = raw if isinstance(raw, dict) else {}
        for f in PERSON_FIELDS:
            v = str(r.get(f) or '').strip()
            if v and v not in seen:
                seen.append(v)
    return seen


# ---------------- 鉴权 ----------------




# ---------------- 权限计算 ----------------


















# ---------------- 审计（字段级留痕，仅追加） ----------------






def self_names(username):
    """行级「仅本人」范围匹配用的名字集合（账号 + 显示名）。"""
    with guard.unlocked():
        u = db.execute('SELECT display_name FROM users WHERE username=?', (username,)).fetchone()
    names = {username}
    if u and u['display_name']:
        names.add(u['display_name'])
    return names


def row_in_scope(payload, scope, names):
    if scope != 'SELF':
        return True
    return any(str(payload.get(f) or '').strip() in names for f in PERSON_FIELDS)


class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    username: str = ''
    password: str = ''
    display_name: str = ''
    is_admin: bool = False
    role_key: str = ''
    dept: str = ''


class PwdIn(BaseModel):
    old: str = ''
    new: str = ''


class RoleIn(BaseModel):
    key: str = ''
    name: str = ''
    perms: dict = {}
    field_perms: dict = {}
    data_scope: str = 'ALL'
    system: str = 'taizhang'


class MergeIn(BaseModel):
    rows: list = []
    overwrite: bool = True


def _dept_modules(dept, is_admin):
    """按部门算可访问模块（纯函数，不查库）；管理员恒全模块。"""
    key = 'management' if is_admin else (dept if dept in DEPT_KEYS else 'bid')
    for d in DEPARTMENTS:
        if d[0] == key:
            return key, d[1], list(d[2])
    return key, key, []


def user_out(r, perms=None):
    role = 'admin' if r['is_admin'] else (r['role_key'] or 'staff')
    dept = (r['dept'] if 'dept' in r.keys() else '') if hasattr(r, 'keys') else ''
    dept_key, dept_name, modules = _dept_modules(dept, bool(r['is_admin']))
    o = {'username': r['username'], 'displayName': r['display_name'] or r['username'],
         'isAdmin': bool(r['is_admin']), 'roleKey': role,
         'dept': dept_key, 'deptName': dept_name, 'modules': modules}
    if perms is not None:
        o['perms'] = perms['perms']
        o['fieldPerms'] = perms['fieldPerms']
        o['roleName'] = perms['roleName']
        o['dataScope'] = perms['dataScope']
    return o


# ---------------- 接口 ----------------
@common_router.post('/login')
def login(body: LoginIn):
    with lock:
        u = db.execute('SELECT * FROM users WHERE username=?', (body.username,)).fetchone()
        if not u or u['pass_hash'] != hash_pw(body.password):
            raise HTTPException(401, '账号或密码错误')
        token = uuid.uuid4().hex
        db.execute('INSERT INTO sessions(token, username, created) VALUES(?,?,?)',
                   (token, u['username'], time.time()))
        db.commit()
    return {'token': token, 'user': user_out(u, perm_of(u['username']))}


@common_router.get('/me')
def me(username: str = Depends(current_user)):
    with guard.unlocked():
        u = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        raise HTTPException(401, '账号不存在')
    return {'user': user_out(u, perm_of(username))}


@common_router.post('/me/password')
def change_my_password(body: PwdIn, authorization: str = Header(default=''),
                       username: str = Depends(current_user)):
    """个人中心：自助重置密码。校验旧密码 → 更新 → 注销其它登录会话（保留当前）。"""
    old, new = body.old or '', body.new or ''
    new = new.strip()
    if not new:
        raise HTTPException(400, '新密码不能为空')
    if len(new) < 6:
        raise HTTPException(400, '新密码至少 6 位')
    if len(new) > 64:
        raise HTTPException(400, '新密码最长 64 位')
    if not old:
        raise HTTPException(400, '请输入原密码')
    if old.strip() == new:
        raise HTTPException(400, '新密码不能与原密码相同')
    with lock:
        u = db.execute('SELECT pass_hash, display_name, is_admin FROM users WHERE username=?',
                       (username,)).fetchone()
        if not u:
            raise HTTPException(404, '账号不存在')
        if u['pass_hash'] != hash_pw(old):
            raise HTTPException(400, '原密码不正确')
        db.execute('UPDATE users SET pass_hash=? WHERE username=?', (hash_pw(new), username))
        token = authorization[7:].strip() if authorization.startswith('Bearer ') else ''
        db.execute('DELETE FROM sessions WHERE username=? AND token<>?', (username, token))
        audit_write([(username, username, 'password', 'password', '', '已重置', True)])
        db.commit()
    # 回写 users.json：否则服务重启时 sync_users() 会用旧密码覆盖
    upsert_file_user(username, password=new, display_name=u['display_name'] or username,
                     is_admin=bool(u['is_admin']))
    return {'ok': True}


def visible_systems(username):
    """当前用户可管理的系统集合：管理部(is_admin/dept=management)可管全部系统，
    普通用户只能管自己部门归属的系统。用于用户/角色/权限目录的系统隔离。"""
    with guard.unlocked():
        r = db.execute('SELECT is_admin, dept FROM users WHERE username=?', (username,)).fetchone()
    if not r:
        raise HTTPException(401, '账号不存在')
    if r['is_admin'] or (r['dept'] if 'dept' in r.keys() else '') == 'management':
        return set(m[0] for m in MODULES)
    return {system_for_dept(r['dept'] if 'dept' in r.keys() else 'bid')}


def check_manage_system(username, system):
    """校验请求的系统是否在可管理范围；返回规范化后的系统 key。

    system=ALL_SYSTEM('all') 是管理中心的「全量视图」专用标识：跨越所有系统的
    用户/角色/权限目录。只有管理部（visible_systems 返回全部模块）可用，
    普通部门请求 all 会在下面的可见性校验被拒。"""
    valid = set(m[0] for m in MODULES) | {ALL_SYSTEM}
    if system not in valid:
        raise HTTPException(400, '系统标识无效')
    if system == ALL_SYSTEM:
        # all 要求「管理部通配」：visible_systems 返回的是全部模块集合
        if '*' in visible_systems(username) or set(m[0] for m in MODULES).issubset(visible_systems(username)):
            return system
        raise HTTPException(403, '仅管理部可查看全量数据')
    if system not in visible_systems(username):
        raise HTTPException(403, '无权管理该系统')
    return system


@common_router.get('/perm/catalog')
def perm_catalog(system: str = 'taizhang', username: str = Depends(current_user)):
    """权限矩阵编辑目录，按系统隔离：只返回该系统可见的权限项。
    台账字段权限仅对 taizhang 系统返回；新增模块只需 PERM_CATALOG/PERM_SYSTEMS 加行。"""
    system = check_manage_system(username, system)
    is_all = (system == ALL_SYSTEM)
    # all 视图返回全集权限项（各系统可见项去重），普通系统只返回该系统可见项
    if is_all:
        perms = list(PERM_CATALOG)
    else:
        perms = perms_for_system(system)
    return {
        # systems 字段：权限项归属的系统集合（管理中心据此按系统分类折叠）
        'perms': [{'key': k, 'label': l, 'group': g, 'systems': list(perm_systems(k))}
                  for k, l, g in perms],
        # 字段权限是台账专属：台账系统与 all 全量视图返回，其它系统空数组（前端整块隐藏）
        'fields': [{'key': k, 'label': l, 'group': g} for k, l, g in FIELD_CATALOG]
                  if (system == 'taizhang' or is_all) else [],
        'levels': [{'key': k, 'label': l} for k, l in LEVELS],
        'masks': [{'key': k, 'label': l} for k, l in MASK_CATALOG],
        'scopes': [{'key': k, 'label': l} for k, l in SCOPES]
                  if (system == 'taizhang' or is_all) else [],
        'departments': [{'key': d[0], 'label': d[1], 'modules': list(d[2])} for d in DEPARTMENTS],
        'modules': [{'key': m[0], 'label': m[1]} for m in MODULES],
        'system': system
    }


# ---------------- 角色与权限（仅管理员） ----------------
def role_out(r):
    try:
        perms = json.loads(r['perms'] or '{}')
    except Exception:
        perms = {}
    try:
        fp = json.loads(r['field_perms'] or '{}')
    except Exception:
        fp = {}
    rsys = r['system'] if 'system' in r.keys() and r['system'] else 'taizhang'
    vis = set(p[0] for p in perms_for_system(rsys))
    return {'key': r['key'], 'name': r['name'], 'isSystem': bool(r['is_system']),
            'system': rsys,
            # perms 只回该系统可见的项，避免台账角色看到财务权限残留
            'perms': {k: bool(perms.get(k)) for k in PERM_KEYS if k in vis},
            'fieldPerms': norm_field_perms(fp) if rsys == 'taizhang' else {},
            'dataScope': (r['data_scope'] if 'data_scope' in r.keys() else None) or 'ALL'}


@common_router.get('/roles')
def list_roles(system: str = 'taizhang', admin: str = Depends(require_admin)):
    """角色列表按系统过滤：管理部可看全部系统，普通管理员只看本系统。"""
    check_manage_system(admin, system)
    with guard.unlocked():
        if system == ALL_SYSTEM:
            rows = db.execute('SELECT * FROM roles ORDER BY system, is_system DESC, key').fetchall()
        else:
            rows = db.execute('SELECT * FROM roles WHERE system=? OR system IS NULL OR system=\'\' '
                              'ORDER BY is_system DESC, key', (system,)).fetchall()
    return {'roles': [role_out(r) for r in rows]}


def _clean_perms(d, system='taizhang'):
    """只保留该系统可见的权限项，防止跨系统权限混入角色。"""
    vis = set(p[0] for p in perms_for_system(system))
    return {k: (1 if d.get(k) else 0) for k in PERM_KEYS if k in vis}


def _clean_scope(v):
    return v if v in [s[0] for s in SCOPES] else 'ALL'


@common_router.post('/roles')
def create_role(body: RoleIn, admin: str = Depends(require_admin)):
    key = body.key.strip()
    if not key:
        raise HTTPException(400, '角色标识不能为空')
    if not body.name.strip():
        raise HTTPException(400, '角色名称不能为空')
    system = check_manage_system(admin, body.system or 'taizhang')
    if system == ALL_SYSTEM:
        raise HTTPException(400, '新建角色必须归属具体系统，不能归属「全部」')
    with lock:
        if db.execute('SELECT 1 FROM roles WHERE key=?', (key,)).fetchone():
            raise HTTPException(400, '角色「' + key + '」已存在')
        db.execute('INSERT INTO roles(key, name, is_system, perms, field_perms, data_scope, system) '
                   'VALUES(?,?,0,?,?,?,?)',
                   (key, body.name.strip(), json.dumps(_clean_perms(body.perms, system), ensure_ascii=False),
                    json.dumps(norm_field_perms(body.field_perms) if system == 'taizhang' else {},
                               ensure_ascii=False),
                    _clean_scope(body.data_scope), system))
        db.commit()
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
    return {'role': role_out(row)}


@common_router.put('/roles/{key}')
def update_role(key: str, body: RoleIn, admin: str = Depends(require_admin)):
    with lock:
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, '角色不存在')
        name = body.name.strip() or row['name']
        # 角色系统归属不可改；按住库的 system 校验可见范围并清洗权限
        rsys = row['system'] if 'system' in row.keys() and row['system'] else 'taizhang'
        check_manage_system(admin, rsys)
        db.execute('UPDATE roles SET name=?, perms=?, field_perms=?, data_scope=? WHERE key=?',
                   (name, json.dumps(_clean_perms(body.perms, rsys), ensure_ascii=False),
                    json.dumps(norm_field_perms(body.field_perms) if rsys == 'taizhang' else {},
                               ensure_ascii=False),
                    _clean_scope(body.data_scope), key))
        db.commit()
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
    return {'role': role_out(row)}


@common_router.delete('/roles/{key}')
def delete_role(key: str, admin: str = Depends(require_admin)):
    with lock:
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, '角色不存在')
        if row['is_system']:
            raise HTTPException(400, '内置角色不可删除')
        used = db.execute('SELECT COUNT(*) c FROM users WHERE role_key=?', (key,)).fetchone()['c']
        if used:
            raise HTTPException(400, '仍有 ' + str(used) + ' 个用户使用该角色，请先调整其角色')
        db.execute('DELETE FROM roles WHERE key=?', (key,))
        db.commit()
    return {'ok': True}


# ---------------- 用户管理（仅管理员） ----------------
def admin_count():
    return db.execute('SELECT COUNT(*) c FROM users WHERE is_admin=1').fetchone()['c']


@common_router.get('/users')
def list_users(system: str = 'taizhang', admin: str = Depends(require_admin)):
    """用户列表按系统隔离：用户按 dept 归属到系统，管理部(is_admin/management)看全部。
    普通部门管理员只能看到本部门归属系统的用户，看不到其它部门账号。"""
    check_manage_system(admin, system)
    # system=all（管理中心）：跨所有系统返回全部用户；否则按 dept 归属过滤单系统。
    all_sys = (system == ALL_SYSTEM)
    with guard.unlocked():
        if all_sys:
            rows = db.execute('SELECT * FROM users ORDER BY is_admin DESC, username').fetchall()
        else:
            # 非管理部：只看 dept 归属到该系统的用户（含管理部账号？不含——管理部由 all_sys 分支看）
            rows = db.execute(
                'SELECT * FROM users WHERE dept=? AND is_admin=0 '
                'ORDER BY is_admin DESC, username', (dept_of_system(system),)).fetchall()
    return {'users': [user_out(r) for r in rows]}


def dept_of_system(system):
    """系统对应的用户 dept（用户归属系统的反向映射）。"""
    for dept, sys_key in DEPT_SYSTEMS.items():
        if sys_key == system:
            return dept
    return 'bid'


@common_router.get('/staff')
def staff_list(q: str = '', dept: str = '', username: str = Depends(current_user)):
    """非管理员用户名单（用于负责人筛选与标书负责人/协作人/审核人选择）。

    q   模糊匹配账号或显示名；
    dept 可选部门过滤（如 bid=投标部）。投标台账只应出现投标部员工，
    不传则保持兼容、返回全部非管理员（其他系统/旧调用方不受影响）。
    """
    kw = '%' + q.strip().lower() + '%'
    cond = ['is_admin=0', '(LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?)']
    args = [kw, kw]
    if dept.strip():
        cond.append('dept=?')
        args.append(dept.strip())
    with guard.unlocked():
        rows = db.execute(
            'SELECT username, display_name FROM users WHERE ' + ' AND '.join(cond) +
            ' ORDER BY display_name, username', tuple(args)).fetchall()
    return {'staff': [{'username': r['username'], 'displayName': r['display_name'] or r['username']}
                      for r in rows]}


# ---------------- 工作日报 ----------------
class ReportIn(BaseModel):
    user: str = ''
    rdate: str
    content: str = ''
    plan: str = ''


def _is_admin(username):
    with guard.unlocked():
        r = db.execute('SELECT is_admin FROM users WHERE username=?', (username,)).fetchone()
    return bool(r and r['is_admin'])


@common_router.get('/reports')
def reports_list(user: str = '', ym: str = '', username: str = Depends(current_user)):
    """某人某年/某月的工作日报（view.reports 页面权限；user 缺省 = 自己）。
    ym 传 '2026' 取整年、'2026-11' 取当月，缺省取全部。
    查看他人日报需要 report.viewOthers 权限（管理员恒可）。"""
    need(username, 'view.reports', '无工作日报查看权限')
    target = (user or username).strip()
    if target != username and not _is_admin(username):
        need(username, 'report.viewOthers', '无查看其他人员日报的权限')
    prefix = ym.strip()
    with guard.unlocked():
        u = db.execute('SELECT * FROM users WHERE username=?', (target,)).fetchone()
        if not u:
            raise HTTPException(404, '账号不存在')
        if prefix:
            rows = db.execute('SELECT rdate, content, plan, updated_at FROM daily_reports '
                              'WHERE username=? AND rdate LIKE ? ORDER BY rdate',
                              (target, prefix + '%')).fetchall()
        else:
            rows = db.execute('SELECT rdate, content, plan, updated_at FROM daily_reports '
                              'WHERE username=? ORDER BY rdate', (target,)).fetchall()
    return {'user': user_out(u, perm_of(target)),
            'reports': [{'rdate': r['rdate'], 'content': r['content'] or '',
                         'plan': r['plan'] or '', 'updatedAt': r['updated_at']} for r in rows]}


@common_router.put('/reports')
def report_save(body: ReportIn, username: str = Depends(current_user)):
    """填写/修改某天的工作日报；只能写自己的（管理员可代写），且不允许写未来日期。"""
    need(username, 'view.reports', '无工作日报权限')
    target = (body.user or username).strip()
    if target != username and not _is_admin(username):
        raise HTTPException(403, '只能填写自己的日报')
    try:
        d = date.fromisoformat(body.rdate)
    except Exception:
        raise HTTPException(400, '日期格式应为 YYYY-MM-DD')
    if d > date.today():
        raise HTTPException(400, '不能填写未来日期的日报')
    with lock:
        db.execute('INSERT INTO daily_reports(username, rdate, content, plan, updated_at) '
                   'VALUES(?,?,?,?,?) ON CONFLICT(username, rdate) DO UPDATE SET '
                   'content=EXCLUDED.content, plan=EXCLUDED.plan, updated_at=EXCLUDED.updated_at',
                   (target, body.rdate, body.content.strip(), body.plan.strip(), time.time()))
        db.commit()
    return {'ok': True, 'rdate': body.rdate}


@common_router.post('/users')
def create_user(body: UserIn, admin: str = Depends(require_admin)):
    name = body.username.strip()
    if not name:
        raise HTTPException(400, '账号不能为空')
    if not body.password:
        raise HTTPException(400, '密码不能为空')
    with lock:
        if db.execute('SELECT 1 FROM users WHERE username=?', (name,)).fetchone():
            raise HTTPException(400, '账号「' + name + '」已存在')
        role_key = body.role_key.strip() or 'staff'
        if not db.execute('SELECT 1 FROM roles WHERE key=?', (role_key,)).fetchone():
            raise HTTPException(400, '角色不存在')
        dept = 'management' if body.is_admin else (body.dept.strip() or 'bid')
        if dept not in DEPT_KEYS:
            raise HTTPException(400, '部门不存在')
        db.execute('INSERT INTO users(username, pass_hash, display_name, is_admin, role_key, dept) '
                   'VALUES(?,?,?,?,?,?)',
                   (name, hash_pw(body.password), body.display_name.strip() or name,
                    1 if body.is_admin else 0, role_key, dept))
        db.commit()
    upsert_file_user(name, password=body.password,
                     display_name=body.display_name.strip() or name, is_admin=body.is_admin,
                     dept=dept)
    return {'ok': True}


@common_router.put('/users/{name}')
def update_user(name: str, body: UserIn, admin: str = Depends(require_admin)):
    with lock:
        row = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        if not row:
            raise HTTPException(404, '用户不存在')
        new_admin = 1 if body.is_admin else 0
        if row['is_admin'] and not new_admin and admin_count() <= 1:
            raise HTTPException(400, '不能取消最后一个管理员的管理权限')
        role_key = body.role_key.strip() or (row['role_key'] or 'staff')
        if not db.execute('SELECT 1 FROM roles WHERE key=?', (role_key,)).fetchone():
            raise HTTPException(400, '角色不存在')
        # 管理员恒 management；普通用户取提交值，空则沿用原部门，再空归 bid
        dept = 'management' if new_admin else (
            body.dept.strip() or (row['dept'] if 'dept' in row.keys() else '') or 'bid')
        if dept not in DEPT_KEYS:
            raise HTTPException(400, '部门不存在')
        if body.password:
            db.execute('UPDATE users SET pass_hash=?, display_name=?, is_admin=?, role_key=?, dept=? '
                       'WHERE username=?',
                       (hash_pw(body.password), body.display_name.strip() or name, new_admin,
                        role_key, dept, name))
        else:
            db.execute('UPDATE users SET display_name=?, is_admin=?, role_key=?, dept=? WHERE username=?',
                       (body.display_name.strip() or name, new_admin, role_key, dept, name))
        db.commit()
    upsert_file_user(name, password=body.password or None,
                     display_name=body.display_name.strip() or name, is_admin=body.is_admin,
                     dept=dept)
    return {'ok': True}


@common_router.delete('/users/{name}')
def delete_user(name: str, admin: str = Depends(require_admin)):
    if name == admin:
        raise HTTPException(400, '不能删除当前登录的账号')
    with lock:
        row = db.execute('SELECT is_admin FROM users WHERE username=?', (name,)).fetchone()
        if not row:
            raise HTTPException(404, '用户不存在')
        if row['is_admin'] and admin_count() <= 1:
            raise HTTPException(400, '不能删除最后一个管理员')
        db.execute('DELETE FROM users WHERE username=?', (name,))
        db.execute('DELETE FROM sessions WHERE username=?', (name,))
        db.commit()
    upsert_file_user(name, remove=True)
    return {'ok': True}


@taizhang_router.get('/bootstrap')
def bootstrap(username: str = Depends(current_user)):
    p = perm_of(username)
    fp = p['fieldPerms']
    names = self_names(username)
    with guard.unlocked():
        rows = []
        for r in db.execute('SELECT * FROM rows').fetchall():
            raw = json.loads(r['payload'])
            if not row_in_scope(raw, p['dataScope'], names):
                continue
            rows.append(pack(r, fp))
        fresh = (not rows) and db.execute('SELECT COUNT(*) c FROM deleted').fetchone()['c'] == 0
    return {'rows': rows, 'ts': time.time(), 'fresh': fresh}


# ---------------- 标书状态流转 ----------------
# 未开始(0) → 进行中(1) → 已结束(2)：员工只能向前推进，回退仅管理员
BID_RANK = {'未开始': 0, '进行中': 1, '已结束': 2}


def bid_rank(v):
    return BID_RANK.get(str(v or '').strip(), 0)


def apply_bid_status(payload, old, is_admin):
    """写入状态切换时间点，并拦截非管理员的回退。
    返回 (payload, err)；err 非空时调用方应拒绝落库。
    · 进行中 → 记录 bidStartAt（已有则保留）
    · 已结束 → 记录 bidEndAt（缺开始时间则一并补齐）
    · 回退到未开始 → 记录 bidWaitAt 并清空后续时间点"""
    new = str(payload.get('bidStatus') or '').strip()
    if not new:
        return payload, None
    if new not in BID_RANK:
        new = '未开始'
        payload['bidStatus'] = new
    prev = str((old or {}).get('bidStatus') or '').strip()
    if new == prev:
        return payload, None
    if prev and bid_rank(new) < bid_rank(prev) and not is_admin:
        return payload, '标书状态不可回退（仅管理员可修改）'
    today = date.today().isoformat()
    if new == '进行中':
        if not payload.get('bidStartAt'):
            payload['bidStartAt'] = today
        payload['bidEndAt'] = ''
    elif new == '已结束':
        payload['bidEndAt'] = today
        if not payload.get('bidStartAt'):
            payload['bidStartAt'] = today
    else:
        payload['bidWaitAt'] = today
        payload['bidStartAt'] = ''
        payload['bidEndAt'] = ''
    return payload, None


# ---------------- 投标项目文件目录（bidfile/年/月/项目名/招标文件｜标书） ----------------
BID_DIR_ROOT = 'bidfile'
BID_SUB_TENDER = '招标文件'      # 招标（投标）过程资料：原「上传项目文件」的去处
BID_SUB_BID = '标书'             # 标书文件：标书状态改为「已结束」前必须已有文件
BID_SUB_DIRS = (('tender', BID_SUB_TENDER), ('bid', BID_SUB_BID))
BID_END_NEED_FILE = ('标书状态改为「已结束」前，请先在项目「标书」目录上传标书文件'
                     '（无文件上传权限的账号请联系有权限的同事）')


def bid_dir_segs(payload):
    """投标行 → 存储根目录下的目录段：bidfile / 开标年 / 开标月 / 项目名。
    开标时间缺失时用当天兜底（正常行开标时间是必填）。"""
    t = str((payload or {}).get('openTime') or '').strip()
    if len(t) >= 7 and t[4] == '-' and t[5:7].isdigit():
        y, m = t[:4], t[5:7]
    else:
        d = date.today()
        y, m = '%04d' % d.year, '%02d' % d.month
    name = str((payload or {}).get('projectName') or '').strip() or '未命名项目'
    return [BID_DIR_ROOT, y, m, name]


def bid_upload_roles():
    """项目目录的协作角色：能进文件存储 + 能编辑台账 → 给上传/查看/改名/覆盖；
    另有删除台账权限的角色再给删除。返回 [(role_key, allow_mask)]。"""
    out = []
    for r in db.execute('SELECT key, perms FROM roles').fetchall():
        try:
            perms = json.loads(r['perms'] or '{}')
        except Exception:
            perms = {}
        if not perms.get('view.files') or not perms.get('bid.edit'):
            continue
        allow = fs_core.OP.UPLOAD | fs_core.OP.VIEW | fs_core.OP.MODIFY | fs_core.OP.OVERWRITE
        if perms.get('bid.delete'):
            allow |= fs_core.OP.DELETE
        out.append((r['key'], allow))
    return out


def files_under(node_id):
    """节点下（含子目录）未删除的文件数。"""
    if not node_id:
        return 0
    return sum(1 for n in fs_core.subtree(node_id) if n['kind'] == 'file' and not n['deleted'])


def ensure_child_dir(parent_id, name, who):
    """在 parent_id 下确保子目录存在（幂等，同层同名不区分大小写复用），返回 node_id。
    权限从项目目录继承（grant_role 默认 inherit=True），无需重复授权。"""
    r = db.execute("SELECT id FROM fs_node WHERE parent_id=? AND lower(name)=lower(?) "
                   "AND kind='dir' AND deleted=0", (parent_id, name)).fetchone()
    if r:
        return r['id']
    nid = fs_core.mkdir(parent_id, name, who)
    fs_core.fs_log(who, 'mkdir', fs_core.get_node(nid), target='投标项目子目录（自动创建）')
    return nid


def ensure_bid_dir(rid, payload, who):
    """确保投标项目的文件目录存在（幂等），项目改名时同步目录名。
    目录：bidfile / 开标年 / 开标月 / 项目名，其下再分「招标文件」「标书」两个子目录。
    调用方须持有 lock；返回 {nodeId,path,files,tenderNodeId,tenderPath,tenderFiles,
    bidNodeId,bidPath,bidFiles}。失败由调用方兜底，不阻断台账保存。"""
    segs = bid_dir_segs(payload)
    want = fs_core.safe_name(segs[-1])
    m = db.execute('SELECT * FROM bid_fs_dir WHERE rid=?', (rid,)).fetchone()
    node_id = ''
    if m:
        node = fs_core.get_alive(m['node_id'])
        if node:
            if node['name'] != want:
                try:
                    fs_core.do_rename(node['id'], want, who)   # 同名冲突则保留旧目录名
                except Exception:
                    pass
            node_id = node['id']
    if not node_id:
        node_id = fs_core.ensure_path(segs, who)
        for role, allow in bid_upload_roles():
            fs_core.grant_role(node_id, role, allow, who)
        fs_core.fs_log(who, 'mkdir', fs_core.get_node(node_id), target='投标项目目录（自动创建）')
    path = fs_core.path_of(node_id)

    # 子目录：老记录缺列 / 目录被删时自动补建
    subs = {}
    for key, name in BID_SUB_DIRS:
        nid = str((m[key + '_node_id'] if m else '') or '')
        if nid and not fs_core.get_alive(nid):
            nid = ''
        if not nid:
            nid = ensure_child_dir(node_id, name, who)
        subs[key] = nid

    db.execute('INSERT INTO bid_fs_dir(rid, node_id, path, tender_node_id, bid_node_id, created_at) '
               'VALUES(?,?,?,?,?,?) ON CONFLICT(rid) DO UPDATE SET node_id=excluded.node_id, '
               'path=excluded.path, tender_node_id=excluded.tender_node_id, '
               'bid_node_id=excluded.bid_node_id',
               (rid, node_id, path, subs['tender'], subs['bid'], time.time()))
    return {'rid': rid, 'name': str((payload or {}).get('projectName') or ''),
            'nodeId': node_id, 'path': path, 'files': files_under(node_id),
            'tenderNodeId': subs['tender'], 'tenderPath': fs_core.path_of(subs['tender']),
            'tenderFiles': files_under(subs['tender']),
            'bidNodeId': subs['bid'], 'bidPath': fs_core.path_of(subs['bid']),
            'bidFiles': files_under(subs['bid'])}


@taizhang_router.put('/rows/{rid}')
def upsert_row(rid: str, body: dict, username: str = Depends(current_user)):
    p = perm_of(username)
    need(username, 'bid.edit', '无台账新增/编辑权限')
    ts = time.time()
    # 读旧值 → 合并 → 落库放在同一把锁内：两个用户同时编辑同一行的不同字段时不会互相覆盖
    with lock:
        old = old_payload(rid)
        payload, rejects = guard_in(rid, clean_payload(body), p['fieldPerms'])
        if rejects:
            # 越权写入：不落库，记审计并要求重新登录前先修正请求
            audit_write([(username, rid, 'reject', k, ov, nv, False)
                         for k, ov, nv, lv in rejects])
            db.commit()
            labels = '、'.join(FIELD_LABELS.get(k, k) for k, _, _, _ in rejects)
            raise HTTPException(403, '无权限修改字段：' + labels)
        # 标书状态：自动写状态时间点 + 拦截员工回退
        payload, bid_err = apply_bid_status(payload, old, p['roleKey'] == 'admin')
        if bid_err:
            audit_write([(username, rid, 'reject', 'bidStatus',
                          (old or {}).get('bidStatus', ''), payload.get('bidStatus', ''), False)])
            db.commit()
            raise HTTPException(403, bid_err)
        if p['dataScope'] == 'SELF' and not row_in_scope(payload, 'SELF', self_names(username)):
            raise HTTPException(403, '无权操作该数据（数据范围：仅本人相关）')
        # 标书状态改为「已结束」：项目「标书」目录必须已有文件（新增/补录历史数据不校验）
        if (old is not None
                and str(payload.get('bidStatus') or '').strip() == '已结束'
                and str((old or {}).get('bidStatus') or '').strip() != '已结束'):
            no_file = False
            try:
                no_file = ensure_bid_dir(rid, payload, username)['bidFiles'] <= 0
            except Exception:
                no_file = False          # 目录准备失败不阻断保存
            if no_file:
                db.commit()              # 自动补建的目录已写入，先提交再拒绝
                audit_write([(username, rid, 'reject', 'bidStatus',
                              str((old or {}).get('bidStatus') or ''), '已结束', False)])
                db.commit()
                raise HTTPException(400, BID_END_NEED_FILE)
        db.execute(
            'INSERT INTO rows(rid, payload, updated_by, updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(rid) DO UPDATE SET payload=excluded.payload, '
            'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (rid, json.dumps(payload, ensure_ascii=False), username, ts))
        db.execute('DELETE FROM deleted WHERE rid=?', (rid,))
        # 新建标段 / 项目改名时，同步在文件存储建（或改）项目目录；失败不影响台账保存
        if old is None or str((old or {}).get('projectName') or '') != str(payload.get('projectName') or ''):
            try:
                ensure_bid_dir(rid, payload, username)
            except Exception:
                pass
        audit_write(audit_diff(username, rid, old or {}, payload,
                               'create' if old is None else 'update'))
        db.commit()
        row = db.execute('SELECT * FROM rows WHERE rid=?', (rid,)).fetchone()
    return pack(row, p['fieldPerms'])


def enroll_project(username: str, project: dict, overwrite: bool = False) -> dict:
    """报名：把采集到的项目写进台账（rows）。

    - 去重口径与前端 rowKey 一致：招标编号+标段；无编号用 项目名+开标时间；
    - 已在台账里 → 默认不重复添加，只把已有行原样返回（existing=True）；
    - overwrite=True 时用最新解析结果补齐，但**不覆盖已有非空值**（人工填的优先）；
    - 字段权限走 guard_in，保留「无权限字段不落库」的既有约束。
    """
    from .bidcrawl.enroll import new_rid
    need(username, 'bid.edit', '无台账新增权限')
    p = perm_of(username)
    ts = time.time()
    project = dict(project or {})
    # 报名人默认成为标书负责人：否则「仅本人相关」数据范围的用户看不到自己报的名
    if not str(project.get('lead') or '').strip():
        project['lead'] = username
    no = str(project.get('bidNo') or '').strip().lower()
    pkg = str(project.get('pkg') or '').strip()
    name = str(project.get('projectName') or '').strip()
    otime = str(project.get('openTime') or '').strip()
    with lock:
        if no:
            hit = db.execute(
                "SELECT rid FROM rows WHERE lower(coalesce(payload::jsonb->>'bidNo',''))=?"
                " AND coalesce(payload::jsonb->>'pkg','')=? LIMIT 1", (no, pkg)).fetchone()
        else:
            hit = db.execute(
                "SELECT rid FROM rows WHERE coalesce(payload::jsonb->>'projectName','')=?"
                " AND coalesce(payload::jsonb->>'openTime','')=? LIMIT 1",
                (name, otime)).fetchone()
        old = {}
        if hit:
            rid = hit['rid']
            if not overwrite:
                row = db.execute('SELECT * FROM rows WHERE rid=?', (rid,)).fetchone()
                return {'ok': True, 'rid': rid, 'existing': True,
                        'project': pack(row, p['fieldPerms']),
                        'message': '该项目已在投标管理中，未重复添加'}
            old = old_payload(rid) or {}
            merged = dict(project)
            for k, v in old.items():
                if k in merged and not str(merged.get(k) or '').strip() \
                        and str(v or '').strip():
                    merged[k] = v          # 人工填过的值不因解析为空而被清掉
            payload, rejects = guard_in(rid, merged, p['fieldPerms'])
            action = 'update'
        else:
            rid = new_rid()
            payload, rejects = guard_in(rid, project, p['fieldPerms'])
            action = 'create'
        if rejects:
            audit_write([(username, rid, 'reject', k, ov, nv, False)
                         for k, ov, nv, lv in rejects])
            db.commit()
            labels = '、'.join(FIELD_LABELS.get(k, k) for k, _, _, _ in rejects)
            raise HTTPException(403, '无权限写入字段：' + labels)
        db.execute(
            'INSERT INTO rows(rid, payload, updated_by, updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(rid) DO UPDATE SET payload=excluded.payload, '
            'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
            (rid, json.dumps(payload, ensure_ascii=False), username, ts))
        db.execute('DELETE FROM deleted WHERE rid=?', (rid,))
        try:
            ensure_bid_dir(rid, payload, username)
        except Exception:
            pass
        audit_write([(username, rid, action, 'projectName',
                      str((old or {}).get('projectName') or ''),
                      str(payload.get('projectName') or ''), True)])
        db.commit()
        row = db.execute('SELECT * FROM rows WHERE rid=?', (rid,)).fetchone()
    return {'ok': True, 'rid': rid, 'existing': bool(hit) and not overwrite,
            'project': pack(row, p['fieldPerms']),
            'message': '已加入投标管理' if not hit else '已用最新解析结果更新'}


@taizhang_router.delete('/rows/{rid}')
def delete_row(rid: str, username: str = Depends(current_user)):
    p = perm_of(username)
    need(username, 'bid.delete', '无台账删除权限')
    ts = time.time()
    with lock:
        old = old_payload(rid)
        if p['dataScope'] == 'SELF' and old is not None and not row_in_scope(old, 'SELF', self_names(username)):
            raise HTTPException(403, '无权操作该数据（数据范围：仅本人相关）')
        old = old or {}
        db.execute('DELETE FROM rows WHERE rid=?', (rid,))
        db.execute('INSERT INTO deleted(rid, ts) VALUES(?,?) '
                   'ON CONFLICT(rid) DO UPDATE SET ts=excluded.ts', (rid, ts))
        audit_write([(username, rid, 'delete', 'projectName',
                      old.get('projectName', ''), '', True)])
        db.commit()
    return {'ok': True, 'ts': ts}


@taizhang_router.get('/bid-dir')
def bid_dir(rid: str = '', username: str = Depends(current_user)):
    """投标项目对应的文件目录（bidfile/开标年/开标月/项目名/招标文件｜标书）：
    不存在则按规则创建，返回两个子目录的节点 id、路径与文件数（供台账上传使用）。"""
    need(username, 'view.manage', '无投标管理查看权限')
    rid = str(rid or '').strip()
    if not rid:
        raise HTTPException(400, '缺少标段 id')
    with lock:
        row = db.execute('SELECT payload FROM rows WHERE rid=?', (rid,)).fetchone()
        if not row:
            raise HTTPException(404, '标段不存在')
        payload = json.loads(row['payload'])
        p = perm_of(username)
        if p['dataScope'] == 'SELF' and not row_in_scope(payload, 'SELF', self_names(username)):
            raise HTTPException(403, '无权查看该数据（数据范围：仅本人相关）')
        try:
            info = ensure_bid_dir(rid, payload, username)
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(400, '创建项目目录失败：' + str(e))
    return info


@taizhang_router.get('/bid-files')
def bid_files(username: str = Depends(current_user)):
    """各标段「标书」目录下的文件数（rid → 文件数），供「需要处理」判断标书是否已上传。
    未建过项目目录的标段不返回（前端按 0 处理）；按当前用户数据范围过滤。"""
    p = perm_of(username)
    names = self_names(username)
    with guard.unlocked():
        allowed = set()
        for r in db.execute('SELECT rid, payload FROM rows').fetchall():
            if p['dataScope'] == 'SELF' and not row_in_scope(json.loads(r['payload']), 'SELF', names):
                continue
            allowed.add(str(r['rid']))
        mapping = {}          # 标书目录节点 id -> rid
        for m in db.execute("SELECT rid, bid_node_id FROM bid_fs_dir WHERE bid_node_id<>''").fetchall():
            if str(m['rid']) in allowed:
                mapping[str(m['bid_node_id'])] = str(m['rid'])
        files = dict((rid, 0) for rid in mapping.values())
        if mapping:
            # 一次递归统计：各「标书」目录（含其子目录）里未删除的文件数
            for r in db.execute(
                    'WITH RECURSIVE tree(root, id) AS ('
                    "  SELECT bid_node_id, bid_node_id FROM bid_fs_dir WHERE bid_node_id<>''"
                    '  UNION'
                    '  SELECT t.root, n.id FROM fs_node n JOIN tree t ON n.parent_id = t.id'
                    '  WHERE n.deleted = 0'
                    ') SELECT t.root AS root, COUNT(*) AS c FROM tree t'
                    "  JOIN fs_node f ON f.id = t.id"
                    "  WHERE f.kind = 'file' AND f.deleted = 0 GROUP BY t.root").fetchall():
                rid = mapping.get(str(r['root']))
                if rid:
                    files[rid] = int(r['c'] or 0)
    return {'files': files, 'ts': time.time()}


@taizhang_router.post('/clear')
@guard.heavy('台账清空', cap=1)
def clear_all(username: str = Depends(current_user)):
    p = perm_of(username)
    need(username, 'bid.delete', '无台账清空权限')
    names = self_names(username)
    ts = time.time()
    with guard.keyed('ledger'), guard.tx(db):
        ids = []
        for r in db.execute('SELECT rid, payload FROM rows').fetchall():
            if p['dataScope'] == 'SELF' and not row_in_scope(json.loads(r['payload']), 'SELF', names):
                continue
            ids.append(r['rid'])
        db.execute('DELETE FROM rows')
        for rid in ids:
            db.execute('INSERT INTO deleted(rid, ts) VALUES(?,?) '
                       'ON CONFLICT(rid) DO UPDATE SET ts=excluded.ts', (rid, ts))
        db.commit()
    return {'ok': True, 'ts': ts, 'deleted': len(ids)}


@taizhang_router.post('/merge')
@guard.heavy('台账导入', cap=1)
def merge(body: MergeIn, username: str = Depends(current_user)):
    """导入合并：与前端 mergeRows 同逻辑（按 rowKey 去重，覆盖时只补非空字段）。
    非「可写」字段的提交值一律丢弃（沿用库中旧值）；批量导入不逐字段写审计，避免日志淹没。"""
    p = perm_of(username)
    need(username, 'bid.import', '无台账导入权限')
    fp = p['fieldPerms']
    names = self_names(username)
    ts = time.time()
    add = upd = skip = 0
    changed_ids = []
    created_users = []
    with guard.keyed('ledger'), guard.tx(db):
        # 1) 预扫所有人员字段，未命中则自动建号（台账字段值保持原显示名不变）
        for pname in collect_person_names(body.rows):
            _, created = ensure_person(pname)
            if created:
                created_users.append(pname)
        # 2) 正常合并
        idx = {}
        for r in db.execute('SELECT * FROM rows').fetchall():
            idx[row_key(json.loads(r['payload']))] = r
        for raw in body.rows:
            r = clean_payload(raw)
            if not str(r.get('projectName') or '').strip():
                continue
            k = row_key(r)
            if k in idx:
                if not body.overwrite:
                    skip += 1
                    continue
                old = idx[k]
                payload = json.loads(old['payload'])
                if p['dataScope'] == 'SELF' and not row_in_scope(payload, 'SELF', names):
                    skip += 1
                    continue
                prev_status = payload.get('bidStatus', '')
                # 导入不覆盖「回退」：非管理员带入的更低状态直接忽略
                new_st = str(r.get('bidStatus') or '').strip()
                if new_st and bid_rank(new_st) < bid_rank(prev_status) and p['roleKey'] != 'admin':
                    r = {kk: vv for kk, vv in r.items() if kk != 'bidStatus'}
                for kk, vv in r.items():
                    if vv != '' and vv is not None and field_level(fp, kk) == 'writable':
                        payload[kk] = vv
                payload, _ = apply_bid_status(payload, {'bidStatus': prev_status},
                                              p['roleKey'] == 'admin')
                db.execute('UPDATE rows SET payload=?, updated_by=?, updated_at=? WHERE rid=?',
                           (json.dumps(payload, ensure_ascii=False), username, ts, old['rid']))
                upd += 1
                changed_ids.append(old['rid'])
            else:
                r, _ = guard_in(None, r, fp)
                if p['dataScope'] == 'SELF' and not row_in_scope(r, 'SELF', names):
                    skip += 1
                    continue
                r, _ = apply_bid_status(r, None, p['roleKey'] == 'admin')
                rid = str(raw.get('id') or uuid.uuid4().hex)
                db.execute('INSERT INTO rows(rid, payload, updated_by, updated_at) VALUES(?,?,?,?)',
                           (rid, json.dumps(r, ensure_ascii=False), username, ts))
                try:
                    ensure_bid_dir(rid, r, username)      # 导入新增也要有项目文件目录
                except Exception:
                    pass
                idx[k] = {'rid': rid, 'payload': json.dumps(r, ensure_ascii=False)}
                add += 1
                changed_ids.append(rid)
        db.commit()
        out = [pack(db.execute('SELECT * FROM rows WHERE rid=?', (rid,)).fetchone(), fp)
               for rid in changed_ids]
    return {'add': add, 'upd': upd, 'skip': skip, 'rows': out, 'ts': ts,
            'created_users': created_users}


@taizhang_router.get('/changes')
def changes(since: float = 0.0, username: str = Depends(current_user)):
    """增量同步：返回 since（服务器时间戳）之后的变更与删除（按当前用户字段权限过滤）。"""
    p = perm_of(username)
    fp = p['fieldPerms']
    names = self_names(username)
    with guard.unlocked():
        changed = []
        for r in db.execute('SELECT * FROM rows WHERE updated_at > ?', (since,)).fetchall():
            raw = json.loads(r['payload'])
            if row_in_scope(raw, p['dataScope'], names):
                changed.append(pack(r, fp))
        deleted = [r['rid'] for r in db.execute(
            'SELECT rid FROM deleted WHERE ts > ?', (since,)).fetchall()]
    return {'changed': changed, 'deleted': deleted, 'ts': time.time()}


@taizhang_router.get('/audit')
def list_audit(limit: int = 100, rid: str = '', username: str = Depends(current_user)):
    """字段级审计日志（需「审计日志」模块权限；管理员默认拥有）。"""
    if not perm_of(username)['perms'].get('view.audit'):
        raise HTTPException(403, '无审计日志查看权限')
    limit = max(1, min(500, int(limit)))
    with guard.unlocked():
        if rid:
            rows = db.execute('SELECT * FROM audit_log WHERE rid=? ORDER BY id DESC LIMIT ?',
                              (rid, limit)).fetchall()
        else:
            rows = db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        total = db.execute('SELECT COUNT(*) c FROM audit_log').fetchone()['c']
    return {'total': total, 'logs': [
        {'id': r['id'], 'ts': r['ts'], 'user': r['username'], 'rid': r['rid'],
         'action': r['action'], 'field': r['field'],
         'fieldLabel': FIELD_LABELS.get(r['field'], r['field']),
         'old': r['old_value'], 'new': r['new_value'], 'allowed': bool(r['allowed'])}
        for r in rows]}


# ---------------- 文件存储模块（/api/fs/*） ----------------
# 必须在 SPA 兜底路由 /{full_path:path} 之前注册，否则会被吞掉
from . import fs as fs_core                                            # noqa: E402
from . import fs_api as fs_api_mod                                     # noqa: E402

fs_core.init_fs(db, lock, perm_of)          # 注入依赖 + 创建存储目录 + 启动清理线程
fs_core.init_tables(db)                     # 幂等建表（老库可直接升级）
app.include_router(fs_api_mod.build_router(current_user),
                   dependencies=[Depends(require_module('taizhang'))])


# ---------------- 开标汇总 AI（/api/ai-summary/*） ----------------
# 同样必须在 SPA 兜底路由 /{full_path:path} 之前注册。
# 本模块只建 PENDING 任务 / 读结果，重活在独立 Python 3.10+ Worker 进程，
# 不占本进程（单 worker）的请求线程，满足「异步不阻塞主进程」。
from . import ai_summary_api as ai_api_mod                  # noqa: E402

ai_api_mod.init_tables(db)                  # 幂等建表 + 老库补列 + 默认字段字典
app.include_router(ai_api_mod.build_router(current_user, {
    'db': db, 'lock': lock, 'need': need, 'perm_of': perm_of,
    'row_in_scope': row_in_scope, 'self_names': self_names,
    'audit_write': audit_write, 'field_level': field_level,
    'mask_value': mask_value, 'fs': fs_core,
    'person_fields': PERSON_FIELDS,
}), dependencies=[Depends(require_module('taizhang'))])


# ---------------- 标讯采集（/api/bidcrawl/*） ----------------
# 只做配置 CRUD、配方管理与任务提交；采集执行在独立 worker 进程。
from . import bidcrawl_api as bidcrawl_api_mod                # noqa: E402

bidcrawl_api_mod.init_tables(db)
app.include_router(bidcrawl_api_mod.build_router(current_user, {
    'need': need, 'enroll_project': enroll_project}),
    dependencies=[Depends(require_module('taizhang'))])
# 定时任务走独立的权限点，不与采集页的 view.bidcrawl 绑定：
# 只有被授予 view.schedule / schedule.* 的角色才能查看与增删改。
app.include_router(bidcrawl_api_mod.build_schedule_router(current_user, {
    'need': need}), dependencies=[Depends(require_module('taizhang'))])


# ---------------- 阶段 3：域路由挂载（新路径为主 + 旧路径兼容）----------------
# 每个域路由定义时用了相对 path（如 '/login'），这里挂两个前缀：
#   /api/common/*    通用能力：认证/用户/角色/权限/日报（公司系统可共享）
#   /api/taizhang/*  台账业务：投标台账专属
#   /api/finance/*   财务业务（server/finance/，新增域照此模式）
# 同时保留 /api/* 旧前缀，前端可渐进迁移，外部历史调用不失效。
# 必须在 SPA 兜底 /{full_path:path} 之前注册。
app.include_router(common_router, prefix='/api/common')
app.include_router(taizhang_router, prefix='/api/taizhang',
                   dependencies=[Depends(require_module('taizhang'))])
app.include_router(common_router, prefix='/api')          # 旧路径兼容
app.include_router(taizhang_router, prefix='/api',        # 旧路径兼容
                   dependencies=[Depends(require_module('taizhang'))])

# 财务域（新业务模块参照）：建表幂等 + 双前缀挂载。
from .finance import api as finance_api                              # noqa: E402
finance_api.init_tables(db)
app.include_router(finance_api.finance_router, prefix='/api/finance',
                   dependencies=[Depends(require_module('finance'))])
app.include_router(finance_api.finance_router, prefix='/api',        # 旧路径兼容
                   dependencies=[Depends(require_module('finance'))])

# 管理中心域：全局看板，仅管理部可进（require_module('admin') 部门准入）。
# 用户/角色全量 CRUD 走 common_router 的 system=all，不在此重复实现。
from .admin_api import admin_router                                  # noqa: E402
app.include_router(admin_router, prefix='/api/admin',
                   dependencies=[Depends(require_module('admin'))])
app.include_router(admin_router, prefix='/api',                       # 旧路径兼容
                   dependencies=[Depends(require_module('admin'))])


# 前端静态托管（frontend/dist，Vite 构建产物）+ SPA history 路由回退：
# 生产环境由 Nginx 托管，此兜底仅为直连 8000 端口联调时刷新 /manage、/users 不 404。
from fastapi.responses import FileResponse, JSONResponse

DIST = ROOT / 'frontend' / 'dist'


@app.get('/healthz', include_in_schema=False)
def healthz():
    """进程存活 / PG 连通 / 任务积压。免登录，供 Nginx 与巡检探测。

    必须注册在 SPA 兜底 /{full_path:path} 之前；DB 不可用时返回 503 而不是 500，
    便于负载均衡与监控识别。"""
    data = guard.health_payload(db)
    return JSONResponse(data, status_code=200 if data['ok'] else 503)


@app.get('/{full_path:path}', include_in_schema=False)
def spa(full_path: str):
    # index.html 不缓存：保证发版后浏览器立即拿到新 hash 的资源；
    # 带 hash 的静态资源仍走浏览器默认缓存（文件名变了自然失效）。
    # 未知 /api/* 路径（如后端未重启缺新路由时）返回 JSON 404，
    # 避免前端把 index.html 当 JSON 解析报「Unexpected token <!DOCTYPE」。
    if full_path.startswith('api/') or full_path == 'api':
        raise HTTPException(404, '接口不存在：/' + full_path)
    if full_path:
        f = (DIST / full_path).resolve()
        if f.is_file() and str(f).startswith(str(DIST)):
            return FileResponse(f)
    return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-cache'})
