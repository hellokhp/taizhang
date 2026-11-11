# -*- coding: utf-8 -*-
"""文件存储模块核心：配置 / 命名安全 / blob 落盘 / 目录树 / 权限位 / 版本备份 / 操作日志 / 清理任务

设计要点：
  · 目录树存 DB（fs_node），磁盘只放内容寻址的 blob（不可变、去重）
  · blob 不可变 => 「覆盖前备份旧版本」只是插一行版本记录，零拷贝
  · 权限为 5 个原子位（上传/查看/删除/修改/覆盖）+ 授权位，节点级 ACL 可继承
  · 版本保留期固化在 keep_until 字段，改配置不追溯存量（除显式同步）
本模块不含 HTTP，由 fs_api.py 组装路由；DB / 锁 / 权限函数由 main.py 注入。
"""
import hashlib
import os
import shutil
import threading
import time
import uuid
from pathlib import Path


# ---------------- 权限位（原子化五动作 + 授权位） ----------------
class OP:
    UPLOAD = 1 << 0      # 上传新文件 / 新建文件夹
    VIEW = 1 << 1        # 查看列表、详情、下载、预览
    MODIFY = 1 << 2      # 重命名、移动、改备注（不含内容替换）
    OVERWRITE = 1 << 3   # 同名覆盖写（产生新版本）
    DELETE = 1 << 4      # 删除（进回收站）/ 彻底删除
    GRANT = 1 << 5       # 给他人授权

ALL_OPS = OP.UPLOAD | OP.VIEW | OP.MODIFY | OP.OVERWRITE | OP.DELETE
OP_KEYS = ['upload', 'view', 'delete', 'modify', 'overwrite']
OP_BIT = {'upload': OP.UPLOAD, 'view': OP.VIEW, 'delete': OP.DELETE,
          'modify': OP.MODIFY, 'overwrite': OP.OVERWRITE, 'grant': OP.GRANT}
OP_NAME = {'upload': '上传', 'view': '查看', 'delete': '删除',
           'modify': '修改', 'overwrite': '覆盖', 'grant': '授权'}


class FsError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.msg = msg
        self.code = code


# ---------------- 配置 ----------------
DEFAULTS = {
    'retention_days': '7',           # 备份（旧版本 + 回收站）保留天数
    'log_retention_days': '180',     # 操作日志保留天数
    'chunk_size': '16777216',        # 分片大小 16MB：大文件减少请求数与 fsync 次数
    'max_file_size': '2147483648',   # 单文件上限 2GB
    'max_name_len': '100',
    'max_depth': '16',
    'quota_bytes': '0',              # 0=不限
    'allow_ext': '',                 # 白名单，空=不限制
    'deny_ext': 'exe,bat,cmd,sh,js,msi,scr',
    'purge_hour': '3',
    'enable_dedup': '1',
    'max_versions': '0',             # 单文件最多版本数，0=不限
    'owner_full_control': '1',       # 创建人对自己节点全权
    'log_detail_level': '1',         # 1=逐文件明细
    'office_server': '',             # ONLYOFFICE Document Server 地址（浏览器可达），空=禁用在线预览/编辑
    'office_secret': '',             # DS 的 JWT 密钥（两边一致才通过校验；DS 未启用 JWT 则留空）
    'office_base_url': '',           # DS 容器回源取文件的地址（如 http://host.docker.internal:8000），空=按请求推导
    'office_url_secret': '',         # 签名 URL 内部密钥（首次启动自动生成，勿手改）
}
ENV_KEYS = {
    'retention_days': 'FS_RETENTION_DAYS',
    'log_retention_days': 'FS_LOG_RETENTION_DAYS',
    'chunk_size': 'FS_CHUNK_SIZE',
    'max_file_size': 'FS_MAX_FILE_SIZE',
    'quota_bytes': 'FS_QUOTA_BYTES',
    'office_server': 'OFFICE_SERVER',
    'office_secret': 'OFFICE_SECRET',
    'office_base_url': 'OFFICE_BASE_URL',
}

ROOT_ID = ''
ROOT_NODE = {'id': ROOT_ID, 'parent_id': None, 'name': '存储根目录', 'kind': 'dir',
             'owner': 'admin', 'deleted': 0, 'created_at': 0, 'updated_at': 0,
             'updated_by': '', 'deleted_at': 0, 'deleted_by': '', 'size': 0, 'note': ''}


class _Ctx:
    def __init__(self, db, lock, perm_of):
        self.db = db
        self.lock = lock
        self.perm_of = perm_of
        self.root = None
        self.tmp = None
        self.blobs = None


ctx = None


def init_fs(db, lock, perm_of, storage_root=None):
    """由 main.py 在启动时调用：注入依赖、创建目录、启动清理线程。"""
    global ctx
    env_root = (storage_root or os.environ.get('STORAGE_ROOT', '')).strip()
    if env_root:
        root = Path(env_root)
    elif os.name == 'nt':
        root = Path(__file__).resolve().parents[1] / 'storage'
    else:
        root = Path('/var/lib/taizhang/storage')
    ctx = _Ctx(db, lock, perm_of)
    ctx.root = root
    ctx.blobs = root / 'blobs'
    ctx.tmp = root / 'tmp'
    for d in (root, ctx.blobs, ctx.tmp):
        d.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(str(root), 0o750)
        except Exception:
            pass
    threading.Thread(target=_gc_loop, daemon=True).start()
    return ctx


