# -*- coding: utf-8 -*-
"""文件存储模块 HTTP 路由（/api/fs/*）

上传分片用裸流 body（Request.stream()），不依赖 python-multipart，且是真流式落盘：
内存占用 ≈ 并发 × 分片大小，与文件大小无关。
"""
import csv
import functools
import hashlib
import inspect
import io
import os
import shutil
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import fs
from . import fs_office


def _op_action(op):
    return fs._bit_key(op)


READ_BUF = 1024 * 1024        # 事件循环里累积到 1MB 再交给线程写盘，减少线程切换


def _rm_tmp(p):
    """清临时目录：失败只记日志。清理失败绝不能让「已成功落库」的上传返回 500
    （Windows 上文件句柄未释放、杀毒占用都会命中）。"""
    try:
        shutil.rmtree(str(p))
    except (Exception, SystemExit) as e:
        print('[fs] 临时目录清理失败 %s: %s' % (p, e))


# ---------------- 上传分片用到的同步小工具（全部经线程池调用，不阻塞事件循环） ----------------
def _load_upload(upload_id, username):
    r = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (upload_id,)).fetchone()
    if not r:
        raise fs.FsError('上传会话不存在或已过期', 404)
    s = dict(r)
    if s['actor'] != username and not fs.is_admin(username):
        raise fs.FsError('无权操作该上传会话', 403)
    return s


def _append_file(path, data):
    with open(str(path), 'ab') as f:
        f.write(data)


def _trunc(path):
    with open(str(path), 'wb'):
        pass


def _unlink_quiet(p):
    try:
        os.unlink(str(p))
    except Exception:
        pass


def _mark_part(upload_id, idx, size):
    # 不拿全局锁：PG 自己能处理并发，而一旦这里等锁会连带把整站串行化
    fs.add_part(upload_id, idx, size)
    fs.db().execute("UPDATE fs_upload SET status='uploading' WHERE id=?", (upload_id,))
    fs.db().commit()
    return fs.part_count(upload_id)


def _part_ok(tdir, idx):
    """磁盘上的分片是否真的可用（存在且非空）。
    分片索引表只记「服务端收过某片」，GC、取消、杀毒占用、手工清理都可能让磁盘先丢文件，
    合并前必须逐片核对磁盘，不能只信 DB。"""
    p = tdir / ('%d.part' % idx)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _drop_parts(upload_id, idxs):
    """把磁盘上已丢失的分片从索引表剔除，让客户端按 409 补齐后重新合并。"""
    for i in idxs:
        fs.db().execute('DELETE FROM fs_upload_part WHERE upload_id=? AND idx=?', (upload_id, i))
    fs.db().commit()


def _revert_upload(upload_id):
    """合并失败时把会话退回 uploading：分片大多还在，客户端补传/重试即可，
    绝不能让会话永久卡在 merging 上再也完不成。"""
    try:
        with fs.lock():
            fs.db().execute("UPDATE fs_upload SET status='uploading' "
                            "WHERE id=? AND status='merging'", (upload_id,))
            fs.db().commit()
    except Exception as e:
        print('[fs] 上传状态回滚失败 %s: %s' % (upload_id, e))


def _wrap(fn):
    """把 FsError 转成 HTTPException（同步 / 异步端点都支持）。"""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*a, **kw):
            try:
                return await fn(*a, **kw)
            except fs.FsError as e:
                raise HTTPException(e.code, e.msg)
        return aw

    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except fs.FsError as e:
            raise HTTPException(e.code, e.msg)
    return w


def disposition(name, inline=False):
    """RFC 5987：中文文件名在 Windows / Linux 浏览器都要正常。"""
    ascii_name = ''.join(c if 32 < ord(c) < 127 and c not in '\\"' else '_' for c in str(name))
    kind = 'inline' if inline else 'attachment'
    return '%s; filename="%s"; filename*=UTF-8\'\'%s' % (kind, ascii_name, quote(str(name)))


def node_out(n, username=None, with_perm=False, parent_mask=None):
    f = fs.file_row(n['id']) if n['kind'] == 'file' else None
    o = {'id': n['id'], 'name': n['name'], 'kind': n['kind'], 'owner': n['owner'],
         'size': int(n['size'] or 0), 'createdAt': n['created_at'], 'updatedAt': n['updated_at'],
         'updatedBy': n['updated_by'] or '', 'parentId': n['parent_id'],
         'deleted': int(n['deleted'] or 0), 'deletedAt': n['deleted_at'] or 0,
         'deletedBy': n['deleted_by'] or '', 'note': n['note'] or '',
         'ext': fs.ext_of(n['name']), 'ver': f['ver'] if f else 0,
         'mime': (f['mime'] if f else '') or ''}
    if with_perm and username:
        o['perm'] = fs.eff_perm(username, n) if parent_mask is None \
            else _decode(fs.child_mask(username, n, parent_mask))
    return o


def _decode(mask):
    out = {k: bool(mask & fs.OP_BIT[k]) for k in fs.OP_KEYS}
    out['grant'] = bool(mask & fs.OP.GRANT)
    return out


