# -*- coding: utf-8 -*-
"""ONLYOFFICE Document Server 集成：office 文档在线预览 / 编辑。

三方协作模型：

    浏览器 ──加载 api.js + WebSocket──→ ONLYOFFICE DS（独立服务，Docker 部署）
       │ POST /api/fs/office/config（会话 token，拿编辑器初始化 JSON）
       │
       └ DS 拉文件：GET  /api/fs/office/file?sig=…（HMAC 签名，免登录，DS 带不了 Bearer）
         DS 保存：  POST /api/fs/office/callback?sig=…（status 2/6 时回传编辑结果）

关键决策：
  · DS 无法携带我们的 Bearer token，所以「拉文件」与「回调」都走 HMAC-SHA256 签名 URL，
    签名绑定 (动作, node_id, 用户, ver, 过期时间)，过期默认 拉文件 8h / 回调 24h。
    每次请求仍会复核该用户的 VIEW / OVERWRITE 权限 —— 授权被撤，签名 URL 立即失效。
  · 保存完全复用既有版本链路：snapshot_version（旧内容留备份，默认 7 天）→ put_blob →
    ver+1 → add_version('overwrite') → fs_log。在线编辑与覆盖上传在同一套版本历史里。
  · 编辑器 config 在 office_secret 非空时用 HS256 JWT 签名（手写 30 行，不引依赖），
    与 DS 容器的 JWT_ENABLED/JWT_SECRET 对齐；DS 关 JWT 则留空。
  · 文档 key = blob_sha 前 16 位 + ver：内容变了 key 就变，DS 视为新文档，
    天然解决「编辑期间被别人覆盖」的缓存错乱；也用于回调时判断会话是否已过期。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

from . import fs


# 可编辑（保存会产生新版本）：OOXML + csv/txt
EDIT_EXT = {'docx': 'word', 'xlsx': 'cell', 'pptx': 'slide',
            'csv': 'cell', 'txt': 'word'}
# 只读预览：DS 自动转换渲染
VIEW_ONLY_EXT = {'pdf': 'pdf', 'doc': 'word', 'xls': 'cell', 'ppt': 'slide'}
SUPPORT_EXT = set(EDIT_EXT) | set(VIEW_ONLY_EXT)

FILE_TTL = 8 * 3600        # 拉文件签名有效期：够编辑器断线重连
CALLBACK_TTL = 24 * 3600   # 回调签名有效期：够一整个工作日会话


def office_server():
    """DS 基地址（浏览器可达），空 = 功能关闭。"""
    return str(fs.setting_str('office_server') or '').strip().rstrip('/')


def doc_type(ext):
    if ext in EDIT_EXT:
        return EDIT_EXT[ext]
    if ext in VIEW_ONLY_EXT:
        return VIEW_ONLY_EXT[ext] if ext != 'pdf' else 'pdf'   # pdf 无 documentType
    return ''


def display_name(username):
    try:
        r = fs.db().execute('SELECT display_name FROM users WHERE username=?',
                             (username,)).fetchone()
        return (r['display_name'] or username) if r else username
    except Exception:
        return username


# ---------------- 签名 URL（拉文件 / 回调共用） ----------------
def _url_secret():
    s = str(fs.setting_str('office_url_secret') or '').strip()
    return (s or 'taizhang-fs-office').encode('utf-8')


def sign(action, node_id, username, ver, ttl):
    """生成 'action|node|user|ver|exp|sig' 形式的签名令牌。"""
    exp = int(time.time() + ttl)
    payload = '%s|%s|%s|%d|%d' % (action, node_id, username, int(ver), exp)
    sig = hmac.new(_url_secret(), payload.encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    return payload + '|' + sig


def unsign(token, action):
    """校验签名令牌；通过返回 {'node','user','ver'}，否则抛 FsError(403)。

    令牌格式：action|node|user|ver|exp|sig（sig = HMAC-SHA256(前五段) 前 32 位）
    """
    parts = str(token or '').split('|')
    if len(parts) != 6:
        raise fs.FsError('签名无效', 403)
    a, nid, user, ver, exp, sig = parts
    payload = '%s|%s|%s|%s|%s' % (a, nid, user, ver, exp)
    expect = hmac.new(_url_secret(), payload.encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    if a != action:
        raise fs.FsError('签名与目标不匹配', 403)
    if not hmac.compare_digest(expect, str(sig)):
        raise fs.FsError('签名校验失败', 403)
    try:
        exp_i = int(exp)
    except ValueError:
        raise fs.FsError('签名无效', 403)
    if exp_i < time.time():
        raise fs.FsError('签名已过期，请重新打开文档', 403)
    return {'node': nid, 'user': user, 'ver': int(ver or 0)}


# ---------------- 编辑器 config（HS256 JWT 可选） ----------------
def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def jwt_sign(payload):
    """office_secret 非空时给 config 签 JWT（DS 校验配置未被篡改）；空则不签。"""
    secret = str(fs.setting_str('office_secret') or '').strip()
    if not secret:
        return ''
    header = _b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode('utf-8'))
    body = _b64(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing = ('%s.%s' % (header, body)).encode('ascii')
    sig = _b64(hmac.new(secret.encode('utf-8'), signing, hashlib.sha256).digest())
    return '%s.%s.%s' % (header, body, sig)


def doc_key(f):
    """同一内容 + 同一版本 → 同一 key；任一变化 → 新文档。"""
    return '%s-%d' % (str(f['blob_sha'])[:16], int(f['ver'] or 0))


def build_config(node, f, username, can_edit, base_url):
    from urllib.parse import quote
    ext = fs.ext_of(node['name'])
    dtype = doc_type(ext)
    cfg = {
        'width': '100%',
        'height': '100%',
        'documentType': dtype,
        'document': {
            'fileType': ext,
            'title': node['name'],
            'key': doc_key(f),
            'url': '%s/api/fs/office/file?sig=%s' % (base_url, quote(sign(
                'view', node['id'], username, f['ver'], FILE_TTL))),
            'permissions': {'edit': bool(can_edit), 'download': True, 'print': True},
        },
        'editorConfig': {
            'mode': 'edit' if can_edit else 'view',
            'lang': 'zh-CN',
            'callbackUrl': '%s/api/fs/office/callback?sig=%s' % (base_url, quote(sign(
                'cb', node['id'], username, f['ver'], CALLBACK_TTL))),
            'user': {'id': username, 'name': display_name(username)},
            'customization': {
                'forcesave': True,        # 工具栏出现保存按钮，status=6 回调，不用关页面才落盘
                'autosave': True,
                'compactHeader': True,
                'hideRightMenu': True,
                'about': False,
                'feedback': False,
                'help': False,
            },
        },
    }
    token = jwt_sign(cfg)
    if token:
        cfg['token'] = token
    return cfg


# ---------------- 回调处理（status 2 = 关闭时保存 / 6 = 手动保存） ----------------
def _download(url, dst, timeout=120):
    """从 DS 下载编辑后的文档（DS 给的是它自己的临时缓存 URL）。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'taizhang-fs-office/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(str(dst), 'wb') as out:
        while True:
            b = resp.read(1024 * 1024)
            if not b:
                break
            out.write(b)