def db():
    return ctx.db


def lock():
    return ctx.lock


# ---------------- 配置读写 ----------------
# 这三项要能在「文件存储 → 设置」里直接改并立即生效，所以 DB 优先、env 只作默认值；
# 其余运维类配置（保留天数、分片大小等）保持 env 覆盖 DB，便于运维统一压制。
_DB_FIRST = ('office_server', 'office_secret', 'office_base_url')


def setting_str(k):
    if k in _DB_FIRST:
        r = db().execute('SELECT v FROM fs_settings WHERE k=?', (k,)).fetchone()
        if r and str(r['v'] or '').strip():
            return str(r['v']).strip()
        env = ENV_KEYS.get(k)
        return (os.environ.get(env, '').strip() if env else '') or DEFAULTS.get(k, '')
    env = ENV_KEYS.get(k)
    if env and os.environ.get(env, '').strip():
        return os.environ[env].strip()
    r = db().execute('SELECT v FROM fs_settings WHERE k=?', (k,)).fetchone()
    return r['v'] if r else DEFAULTS.get(k, '')


def setting_int(k):
    try:
        return int(setting_str(k))
    except Exception:
        return int(DEFAULTS.get(k, '0'))


def set_setting(k, v, who):
    if k not in DEFAULTS:
        raise FsError('未知配置项：' + str(k))
    with lock():
        db().execute('INSERT INTO fs_settings(k, v, updated_at, updated_by) VALUES(?,?,?,?) '
                     'ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at, '
                     'updated_by=excluded.updated_by',
                     (k, str(v), time.time(), who))
        db().commit()


def all_settings():
    return {k: setting_str(k) for k in DEFAULTS}


# ---------------- 命名安全（跨平台：统一按 Windows 严格集） ----------------
ILLEGAL = set('<>:"/\\|?*') | set(chr(c) for c in range(32))
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} \
    | {'LPT%d' % i for i in range(1, 10)}


def safe_name(raw, max_len=None):
    """名称归一化。

    注意：只清理「尾部」的点和空格（Windows 不允许文件名以 . 或空格结尾），
    开头的点必须保留 —— .github / .gitignore / .env 这类隐藏文件是合法的，
    之前用 strip(' .') 会把 .github 变成 github，导致整文件夹上传报「非法路径」。
    """
    max_len = max_len or setting_int('max_name_len')
    name = str(raw or '').strip().replace('\\', '/').split('/')[-1]
    name = ''.join('_' if ch in ILLEGAL else ch for ch in name)
    name = name.strip(' ').rstrip(' .')
    if not name or name in ('.', '..'):
        raise FsError('名称不能为空（且不合法字符已被替换）')
    if len(name) > max_len:
        name = name[:max_len].rstrip(' .') or ('unnamed')
    if name.split('.')[0].upper() in RESERVED:
        name = '_' + name
    return name


def rel_bad(rel):
    """返回相对路径中第一个非法片段，合法则返回 ''（用于给出精确报错）。"""
    if not rel:
        return ''
    if rel.startswith('/') or '\\' in rel:
        return rel
    for seg in rel.split('/'):
        if ':' in seg:
            return seg
        if seg in ('', '.', '..'):
            return seg or rel
        try:
            if safe_name(seg) != seg:
                return seg
        except Exception:
            return seg
    return ''


def rel_ok(rel):
    """整文件夹上传的相对路径校验：防 zip-slip / 路径穿越。"""
    if not rel:
        return False
    return rel_bad(rel) == ''


def ext_of(name):
    p = str(name or '').rsplit('.', 1)
    return p[1].lower() if len(p) == 2 and p[1] else ''


def check_ext(name):
    ext = ext_of(name)
    deny = [x.strip().lower() for x in setting_str('deny_ext').split(',') if x.strip()]
    if ext and ext in deny:
        raise FsError('禁止上传 .%s 类型文件' % ext)
    allow = [x.strip().lower() for x in setting_str('allow_ext').split(',') if x.strip()]
    if allow and (not ext or ext not in allow):
        raise FsError('仅允许上传：' + ', '.join(allow))


# ---------------- blob 实体 ----------------
def blob_path(sha):
    p = ctx.blobs / sha[0:2] / sha[2:4] / sha
    return p


def ensure_blob_dir(sha):
    d = ctx.blobs / sha[0:2] / sha[2:4]
    d.mkdir(parents=True, exist_ok=True)
    return d