def build_router(current_user):
    router = APIRouter(prefix='/api/fs', tags=['fs'])

    def meta(request):
        if not request:
            return '', ''
        return (request.headers.get('x-real-ip') or (request.client.host if request.client else '') or '',
                request.headers.get('user-agent', ''))

    def alive(nid):
        n = fs.get_node(nid) if not nid else fs.get_alive(nid)
        if not n:
            raise fs.FsError('节点不存在', 404)
        return n

    def check(username, node, op, request=None):
        ip, ua = meta(request)
        try:
            fs.require_op(username, node, op)
        except fs.FsError as e:
            with fs.lock():
                fs.fs_log(username, _op_action(op), node, result=0, reason=e.msg, ip=ip, ua=ua)
                fs.db().commit()
            raise

    # ---------------- 目录浏览 ----------------
    @router.get('/perm')
    @_wrap
    def get_perm(node_id: str = '', username: str = Depends(current_user)):
        node = fs.get_node(node_id)
        if not node:
            raise fs.FsError('节点不存在', 404)
        return {'nodeId': node_id, 'name': node['name'], 'kind': node['kind'],
                'path': fs.path_of(node_id),
                'perm': fs.eff_perm(username, node),
                'canGrant': fs.can_grant(username, node),
                'isAdmin': fs.is_admin(username)}

    @router.get('/list')
    @_wrap
    def list_dir(parent_id: str = '', kw: str = '', limit: int = 300, offset: int = 0,
                 username: str = Depends(current_user)):
        parent = fs.get_node(parent_id)
        if not parent or parent['deleted']:
            raise fs.FsError('目录不存在', 404)
        check(username, parent, fs.OP.VIEW)
        rows = fs.children(parent_id, kw, limit=max(1, min(1000, limit)), offset=max(0, offset))
        pmask = fs.eff_mask(username, parent)
        out = []
        for n in rows:
            m = fs.child_mask(username, n, pmask)
            if not (m & fs.OP.VIEW):
                continue                       # 显式 deny 掉的子节点不展示
            out.append(node_out(n, username, True, pmask))
        return {'parentId': parent_id, 'path': fs.path_of(parent_id), 'name': parent['name'],
                'perm': _decode(pmask), 'items': out,
                'trail': [{'id': x['id'], 'name': x['name']} for x in fs.chain_of(parent_id)]}

    @router.get('/tree')
    @_wrap
    def tree(parent_id: str = '', username: str = Depends(current_user)):
        parent = fs.get_node(parent_id)
        if not parent or parent['deleted']:
            return {'items': []}
        if not (fs.eff_mask(username, parent) & fs.OP.VIEW):
            return {'items': []}
        rows = fs.children(parent_id, '', limit=500)
        pmask = fs.eff_mask(username, parent)
        items = []
        for n in rows:
            if n['kind'] != 'dir':
                continue
            if not (fs.child_mask(username, n, pmask) & fs.OP.VIEW):
                continue
            has = bool(fs.db().execute(
                "SELECT 1 FROM fs_node WHERE parent_id=? AND kind='dir' AND deleted=0 LIMIT 1",
                (n['id'],)).fetchone())
            items.append({'id': n['id'], 'name': n['name'], 'hasChild': has})
        return {'items': items}

    # ---------------- 目录操作 ----------------
    class MkdirIn(BaseModel):
        parentId: str = ''
        name: str = ''

    class BatchIn(BaseModel):
        parentId: str = ''
        paths: list = []

    class RenameIn(BaseModel):
        nodeId: str = ''
        name: str = ''

    class MoveIn(BaseModel):
        nodeId: str = ''
        targetId: str = ''

    @router.post('/mkdir')
    @_wrap
    def mkdir(body: MkdirIn, username: str = Depends(current_user)):
        parent = alive(body.parentId)
        check(username, parent, fs.OP.UPLOAD)
        with fs.lock():
            nid = fs.mkdir(body.parentId, body.name, username)
            node = fs.get_node(nid)
            fs.fs_log(username, 'mkdir', node)
            fs.db().commit()
        return {'id': nid, 'node': node_out(node, username, True)}

    @router.post('/mkdir/batch')
    @_wrap
    def mkdir_batch(body: BatchIn, username: str = Depends(current_user)):
        parent = alive(body.parentId)
        check(username, parent, fs.OP.UPLOAD)
        with fs.lock():
            mp = fs.mkdir_batch(body.parentId, [str(p) for p in body.paths], username)
            ids = sorted(set(mp.values()))
            for i in ids:
                fs.fs_log(username, 'mkdir', fs.get_node(i))
            fs.db().commit()
        return {'map': mp}

    @router.post('/rename')
    @_wrap
    def rename(body: RenameIn, request: Request, username: str = Depends(current_user)):
        node = alive(body.nodeId)
        check(username, node, fs.OP.MODIFY, request)
        ip, ua = meta(request)
        with fs.lock():
            fs.do_rename(body.nodeId, body.name, username, ip, ua)
            fs.db().commit()
        return {'ok': True, 'node': node_out(alive(body.nodeId), username, True)}

    @router.post('/move')
    @_wrap
    def move(body: MoveIn, request: Request, username: str = Depends(current_user)):
        node = alive(body.nodeId)
        target = fs.get_node(body.targetId)
        if not target or target['deleted'] or target['kind'] != 'dir':
            raise fs.FsError('目标目录不存在', 404)
        check(username, node, fs.OP.MODIFY, request)
        check(username, target, fs.OP.UPLOAD, request)
        ip, ua = meta(request)
        with fs.lock():
            fs.do_move(body.nodeId, body.targetId, username, ip, ua)
            fs.db().commit()
        return {'ok': True}

    @router.delete('/node/{node_id}')
    @_wrap
    def delete_node(node_id: str, request: Request, username: str = Depends(current_user)):
        node = alive(node_id)
        for n in fs.subtree(node_id):
            if n['deleted']:
                continue
            check(username, n, fs.OP.DELETE, request)      # 子树逐节点校验，任一无权则整棵拒绝
        ip, ua = meta(request)
        with fs.lock():
            cnt = fs.do_delete(node_id, username, ip, ua)
            fs.db().commit()
        return {'ok': True, 'count': cnt}

    # ---------------- 下载 / 预览 ----------------
    def _open_file(node_id, username, op, request):
        node = alive(node_id)
        check(username, node, op, request)
        f = fs.file_row(node_id)
        if not f:
            raise fs.FsError('不是文件或内容缺失', 404)
        p = fs.blob_path(f['blob_sha'])
        if not p.exists():
            raise fs.FsError('文件实体已丢失，请联系管理员', 404)
        return node, f, p

    @router.get('/download')
    @_wrap
    def download(node_id: str, request: Request, username: str = Depends(current_user)):
        t0 = time.time()
        node, f, p = _open_file(node_id, username, fs.OP.VIEW, request)
        ip, ua = meta(request)
        with fs.lock():
            fs.fs_log(username, 'download', node, size=f['size'], ver=f['ver'], ip=ip, ua=ua,
                      duration_ms=int((time.time() - t0) * 1000))
            fs.db().commit()

        def it():
            with open(str(p), 'rb') as fh:
                while True:
                    b = fh.read(65536)
                    if not b:
                        break
                    yield b
        return StreamingResponse(it(), media_type=f['mime'] or 'application/octet-stream',
                                 headers={'Content-Disposition': disposition(node['name']),
                                          'Content-Length': str(f['size']),
                                          'X-Content-Type-Options': 'nosniff'})

    @router.get('/preview')
    @_wrap
    def preview(node_id: str, request: Request, username: str = Depends(current_user)):
        node, f, p = _open_file(node_id, username, fs.OP.VIEW, request)
        ext = fs.ext_of(node['name'])
        if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'pdf', 'txt'):
            raise fs.FsError('该文件类型不支持在线预览，请下载查看')

        def it():
            with open(str(p), 'rb') as fh:
                while True:
                    b = fh.read(65536)
                    if not b:
                        break
                    yield b
        return StreamingResponse(it(), media_type=f['mime'] or 'application/octet-stream',
                                 headers={'Content-Disposition': disposition(node['name'], True),
                                          'X-Content-Type-Options': 'nosniff'})

    # ---------------- ONLYOFFICE 在线预览 / 编辑 ----------------
    def _base_url(request):
        """构造「Document Server 可达」的后端基地址。

        默认用请求自身的 scheme+host（浏览器怎么访问，DS 就怎么回源）；
        DS 跑在 Docker 里解析不了内网域名时，用环境变量 OFFICE_BASE_URL 显式指定
        （如 http://host.docker.internal:8000，容器经 host-gateway 回到宿主机）。

        容错：OFFICE_BASE_URL 只写了主机没写端口（如 http://host.docker.internal）时，
        自动补上「浏览器访问后端的端口」。否则 DS 会去连容器自己的 80 端口，
        表现为编辑器一直转圈或报「无法下载文件」。
        """
        env = (fs.setting_str('office_base_url') or '').strip().rstrip('/')
        if env:
            if '://' in env:
                proto, rest = env.split('://', 1)
                if '/' not in rest and ':' not in rest:          # 只有主机名，无端口
                    port = request.url.port
                    if port and port not in (80, 443):
                        env = '%s://%s:%d' % (proto, rest, port)
            return env
        proto = request.headers.get('x-forwarded-proto') or request.url.scheme
        host = (request.headers.get('x-forwarded-host') or request.headers.get('host')
                or request.url.netloc)
        return '%s://%s' % (proto, host)

    @router.get('/office/enabled')
    @_wrap
    def office_enabled(username: str = Depends(current_user)):
        return {'enabled': bool(fs_office.office_server()),
                'server': fs_office.office_server()}

    @router.get('/office/diag')
    @_wrap
    def office_diag(request: Request, node_id: str = '',
                    username: str = Depends(current_user)):
        """一键自检：定位「编辑器异常」到底卡在哪一环。

        返回各环节的真值与结论，前端直接摊开给用户看，不用再翻日志猜。
        """
        import time as _t
        import urllib.request as _u
        from urllib.parse import quote

        server = fs_office.office_server()
        base = _base_url(request)
        secret = str(fs.setting_str('office_secret') or '').strip()
        out = {
            'server': server, 'baseUrl': base,
            'secretSet': bool(secret), 'secretLen': len(secret),
            'serverOk': False, 'serverDetail': '', 'dsVersion': '',
            'reverseOk': False, 'reverseDetail': '', 'hints': [],
        }

        # ① 浏览器/后端能否连上 DS（DS 没起来或地址写错，这里就红）
        if server:
            try:
                t0 = _t.time()
                with _u.urlopen(server + '/healthcheck', timeout=6) as r:
                    body = r.read(32).decode('utf-8', 'replace').strip()
                out['serverOk'] = (r.status == 200 and body.lower().startswith('true'))
                out['serverDetail'] = 'HTTP %d · %s · %.0f ms' % (
                    r.status, body[:12], (_t.time() - t0) * 1000)
            except Exception as e:
                out['serverDetail'] = '连接失败：%s' % e
            try:
                with _u.urlopen(server + '/hosting/discovery', timeout=6) as r:
                    txt = r.read(8192).decode('utf-8', 'replace')
                import re as _re
                m = _re.search(r'<title>([^<]*)</title>|version="([\d.]+)"', txt)
                if m:
                    out['dsVersion'] = (m.group(1) or m.group(2) or '')[:40]
            except Exception:
                pass

        # ② DS 能不能回源取到我们的文件（核心：baseUrl 必须是「容器视角」可达的）
        #    这里由后端代发一次同一签名的请求：能通说明地址/路由正确；
        #    不通则 DS 100% 也拿不到（反之不绝对，见 hints）。
        if node_id and server:
            try:
                node = fs.get_alive(node_id)
                f = fs.file_row(node_id) if node else None
                if f:
                    sig = quote(fs_office.sign('view', node_id, username, f['ver'], 600))
                    url = '%s/api/fs/office/file?sig=%s' % (base, sig)
                    t0 = _t.time()
                    req = _u.Request(url, headers={'User-Agent': 'taizhang-office-diag'})
                    with _u.urlopen(req, timeout=10) as r:
                        n = 0
                        while n < 65536:
                            b = r.read(4096)
                            if not b:
                                break
                            n += len(b)
                    out['reverseOk'] = (r.status == 200 and n > 0)
                    out['reverseDetail'] = 'HTTP %d · 读到 %d KB · %.0f ms' % (
                        r.status, max(1, n // 1024), (_t.time() - t0) * 1000)
                else:
                    out['reverseDetail'] = '文件不存在，跳过'
            except Exception as e:
                # 宿主机（尤其 Linux）往往解析不了 host.docker.internal，
                # 这里失败不代表 DS 拉不到，标注清楚避免误导。
                if 'host.docker.internal' in base:
                    out['reverseDetail'] = '宿主机自测失败（%s）—— DS 容器内通常仍可通，以编辑器实际表现为准' % e
                else:
                    out['reverseDetail'] = '回源失败：%s' % e

        # ③ 结论与建议（按命中率排序）
        h = out['hints']
        if not server:
            h.append('还没配置 ONLYOFFICE 服务地址：打开「文件存储 → 设置 → ONLYOFFICE 服务地址」，'
                     '填 http://127.0.0.1:8888（本机）或 http://内网IP:8888（局域网）。')
        elif not out['serverOk']:
            h.append('连不上 Document Server：确认容器在跑（docker ps 里 taizhang-onlyoffice 是 Up），'
                     '端口映射是 8888→80，防火墙放行 8888。')
        if base and ('127.0.0.1' in base or 'localhost' in base):
            h.append('回源地址是 %s —— 这是**浏览器**的 127.0.0.1，DS 在容器里访问到的是容器自己，'
                     '必然拉不到文件。请设置环境变量 OFFICE_BASE_URL=http://host.docker.internal:8000 '
                     '（Docker Desktop）或 http://宿主机内网IP:8000（Linux 需 --add-host）。' % base)
        if base and base.startswith('https://') and server.startswith('http://'):
            h.append('后端是 https、DS 是 http：浏览器会拦截混合内容，编辑器白屏。两者需同为 http 或同为 https。')
        if not secret:
            h.append('未设置 JWT 密钥。DS 默认开启 JWT（JWT_ENABLED=true），此时必须在'
                     '「设置 → ONLYOFFICE JWT 密钥」里填与容器 JWT_SECRET 完全相同的值，否则编辑器拒绝加载。')
        elif out['secretLen'] < 8:
            h.append('JWT 密钥看起来过短（%d 位），确认与容器 JWT_SECRET 逐字符一致。' % out['secretLen'])
        if out['reverseOk'] is False and out['reverseDetail'].startswith('回源失败'):
            h.append('后端自己都无法通过 %s 取到文件 → DS 更不可能取到。优先修 OFFICE_BASE_URL。' % base)
        if out['secretSet'] and out['serverOk'] and out['reverseOk']:
            h.append('链路各环节均通。若仍报错，多半是 JWT 密钥不一致（后端与容器 JWT_SECRET）或文件本身损坏：'
                     '先在浏览器直接访问 /api/fs/office/file 的签名地址确认内容能下载。')
        return out

    @router.get('/office/config')
    @_wrap
    def office_config(node_id: str, request: Request, username: str = Depends(current_user)):
        node = alive(node_id)
        check(username, node, fs.OP.VIEW, request)
        server = fs_office.office_server()
        if not server:
            raise fs.FsError('未配置 ONLYOFFICE 服务地址（office_server），在线预览/编辑不可用', 503)
        if node['kind'] != 'file':
            raise fs.FsError('只能预览 / 编辑文件')
        ext = fs.ext_of(node['name'])
        if ext not in fs_office.SUPPORT_EXT:
            raise fs.FsError('该类型不支持在线预览编辑：.' + ext)
        f = fs.file_row(node_id)
        if not f or not fs.blob_path(f['blob_sha']).exists():
            raise fs.FsError('文件内容缺失', 404)
        can_edit = (ext in fs_office.EDIT_EXT
                    and bool(fs.eff_mask(username, node) & fs.OP.OVERWRITE))
        cfg = fs_office.build_config(node, f, username, can_edit, _base_url(request))
        return {'server': server, 'mode': 'edit' if can_edit else 'view', 'config': cfg}

    @router.get('/office/file')
    @_wrap
    def office_file(sig: str = ''):
        """DS 拉取文档内容：签名 URL 免登录，但仍复核该用户的 VIEW 权限。"""
        info = fs_office.unsign(sig, 'view')
        node = fs.get_alive(info['node'])
        f = fs.file_row(info['node'])
        if not node or not f:
            raise fs.FsError('文件不存在', 404)
        if not (fs.eff_mask(info['user'], node) & fs.OP.VIEW):
            raise fs.FsError('该用户已无查看权限', 403)
        p = fs.blob_path(f['blob_sha'])
        if not p.exists():
            raise fs.FsError('文件实体已丢失', 404)

        def it():
            with open(str(p), 'rb') as fh:
                while True:
                    b = fh.read(65536)
                    if not b:
                        break
                    yield b
        return StreamingResponse(it(), media_type=f['mime'] or 'application/octet-stream',
                                 headers={'Content-Disposition': disposition(node['name'], True),
                                          'X-Content-Type-Options': 'nosniff'})

    @router.post('/office/callback')
    async def office_callback(sig: str = '', request: Request = None):
        """DS 保存回调（status 2/6 带 url）。必须在锁外线程池里处理（下载 + 落盘）。
        无 Bearer token：靠签名校验 + 回调时复核 OVERWRITE 权限。"""
        try:
            info = fs_office.unsign(sig, 'cb')
            node = fs.get_alive(info['node'])
            if not node:
                return {'error': 0}                      # 文档已删除：确认收到即可
            if not (fs.eff_mask(info['user'], node) & fs.OP.OVERWRITE):
                fs.fs_log(info['user'], 'overwrite', node, result=0,
                          reason='该用户已无覆盖权限，在线编辑结果未保存', ua='onlyoffice')
                fs.db().commit()
                return {'error': 0}
            data = await request.json()
            return await run_in_threadpool(fs_office.handle_callback, node, info['user'],
                                           data, info['ver'])
        except fs.FsError as e:
            raise HTTPException(e.code, e.msg)
        except Exception as e:
            print('[office] 回调处理失败 %s: %s' % (sig[:80] if sig else '', e))
            return {'error': 1}                          # DS 会重试，重试仍失败则提示用户

    # ---------------- 上传（三段式） ----------------
    class InitIn(BaseModel):
        parentId: str = ''
        name: str = ''
        size: int = 0
        sha256: str = ''
        conflict: str = 'reject'      # reject | overwrite | rename

    class CompleteIn(BaseModel):
        uploadId: str = ''

    class AbortIn(BaseModel):
        uploadId: str = ''

    @router.post('/upload/init')
    @_wrap
    def upload_init(body: InitIn, request: Request, username: str = Depends(current_user)):
        parent = alive(body.parentId)
        name = fs.safe_name(body.name)
        size = int(body.size or 0)
        if size <= 0:
            raise fs.FsError('空文件不可上传')
        if size > fs.setting_int('max_file_size'):
            raise fs.FsError('单个文件不能超过 %.0f MB' % (fs.setting_int('max_file_size') / 1048576.0))
        fs.check_ext(name)
        fs.check_quota(size)
        ip, ua = meta(request)
        t0 = time.time()

        exist = fs.db().execute("SELECT * FROM fs_node WHERE parent_id=? AND lower(name)=lower(?) "
                                "AND kind='file' AND deleted=0",
                                (body.parentId, name)).fetchone()
        exist = dict(exist) if exist else None
        node_id = ''
        if exist:
            conflict = body.conflict or 'reject'
            if conflict == 'reject':
                raise fs.FsError('已存在同名文件：' + name, 409)
            if conflict == 'overwrite':
                check(username, exist, fs.OP.OVERWRITE, request)
                node_id = exist['id']
            else:
                base, ext = (name.rsplit('.', 1) + [''])[:2] if '.' in name else (name, '')
                i = 1
                while fs.name_taken(body.parentId, '%s(%d)%s' % (base, i, ('.' + ext) if ext else '')):
                    i += 1
                name = '%s(%d)%s' % (base, i, ('.' + ext) if ext else '')
                check(username, parent, fs.OP.UPLOAD, request)
        else:
            check(username, parent, fs.OP.UPLOAD, request)

        chunk = fs.setting_int('chunk_size')
        total = (size + chunk - 1) // chunk

        # 秒传：内容已存在则直接建节点，不传数据
        if fs.setting_int('enable_dedup') and body.sha256:
            hit = fs.db().execute('SELECT size FROM fs_blob WHERE sha256=?',
                                  (body.sha256,)).fetchone()
            if hit:
                with fs.lock():
                    nid, ver = _create_or_replace(body.parentId, name, body.sha256,
                                                  int(hit['size'] or size), username, node_id,
                                                  _mime_of(name), ip, ua)
                    # 秒传走的是纯元数据建节点，没有 complete 阶段，日志必须在这里写
                    fs.fs_log(username, 'overwrite' if node_id else 'upload', fs.get_node(nid),
                              size=int(hit['size'] or size), ver=ver, ip=ip, ua=ua,
                              target='秒传（内容已存在，未重复传输）')
                    fs.db().commit()
                return {'instant': True, 'uploadId': '', 'chunkSize': chunk, 'total': 0,
                        'got': [], 'nodeId': nid, 'ver': ver, 'name': name}

        uid = fs.new_id()
        # 同一用户并行上传会话上限：防止一人开多标签页把磁盘/连接打满（24h 内的活跃会话）
        busy = fs.db().execute("SELECT COUNT(*) c FROM fs_upload WHERE actor=? "
                               "AND status IN ('init','uploading') AND ts > ?",
                               (username, time.time() - 86400)).fetchone()
        if int(busy['c'] if busy else 0) >= 6:
            raise fs.FsError('你同时有 6 个上传任务在进行，请等它们完成或取消后再试', 429)
        tdir = fs.ctx.tmp / uid
        tdir.mkdir(parents=True, exist_ok=True)
        with fs.lock():
            fs.db().execute('INSERT INTO fs_upload(id, parent_id, rel_name, name, size, chunk_size, '
                            'total, got, sha256, node_id, conflict, actor, ts, status) '
                            'VALUES(?,?,?,?,?,?,?,\'\',?,?,?,?,?,\'init\')',
                            (uid, body.parentId, name, name, size, chunk, total, body.sha256 or '',
                             node_id, body.conflict or 'reject', username, time.time()))
            fs.db().commit()
        return {'instant': False, 'uploadId': uid, 'chunkSize': chunk, 'total': total,
                'got': [], 'nodeId': node_id, 'ver': 0, 'name': name,
                'elapsedMs': int((time.time() - t0) * 1000)}

    @router.post('/upload/chunk')
    @_wrap
    async def upload_chunk(upload_id: str, index: int, request: Request,
                           username: str = Depends(current_user)):
        # 所有同步 IO（DB / 文件写 / os.replace）都丢进线程池：
        # 事件循环只负责读流，否则一个 16MB 分片的写盘会把整个服务卡住几百毫秒
        s = await run_in_threadpool(_load_upload, upload_id, username)
        if s['status'] == 'done':
            return {'received': int(s['total']), 'total': int(s['total']), 'done': True}
        if s['status'] in ('aborted', 'merging'):
            # 会话已取消 / 正在合并：拒绝写盘。否则迟到的分片请求会重新建出临时目录，
            # 既留垃圾文件，又让「DB 说收齐了但磁盘没有」这种不一致再次出现
            raise fs.FsError('上传已取消或正在合并，该分片已被拒绝', 409)
        idx = int(index)
        if idx < 0 or idx >= int(s['total']):
            raise fs.FsError('分片下标越界')
        tdir = fs.ctx.tmp / upload_id
        part = tdir / ('%d.part' % idx)
        tmpf = tdir / ('%d.part.w' % idx)
        await run_in_threadpool(lambda: tdir.mkdir(parents=True, exist_ok=True))
        await run_in_threadpool(_trunc, tmpf)
        limit = int(s['chunk_size']) + 1024
        wrote = 0
        buf = bytearray()
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                wrote += len(chunk)
                if wrote > limit:
                    raise fs.FsError('分片大小超过限制')
                buf += chunk
                if len(buf) >= READ_BUF:
                    data = bytes(buf)
                    buf.clear()
                    await run_in_threadpool(_append_file, tmpf, data)
            if buf:
                data = bytes(buf)
                buf.clear()
                await run_in_threadpool(_append_file, tmpf, data)
            await run_in_threadpool(os.replace, str(tmpf), str(part))
        except BaseException:
            await run_in_threadpool(_unlink_quiet, tmpf)
            raise
        recv = await run_in_threadpool(_mark_part, upload_id, idx, wrote)
        return {'received': recv, 'total': int(s['total']), 'index': idx}

    @router.get('/upload/status')
    @_wrap
    def upload_status(upload_id: str, username: str = Depends(current_user)):
        s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (upload_id,)).fetchone()
        if not s:
            raise fs.FsError('上传会话不存在', 404)
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        got = fs.parts_of(upload_id)
        missing = [i for i in range(int(s['total'])) if i not in set(got)][:2000]
        return {'uploadId': upload_id, 'total': int(s['total']), 'got': got, 'missing': missing,
                'status': s['status'], 'size': int(s['size']), 'name': s['name'],
                'parentId': s['parent_id'], 'nodeId': s['node_id'] or ''}

    @router.post('/upload/complete')
    @_wrap
    def upload_complete(body: CompleteIn, request: Request, username: str = Depends(current_user)):
        with fs.lock():
            s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (body.uploadId,)).fetchone()
        if not s:
            raise fs.FsError('上传会话不存在或已过期', 404)
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        # 幂等：客户端超时重发 complete 时直接回报已完成，不再二次合并
        if s['status'] == 'done':
            return {'ok': True, 'already': True, 'nodeId': s['node_id'] or '', 'ver': 0,
                    'sha256': s['sha256'] or '', 'size': int(s['size']), 'elapsedMs': 0}
        if s['status'] == 'aborted':
            raise fs.FsError('该上传已被取消，请重新上传', 409)

        total = int(s['total'])
        tdir = fs.ctx.tmp / s['id']
        got = set(fs.parts_of(s['id']))
        # 合并前核对磁盘：DB 里的分片索引可能因 GC / 取消 / 杀毒占用而与磁盘不一致，
        # 不一致就剔除并让客户端补传，绝不能把 FileNotFoundError 抛成 500
        broken = [i for i in sorted(got) if not _part_ok(tdir, i)]
        if broken:
            with fs.lock():
                _drop_parts(s['id'], broken)
            got.difference_update(broken)
        missing = [i for i in range(total) if i not in got]
        if missing:
            raise fs.FsError('还缺 %d 个分片未完成（已剔除失效分片，请重新上传）' % len(missing), 409)

        # 进入 merging：这期间 abort / GC 都不再动临时目录。
        # 合并 1.8GB 要好几秒，之前正是这段窗口被并行 abort 删掉目录，才导致 500 + 1.6GB 孤儿 merge
        with fs.lock():
            cur = fs.db().execute('SELECT status FROM fs_upload WHERE id=?', (s['id'],)).fetchone()
            if cur and cur['status'] in ('aborted', 'done'):
                raise fs.FsError('该上传已被取消或已完成，请刷新后重试', 409)
            fs.db().execute("UPDATE fs_upload SET status='merging' WHERE id=?", (s['id'],))
            fs.db().commit()

        ip, ua = meta(request)
        t0 = time.time()
        merge = fs.ctx.tmp / (s['id'] + '.merge')
        h = hashlib.sha256()
        size = 0
        try:
            try:
                with open(str(merge), 'wb') as out:
                    for i in range(total):
                        with open(str(tdir / ('%d.part' % i)), 'rb') as f:
                            while True:
                                b = f.read(1024 * 1024)
                                if not b:
                                    break
                                out.write(b)
                                h.update(b)
                                size += len(b)
            except OSError as e:
                raise fs.FsError('合并分片失败（临时目录被清理或磁盘异常：%s），请重新上传该文件'
                                 % (e.strerror or e), 409)
            sha = h.hexdigest()
            if s['sha256'] and sha != s['sha256']:
                raise fs.FsError('文件校验失败（内容已变化），请重新上传')
            if int(s['size']) and size != int(s['size']):
                raise fs.FsError('文件大小与声明不一致（实际 %d / 声明 %d）' % (size, int(s['size'])))
            sha = fs.put_blob(str(merge), sha)
            with fs.lock():
                nid, ver = _create_or_replace(s['parent_id'], s['name'], sha, size, username,
                                              s['node_id'] or '', _mime_of(s['name']), ip, ua)
                fs.db().execute("UPDATE fs_upload SET status='done', node_id=? WHERE id=?",
                                (nid, s['id']))
                fs.clear_parts(s['id'])
                fs.fs_log(username, 'overwrite' if s['node_id'] else 'upload',
                          fs.get_node(nid), size=size, ver=ver,
                          duration_ms=int((time.time() - t0) * 1000), ip=ip, ua=ua)
                fs.db().commit()
        except BaseException:
            # 失败一定要清掉可能残留的 .merge（1.8GB 的孤儿文件足以把磁盘吃光），
            # 并把会话退回 uploading —— 分片还在，客户端补传后能继续
            _unlink_quiet(merge)
            _revert_upload(s['id'])
            raise
        _rm_tmp(tdir)
        return {'ok': True, 'nodeId': nid, 'ver': ver, 'sha256': sha, 'size': size,
                'elapsedMs': int((time.time() - t0) * 1000)}

    @router.post('/upload/abort')
    @_wrap
    def upload_abort(body: AbortIn, username: str = Depends(current_user)):
        s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (body.uploadId,)).fetchone()
        if not s:
            return {'ok': True}
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        if s['status'] == 'done':
            return {'ok': True, 'already': True}      # 已入库的文件不能被一次迟到的取消抹掉
        if s['status'] == 'merging':
            # 合并中删临时目录 = 制造 500 与孤儿 .merge 文件，让客户端稍后再取消
            raise fs.FsError('文件正在合并，请稍候再取消', 409)
        # 先落状态再删目录：迟到的分片请求会被状态挡在门外，不会重建出孤儿目录
        with fs.lock():
            fs.db().execute('UPDATE fs_upload SET status=\'aborted\' WHERE id=?', (s['id'],))
            fs.clear_parts(s['id'])
            fs.db().commit()
        _rm_tmp(fs.ctx.tmp / s['id'])
        _unlink_quiet(fs.ctx.tmp / (s['id'] + '.merge'))
        return {'ok': True}

    # ---------------- 版本 / 备份 ----------------
    @router.get('/versions')
    @_wrap
    def versions(node_id: str, username: str = Depends(current_user)):
        node = alive(node_id)
        check(username, node, fs.OP.VIEW)
        rows = fs.list_versions(node_id)
        nowt = time.time()
        return {'nodeId': node_id, 'name': node['name'],
                'retentionDays': fs.setting_int('retention_days'),
                'items': [{'id': r['id'], 'ver': r['ver'], 'size': r['size'], 'ts': r['ts'],
                           'actor': r['actor'], 'type': r['change_type'], 'name': r['name'],
                           'path': r['parent_path'], 'note': r['note'] or '',
                           'pinned': bool(r['pinned']), 'keepUntil': r['keep_until'],
                           'expired': r['keep_until'] < nowt, 'sha256': r['blob_sha'][:12]}
                          for r in rows]}

    @router.post('/versions/{vid}/restore')
    @_wrap
    def restore_version(vid: int, request: Request, username: str = Depends(current_user)):
        r = fs.db().execute('SELECT * FROM fs_version WHERE id=? AND purged=0', (vid,)).fetchone()
        if not r:
            raise fs.FsError('版本不存在或已被清理', 404)
        r = dict(r)
        node = alive(r['node_id'])
        check(username, node, fs.OP.OVERWRITE, request)
        ip, ua = meta(request)
        with fs.lock():
            f = fs.file_row(node['id'])
            if not f:
                raise fs.FsError('当前文件已丢失', 404)
            fs.snapshot_version(node['id'], 'overwrite', username, '回滚前备份 v%d' % f['ver'])
            ver = f['ver'] + 1
            fs.db().execute('UPDATE fs_file SET blob_sha=?, size=?, ver=? WHERE node_id=?',
                            (r['blob_sha'], r['size'], ver, node['id']))
            fs.db().execute('UPDATE fs_node SET size=?, updated_at=?, updated_by=? WHERE id=?',
                            (r['size'], time.time(), username, node['id']))
            fs.add_version(node['id'], ver, r['blob_sha'], r['size'], 'restore', username,
                           node['name'], fs.path_of(node['id']), '由 v%d 回滚' % r['ver'])
            fs.fs_log(username, 'rollback', node, target='v%d' % r['ver'], size=r['size'], ver=ver,
                      ip=ip, ua=ua)
            fs.db().commit()
        return {'ok': True, 'ver': ver}

    @router.post('/versions/{vid}/pin')
    @_wrap
    def pin_version(vid: int, pinned: int = 1, username: str = Depends(current_user)):
        r = fs.db().execute('SELECT * FROM fs_version WHERE id=?', (vid,)).fetchone()
        if not r:
            raise fs.FsError('版本不存在', 404)
        r = dict(r)
        node = fs.get_node(r['node_id'])
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')
                or (node and node['owner'] == username)):
            raise fs.FsError('需要管理员或文件创建人权限', 403)
        with fs.lock():
            fs.db().execute('UPDATE fs_version SET pinned=? WHERE id=?', (1 if pinned else 0, vid))
            fs.db().commit()
        return {'ok': True, 'pinned': bool(pinned)}

    # ---------------- 回收站 ----------------
    class RecycleBatchIn(BaseModel):
        ids: list = []

    def _bin_perm(username):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)

    def _bin_deleted(n):
        return bool(n) and int(n.get('deleted') or 0) == 1

    def _bin_all_deleted():
        """一次取回回收站全部节点（含子树），供祖先链 / 后代推断，避免逐节点查库。"""
        return {r['id']: dict(r) for r in
                fs.db().execute('SELECT * FROM fs_node WHERE deleted=1').fetchall()}

    def _bin_order(nodes):
        """父节点排前：恢复时祖先先复活（子节点才能回到原路径），
        彻底删除时父目录先删（子树一并清掉），后面的子项自然跳过。"""
        pending = {n['id']: n for n in nodes}
        out = []
        guard = 20000
        while pending and guard > 0:
            guard -= 1
            batch = [n for n in pending.values()
                     if not n.get('parent_id') or n['parent_id'] not in pending]
            if not batch:                        # 仅在异常成环时兜底
                batch = list(pending.values())
            batch.sort(key=lambda x: str(x.get('name') or ''))
            out.extend(batch)
            for n in batch:
                pending.pop(n['id'], None)
        return out

    def _bin_collect(ids, deleted, expand):
        by_parent = {}
        for n in deleted.values():
            by_parent.setdefault(n.get('parent_id') or '', []).append(n['id'])
        want = set()
        for rid in ids:
            if rid not in deleted:
                continue
            want.add(rid)
            p = deleted[rid].get('parent_id') or ''   # 被一并删除的祖先也要恢复，否则会掉到根目录
            guard = 200
            while p in deleted and guard > 0:
                guard -= 1
                if p in want:
                    break
                want.add(p)
                p = deleted[p].get('parent_id') or ''
            if expand:                                # 目录连同回收站内的后代一起恢复
                q = [rid]
                while q:
                    for c in by_parent.get(q.pop(0), []):
                        if c not in want:
                            want.add(c)
                            q.append(c)
        return [deleted[i] for i in want if i in deleted]

    def _bin_run(ids, action, who, ip='', ua=''):
        """批量恢复 / 批量彻底删除。单项失败不影响其余，返回明细供前端如实提示。
        调用方须持有 lock。"""
        ids = list(dict.fromkeys([str(x).strip() for x in (ids or []) if str(x).strip()]))
        if not ids:
            raise fs.FsError('请先选择要处理的项目', 400)
        if len(ids) > 500:
            raise fs.FsError('单次最多处理 500 项', 400)
        deleted = _bin_all_deleted()
        if action == 'restore':
            targets = _bin_order(_bin_collect(ids, deleted, True))
        else:
            # 彻底删除不向上扩展祖先（只删选中的），do_purge 自己会清掉子树
            targets = _bin_order([deleted[i] for i in ids if i in deleted])
        ok = skipped = 0
        failed = []
        for n in targets:
            cur = fs.get_node(n['id'])
            if action == 'restore' and not _bin_deleted(cur):
                skipped += 1
                continue
            if action == 'purge' and not cur:      # 已被父目录的 purge 一并删除
                skipped += 1
                continue
            try:
                if action == 'restore':
                    fs.do_restore(n['id'], who, None, ip, ua)
                else:
                    fs.do_purge(n['id'], who)
                ok += 1
            except Exception as e:
                try:
                    fs.db().rollback()            # PG 事务可能已 abort，清掉才能继续下一项
                except Exception:
                    pass
                failed.append({'id': n['id'], 'name': n.get('name') or '',
                               'reason': str(e)[:160]})
        fs.fs_log(who, 'restore' if action == 'restore' else 'purge', {},
                  target='批量%s %d/%d 项' % ('恢复' if action == 'restore' else '彻底删除',
                                             ok, len(ids)),
                  result=0 if failed else 1,
                  reason=('; '.join('%s: %s' % (f['name'], f['reason'])
                                    for f in failed[:3]))[:200],
                  ip=ip, ua=ua)
        fs.db().commit()
        return {'ok': not failed, 'action': action, 'requested': len(ids),
                'succeeded': ok, 'skipped': skipped,
                'failedCount': len(failed), 'failed': failed[:10]}

    @router.get('/recycle')
    @_wrap
    def recycle(limit: int = 200, username: str = Depends(current_user)):
        _bin_perm(username)
        rows = [dict(r) for r in fs.db().execute(
            'SELECT * FROM fs_node WHERE deleted=1 ORDER BY deleted_at DESC LIMIT ?',
            (max(1, min(500, limit)),)).fetchall()]
        # isTop：父节点不在回收站（被删目录只有根是顶层），前端据此默认折叠掉整棵子树
        all_del = {r['id'] for r in
                   fs.db().execute('SELECT id FROM fs_node WHERE deleted=1').fetchall()}
        keep = fs.setting_int('retention_days')
        nowt = time.time()
        return {'retentionDays': keep, 'total': len(all_del), 'items': [{
            'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'owner': r['owner'],
            'size': int(r['size'] or 0), 'deletedAt': r['deleted_at'], 'deletedBy': r['deleted_by'],
            'path': fs.path_of(r['id']), 'expireAt': r['deleted_at'] + keep * 86400,
            'expired': (r['deleted_at'] + keep * 86400) < nowt,
            'parentId': r['parent_id'],
            'isTop': (r['parent_id'] or '') not in all_del} for r in rows]}

    # 批量路由先于 /recycle/{node_id}/restore 注册，避免路径参数误吞
    @router.post('/recycle/batch-restore')
    @_wrap
    def recycle_batch_restore(body: RecycleBatchIn, request: Request,
                              username: str = Depends(current_user)):
        _bin_perm(username)
        ip, ua = meta(request)
        with fs.lock():
            return _bin_run(body.ids, 'restore', username, ip, ua)

    @router.post('/recycle/batch-purge')
    @_wrap
    def recycle_batch_purge(body: RecycleBatchIn, request: Request,
                            username: str = Depends(current_user)):
        _bin_perm(username)
        ip, ua = meta(request)
        with fs.lock():
            return _bin_run(body.ids, 'purge', username, ip, ua)

    @router.post('/recycle/{node_id}/restore')
    @_wrap
    def recycle_restore(node_id: str, request: Request, username: str = Depends(current_user)):
        _bin_perm(username)
        ip, ua = meta(request)
        with fs.lock():
            r = _bin_run([node_id], 'restore', username, ip, ua)
        if not r['succeeded']:
            raise fs.FsError('回收站中无此节点', 404)
        return {'ok': True, 'restored': r['succeeded']}

    @router.delete('/recycle/{node_id}')
    @_wrap
    def recycle_purge(node_id: str, request: Request, username: str = Depends(current_user)):
        _bin_perm(username)
        ip, ua = meta(request)
        with fs.lock():
            r = _bin_run([node_id], 'purge', username, ip, ua)
        if not r['succeeded']:
            raise fs.FsError('回收站中无此节点', 404)
        return {'ok': True}

    # ---------------- 权限（ACL） ----------------
    @router.get('/grant-targets')
    @_wrap
    def grant_targets(node_id: str = '', username: str = Depends(current_user)):
        node = fs.get_node(node_id)
        if not node:
            raise fs.FsError('节点不存在', 404)
        if not fs.can_grant(username, node):
            raise fs.FsError('无授权权限', 403)
        users = [dict(r) for r in fs.db().execute(
            'SELECT username, display_name FROM users ORDER BY username').fetchall()]
        roles = [dict(r) for r in fs.db().execute(
            'SELECT key, name FROM roles ORDER BY key').fetchall()]
        return {'users': [{'key': u['username'], 'name': u['display_name'] or u['username']}
                          for u in users],
                'roles': [{'key': r['key'], 'name': r['name']} for r in roles]}

    @router.get('/acl')
    @_wrap
    def get_acl(node_id: str = '', username: str = Depends(current_user)):
        node = fs.get_node(node_id)
        if not node:
            raise fs.FsError('节点不存在', 404)
        rows = [dict(r) for r in fs.db().execute(
            'SELECT * FROM fs_acl WHERE node_id=? ORDER BY id', (node_id,)).fetchall()]
        return {'nodeId': node_id, 'path': fs.path_of(node_id),
                'canGrant': fs.can_grant(username, node),
                'items': [{'id': r['id'], 'subjectType': r['subject_type'],
                           'subjectKey': r['subject_key'], 'allow': r['allow_mask'],
                           'deny': r['deny_mask'], 'inherit': bool(r['inherit']),
                           'perm': _decode(int(r['allow_mask'])),
                           'denyPerm': _decode(int(r['deny_mask'])),
                           'grantedBy': r['granted_by'] or '', 'grantedAt': r['granted_at'] or 0}
                          for r in rows]}

    class AclIn(BaseModel):
        nodeId: str = ''
        subjectType: str = 'user'
        subjectKey: str = ''
        ops: dict = {}
        deny: dict = {}
        inherit: bool = True

    @router.put('/acl')
    @_wrap
    def put_acl(body: AclIn, request: Request, username: str = Depends(current_user)):
        node = fs.get_node(body.nodeId)
        if not node:
            raise fs.FsError('节点不存在', 404)
        if not fs.can_grant(username, node):
            raise fs.FsError('无授权权限', 403)
        allow = _encode(body.ops or {})
        deny = _encode(body.deny or {})
        ip, ua = meta(request)
        with fs.lock():
            fs.db().execute('INSERT INTO fs_acl(node_id, subject_type, subject_key, allow_mask, '
                            'deny_mask, inherit, granted_by, granted_at) VALUES(?,?,?,?,?,?,?,?) '
                            'ON CONFLICT(node_id, subject_type, subject_key) DO UPDATE SET '
                            'allow_mask=excluded.allow_mask, deny_mask=excluded.deny_mask, '
                            'inherit=excluded.inherit, granted_by=excluded.granted_by, '
                            'granted_at=excluded.granted_at',
                            (body.nodeId, body.subjectType, body.subjectKey, allow, deny,
                             1 if body.inherit else 0, username, time.time()))
            fs.fs_log(username, 'grant', node,
                      target='%s:%s %s' % (body.subjectType, body.subjectKey,
                                           _ops_text(allow, deny)), ip=ip, ua=ua)
            fs.db().commit()
        return {'ok': True, 'allow': allow, 'deny': deny}

    @router.delete('/acl/{acl_id}')
    @_wrap
    def del_acl(acl_id: int, request: Request, username: str = Depends(current_user)):
        r = fs.db().execute('SELECT * FROM fs_acl WHERE id=?', (acl_id,)).fetchone()
        if not r:
            raise fs.FsError('记录不存在', 404)
        r = dict(r)
        node = fs.get_node(r['node_id'])
        if not node or not fs.can_grant(username, node):
            raise fs.FsError('无授权权限', 403)
        ip, ua = meta(request)
        with fs.lock():
            fs.db().execute('DELETE FROM fs_acl WHERE id=?', (acl_id,))
            fs.fs_log(username, 'revoke', node,
                      target='%s:%s' % (r['subject_type'], r['subject_key']), ip=ip, ua=ua)
            fs.db().commit()
        return {'ok': True}

    # ---------------- 操作日志 ----------------
    @router.get('/logs')
    @_wrap
    def logs(limit: int = 100, offset: int = 0, actor: str = '', op: str = '',
             node_id: str = '', result: str = '', kw: str = '',
             username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        where, args = [], []
        if actor:
            where.append('actor = ?')
            args.append(actor)
        if op:
            where.append('op = ?')
            args.append(op)
        if node_id:
            where.append('node_id = ?')
            args.append(node_id)
        if result in ('0', '1'):
            where.append('result = ?')
            args.append(int(result))
        if kw:
            where.append('(lower(actor) LIKE ? OR lower(node_name) LIKE ? OR '
                         'lower(path_snapshot) LIKE ? OR lower(target) LIKE ?)')
            args += ['%' + kw.lower() + '%'] * 4
        sql = 'SELECT * FROM fs_op_log'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
        args += [max(1, min(500, limit)), max(0, offset)]
        rows = [dict(r) for r in fs.db().execute(sql, args).fetchall()]
        tsql = 'SELECT COUNT(*) c FROM fs_op_log' + (' WHERE ' + ' AND '.join(where) if where else '')
        total = fs.db().execute(tsql, args[:-2] if where else []).fetchone()['c']
        return {'total': total, 'items': [_log_out(r) for r in rows]}

    @router.get('/logs/export')
    @_wrap
    def logs_export(limit: int = 5000, actor: str = '', op: str = '',
                    username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        where, args = [], []
        if actor:
            where.append('actor = ?')
            args.append(actor)
        if op:
            where.append('op = ?')
            args.append(op)
        sql = 'SELECT * FROM fs_op_log' + (' WHERE ' + ' AND '.join(where) if where else '')
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(max(1, min(20000, limit)))
        rows = [dict(r) for r in fs.db().execute(sql, args).fetchall()]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['时间', '操作人', '动作', '对象', '路径', '备注', '大小', '版本', '结果', '原因', 'IP'])
        for r in rows:
            o = _log_out(r)
            w.writerow([o['tsText'], o['actor'], o['opText'], o['nodeName'], o['path'],
                        o['target'], o['size'], o['ver'], o['resultText'], o['reason'], o['ip']])
        data = '\ufeff' + buf.getvalue()
        return Response(content=data.encode('utf-8'), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': disposition('存储操作日志.csv')})

    # ---------------- 设置 / 统计 / 手动清理 ----------------
    @router.get('/settings')
    @_wrap
    def settings(username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        return {'values': fs.all_settings(), 'defaults': dict(fs.DEFAULTS),
                'labels': SETTING_LABELS, 'root': str(fs.ctx.root)}

    class SetIn(BaseModel):
        k: str = ''
        v: str = ''
        applyExisting: bool = False

    @router.put('/settings')
    @_wrap
    def put_settings(body: SetIn, username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        with fs.lock():
            fs.set_setting(body.k, body.v, username)
            if body.applyExisting and body.k == 'retention_days':
                days = int(body.v or 0)
                fs.db().execute('UPDATE fs_version SET keep_until=ts+? WHERE purged=0 AND pinned=0',
                                (days * 86400,))
            fs.db().commit()
        return {'ok': True, 'values': fs.all_settings()}

    @router.get('/stats')
    @_wrap
    def stats(username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        def one(sql):
            return int(fs.db().execute(sql).fetchone()['c'] or 0)
        return {'files': one("SELECT COUNT(*) c FROM fs_node WHERE kind='file' AND deleted=0"),
                'dirs': one("SELECT COUNT(*) c FROM fs_node WHERE kind='dir' AND deleted=0"),
                'used': one("SELECT COALESCE(SUM(size),0) c FROM fs_node WHERE kind='file' AND deleted=0"),
                'versions': one('SELECT COUNT(*) c FROM fs_version WHERE purged=0'),
                'versionBytes': one('SELECT COALESCE(SUM(size),0) c FROM fs_version WHERE purged=0'),
                'recycle': one('SELECT COUNT(*) c FROM fs_node WHERE deleted=1'),
                'blobs': one('SELECT COUNT(*) c FROM fs_blob WHERE purged=0'),
                'logs': one('SELECT COUNT(*) c FROM fs_op_log'),
                'retentionDays': fs.setting_int('retention_days'),
                'root': str(fs.ctx.root)}

    @router.post('/gc')
    @_wrap
    def gc(username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        return {'ok': True, 'stats': fs.run_gc_now()}

    @router.post('/maintenance/dedupe-versions')
    @_wrap
    def dedupe_versions(username: str = Depends(current_user)):
        """一次性维护：清理历史上重复的版本行（同一 node+ver+sha 多条）。"""
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        return {'ok': True, 'stats': fs.dedupe_versions()}

    return router


SETTING_LABELS = {
    'retention_days': '备份保留天数（旧版本 + 回收站）',
    'log_retention_days': '操作日志保留天数',
    'chunk_size': '分片大小（字节）',
    'max_file_size': '单文件上限（字节）',
    'max_name_len': '单级名称字符上限',
    'max_depth': '目录最大层级',
    'quota_bytes': '存储总量配额（字节，0=不限）',
    'allow_ext': '扩展名白名单（逗号分隔，空=不限）',
    'deny_ext': '扩展名黑名单（逗号分隔）',
    'purge_hour': '每日清理时刻（小时）',
    'enable_dedup': '秒传/去重开关（1/0）',
    'max_versions': '单文件版本数上限（0=不限）',
    'owner_full_control': '创建人对自己节点全权（1/0）',
    'log_detail_level': '日志记录明细级别（1=逐文件）',
    'office_server': 'ONLYOFFICE 服务地址（如 http://192.168.1.10:8888，空=关闭在线预览/编辑）',
    'office_secret': 'ONLYOFFICE JWT 密钥（DS 开启 JWT 时必填，与容器一致）',
    'office_base_url': 'DS 回源取文件的地址（如 http://host.docker.internal:8000，空=按浏览器请求推导）',
}

MIME_MAP = {
    'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp', 'txt': 'text/plain; charset=utf-8',
    'csv': 'text/csv; charset=utf-8', 'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'zip': 'application/zip', 'rar': 'application/x-rar-compressed', '7z': 'application/x-7z-compressed',
}

OP_TEXT = {'mkdir': '新建文件夹', 'upload': '上传', 'overwrite': '覆盖', 'modify': '修改',
           'rename': '重命名', 'move': '移动', 'delete': '删除', 'restore': '恢复',
           'purge': '清理', 'download': '下载', 'grant': '授权', 'revoke': '撤权',
           'rollback': '版本回滚'}


def _mime_of(name):
    return MIME_MAP.get(fs.ext_of(name), 'application/octet-stream')


def _encode(ops):
    m = 0
    for k, v in (ops or {}).items():
        if v and k in fs.OP_BIT:
            m |= fs.OP_BIT[k]
    return m


def _ops_text(allow, deny):
    a = [fs.OP_NAME[fs._bit_key(b)] for b in fs.OP_BIT.values() if allow & b]
    d = [fs.OP_NAME[fs._bit_key(b)] for b in fs.OP_BIT.values() if deny & b]
    return ('允许 ' + '/'.join(a) if a else '无') + ('；拒绝 ' + '/'.join(d) if d else '')


def _log_out(r):
    return {'id': r['id'], 'ts': r['ts'], 'actor': r['actor'], 'op': r['op'],
            'opText': OP_TEXT.get(r['op'], r['op']), 'nodeId': r['node_id'],
            'nodeName': r['node_name'], 'path': r['path_snapshot'], 'target': r['target'],
            'size': int(r['size'] or 0), 'ver': int(r['ver'] or 0),
            'result': bool(r['result']), 'resultText': '成功' if r['result'] else '被拒绝',
            'reason': r['reason'] or '', 'ip': r['ip'] or '',
            'durationMs': int(r['duration_ms'] or 0),
            'tsText': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))}


def _create_or_replace(parent_id, name, sha, size, who, node_id, mime, ip='', ua=''):
    """建新文件或覆盖已有文件（调用方须持有 lock）。返回 (node_id, ver)。
    覆盖前先把旧内容存为备份版本。"""
    t = time.time()
    if node_id:
        f = fs.file_row(node_id)
        if f:
            fs.snapshot_version(node_id, 'overwrite', who, '覆盖前备份 v%d' % f['ver'])
            ver = int(f['ver']) + 1
            fs.db().execute('UPDATE fs_file SET blob_sha=?, size=?, ver=? WHERE node_id=?',
                            (sha, size, ver, node_id))
        else:
            ver = 1
            fs.db().execute('INSERT INTO fs_file(node_id, blob_sha, size, mime, ver) '
                            'VALUES(?,?,?,?,1)', (node_id, sha, size, mime))
        fs.db().execute('UPDATE fs_node SET size=?, updated_at=?, updated_by=? WHERE id=?',
                        (size, t, who, node_id))
        fs.add_version(node_id, ver, sha, size, 'overwrite', who, name, fs.path_of(node_id))
        return node_id, ver
    nid = fs.new_id()
    fs.db().execute('INSERT INTO fs_node(id, parent_id, name, kind, owner, created_at, '
                    'updated_at, updated_by, size) VALUES(?,?,?,\'file\',?,?,?,?,?)',
                    (nid, parent_id, name, who, t, t, who, size))
    fs.db().execute('INSERT INTO fs_file(node_id, blob_sha, size, mime, ver) VALUES(?,?,?,?,1)',
                    (nid, sha, size, mime))
    fs.add_version(nid, 1, sha, size, 'upload', who, name, fs.path_of(nid))
    return nid, 1
