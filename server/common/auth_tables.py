# -*- coding: utf-8 -*-
"""用户/角色/会话建表与种子（从 main.py 原样抽出，函数体逐字不变）。

重要：本模块只定义纯函数，不含任何启动期副作用调用。init_db() / seed_roles() /
backfill_role_perms() / sync_users() 仍由 server.main 按原顺序在启动时调用，
保证与重构前完全一致的初始化时序；将来拆成多个 app 时也不会被重复触发。
"""
import json
import time

from . import guard
from .catalog import ROLE_SEEDS
from .database import DB_PATH, USERS_PATH, db
from .security import hash_pw


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
    with guard.lock:
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
          tender_node_id TEXT DEFAULT '',
          bid_node_id TEXT DEFAULT '',
          created_at DOUBLE PRECISION NOT NULL
        );
        ''')
        # 老库升级：bid_fs_dir 补「招标文件 / 标书」两个子目录的节点 id（幂等）
        for col in ('tender_node_id', 'bid_node_id'):
            try:
                db.execute('ALTER TABLE bid_fs_dir ADD COLUMN ' + col + " TEXT DEFAULT ''")
                db.commit()
            except Exception:
                db.rollback()
        migrate_from_sqlite()
        # 删除墓碑只用于增量同步回放，保留 90 天足够覆盖长期离线客户端，避免表无限增长
        db.execute('DELETE FROM deleted WHERE ts < ?', (time.time() - 90 * 86400,))
        db.commit()


def seed_roles():
    """初始化内置角色（已存在则跳过，不覆盖管理员改动）。"""
    with guard.lock:
        for key, name, perms, fp, scope in ROLE_SEEDS:
            if db.execute('SELECT 1 FROM roles WHERE key=?', (key,)).fetchone():
                continue
            db.execute('INSERT INTO roles(key, name, is_system, perms, field_perms, data_scope) '
                       'VALUES(?,?,1,?,?,?)',
                       (key, name, json.dumps(perms, ensure_ascii=False),
                        json.dumps(fp, ensure_ascii=False), scope))
        db.commit()


def backfill_role_perms():
    """新增页面权限时给存量角色补默认值：跟随该角色「投标管理」页的开关，一次性补齐。
    view.files 跟随 view.manage（能进台账就能进文件存储）；file.admin 跟随 view.audit；
    report.viewOthers 跟随 view.audit（管理属性权限，默认仅管理层可看他人日报）。"""
    page_perms = ('view.timeline', 'view.assign', 'view.alerts', 'view.reports',
                  'view.files', 'view.aiSummary', 'view.bidcrawl', 'view.finance')
    follow = {'file.admin': 'view.audit', 'report.viewOthers': 'view.audit',
              'ai.generate': 'view.manage', 'ai.confirm': 'bid.edit',
              'ai.layout': 'view.audit', 'bidcrawl.manage': 'bid.edit'}
    with guard.lock:
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


def sync_users():
    """把 users.json 里的账号同步进库（每次启动执行，改密码重启即生效）。"""
    if not USERS_PATH.exists():
        USERS_PATH.write_text(json.dumps({
            "admin": {"password": "123456", "display_name": "管理员"}
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    data = json.loads(USERS_PATH.read_text(encoding='utf-8'))
    with guard.lock:
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


def upsert_file_user(name, password=None, display_name=None, is_admin=None, remove=False):
    """把页面上的账号变动写回 users.json，重启服务后不丢失。

    文件读-改-写不是 DB 事务，去全局锁后必须用自己的细粒度锁保护，
    否则并发的用户管理与台账自动建号会互相覆盖整个文件。"""
    with guard.keyed('users_json'):
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