def hash_file(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def put_blob(src_path, sha=None):
    """把已写好的临时文件落为 blob（先算哈希再原子替换）。返回 sha256。"""
    if sha is None:
        sha = hash_file(src_path)
    dst = blob_path(sha)
    if not dst.exists():
        ensure_blob_dir(sha)
        try:
            os.replace(str(src_path), str(dst))     # 同盘原子替换；已存在则丢弃临时文件
        except OSError:
            # Windows：文件刚关闭时杀毒/索引可能还占着句柄，replace 报 WinError 5/32
            time.sleep(0.15)
            try:
                os.replace(str(src_path), str(dst))
            except OSError:
                shutil.copyfile(str(src_path), str(dst))
                try:
                    os.remove(str(src_path))
                except OSError:
                    pass
    else:
        try:
            os.remove(str(src_path))
        except Exception:
            pass
    with lock():
        row = db().execute('SELECT 1 FROM fs_blob WHERE sha256=?', (sha,)).fetchone()
        if not row:
            db().execute('INSERT INTO fs_blob(sha256, size, ref_count, created_at) VALUES(?,?,1,?)',
                         (sha, os.path.getsize(str(dst)), time.time()))
        else:
            bump_ref(sha, 1)
        # 必须提交：原来只在不插入时漏了 commit，连接会停在 idle in transaction
        # 并一直持有该 blob 行的锁，后续同名内容上传全部阻塞，进而在全局锁上把整个服务拖死
        db().commit()
    return sha


def bump_ref(sha, delta):
    db().execute('UPDATE fs_blob SET ref_count=GREATEST(ref_count+?,0) WHERE sha256=?', (delta, sha))


def used_bytes():
    r = db().execute("SELECT COALESCE(SUM(size),0) c FROM fs_node WHERE kind='file' "
                     "AND deleted=0").fetchone()
    return int(r['c'] or 0)


def check_quota(add=0):
    quota = setting_int('quota_bytes')
    if quota > 0 and used_bytes() + add > quota:
        raise FsError('存储空间不足：已超出配额上限')
    try:
        free = shutil.disk_usage(str(ctx.root)).free
    except Exception:
        free = 0
    if free and add and free < add * 1.2:
        raise FsError('服务器磁盘剩余空间不足，无法上传')


# ---------------- 节点 ----------------
def now():
    return time.time()


def new_id():
    return uuid.uuid4().hex


# ---------------- 上传分片索引 ----------------
# 每片一行，靠主键 (upload_id, idx) 保证幂等；并发写互不干扰，重传覆盖也不报错。
def add_part(upload_id, idx, size=0):
    db().execute('INSERT INTO fs_upload_part(upload_id, idx, size, ts) VALUES(?,?,?,?) '
                 'ON CONFLICT(upload_id, idx) DO UPDATE SET size=EXCLUDED.size, ts=EXCLUDED.ts',
                 (upload_id, int(idx), int(size), time.time()))


def parts_of(upload_id):
    return [int(r['idx']) for r in db().execute(
        'SELECT idx FROM fs_upload_part WHERE upload_id=? ORDER BY idx', (upload_id,)).fetchall()]


def part_count(upload_id):
    r = db().execute('SELECT COUNT(*) c FROM fs_upload_part WHERE upload_id=?',
                     (upload_id,)).fetchone()
    return int(r['c'] if r else 0)


def clear_parts(upload_id):
    db().execute('DELETE FROM fs_upload_part WHERE upload_id=?', (upload_id,))


def get_node(nid):
    if not nid:
        return dict(ROOT_NODE)
    r = db().execute('SELECT * FROM fs_node WHERE id=?', (nid,)).fetchone()
    return dict(r) if r else None


def get_alive(nid):
    n = get_node(nid)
    if not n or n['deleted']:
        return None
    return n


def chain_of(nid):
    """根 → ... → 当前节点（含虚拟根），最多 max_depth+2 层防环。"""
    out = []
    cur = get_node(nid)
    guard = setting_int('max_depth') + 4
    while cur and guard > 0:
        out.append(cur)
        guard -= 1
        if not cur['parent_id']:
            break
        cur = get_node(cur['parent_id'])
        if cur is None:
            break
    out.reverse()
    if not out or out[0]['id'] != ROOT_ID:
        out.insert(0, dict(ROOT_NODE))
    return out


def path_of(nid):
    return '/' + '/'.join(n['name'] for n in chain_of(nid)[1:]) if nid else '/'


def depth_of(parent_id):
    return max(0, len(chain_of(parent_id)) - 1)


def children(parent_id, kw='', limit=500, offset=0):
    sql = 'SELECT * FROM fs_node WHERE parent_id=? AND deleted=0'
    args = [parent_id]
    if kw:
        sql += ' AND lower(name) LIKE ?'
        args.append('%' + str(kw).lower() + '%')
    sql += " ORDER BY (kind='file'), lower(name) LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def subtree(nid, include_self=True):
    """广度收集子树全部节点（含自身）。"""
    out = []
    if include_self:
        n = get_node(nid)
        if n:
            out.append(n)
    queue = [nid]
    guard = 20000
    while queue and guard > 0:
        guard -= 1
        pid = queue.pop(0)
        for r in db().execute('SELECT * FROM fs_node WHERE parent_id=?', (pid,)).fetchall():
            out.append(dict(r))
            queue.append(r['id'])
    return out


def name_taken(parent_id, name, exclude=None):
    sql = 'SELECT 1 FROM fs_node WHERE parent_id=? AND lower(name)=lower(?) AND deleted=0'
    args = [parent_id, name]
    if exclude:
        sql += ' AND id<>?'
        args.append(exclude)
    return bool(db().execute(sql, args).fetchone())


def mkdir(parent_id, name, who):
    name = safe_name(name)
    if depth_of(parent_id) + 1 > setting_int('max_depth'):
        raise FsError('目录层级超过上限 %d 层' % setting_int('max_depth'))
    if name_taken(parent_id, name):
        raise FsError('同目录下已存在同名（不区分大小写）：' + name)
    nid = new_id()
    t = now()
    db().execute('INSERT INTO fs_node(id, parent_id, name, kind, owner, created_at, updated_at, '
                 'updated_by) VALUES(?,?,?,\'dir\',?,?,?,?)',
                 (nid, parent_id, name, who, t, t, who))
    return nid


def mkdir_batch(parent_id, paths, who):
    """按相对路径批量建目录，返回 {rel: node_id}（用于整文件夹上传）。"""
    cache = {'': parent_id}
    out = {}
    for rel in paths:
        if not rel_ok(rel):
            bad = rel_bad(rel)
            raise FsError('非法路径：%s（片段「%s」不合法：不能包含 \\ / : * ? " < > | 等字符，'
                          '不能为 . 或 ..，也不能以点或空格结尾）' % (str(rel), bad))
        cur = parent_id
        acc = ''
        segs = rel.split('/')
        for i, seg in enumerate(segs):
            acc = seg if i == 0 else acc + '/' + seg
            if acc in cache:
                cur = cache[acc]
                continue
            try:
                nid = mkdir(cur, seg, who)
            except FsError:
                r = db().execute('SELECT id FROM fs_node WHERE parent_id=? AND lower(name)=lower(?) '
                                 'AND kind=\'dir\' AND deleted=0', (cur, seg)).fetchone()
                if not r:
                    raise
                nid = r['id']
            cache[acc] = nid
            cur = nid
        out[rel] = cur
    return out


def ensure_path(segs, owner):
    """在根目录下逐级确保目录存在（同层同名不区分大小写复用），返回最深层 node_id。
    业务侧自动建目录用；调用方须持有 lock，事务由调用方提交。"""
    parent = ROOT_ID
    for seg in (segs or []):
        name = safe_name(seg)
        r = db().execute("SELECT id FROM fs_node WHERE parent_id=? AND lower(name)=lower(?) "
                         "AND kind='dir' AND deleted=0", (parent, name)).fetchone()
        if r:
            parent = r['id']
            continue
        parent = mkdir(parent, name, owner)
    return parent


def grant_role(node_id, role, allow, who, inherit=True):
    """给某角色授予节点权限（幂等）：业务目录创建时一次性放开团队协作位。"""
    db().execute('INSERT INTO fs_acl(node_id, subject_type, subject_key, allow_mask, deny_mask, '
                 'inherit, granted_by, granted_at) VALUES(?,?,?,?,0,?,?,?) '
                 'ON CONFLICT(node_id, subject_type, subject_key) DO UPDATE SET '
                 'allow_mask=excluded.allow_mask, inherit=excluded.inherit, '
                 'granted_by=excluded.granted_by, granted_at=excluded.granted_at',
                 (node_id, 'role', str(role), int(allow), 1 if inherit else 0, who, now()))


# ---------------- 权限 ----------------
def perm_of(username):
    return ctx.perm_of(username)


def is_admin(username):
    try:
        return perm_of(username)['roleKey'] == 'admin'
    except Exception:
        return False


def role_of(username):
    try:
        return perm_of(username)['roleKey'] or 'staff'
    except Exception:
        return 'staff'


def mask_of(allow, deny):
    return int(allow or 0) & ~int(deny or 0)


def apply_acl(mask, node_id, username, role, is_self=True):
    """叠加某节点自身的 ACL 行（同节点内：拒绝优先、用户级 > 角色级）。"""
    rows = [dict(r) for r in db().execute(
        'SELECT * FROM fs_acl WHERE node_id=? ORDER BY id', (node_id,)).fetchall()]
    if not rows:
        return mask
    for r in [x for x in rows if x['subject_type'] == 'role']:
        if not is_self and not r['inherit']:
            continue
        if r['subject_key'] != role:
            continue
        mask = (mask | int(r['allow_mask'])) & ~int(r['deny_mask'])
    for r in [x for x in rows if x['subject_type'] == 'user']:
        if not is_self and not r['inherit']:
            continue
        if r['subject_key'] != username:
            continue
        mask = (mask | int(r['allow_mask'])) & ~int(r['deny_mask'])
    return mask


def base_mask(username):
    p = perm_of(username)
    return OP.VIEW if p['perms'].get('view.files') else 0


def eff_mask(username, node):
    """有效权限位：admin 直通 → owner 直通 → 角色默认(VIEW) → 自顶向下叠加 ACL。"""
    if is_admin(username):
        return ALL_OPS | OP.GRANT
    node = node or dict(ROOT_NODE)
    if setting_int('owner_full_control') and node['owner'] == username and node['id'] != ROOT_ID:
        return ALL_OPS | OP.GRANT
    role = role_of(username)
    mask = base_mask(username)
    for n in chain_of(node['id']):
        mask = apply_acl(mask, n['id'], username, role, is_self=(n['id'] == node['id']))
    return mask


def child_mask(username, node, parent_mask=None):
    """列表场景：父目录权限已算好，子节点只需再叠加自身 ACL。"""
    if is_admin(username):
        return ALL_OPS | OP.GRANT
    if parent_mask is None:
        parent_mask = eff_mask(username, get_node(node['parent_id']))
    if setting_int('owner_full_control') and node['owner'] == username:
        return ALL_OPS | OP.GRANT
    return apply_acl(parent_mask, node['id'], username, role_of(username), is_self=True)


def eff_perm(username, node):
    m = eff_mask(username, node)
    out = {k: bool(m & OP_BIT[k]) for k in OP_KEYS}
    out['grant'] = bool(m & OP.GRANT)
    return out


def require_op(username, node, op):
    """校验单个原子动作；失败抛 403。"""
    if not (eff_mask(username, node) & op):
        raise FsError('无「%s」权限' % OP_NAME.get(_bit_key(op), '该操作'), 403)


def _bit_key(op):
    for k, v in OP_BIT.items():
        if v == op:
            return k
    return 'op'


def can_grant(username, node):
    if is_admin(username):
        return True
    if perm_of(username)['perms'].get('file.admin'):
        return True
    return bool(eff_mask(username, node) & OP.GRANT)


# ---------------- 版本 / 备份 ----------------
def add_version(node_id, ver, sha, size, change_type, actor, name='', path='', note=''):
    keep = now() + setting_int('retention_days') * 86400
    db().execute('INSERT INTO fs_version(node_id, ver, blob_sha, size, name, parent_path, '
                 'change_type, actor, ts, keep_until, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 (node_id, ver, sha, size, name, path, change_type, actor, now(), keep, note))
    bump_ref(sha, 1)


def file_row(node_id):
    r = db().execute('SELECT * FROM fs_file WHERE node_id=?', (node_id,)).fetchone()
    return dict(r) if r else None


def snapshot_version(node_id, change_type, actor, note=''):
    """操作前把「当前状态」存为备份版本（覆盖/改名/删除都走这里）。

    幂等：每次内容变更本身就会写一条版本行，所以「当前内容」在历史里通常已经有记录；
    这里按 (node_id, ver, blob_sha) 判重，避免同一份内容被重复记录一遍。
    只有在当前内容没有版本行时（例如早期导入/历史数据缺失）才真正补写。
    """
    f = file_row(node_id)
    if not f:
        return
    dup = db().execute('SELECT id FROM fs_version WHERE node_id=? AND ver=? AND blob_sha=? LIMIT 1',
                       (node_id, f['ver'], f['blob_sha'])).fetchone()
    if dup:
        return
    n = get_node(node_id)
    add_version(node_id, f['ver'], f['blob_sha'], f['size'], change_type, actor,
                (n or {}).get('name', ''), path_of(node_id), note)


def dedupe_versions():
    """清理历史遗留的重复版本行（早期「覆盖前备份」与内容行重复写入导致）。

    同一 (node_id, ver, blob_sha) 只保留最早的一条；组内若有锁定版本，锁定状态合并到保留行。
    返回 (删除条数, 合并锁定数)。
    """
    with lock():
        merged = db().execute(
            'UPDATE fs_version SET pinned=1 WHERE id IN ('
            'SELECT MIN(id) FROM fs_version GROUP BY node_id, ver, blob_sha '
            'HAVING SUM(CASE WHEN pinned=1 THEN 1 ELSE 0 END)>0)')
        merged = merged.rowcount if merged.rowcount and merged.rowcount > 0 else 0
        cur = db().execute(
            'DELETE FROM fs_version a USING fs_version b '
            'WHERE a.node_id=b.node_id AND a.ver=b.ver AND a.blob_sha=b.blob_sha AND a.id>b.id')
        removed = max(0, cur.rowcount or 0)
        if removed:
            _recompute_ref()
        db().commit()
    return {'removed': removed, 'pinnedMerged': merged}


def list_versions(node_id):
    return [dict(r) for r in db().execute(
        'SELECT * FROM fs_version WHERE node_id=? AND purged=0 ORDER BY id DESC',
        (node_id,)).fetchall()]


# ---------------- 操作日志 ----------------
def fs_log(actor, op, node=None, target='', size=0, ver=0, result=1, reason='',
           ip='', ua='', duration_ms=0):
    node = node or {}
    db().execute('INSERT INTO fs_op_log(ts, actor, op, node_id, node_name, path_snapshot, target, '
                 'size, ver, result, reason, ip, ua, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (now(), actor, op, node.get('id', '') or '', node.get('name', '') or '',
                  path_of(node.get('id', '')) if node.get('id') else '', target,
                  int(size or 0), int(ver or 0), 1 if result else 0, reason or '',
                  ip or '', (ua or '')[:200], int(duration_ms or 0)))


# ---------------- 节点操作（调用方须持有 lock） ----------------
def do_rename(node_id, name, who, ip='', ua=''):
    node = get_alive(node_id)
    if not node:
        raise FsError('节点不存在', 404)
    name = safe_name(name)
    if name == node['name']:
        return node_id
    if name_taken(node['parent_id'], name, exclude=node_id):
        raise FsError('同目录下已存在同名：' + name)
    if node['kind'] == 'file':
        snapshot_version(node_id, 'modify', who, '重命名：%s → %s' % (node['name'], name))
    db().execute('UPDATE fs_node SET name=?, updated_at=?, updated_by=? WHERE id=?',
                 (name, now(), who, node_id))
    fs_log(who, 'rename', node, target=name)
    return node_id


def do_move(node_id, target_id, who, ip='', ua=''):
    node = get_alive(node_id)
    if not node:
        raise FsError('节点不存在', 404)
    target = get_alive(target_id) if target_id else dict(ROOT_NODE)
    if not target or target['kind'] != 'dir':
        raise FsError('目标目录不存在', 404)
    if target_id == node_id or (node['kind'] == 'dir' and target_id in
                                [x['id'] for x in subtree(node_id)]):
        raise FsError('不能移动到自己或其子目录下')
    if depth_of(target_id) + (len(subtree(node_id)) and 1 or 1) > setting_int('max_depth'):
        raise FsError('移动后层级超过上限')
    if name_taken(target_id, node['name'], exclude=node_id):
        raise FsError('目标目录下已存在同名：' + node['name'])
    old_path = path_of(node_id)
    if node['kind'] == 'file':
        snapshot_version(node_id, 'modify', who, '移动：%s → %s' % (old_path, path_of(target_id) + '/' + node['name']))
    db().execute('UPDATE fs_node SET parent_id=?, updated_at=?, updated_by=? WHERE id=?',
                 (target_id, now(), who, node_id))
    fs_log(who, 'move', node, target=path_of(target_id) or '/')
    return node_id


def do_delete(node_id, who, ip='', ua=''):
    node = get_alive(node_id)
    if not node:
        raise FsError('节点不存在', 404)
    nodes = subtree(node_id)
    for n in nodes:
        if n['deleted']:
            continue
    t = now()
    files = [n for n in nodes if n['kind'] == 'file']
    for n in files:                       # 删除前逐个留备份版本
        snapshot_version(n['id'], 'delete', who, '删除前备份')
    ids = [n['id'] for n in nodes]
    db().executemany('UPDATE fs_node SET deleted=1, deleted_at=?, deleted_by=? WHERE id=?',
                     [(t, who, i) for i in ids])
    fs_log(who, 'delete', node, target='%d 个节点（%d 个文件）' % (len(ids), len(files)),
           size=sum(n.get('size') or 0 for n in files))
    return len(ids)


def do_restore(node_id, who, target_id=None, ip='', ua=''):
    node = get_node(node_id)
    if not node or not node['deleted']:
        raise FsError('回收站中无此节点', 404)
    parent = target_id if target_id is not None else node['parent_id']
    if parent and not get_alive(parent):
        parent = ROOT_ID                  # 原父目录已删除 → 恢复到根目录
    if name_taken(parent, node['name'], exclude=node_id):
        base = node['name']
        i = 1
        while name_taken(parent, '%s(%d)' % (base, i), exclude=node_id):
            i += 1
        db().execute('UPDATE fs_node SET name=? WHERE id=?', ('%s(%d)' % (base, i), node_id))
        node['name'] = '%s(%d)' % (base, i)
    db().execute('UPDATE fs_node SET deleted=0, deleted_at=0, deleted_by=\'\', parent_id=?, '
                 'updated_at=?, updated_by=? WHERE id=?', (parent, now(), who, node_id))
    fs_log(who, 'restore', node)
    return node_id


def do_purge(node_id, who):
    """彻底删除（回收站内）：删节点、删当前内容引用、版本标记待清理。"""
    nodes = subtree(node_id)
    ids = [n['id'] for n in nodes]
    fids = [n['id'] for n in nodes if n['kind'] == 'file']
    if fids:
        db().executemany('DELETE FROM fs_file WHERE node_id=?', [(i,) for i in fids])
    db().executemany('DELETE FROM fs_acl WHERE node_id=?', [(i,) for i in ids])
    db().executemany('DELETE FROM fs_node WHERE id=?', [(i,) for i in ids])
    fs_log(who, 'purge', {'id': '', 'name': ''}, target='%d 个节点' % len(ids))


# ---------------- 清理任务 ----------------
def _recompute_ref():
    """引用计数以「fs_file 当前引用 + fs_version 未清理引用」重算为准，避免增量维护出错误删实体。"""
    db().execute("UPDATE fs_blob SET ref_count=("
                 "(SELECT COUNT(*) FROM fs_file WHERE blob_sha=fs_blob.sha256)"
                 "+(SELECT COUNT(*) FROM fs_version WHERE blob_sha=fs_blob.sha256 AND purged=0)"
                 ")")


def gc_once():
    """一轮清理：过期版本 / 过期回收站 / 孤儿 blob / 过期日志 / 临时分片。"""
    stats = {}
    t0 = now()
    with lock():
        # 1) 版本过期
        cur = db().execute('UPDATE fs_version SET purged=1 WHERE purged=0 AND pinned=0 '
                           'AND keep_until < ?', (now(),))
        stats['versions'] = cur.rowcount or 0
        # 单文件版本数上限（保留最新 N 个 + 锁定的）
        mx = setting_int('max_versions')
        if mx > 0:
            for r in db().execute('SELECT node_id FROM fs_version WHERE purged=0 AND pinned=0 '
                                  'GROUP BY node_id HAVING COUNT(*) > ?', (mx,)).fetchall():
                db().execute('UPDATE fs_version SET purged=1 WHERE id IN ('
                             'SELECT id FROM fs_version WHERE node_id=? AND purged=0 AND pinned=0 '
                             'ORDER BY id DESC LIMIT 100000 OFFSET ?)', (r['node_id'], mx))
        # 2) 回收站过期 → 彻底删除
        exp = now() - setting_int('retention_days') * 86400
        old = [dict(r) for r in db().execute(
            'SELECT id FROM fs_node WHERE deleted=1 AND deleted_at>0 AND deleted_at < ?',
            (exp,)).fetchall()]
        for r in old:
            n = get_node(r['id'])
            if n and n['deleted']:
                try:
                    do_purge(r['id'], '_gc')
                except Exception:
                    pass
        stats['purged_nodes'] = len(old)
        # 3) blob GC（引用归零且已无引用）
        _recompute_ref()
        orphans = [dict(r) for r in db().execute(
            'SELECT sha256 FROM fs_blob WHERE ref_count<=0 AND purged=0').fetchall()]
        freed = 0
        for r in orphans:
            p = blob_path(r['sha256'])
            try:
                if p.exists():
                    freed += p.stat().st_size
                    p.unlink()
            except Exception:
                continue
            db().execute('UPDATE fs_blob SET purged=1 WHERE sha256=?', (r['sha256'],))
        stats['blobs'] = len(orphans)
        stats['freed'] = freed
        # 4) 日志过期
        cur = db().execute('DELETE FROM fs_op_log WHERE ts < ?',
                           (now() - setting_int('log_retention_days') * 86400,))
        stats['logs'] = cur.rowcount or 0
        db().commit()
    # 5) 临时分片目录（24h 前）
    try:
        cutoff = now() - 86400
        # 超期未完成的上传会话标记为 expired：否则它们会永远占着「同时 6 个上传任务」的名额
        db().execute("UPDATE fs_upload SET status='expired' WHERE status IN "
                     "('init','uploading') AND ts < ?", (now() - 7 * 86400,))
        db().commit()
        # 仍在传 / 正在合并的会话目录一律不动：合并 2GB 文件时目录 mtime 会长时间停住，
        # 只看 mtime 会把正在合并的目录当垃圾删掉（曾导致 complete 抛 FileNotFoundError）
        active = set()
        for r in db().execute("SELECT id FROM fs_upload WHERE status IN "
                              "('init','uploading','merging')").fetchall():
            active.add(r['id'])
        for d in ctx.tmp.iterdir():
            if not d.is_dir() or d.name in active:
                continue
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(str(d), ignore_errors=True)
            except Exception:
                pass
        for f in ctx.tmp.glob('*.merge'):
            try:
                if f.stem in active:
                    continue
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass
    with lock():
        fs_log('_gc', 'purge', {'id': '', 'name': ''},
               target='版本 %d · 节点 %d · blob %d · 日志 %d，释放 %.1f MB' %
                      (stats.get('versions', 0), stats.get('purged_nodes', 0),
                       stats.get('blobs', 0), stats.get('logs', 0),
                       stats.get('freed', 0) / 1048576.0),
               duration_ms=int((now() - t0) * 1000))
        db().commit()
    return stats


def _gc_loop():
    last_day = ''
    while True:
        time.sleep(600)          # 每 10 分钟巡检
        try:
            day = time.strftime('%Y-%m-%d')
            hour = int(time.strftime('%H'))
            if day != last_day and hour >= setting_int('purge_hour'):
                gc_once()
                last_day = day
        except Exception:
            pass


def run_gc_now():
    """手动触发（管理端「立即清理」按钮）。"""
    return gc_once()


# ---------------- 建表（幂等，老库可直接升级） ----------------
# 注意：PgDB 会把 ? 转成 %s，DDL 内不得出现 ? 占位符
FS_DDL = '''
CREATE TABLE IF NOT EXISTS fs_node(
  id          TEXT PRIMARY KEY,
  parent_id   TEXT NOT NULL DEFAULT '',
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,
  owner       TEXT NOT NULL,
  created_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_by  TEXT DEFAULT '',
  deleted     INTEGER DEFAULT 0,
  deleted_at  DOUBLE PRECISION DEFAULT 0,
  deleted_by  TEXT DEFAULT '',
  size        BIGINT DEFAULT 0,
  note        TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fs_node_name
  ON fs_node(parent_id, lower(name)) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS idx_fs_node_parent ON fs_node(parent_id);
CREATE INDEX IF NOT EXISTS idx_fs_node_del    ON fs_node(deleted, deleted_at);

CREATE TABLE IF NOT EXISTS fs_blob(
  sha256      TEXT PRIMARY KEY,
  size        BIGINT NOT NULL DEFAULT 0,
  ref_count   INTEGER NOT NULL DEFAULT 0,
  created_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
  purged      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fs_file(
  node_id   TEXT PRIMARY KEY,
  blob_sha  TEXT NOT NULL,
  size      BIGINT NOT NULL DEFAULT 0,
  mime      TEXT DEFAULT '',
  ver       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fs_version(
  id           BIGSERIAL PRIMARY KEY,
  node_id      TEXT NOT NULL,
  ver          INTEGER NOT NULL DEFAULT 1,
  blob_sha     TEXT NOT NULL,
  size         BIGINT NOT NULL DEFAULT 0,
  name         TEXT DEFAULT '',
  parent_path  TEXT DEFAULT '',
  change_type  TEXT NOT NULL,
  actor        TEXT NOT NULL,
  ts           DOUBLE PRECISION NOT NULL,
  keep_until   DOUBLE PRECISION NOT NULL,
  pinned       INTEGER DEFAULT 0,
  purged       INTEGER DEFAULT 0,
  note         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fs_ver_node ON fs_version(node_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_fs_ver_gc   ON fs_version(keep_until) WHERE purged = 0;

CREATE TABLE IF NOT EXISTS fs_acl(
  id           BIGSERIAL PRIMARY KEY,
  node_id      TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_key  TEXT NOT NULL,
  allow_mask   INTEGER NOT NULL DEFAULT 0,
  deny_mask    INTEGER NOT NULL DEFAULT 0,
  inherit      INTEGER NOT NULL DEFAULT 1,
  granted_by   TEXT DEFAULT '',
  granted_at   DOUBLE PRECISION DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fs_acl ON fs_acl(node_id, subject_type, subject_key);

CREATE TABLE IF NOT EXISTS fs_upload(
  id          TEXT PRIMARY KEY,
  parent_id   TEXT NOT NULL,
  rel_name    TEXT NOT NULL,
  name        TEXT NOT NULL,
  size        BIGINT NOT NULL DEFAULT 0,
  chunk_size  INTEGER NOT NULL DEFAULT 5242880,
  total       INTEGER NOT NULL DEFAULT 0,
  got         TEXT NOT NULL DEFAULT '',
  sha256      TEXT DEFAULT '',
  node_id     TEXT DEFAULT '',
  conflict    TEXT DEFAULT 'reject',
  actor       TEXT NOT NULL,
  ts          DOUBLE PRECISION NOT NULL,
  status      TEXT NOT NULL DEFAULT 'init'
);
CREATE INDEX IF NOT EXISTS idx_fs_upload_actor ON fs_upload(actor, ts);

-- 分片索引独立成表：并发写不会互相覆盖（原 got 字符串读-改-写会丢分片），
-- 也不会随分片数增长（2GB/16MB=128 片）把一串索引反复读写
CREATE TABLE IF NOT EXISTS fs_upload_part(
  upload_id TEXT NOT NULL,
  idx       INTEGER NOT NULL,
  size      BIGINT DEFAULT 0,
  ts        DOUBLE PRECISION DEFAULT 0,
  PRIMARY KEY(upload_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_fs_part_upload ON fs_upload_part(upload_id);

CREATE TABLE IF NOT EXISTS fs_op_log(
  id            BIGSERIAL PRIMARY KEY,
  ts            DOUBLE PRECISION NOT NULL,
  actor         TEXT NOT NULL,
  op            TEXT NOT NULL,
  node_id       TEXT DEFAULT '',
  node_name     TEXT DEFAULT '',
  path_snapshot TEXT DEFAULT '',
  target        TEXT DEFAULT '',
  size          BIGINT DEFAULT 0,
  ver           INTEGER DEFAULT 0,
  result        INTEGER DEFAULT 1,
  reason        TEXT DEFAULT '',
  ip            TEXT DEFAULT '',
  ua            TEXT DEFAULT '',
  duration_ms   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fs_log_ts   ON fs_op_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_fs_log_node ON fs_op_log(node_id);
CREATE INDEX IF NOT EXISTS idx_fs_log_act  ON fs_op_log(actor, ts DESC);

CREATE TABLE IF NOT EXISTS fs_settings(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_by TEXT DEFAULT ''
);
'''


def init_tables(dbobj):
    dbobj.executescript(FS_DDL)
    with lock():
        for k, v in DEFAULTS.items():
            dbobj.execute('INSERT INTO fs_settings(k, v, updated_at) VALUES(?,?,?) '
                          'ON CONFLICT(k) DO NOTHING', (k, v, time.time()))
        dbobj.commit()
    # 老库升级：补列（PostgreSQL 不支持 ADD COLUMN IF NOT EXISTS，用 DO 块兜底）
    for col, typ in (('deleted_at', 'DOUBLE PRECISION DEFAULT 0'),
                     ('deleted_by', "TEXT DEFAULT ''"),
                     ('size', 'BIGINT DEFAULT 0'),
                     ('note', "TEXT DEFAULT ''")):
        try:
            dbobj.execute('ALTER TABLE fs_node ADD COLUMN ' + col + ' ' + typ)
            dbobj.commit()
        except Exception:
            dbobj.rollback()
    # 老库升级 1：分片 5MB -> 16MB（仅当值还是旧默认，用户手动改过的不动）
    try:
        dbobj.execute('UPDATE fs_settings SET v=? WHERE k=? AND v=?',
                      (DEFAULTS['chunk_size'], 'chunk_size', '5242880'))
        dbobj.commit()
    except Exception:
        dbobj.rollback()
    # 老库升级 2：把 fs_upload.got 里的分片索引回填到 fs_upload_part（正在传的会话不丢进度）
    try:
        rows = dbobj.execute("SELECT id, got FROM fs_upload "
                             "WHERE status IN ('init','uploading') AND got <> ''").fetchall()
        for r in rows:
            for x in str(r['got'] or '').split(','):
                x = x.strip()
                if x.isdigit():
                    dbobj.execute('INSERT INTO fs_upload_part(upload_id, idx, ts) VALUES(?,?,?) '
                                  'ON CONFLICT(upload_id, idx) DO NOTHING',
                                  (r['id'], int(x), time.time()))
        dbobj.commit()
    except Exception:
        dbobj.rollback()
    # 老库升级 3：ONLYOFFICE 签名 URL 密钥不存在/为空时自动生成（重启不变化，老签名不失效）
    try:
        r = dbobj.execute("SELECT v FROM fs_settings WHERE k='office_url_secret'").fetchone()
        if not r or not str(r['v'] or '').strip():
            dbobj.execute("INSERT INTO fs_settings(k, v, updated_at) VALUES('office_url_secret', ?, ?) "
                          "ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v",
                          (uuid.uuid4().hex + uuid.uuid4().hex, time.time()))
            dbobj.commit()
    except Exception:
        dbobj.rollback()
