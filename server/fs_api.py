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

from . import fs


def _op_action(op):
    return fs._bit_key(op)


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
        with fs.lock():
            s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (upload_id,)).fetchone()
        if not s:
            raise fs.FsError('上传会话不存在或已过期', 404)
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        if s['status'] == 'done':
            return {'received': int(s['total']), 'total': int(s['total']), 'done': True}
        idx = int(index)
        if idx < 0 or idx >= int(s['total']):
            raise fs.FsError('分片下标越界')
        tdir = fs.ctx.tmp / upload_id
        tdir.mkdir(parents=True, exist_ok=True)
        part = tdir / ('%d.part' % idx)
        tmpf = tdir / ('%d.part.w' % idx)
        wrote = 0
        with open(str(tmpf), 'wb') as out:
            async for buf in request.stream():
                if not buf:
                    continue
                out.write(buf)
                wrote += len(buf)
        limit = int(s['chunk_size']) + 1024
        if wrote > limit:
            try:
                tmpf.unlink()
            except Exception:
                pass
            raise fs.FsError('分片大小超过限制')
        os.replace(str(tmpf), str(part))
        with fs.lock():
            got = set(int(x) for x in (s['got'] or '').split(',') if x != '')
            got.add(idx)
            fs.db().execute('UPDATE fs_upload SET got=?, status=? WHERE id=?',
                            (','.join(str(x) for x in sorted(got)), 'uploading', upload_id))
            fs.db().commit()
        return {'received': len(got), 'total': int(s['total']), 'index': idx}

    @router.get('/upload/status')
    @_wrap
    def upload_status(upload_id: str, username: str = Depends(current_user)):
        s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (upload_id,)).fetchone()
        if not s:
            raise fs.FsError('上传会话不存在', 404)
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        got = [int(x) for x in (s['got'] or '').split(',') if x != '']
        missing = [i for i in range(int(s['total'])) if i not in got][:2000]
        return {'uploadId': upload_id, 'total': int(s['total']), 'got': got, 'missing': missing,
                'status': s['status'], 'size': int(s['size']), 'name': s['name'],
                'parentId': s['parent_id'], 'nodeId': s['node_id'] or ''}

    @router.post('/upload/complete')
    @_wrap
    def upload_complete(body: CompleteIn, request: Request, username: str = Depends(current_user)):
        with fs.lock():
            s = fs.db().execute('SELECT * FROM fs_upload WHERE id=?', (body.uploadId,)).fetchone()
        if not s:
            raise fs.FsError('上传会话不存在', 404)
        s = dict(s)
        if s['actor'] != username and not fs.is_admin(username):
            raise fs.FsError('无权操作该上传会话', 403)
        total = int(s['total'])
        got = set(int(x) for x in (s['got'] or '').split(',') if x != '')
        missing = [i for i in range(total) if i not in got]
        if missing:
            raise fs.FsError('还缺 %d 个分片未完成' % len(missing), 409)
        ip, ua = meta(request)
        t0 = time.time()
        tdir = fs.ctx.tmp / s['id']
        merge = fs.ctx.tmp / (s['id'] + '.merge')
        h = hashlib.sha256()
        size = 0
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
        sha = h.hexdigest()
        if s['sha256'] and sha != s['sha256']:
            try:
                merge.unlink()
            except Exception:
                pass
            raise fs.FsError('文件校验失败（内容已变化），请重新上传')
        if int(s['size']) and size != int(s['size']):
            try:
                merge.unlink()
            except Exception:
                pass
            raise fs.FsError('文件大小与声明不一致')
        sha = fs.put_blob(str(merge), sha)
        with fs.lock():
            nid, ver = _create_or_replace(s['parent_id'], s['name'], sha, size, username,
                                          s['node_id'] or '', _mime_of(s['name']), ip, ua)
            fs.db().execute('UPDATE fs_upload SET status=\'done\' WHERE id=?', (s['id'],))
            fs.fs_log(username, 'overwrite' if s['node_id'] else 'upload',
                      fs.get_node(nid), size=size, ver=ver,
                      duration_ms=int((time.time() - t0) * 1000), ip=ip, ua=ua)
            fs.db().commit()
        shutil.rmtree(str(tdir), ignore_errors=True)
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
        shutil.rmtree(str(fs.ctx.tmp / s['id']), ignore_errors=True)
        with fs.lock():
            fs.db().execute('UPDATE fs_upload SET status=\'aborted\' WHERE id=?', (s['id'],))
            fs.db().commit()
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
    @router.get('/recycle')
    @_wrap
    def recycle(limit: int = 200, username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        rows = [dict(r) for r in fs.db().execute(
            'SELECT * FROM fs_node WHERE deleted=1 ORDER BY deleted_at DESC LIMIT ?',
            (max(1, min(500, limit)),)).fetchall()]
        keep = fs.setting_int('retention_days')
        nowt = time.time()
        return {'retentionDays': keep, 'items': [{
            'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'owner': r['owner'],
            'size': int(r['size'] or 0), 'deletedAt': r['deleted_at'], 'deletedBy': r['deleted_by'],
            'path': fs.path_of(r['id']), 'expireAt': r['deleted_at'] + keep * 86400,
            'expired': (r['deleted_at'] + keep * 86400) < nowt} for r in rows]}

    @router.post('/recycle/{node_id}/restore')
    @_wrap
    def recycle_restore(node_id: str, request: Request, username: str = Depends(current_user)):
        node = fs.get_node(node_id)
        if not node or not node['deleted']:
            raise fs.FsError('回收站中无此节点', 404)
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        ip, ua = meta(request)
        with fs.lock():
            fs.do_restore(node_id, username, None, ip, ua)
            fs.db().commit()
        return {'ok': True}

    @router.delete('/recycle/{node_id}')
    @_wrap
    def recycle_purge(node_id: str, request: Request, username: str = Depends(current_user)):
        if not (fs.is_admin(username) or fs.perm_of(username)['perms'].get('file.admin')):
            raise fs.FsError('需要存储管理权限', 403)
        ip, ua = meta(request)
        with fs.lock():
            n = fs.get_node(node_id)
            fs.do_purge(node_id, username)
            fs.fs_log(username, 'delete', n or {}, target='彻底删除', ip=ip, ua=ua)
            fs.db().commit()
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
