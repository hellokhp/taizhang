# -*- coding: utf-8 -*-
"""亚德彩投标台账 · 内网协作后端（FastAPI + PostgreSQL）

启动：
    python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
（或直接双击 start_api.bat）

账号管理：编辑本目录 users.json 后重启服务即可，启动时自动同步进数据库。
数据库：PostgreSQL，默认 127.0.0.1:5432 / 库 yadecai；
        可用环境变量 PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DB 覆盖。
首次启动自动建表；若 PG 为空且存在历史 SQLite 文件 server/data.db，会自动迁移其中全部数据。
"""
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import date
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DB_PATH = BASE / 'data.db'          # 历史 SQLite 数据文件（仅用于一次性迁移，迁移后作为回滚备份保留）
USERS_PATH = BASE / 'users.json'

PG_CONF = {
    'host': os.environ.get('PG_HOST', '127.0.0.1'),
    'port': int(os.environ.get('PG_PORT', '5432')),
    'user': os.environ.get('PG_USER', 'postgres'),
    'password': os.environ.get('PG_PASSWORD', 'yinhe123.'),
    'dbname': os.environ.get('PG_DB', 'yadecai'),
}

lock = threading.RLock()
_local = threading.local()


def _conn():
    """每线程一个连接（FastAPI 同步接口在线程池中执行，psycopg2 连接不可跨线程共享）。"""
    conn = getattr(_local, 'conn', None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(**PG_CONF)
        conn.autocommit = False
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


def migrate_from_sqlite():
    """把历史 SQLite 数据（server/data.db）一次性迁移到 PostgreSQL。
    仅当 PG 中还没有用户/角色数据时执行，重复启动不会重复写入。"""
    import sqlite3

    if not DB_PATH.exists():
        return
    if db.execute('SELECT 1 FROM users LIMIT 1').fetchone() or \
            db.execute('SELECT 1 FROM roles LIMIT 1').fetchone():
        return
    src = sqlite3.connect(str(DB_PATH))
    src.row_factory = sqlite3.Row
    moved = {}
    try:
        users = src.execute(
            "SELECT username, pass_hash, COALESCE(display_name,'') AS display_name, "
            "COALESCE(is_admin,0) AS is_admin, COALESCE(role_key,'staff') AS role_key "
            "FROM users").fetchall()
        for r in users:
            db.execute('INSERT INTO users(username, pass_hash, display_name, is_admin, role_key) '
                       'VALUES(?,?,?,?,?) ON CONFLICT(username) DO NOTHING',
                       (r['username'], r['pass_hash'], r['display_name'],
                        r['is_admin'], r['role_key']))
        moved['users'] = len(users)

        roles = src.execute('SELECT key, name, is_system, perms, field_perms, data_scope '
                            'FROM roles').fetchall()
        for r in roles:
            db.execute('INSERT INTO roles(key, name, is_system, perms, field_perms, data_scope) '
                       'VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING',
                       (r['key'], r['name'], r['is_system'], r['perms'],
                        r['field_perms'], r['data_scope']))
        moved['roles'] = len(roles)

        rows = src.execute('SELECT rid, payload, updated_by, updated_at FROM rows').fetchall()
        for r in rows:
            db.execute('INSERT INTO rows(rid, payload, updated_by, updated_at) VALUES(?,?,?,?) '
                       'ON CONFLICT(rid) DO NOTHING',
                       (r['rid'], r['payload'], r['updated_by'], r['updated_at']))
        moved['rows'] = len(rows)

        deleted = src.execute('SELECT rid, ts FROM deleted').fetchall()
        for r in deleted:
            db.execute('INSERT INTO deleted(rid, ts) VALUES(?,?) ON CONFLICT(rid) DO NOTHING',
                       (r['rid'], r['ts']))
        moved['deleted'] = len(deleted)

        sessions = src.execute('SELECT token, username, created FROM sessions').fetchall()
        for r in sessions:
            db.execute('INSERT INTO sessions(token, username, created) VALUES(?,?,?) '
                       'ON CONFLICT(token) DO NOTHING',
                       (r['token'], r['username'], r['created']))
        moved['sessions'] = len(sessions)

        logs = src.execute('SELECT id, ts, username, rid, action, field, old_value, '
                           'new_value, allowed FROM audit_log ORDER BY id').fetchall()
        for r in logs:
            db.execute('INSERT INTO audit_log(id, ts, username, rid, action, field, '
                       'old_value, new_value, allowed) VALUES(?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(id) DO NOTHING',
                       (r['id'], r['ts'], r['username'], r['rid'], r['action'], r['field'],
                        r['old_value'], r['new_value'], r['allowed']))
        moved['audit_log'] = len(logs)
        db.commit()
        # 自增序列对齐历史最大 id，避免后续插入主键冲突
        db.execute("SELECT setval(pg_get_serial_sequence('audit_log','id'), "
                   "COALESCE((SELECT MAX(id) FROM audit_log), 1), "
                   "EXISTS(SELECT 1 FROM audit_log))")
        db.commit()
        print('[migrate] SQLite -> PostgreSQL 完成：' + json.dumps(moved, ensure_ascii=False))
    except Exception as e:
        db.rollback()
        print('[migrate] 迁移失败（服务继续使用 PG 空库）：' + repr(e))
    finally:
        src.close()


def init_db():
    """建表（幂等）并尝试迁移历史 SQLite 数据。"""
    with lock:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          username TEXT PRIMARY KEY,
          pass_hash TEXT NOT NULL,
          display_name TEXT DEFAULT '',
          is_admin INTEGER DEFAULT 0,
          role_key TEXT DEFAULT 'staff'
        );
        CREATE TABLE IF NOT EXISTS rows(
          rid TEXT PRIMARY KEY,
          payload TEXT NOT NULL,
          updated_by TEXT DEFAULT '',
          updated_at DOUBLE PRECISION NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rows_ts ON rows(updated_at);
        CREATE TABLE IF NOT EXISTS deleted(
          rid TEXT PRIMARY KEY,
          ts DOUBLE PRECISION NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY,
          username TEXT NOT NULL,
          created DOUBLE PRECISION NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roles(
          key TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          is_system INTEGER DEFAULT 0,
          perms TEXT NOT NULL DEFAULT '{}',
          field_perms TEXT NOT NULL DEFAULT '{}',
          data_scope TEXT DEFAULT 'ALL'
        );
        CREATE TABLE IF NOT EXISTS audit_log(
          id BIGSERIAL PRIMARY KEY,
          ts DOUBLE PRECISION NOT NULL,
          username TEXT NOT NULL,
          rid TEXT DEFAULT '',
          action TEXT NOT NULL,
          field TEXT NOT NULL,
          old_value TEXT DEFAULT '',
          new_value TEXT DEFAULT '',
          allowed INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_deleted_ts ON deleted(ts);
        CREATE TABLE IF NOT EXISTS daily_reports(
          username TEXT NOT NULL,
          rdate TEXT NOT NULL,
          content TEXT DEFAULT '',
          plan TEXT DEFAULT '',
          updated_at DOUBLE PRECISION NOT NULL,
          PRIMARY KEY(username, rdate)
        );
        CREATE TABLE IF NOT EXISTS bid_fs_dir(
          rid TEXT PRIMARY KEY,
          node_id TEXT NOT NULL,
          path TEXT DEFAULT '',
          created_at DOUBLE PRECISION NOT NULL
        );
        ''')
        migrate_from_sqlite()
        # 删除墓碑只用于增量同步回放，保留 90 天足够覆盖长期离线客户端，避免表无限增长
        db.execute('DELETE FROM deleted WHERE ts < ?', (time.time() - 90 * 86400,))
        db.commit()


init_db()

# ---------------- 权限模型 ----------------
# 台账 48 个字段（与前端 lib/fields.js 的 F 保持一致，用于权限矩阵与字段屏蔽）
FIELD_CATALOG = [
    ('projectName', '项目名称', '项目基础'), ('bidNo', '招标编号', '项目基础'),
    ('pkg', '标段/包号', '项目基础'), ('agency', '代理机构', '项目基础'),
    ('owner', '采购单位（业主）', '项目基础'), ('region', '项目所在地', '项目基础'),
    ('method', '招标方式', '项目基础'), ('bizType', '业务类型', '项目基础'),
    ('projType', '类型', '项目基础'), ('industry', '行业', '项目基础'),
    ('province', '省份', '项目基础'),
    ('deadline', '投标截止时间', '时间与地点'), ('openTime', '开标时间', '时间与地点'),
    ('openAddr', '开标地点', '时间与地点'),
    ('budget', '预算/最高限价', '金额与保证金'), ('ourPrice', '我方报价', '金额与保证金'),
    ('bondAmount', '投标保证金金额', '金额与保证金'), ('bondType', '保证金形式', '金额与保证金'),
    ('bondStatus', '投标保证金状态', '金额与保证金'),
    ('bondBackDate', '投标保证金退回时间', '金额与保证金'),
    ('bondIssuer', '投标保函开具行/收款单位', '金额与保证金'),
    ('bondNo', '投标保函/凭证编号', '金额与保证金'), ('bondPayDate', '投标缴纳/开具日期', '金额与保证金'),
    ('bondDue', '投标保函有效期', '金额与保证金'),
    ('perfAmount', '履约保证金金额', '金额与保证金'), ('perfStatus', '履约保证金状态', '金额与保证金'),
    ('perfBackDate', '履约保证金退回时间', '金额与保证金'),
    ('perfIssuer', '履约保函开具行/收款单位', '金额与保证金'),
    ('perfNo', '履约保函/凭证编号', '金额与保证金'), ('perfPayDate', '履约缴纳/开具日期', '金额与保证金'),
    ('perfDue', '履约保函有效期', '金额与保证金'),
    ('lead', '标书负责人', '人员分工'), ('copilot', '协作人', '人员分工'),
    ('reviewer', '审核人', '人员分工'),
    ('bidStatus', '标书状态', '标书进度'), ('bidWaitAt', '未开始时间', '标书进度'),
    ('bidStartAt', '进行中时间', '标书进度'), ('bidEndAt', '已结束时间', '标书进度'),
    ('bidEffort', '标书投入时间', '标书进度'),
    ('docFee', '购买标书费', '项目开销'), ('travelFee', '差旅费', '项目开销'),
    ('sampleFee', '样品费', '项目开销'),
    ('result', '开标结果', '开标结果'), ('winner', '中标单位', '开标结果'),
    ('winAmount', '中标金额', '开标结果'), ('rank', '我方排名', '开标结果'),
    ('noticeUrl', '中标公告链接', '开标结果'), ('remark', '备注', '开标结果')
]
FIELD_KEYS = [f[0] for f in FIELD_CATALOG]

# 模块/操作权限（新增功能模块时在这里追加一项即可，前端自动出现勾选项）
PERM_CATALOG = [
    ('view.dashboard', '首页看板', '页面'),
    ('view.manage', '投标管理', '页面'),
    ('view.timeline', '标源总览', '页面'),
    ('view.assign', '项目分配表', '页面'),
    ('view.alerts', '需要处理', '页面'),
    ('view.reports', '工作日报', '页面'),
    ('view.users', '用户管理', '页面'),
    ('view.roles', '角色与权限', '页面'),
    ('view.audit', '审计日志', '页面'),
    ('view.files', '文件存储', '页面'),
    ('bid.edit', '台账 · 新增/编辑', '操作'),
    ('bid.delete', '台账 · 删除', '操作'),
    ('bid.import', '台账 · 导入', '操作'),
    ('bid.export', '台账 · 导出/模板', '操作'),
    ('file.admin', '存储 · 全局管理（回收站/备份/日志）', '操作'),
    ('report.viewOthers', '日报 · 查看其他人员数据', '操作'),
]
PERM_KEYS = [p[0] for p in PERM_CATALOG]

# 字段权限四档（对应流程文档：隐藏 / 脱敏 / 只读 / 可写）
LEVELS = [
    ('writable', '可编辑'), ('readonly', '只读'), ('masked', '脱敏'), ('hidden', '不可见')
]
LEVEL_KEYS = [l[0] for l in LEVELS]
# 脱敏规则（仅用于 masked 档）
MASK_CATALOG = [
    ('amount', '金额（保留首位，如 1***）'), ('phone', '手机号（138****1234）'),
    ('name', '姓名/单位（保留首字，如 张**）'), ('text', '文本（保留前2字，如 某某***）'),
    ('full', '全部（***）')
]
MASK_KEYS = [m[0] for m in MASK_CATALOG]
# 行级数据范围（台账无部门字段，DEPT 暂未启用；新增部门字段后在此扩展）
SCOPES = [('ALL', '全部数据'), ('SELF', '仅本人相关（负责人/协作人/审核人）')]

_ALL_FIELDS_READONLY = {k: {'level': 'readonly', 'mask': 'full'} for k in FIELD_KEYS}

ROLE_SEEDS = [
    ('manager', '投标经理', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                             'view.audit': 1, 'view.files': 1, 'bid.edit': 1, 'bid.delete': 1, 'bid.import': 1,
                             'bid.export': 1, 'file.admin': 1, 'report.viewOthers': 1}, {}, 'ALL'),
    ('staff', '投标专员', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                           'view.audit': 0, 'view.files': 1, 'bid.edit': 1, 'bid.delete': 0, 'bid.import': 1,
                           'bid.export': 1, 'file.admin': 0, 'report.viewOthers': 0}, {}, 'ALL'),
    ('viewer', '只读查看', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                            'view.audit': 0, 'view.files': 1, 'bid.edit': 0, 'bid.delete': 0, 'bid.import': 0,
                            'bid.export': 0, 'file.admin': 0, 'report.viewOthers': 0}, _ALL_FIELDS_READONLY, 'ALL'),
]


def seed_roles():
    """初始化内置角色（已存在则跳过，不覆盖管理员改动）。"""
    with lock:
        for key, name, perms, fp, scope in ROLE_SEEDS:
            if db.execute('SELECT 1 FROM roles WHERE key=?', (key,)).fetchone():
                continue
            db.execute('INSERT INTO roles(key, name, is_system, perms, field_perms, data_scope) '
                       'VALUES(?,?,1,?,?,?)',
                       (key, name, json.dumps(perms, ensure_ascii=False),
                        json.dumps(fp, ensure_ascii=False), scope))
        db.commit()


seed_roles()


def backfill_role_perms():
    """新增页面权限时给存量角色补默认值：跟随该角色「投标管理」页的开关，一次性补齐。
    view.files 跟随 view.manage（能进台账就能进文件存储）；file.admin 跟随 view.audit；
    report.viewOthers 跟随 view.audit（管理属性权限，默认仅管理层可看他人日报）。"""
    page_perms = ('view.timeline', 'view.assign', 'view.alerts', 'view.reports', 'view.files')
    follow = {'file.admin': 'view.audit', 'report.viewOthers': 'view.audit'}
    with lock:
        changed = False
        for r in db.execute('SELECT key, perms FROM roles').fetchall():
            try:
                perms = json.loads(r['perms'] or '{}')
            except Exception:
                perms = {}
            missing = [k for k in page_perms if k not in perms]
            for k in follow:
                if k not in perms:
                    missing.append(k)
            if not missing:
                continue
            for k in missing:
                perms[k] = 1 if perms.get(follow.get(k, 'view.manage')) else 0
            db.execute('UPDATE roles SET perms=? WHERE key=?',
                       (json.dumps(perms, ensure_ascii=False), r['key']))
            changed = True
        if changed:
            db.commit()


backfill_role_perms()


def hash_pw(pw):
    return hashlib.sha256(('yadecai|' + pw).encode('utf-8')).hexdigest()


def sync_users():
    """把 users.json 里的账号同步进库（每次启动执行，改密码重启即生效）。"""
    if not USERS_PATH.exists():
        USERS_PATH.write_text(json.dumps({
            "admin": {"password": "123456", "display_name": "管理员"}
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    data = json.loads(USERS_PATH.read_text(encoding='utf-8'))
    with lock:
        for name, cfg in data.items():
            if str(name).startswith('_'):
                continue
            db.execute(
                'INSERT INTO users(username, pass_hash, display_name, is_admin) VALUES(?,?,?,?) '
                'ON CONFLICT(username) DO UPDATE SET pass_hash=excluded.pass_hash, '
                'display_name=excluded.display_name, is_admin=excluded.is_admin',
                (name, hash_pw(cfg.get('password', '')), cfg.get('display_name', name),
                 1 if cfg.get('is_admin') else 0))
        db.commit()


sync_users()


def upsert_file_user(name, password=None, display_name=None, is_admin=None, remove=False):
    """把页面上的账号变动写回 users.json，重启服务后不丢失。"""
    try:
        data = json.loads(USERS_PATH.read_text(encoding='utf-8'))
    except Exception:
        data = {}
    if remove:
        data.pop(name, None)
    else:
        cfg = data.get(name) if isinstance(data.get(name), dict) else {}
        if password:
            cfg['password'] = password
        if display_name is not None:
            cfg['display_name'] = display_name
        if is_admin is not None:
            cfg['is_admin'] = bool(is_admin)
        data[name] = cfg
    try:
        USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

app = FastAPI(title='亚德彩投标台账')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

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
def current_user(authorization: str = Header(default='')):
    token = authorization[7:].strip() if authorization.startswith('Bearer ') else ''
    if not token:
        raise HTTPException(401, '未登录')
    with lock:
        row = db.execute('SELECT username FROM sessions WHERE token=?', (token,)).fetchone()
    if not row:
        raise HTTPException(401, '登录已过期，请重新登录')
    return row['username']


def require_admin(username: str = Depends(current_user)):
    with lock:
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
    with lock:
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
    with lock:
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


# ---------------- 审计（字段级留痕，仅追加） ----------------
FIELD_LABELS = {k: l for k, l, g in FIELD_CATALOG}


def audit_write(entries):
    """写入审计日志（调用方须持有 lock）。
    entries = [(username, rid, action, field, old, new, allowed)]"""
    if not entries:
        return
    db.executemany(
        'INSERT INTO audit_log(ts, username, rid, action, field, old_value, new_value, allowed) '
        'VALUES(?,?,?,?,?,?,?,?)',
        [(time.time(), u, rid, act, f, '' if ov is None else str(ov),
          '' if nv is None else str(nv), 1 if ok else 0)
         for u, rid, act, f, ov, nv, ok in entries])
    # 只保留最近 5000 条，避免库无限增长
    db.execute('DELETE FROM audit_log WHERE id NOT IN '
               '(SELECT id FROM audit_log ORDER BY id DESC LIMIT 5000)')


def audit_diff(username, rid, old, new, action='update'):
    """对比新旧值，仅记录实际变更的字段。"""
    entries = []
    for k in FIELD_KEYS:
        ov = (old or {}).get(k, '')
        nv = (new or {}).get(k, '')
        if str(ov or '') == str(nv or ''):
            continue
        entries.append((username, rid, action, k, ov, nv, True))
    return entries


def self_names(username):
    """行级「仅本人」范围匹配用的名字集合（账号 + 显示名）。"""
    with lock:
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


class PwdIn(BaseModel):
    old: str = ''
    new: str = ''


class RoleIn(BaseModel):
    key: str = ''
    name: str = ''
    perms: dict = {}
    field_perms: dict = {}
    data_scope: str = 'ALL'


class MergeIn(BaseModel):
    rows: list = []
    overwrite: bool = True


def user_out(r, perms=None):
    role = 'admin' if r['is_admin'] else (r['role_key'] or 'staff')
    o = {'username': r['username'], 'displayName': r['display_name'] or r['username'],
         'isAdmin': bool(r['is_admin']), 'roleKey': role}
    if perms is not None:
        o['perms'] = perms['perms']
        o['fieldPerms'] = perms['fieldPerms']
        o['roleName'] = perms['roleName']
        o['dataScope'] = perms['dataScope']
    return o


# ---------------- 接口 ----------------
@app.post('/api/login')
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


@app.get('/api/me')
def me(username: str = Depends(current_user)):
    with lock:
        u = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        raise HTTPException(401, '账号不存在')
    return {'user': user_out(u, perm_of(username))}


@app.post('/api/me/password')
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


@app.get('/api/perm/catalog')
def perm_catalog(username: str = Depends(current_user)):
    """权限矩阵编辑用的目录：模块/操作 + 台账字段。新增模块只需后端 PERM_CATALOG 加一行。"""
    return {
        'perms': [{'key': k, 'label': l, 'group': g} for k, l, g in PERM_CATALOG],
        'fields': [{'key': k, 'label': l, 'group': g} for k, l, g in FIELD_CATALOG],
        'levels': [{'key': k, 'label': l} for k, l in LEVELS],
        'masks': [{'key': k, 'label': l} for k, l in MASK_CATALOG],
        'scopes': [{'key': k, 'label': l} for k, l in SCOPES]
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
    return {'key': r['key'], 'name': r['name'], 'isSystem': bool(r['is_system']),
            'perms': {k: bool(perms.get(k)) for k in PERM_KEYS},
            'fieldPerms': norm_field_perms(fp),
            'dataScope': (r['data_scope'] if 'data_scope' in r.keys() else None) or 'ALL'}


@app.get('/api/roles')
def list_roles(admin: str = Depends(require_admin)):
    with lock:
        rows = db.execute('SELECT * FROM roles ORDER BY is_system DESC, key').fetchall()
    return {'roles': [role_out(r) for r in rows]}


def _clean_perms(d):
    return {k: 1 if d.get(k) else 0 for k in PERM_KEYS}


def _clean_scope(v):
    return v if v in [s[0] for s in SCOPES] else 'ALL'


@app.post('/api/roles')
def create_role(body: RoleIn, admin: str = Depends(require_admin)):
    key = body.key.strip()
    if not key:
        raise HTTPException(400, '角色标识不能为空')
    if not body.name.strip():
        raise HTTPException(400, '角色名称不能为空')
    with lock:
        if db.execute('SELECT 1 FROM roles WHERE key=?', (key,)).fetchone():
            raise HTTPException(400, '角色「' + key + '」已存在')
        db.execute('INSERT INTO roles(key, name, is_system, perms, field_perms, data_scope) '
                   'VALUES(?,?,0,?,?,?)',
                   (key, body.name.strip(), json.dumps(_clean_perms(body.perms), ensure_ascii=False),
                    json.dumps(norm_field_perms(body.field_perms), ensure_ascii=False),
                    _clean_scope(body.data_scope)))
        db.commit()
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
    return {'role': role_out(row)}


@app.put('/api/roles/{key}')
def update_role(key: str, body: RoleIn, admin: str = Depends(require_admin)):
    with lock:
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, '角色不存在')
        name = body.name.strip() or row['name']
        db.execute('UPDATE roles SET name=?, perms=?, field_perms=?, data_scope=? WHERE key=?',
                   (name, json.dumps(_clean_perms(body.perms), ensure_ascii=False),
                    json.dumps(norm_field_perms(body.field_perms), ensure_ascii=False),
                    _clean_scope(body.data_scope), key))
        db.commit()
        row = db.execute('SELECT * FROM roles WHERE key=?', (key,)).fetchone()
    return {'role': role_out(row)}


@app.delete('/api/roles/{key}')
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


@app.get('/api/users')
def list_users(admin: str = Depends(require_admin)):
    with lock:
        rows = db.execute('SELECT * FROM users ORDER BY is_admin DESC, username').fetchall()
    return {'users': [user_out(r) for r in rows]}


@app.get('/api/staff')
def staff_list(q: str = '', username: str = Depends(current_user)):
    """非管理员用户名单（用于负责人筛选与标书负责人/协作人/审核人选择），q 模糊匹配账号或显示名。"""
    kw = '%' + q.strip().lower() + '%'
    with lock:
        rows = db.execute(
            'SELECT username, display_name FROM users WHERE is_admin=0 '
            'AND (LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?) '
            'ORDER BY display_name, username', (kw, kw)).fetchall()
    return {'staff': [{'username': r['username'], 'displayName': r['display_name'] or r['username']}
                      for r in rows]}


# ---------------- 工作日报 ----------------
class ReportIn(BaseModel):
    user: str = ''
    rdate: str
    content: str = ''
    plan: str = ''


def _is_admin(username):
    with lock:
        r = db.execute('SELECT is_admin FROM users WHERE username=?', (username,)).fetchone()
    return bool(r and r['is_admin'])


@app.get('/api/reports')
def reports_list(user: str = '', ym: str = '', username: str = Depends(current_user)):
    """某人某年/某月的工作日报（view.reports 页面权限；user 缺省 = 自己）。
    ym 传 '2026' 取整年、'2026-11' 取当月，缺省取全部。
    查看他人日报需要 report.viewOthers 权限（管理员恒可）。"""
    need(username, 'view.reports', '无工作日报查看权限')
    target = (user or username).strip()
    if target != username and not _is_admin(username):
        need(username, 'report.viewOthers', '无查看其他人员日报的权限')
    prefix = ym.strip()
    with lock:
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


@app.put('/api/reports')
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


@app.post('/api/users')
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
        db.execute('INSERT INTO users(username, pass_hash, display_name, is_admin, role_key) '
                   'VALUES(?,?,?,?,?)',
                   (name, hash_pw(body.password), body.display_name.strip() or name,
                    1 if body.is_admin else 0, role_key))
        db.commit()
    upsert_file_user(name, password=body.password,
                     display_name=body.display_name.strip() or name, is_admin=body.is_admin)
    return {'ok': True}


@app.put('/api/users/{name}')
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
        if body.password:
            db.execute('UPDATE users SET pass_hash=?, display_name=?, is_admin=?, role_key=? '
                       'WHERE username=?',
                       (hash_pw(body.password), body.display_name.strip() or name, new_admin,
                        role_key, name))
        else:
            db.execute('UPDATE users SET display_name=?, is_admin=?, role_key=? WHERE username=?',
                       (body.display_name.strip() or name, new_admin, role_key, name))
        db.commit()
    upsert_file_user(name, password=body.password or None,
                     display_name=body.display_name.strip() or name, is_admin=body.is_admin)
    return {'ok': True}


@app.delete('/api/users/{name}')
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


@app.get('/api/bootstrap')
def bootstrap(username: str = Depends(current_user)):
    p = perm_of(username)
    fp = p['fieldPerms']
    names = self_names(username)
    with lock:
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


# ---------------- 投标项目文件目录（bidfile/年/月/项目名） ----------------
BID_DIR_ROOT = 'bidfile'


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


def ensure_bid_dir(rid, payload, who):
    """确保投标项目的文件目录存在（幂等），项目改名时同步目录名。
    调用方须持有 lock；返回 (node_id, 路径)。失败由调用方兜底，不阻断台账保存。"""
    segs = bid_dir_segs(payload)
    want = fs_core.safe_name(segs[-1])
    m = db.execute('SELECT * FROM bid_fs_dir WHERE rid=?', (rid,)).fetchone()
    if m:
        node = fs_core.get_alive(m['node_id'])
        if node:
            if node['name'] != want:
                try:
                    fs_core.do_rename(node['id'], want, who)   # 同名冲突则保留旧目录名
                except Exception:
                    pass
            path = fs_core.path_of(node['id'])
            if path != (m['path'] or ''):
                db.execute('UPDATE bid_fs_dir SET path=? WHERE rid=?', (path, rid))
            return node['id'], path
    node_id = fs_core.ensure_path(segs, who)
    for role, allow in bid_upload_roles():
        fs_core.grant_role(node_id, role, allow, who)
    fs_core.fs_log(who, 'mkdir', fs_core.get_node(node_id), target='投标项目目录（自动创建）')
    path = fs_core.path_of(node_id)
    db.execute('INSERT INTO bid_fs_dir(rid, node_id, path, created_at) VALUES(?,?,?,?) '
               'ON CONFLICT(rid) DO UPDATE SET node_id=excluded.node_id, path=excluded.path',
               (rid, node_id, path, time.time()))
    return node_id, path


@app.put('/api/rows/{rid}')
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


@app.delete('/api/rows/{rid}')
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


@app.get('/api/bid-dir')
def bid_dir(rid: str = '', username: str = Depends(current_user)):
    """投标项目对应的文件目录（bidfile/开标年/开标月/项目名）：
    不存在则按规则创建，返回目录节点 id、路径与文件数（供台账「上传文件」使用）。"""
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
            node_id, path = ensure_bid_dir(rid, payload, username)
            cnt = db.execute("SELECT COUNT(*) c FROM fs_node WHERE parent_id=? AND kind='file' "
                             "AND deleted=0", (node_id,)).fetchone()['c']
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(400, '创建项目目录失败：' + str(e))
    return {'rid': rid, 'nodeId': node_id, 'path': path, 'files': int(cnt or 0),
            'name': payload.get('projectName', '')}


@app.post('/api/clear')
def clear_all(username: str = Depends(current_user)):
    p = perm_of(username)
    need(username, 'bid.delete', '无台账清空权限')
    names = self_names(username)
    ts = time.time()
    with lock:
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


@app.post('/api/merge')
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
    with lock:
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


@app.get('/api/changes')
def changes(since: float = 0.0, username: str = Depends(current_user)):
    """增量同步：返回 since（服务器时间戳）之后的变更与删除（按当前用户字段权限过滤）。"""
    p = perm_of(username)
    fp = p['fieldPerms']
    names = self_names(username)
    with lock:
        changed = []
        for r in db.execute('SELECT * FROM rows WHERE updated_at > ?', (since,)).fetchall():
            raw = json.loads(r['payload'])
            if row_in_scope(raw, p['dataScope'], names):
                changed.append(pack(r, fp))
        deleted = [r['rid'] for r in db.execute(
            'SELECT rid FROM deleted WHERE ts > ?', (since,)).fetchall()]
    return {'changed': changed, 'deleted': deleted, 'ts': time.time()}


@app.get('/api/audit')
def list_audit(limit: int = 100, rid: str = '', username: str = Depends(current_user)):
    """字段级审计日志（需「审计日志」模块权限；管理员默认拥有）。"""
    if not perm_of(username)['perms'].get('view.audit'):
        raise HTTPException(403, '无审计日志查看权限')
    limit = max(1, min(500, int(limit)))
    with lock:
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
app.include_router(fs_api_mod.build_router(current_user))


# 前端静态托管（frontend/dist，Vite 构建产物）+ SPA history 路由回退：
# 生产环境由 Nginx 托管，此兜底仅为直连 8000 端口联调时刷新 /manage、/users 不 404。
from fastapi.responses import FileResponse

DIST = ROOT / 'frontend' / 'dist'


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