def _unlink_quiet(p):
    try:
        os.unlink(str(p))
    except Exception:
        pass


def _own_chain(node_id, user, session_ver):
    """key 不匹配时的放行判定：自会话打开以来，该文件只被「同一用户的在线编辑保存」更新过。

    典型场景：forcesave 保存成功后 ver+1，但编辑器会话还拿着旧 key 继续编辑，
    下一次保存回调的 key 必然「过期」——这不是真过期，必须允许保存。
    判据（够用且保守）：最新一条版本 actor==当前用户 且 备注为 OnlyOffice 保存。
    若期间任何其他人（或该用户走普通上传/回滚）动过文件，最新版本就不是
    OnlyOffice 保存 → 拒绝，避免旧会话覆盖掉别人的成果。
    """
    r = fs.db().execute("SELECT actor, note FROM fs_version WHERE node_id=? AND purged=0 "
                        "ORDER BY id DESC LIMIT 1", (node_id,)).fetchone()
    if not r:
        return False
    return (str(r['actor']) == str(user) and 'OnlyOffice' in str(r['note'] or ''))


def handle_callback(node, user, data, session_ver=0):
    """保存编辑结果为新版本。返回 {'error': 0}（DS 要求 0 表示已正确处理）。

    幂等与过期保护：
      · 内容 sha 与当前一致 → 什么都不做（DS 关闭时可能重复回调）；
      · 回调 key 与当前 key 不一致 → 会话基于旧版本打开。若期间只是本会话自己
        forcesave 过（_own_chain 判定）则照常保存；否则拒绝并写失败日志。
    """
    status = int(data.get('status') or 0)
    url = str(data.get('url') or '')
    if status not in (2, 6) or not url:
        return {'error': 0}

    f = fs.file_row(node['id'])
    if not f:
        _unlink_quiet(fs.ctx.tmp / ('office-' + node['id']))
        return {'error': 0}
    if data.get('key') and data['key'] != doc_key(f) and not _own_chain(node['id'], user, session_ver):
        fs.fs_log(user, 'overwrite', node, result=0, ver=f['ver'],
                  reason='在线编辑会话已过期（文档已被更新，本次修改未保存）', ua='onlyoffice')
        fs.db().commit()
        return {'error': 0}

    tmp = fs.ctx.tmp / ('office-' + fs.new_id())
    _download(url, tmp)
    sha = fs.hash_file(str(tmp))
    size = os.path.getsize(str(tmp))
    if sha == f['blob_sha']:
        _unlink_quiet(tmp)                     # 内容没变
        return {'error': 0}

    t0 = time.time()
    with fs.lock():
        f = fs.file_row(node['id'])            # 锁内重读（拿到锁时文件可能已被删/被改）
        if not f:
            _unlink_quiet(tmp)
            return {'error': 0}
        fs.snapshot_version(node['id'], 'overwrite', user, '在线编辑保存前备份 v%d' % f['ver'])
        sha = fs.put_blob(str(tmp), sha)       # 内容寻址，可能命中去重
        ver = int(f['ver']) + 1
        fs.db().execute('UPDATE fs_file SET blob_sha=?, size=?, ver=? WHERE node_id=?',
                        (sha, size, ver, node['id']))
        fs.db().execute('UPDATE fs_node SET size=?, updated_at=?, updated_by=? WHERE id=?',
                        (size, time.time(), user, node['id']))
        fs.add_version(node['id'], ver, sha, size, 'overwrite', user, node['name'],
                       fs.path_of(node['id']), 'OnlyOffice 在线编辑保存')
        fs.fs_log(user, 'overwrite', node, size=size, ver=ver, target='OnlyOffice 在线编辑',
                  ua='onlyoffice-callback', duration_ms=int((time.time() - t0) * 1000))
        fs.db().commit()
    _unlink_quiet(tmp)
    return {'error': 0}
