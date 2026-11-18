# -*- coding: utf-8 -*-
"""标讯采集 · 后端接口（/api/bidcrawl/*）。

职责边界：配置 CRUD、平台/配方管理、任务提交与查询、结果读取。
重活（翻页、详情、入库）全部在独立 worker；本模块不发起任何站点请求。

SQL 用 `%s`（bidcrawl.db），与 main.PgDB 的 `?` 分属两套连接，互不影响。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response

from .bidcrawl import config as bcfg
from .bidcrawl import db as bdb
from .bidcrawl import enroll as enroll_mod
from .bidcrawl import filter as bfilter
from .bidcrawl import jobs as bjobs
from .bidcrawl import recipe as brecipe

SCHEMA_PATH = Path(__file__).with_name('bidcrawl') / 'schema.sql'


# 「已报名」标记：台账里出现过的 noticeUrl 集合（20 秒缓存，报名后立即失效）
_enrolled_cache = {'at': 0.0, 'set': set()}


def _enrolled_urls(force: bool = False) -> set:
    now = time.time()
    if not force and now - _enrolled_cache['at'] < 20 and _enrolled_cache['set']:
        return _enrolled_cache['set']
    urls = set()
    try:
        for r in bdb.q("SELECT payload FROM rows WHERE payload LIKE %s",
                       ('%noticeUrl%',)):
            try:
                p = json.loads(r['payload'])
            except Exception:
                continue
            u = str((p or {}).get('noticeUrl') or '').strip()
            if u:
                urls.add(u)
    except Exception:
        urls = set(_enrolled_cache['set'])
    _enrolled_cache.update(at=now, set=urls)
    return urls


# ------------------------------------------------------------------ DDL
def init_tables(dbobj=None):
    """幂等建表；dbobj 仅为签名兼容，实际使用 bidcrawl 自己的连接。"""
    bdb.init_schema()
    return True


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _rows(rows):
    return [_jsonable(dict(r)) for r in rows]


def _need_write(need, username):
    need(username, 'bidcrawl.manage', '无权修改采集配置')


# ------------------------------------------------------------------ CRUD 公共
_TABLES = {
    'keyword': ('bid_keyword', ('word', 'scope', 'enabled', 'is_sample', 'sort_no', 'note')),
    'buyer': ('bid_buyer_pattern', ('pattern', 'match_type', 'scope', 'enabled', 'sort_no', 'note')),
    'rule': ('bid_exclude_rule', ('name', 'rule_type', 'value', 'scope', 'enabled', 'sort_no', 'note')),
}


def _list_entity(entity: str, scope: str = '', q: str = '', enabled: str = '') -> list[dict]:
    table, _ = _TABLES[entity]
    sql = 'SELECT * FROM %s WHERE deleted_at IS NULL' % table
    args = []
    if scope:
        sql += ' AND scope=%s'
        args.append(scope)
    if enabled in ('0', '1'):
        sql += ' AND enabled=%s'
        args.append(enabled == '1')
    if q:
        key = {'keyword': 'word', 'buyer': 'pattern', 'rule': 'name'}[entity]
        sql += ' AND %s ILIKE %%s' % key
        args.append('%' + q + '%')
    sql += ' ORDER BY scope, sort_no, id'
    return _rows(bdb.q(sql, tuple(args)))


def _get_entity(entity: str, eid: int) -> dict | None:
    table, _ = _TABLES[entity]
    r = bdb.one('SELECT * FROM %s WHERE id=%%s AND deleted_at IS NULL' % table, (eid,))
    return _jsonable(dict(r)) if r else None


def _create(entity: str, body: dict, operator: str) -> dict:
    body = dict(body)
    body.setdefault('scope', 'global')
    body.setdefault('enabled', True)
    body.setdefault('sort_no', 0)
    if entity == 'buyer':
        body.setdefault('match_type', 'contains')
    errs = bcfg.validate_entry(entity, body)
    if errs:
        raise HTTPException(400, '；'.join(errs))
    table, cols = _TABLES[entity]
    if entity == 'keyword':
        word = str(body.get('word')).strip()
        scope = body.get('scope') or 'global'
        row = bdb.one(
            'INSERT INTO bid_keyword(word, scope, enabled, is_sample, sort_no, note) '
            'VALUES (%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT (word, scope) DO UPDATE SET deleted_at=NULL, '
            'enabled=EXCLUDED.enabled, is_sample=EXCLUDED.is_sample, '
            'sort_no=EXCLUDED.sort_no, note=EXCLUDED.note, updated_at=now() RETURNING *',
            (word, scope, bool(body.get('enabled', True)), bool(body.get('is_sample')),
             int(body.get('sort_no') or 0), body.get('note') or ''))
    else:
        vals = []
        for c in cols:
            v = body.get(c)
            if c == 'value':
                v = v if isinstance(v, str) else json.dumps(v or {}, ensure_ascii=False)
            vals.append(v)
        row = bdb.one('INSERT INTO %s(%s) VALUES (%s) RETURNING *'
                       % (table, ', '.join(cols), ', '.join(['%s'] * len(cols))), tuple(vals))
    bcfg.invalidate()
    out = _jsonable(dict(row))
    bcfg.audit(entity, out.get('id'), 'create', None, out, operator)
    return out


def _update(entity: str, eid: int, body: dict, operator: str) -> dict:
    before = _get_entity(entity, eid)
    if not before:
        raise HTTPException(404, '配置不存在')
    merged = dict(before)
    merged.update({k: v for k, v in body.items() if k in _TABLES[entity][1]})
    errs = bcfg.validate_entry(entity, merged)
    if errs:
        raise HTTPException(400, '；'.join(errs))
    table, cols = _TABLES[entity]
    sets, args = [], []
    for c in cols:
        if c not in body:
            continue
        v = body[c]
        if c == 'value':
            v = v if isinstance(v, str) else json.dumps(v or {}, ensure_ascii=False)
        sets.append('%s=%%s' % c)
        args.append(v)
    if not sets:
        return before
    args.append(eid)
    row = bdb.one('UPDATE %s SET %s, updated_at=now() WHERE id=%%s AND deleted_at IS NULL '
                  'RETURNING *' % (table, ', '.join(sets)), tuple(args))
    bcfg.invalidate()
    out = _jsonable(dict(row))
    bcfg.audit(entity, eid, 'update', before, out, operator)
    return out


def _delete(entity: str, eid: int, operator: str) -> dict:
    before = _get_entity(entity, eid)
    if not before:
        raise HTTPException(404, '配置不存在')
    table, _ = _TABLES[entity]
    bdb.x('UPDATE %s SET deleted_at=now(), updated_at=now() WHERE id=%%s' % table, (eid,))
    bdb.commit()
    bcfg.invalidate()
    bcfg.audit(entity, eid, 'delete', before, None, operator)
    return {'id': eid, 'deleted': True}


# ------------------------------------------------------------------ 路由
def build_router(current_user, ctx):
    need = ctx['need']

    def _need_read(username: str = Depends(current_user)):
        need(username, 'view.bidcrawl', '无权查看标讯采集')

    router = APIRouter(prefix='/api/bidcrawl', tags=['bidcrawl'],
                       dependencies=[Depends(_need_read)])

    # ---------------- 平台与配方 ----------------
    @router.get('/platforms')
    def platforms(username: str = Depends(current_user)):
        return {'platforms': _rows(brecipe.list_platforms())}

    @router.post('/platforms')
    def create_platform(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        pid = str(body.get('id') or '').strip()
        name = str(body.get('name') or '').strip()
        host = str(body.get('host') or '').strip()
        if not pid or not name or not host:
            raise HTTPException(400, 'id / name / host 不能为空')
        row = bdb.one(
            'INSERT INTO bid_platform(id, name, host, entry_url) VALUES (%s,%s,%s,%s) '
            'ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, host=EXCLUDED.host, '
            'entry_url=COALESCE(EXCLUDED.entry_url, bid_platform.entry_url) RETURNING *',
            (pid, name, host, body.get('entry_url') or ''))
        bdb.commit()
        bcfg.audit('platform', None, 'create', None, _jsonable(dict(row)), username)
        return _jsonable(dict(row))

    @router.post('/analyze')
    def analyze_url(body: dict = Body(...), username: str = Depends(current_user)):
        """生成采集配方。两种入口：

        - 证据模式（推荐）：body.mode = api_list / html_list，用户粘贴列表/详情的
          curl 与真实返回样本，系统确定性反推 jsonpath / css / 参数模板，LLM 只做
          字段语义映射，并在样本与线上各校验一次；
        - 自动探测（旧）：只给 url，抓入口页 JS 试探接口后组装配方。
        返回 {ok, recipe, verify, stages, evidence, error, manual_steps, warning}。
        """
        from .bidcrawl import ai_analyze, manual
        mode = str(body.get('mode') or '').strip()
        keyword = str(body.get('keyword') or '').strip()[:60]
        with_llm = body.get('with_llm', True) is not False
        verify = body.get('verify', True) is not False

        def _crash(e: Exception) -> dict:
            """生成器自身异常：返回可读报错 + 末几行 traceback，避免整站 500。"""
            import traceback
            tb = traceback.format_exc(limit=6)
            try:
                print('[bidcrawl] analyze failed\n%s' % tb, flush=True)
            except Exception:
                pass
            return {'ok': False, 'recipe': None,
                    'verify': {'valid': False, 'errors': [str(e)[:200]]},
                    'stages': [{'name': '生成配方', 'status': 'error',
                                'detail': '内部异常：%s' % str(e)[:300], 'ms': 0}],
                    'error': '生成配方时发生内部错误（已记录日志）：%s' % str(e)[:200],
                    'traceback': tb.splitlines()[-4:]}

        if mode in manual.VALID_MODES:
            if not str(body.get('list_curl') or '').strip():
                raise HTTPException(400, '请粘贴列表请求的 curl')
            if not str(body.get('list_sample') or '').strip():
                raise HTTPException(400, '请粘贴列表接口的返回样本')
            payload = dict(body)
            payload['mode'] = mode
            payload['keyword'] = keyword
            payload['with_llm'] = with_llm
            payload['verify'] = verify
            try:
                out = manual.analyze(payload)
            except Exception as e:  # noqa: BLE001
                return _crash(e)
            url = str(body.get('site_url') or '').strip()
        else:
            url = str(body.get('url') or '').strip()
            if not url:
                raise HTTPException(400, 'url 不能为空')
            try:
                out = ai_analyze.analyze(url, keyword, with_llm=with_llm,
                                         verify=verify)
            except Exception as e:  # noqa: BLE001
                return _crash(e)
        # 只要产出了配方就先建平台占位记录（paused），便于用户直接编辑保存
        rec = out.get('recipe') or {}
        meta = rec.get('meta') or {}
        pid = str(meta.get('id') or '').strip()
        name = str(meta.get('name') or '').strip()
        host = str(meta.get('host') or '').strip()
        if pid and name and host:
            try:
                bdb.x('INSERT INTO bid_platform(id, name, host, entry_url, status) '
                      'VALUES (%s,%s,%s,%s,%s) '
                      'ON CONFLICT (id) DO NOTHING',
                      (pid, name, host, meta.get('entry_url') or url, 'paused'))
                bdb.commit()
            except Exception:
                bdb.rollback()
        bcfg.audit('platform', None, 'analyze', None,
                   {'url': url, 'keyword': keyword, 'platform_id': pid,
                    'ok': bool(out.get('ok'))}, username)
        return out

    @router.get('/platforms/{pid}/recipe')
    def get_recipe(pid: str, username: str = Depends(current_user)):
        p = bdb.one('SELECT p.*, v.recipe, v.verify, v.created_at AS ver_at '
                    'FROM bid_platform p LEFT JOIN bid_recipe_version v '
                    'ON v.platform_id=p.id AND v.version=p.active_ver WHERE p.id=%s', (pid,))
        if not p:
            raise HTTPException(404, '平台不存在')
        return {'platform': _jsonable({k: v for k, v in p.items()
                                       if k not in ('recipe', 'verify')}),
                'recipe': _jsonable(p.get('recipe')),
                'verify': _jsonable(p.get('verify'))}

    @router.put('/platforms/{pid}/recipe')
    def put_recipe(pid: str, body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        raw = body.get('recipe')
        if not isinstance(raw, dict):
            raise HTTPException(400, 'recipe 必须是 JSON 对象')
        raw.setdefault('meta', {})['id'] = pid
        try:
            out = brecipe.save_version(raw, created_by=username,
                                       activate=bool(body.get('activate', True)),
                                       verify=body.get('verify'))
        except brecipe.RecipeError as e:
            raise HTTPException(400, str(e))
        bcfg.invalidate(pid)
        bcfg.audit('recipe', None, 'update', None,
                   {'platform_id': pid, 'version': out['version']}, username)
        return out

    @router.delete('/platforms/{pid}/recipe')
    def delete_recipe(pid: str, username: str = Depends(current_user)):
        _need_write(need, username)
        try:
            out = brecipe.delete_recipe(pid)
        except brecipe.RecipeError as e:
            raise HTTPException(400, str(e))
        bcfg.invalidate(pid)
        bcfg.audit('recipe', None, 'delete', None, out, username)
        return out

    @router.delete('/platforms/{pid}')
    def remove_platform(pid: str, force: int = 0, username: str = Depends(current_user)):
        """彻底删除平台及其全部关联数据；有运行中任务时默认拒绝。"""
        _need_write(need, username)
        try:
            out = brecipe.delete_platform(pid, force=bool(force))
        except brecipe.RecipeError as e:
            raise HTTPException(400, str(e))
        bcfg.invalidate(pid)
        bcfg.audit('platform', None, 'delete', None, out, username)
        return out

    @router.post('/platforms/{pid}/activate')
    def activate(pid: str, body: dict = Body(default={}), username: str = Depends(current_user)):
        _need_write(need, username)
        try:
            out = brecipe.activate(pid, body.get('version'))
        except brecipe.RecipeError as e:
            raise HTTPException(400, str(e))
        bcfg.invalidate(pid)
        bcfg.audit('recipe', None, 'enable', None, out, username)
        return out

    @router.post('/platforms/{pid}/status')
    def set_status(pid: str, body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        status = str(body.get('status') or '')
        try:
            ok = brecipe.set_status(pid, status, body.get('error') or '')
        except brecipe.RecipeError as e:
            raise HTTPException(400, str(e))
        if not ok:
            raise HTTPException(404, '平台不存在')
        bcfg.invalidate(pid)
        bcfg.audit('platform', None, 'enable' if status == 'active' else 'disable',
                   None, {'status': status}, username)
        return {'platform_id': pid, 'status': status}

    @router.get('/platforms/{pid}/runs')
    def runs(pid: str, limit: int = 50, username: str = Depends(current_user)):
        rows = bdb.q('SELECT * FROM bid_crawl_run WHERE platform_id=%s '
                     'ORDER BY id DESC LIMIT %s', (pid, max(1, min(200, limit))))
        return {'runs': _rows(rows)}

    @router.get('/platforms/{pid}/fields')
    def platform_fields(pid: str, username: str = Depends(current_user)):
        rows = bdb.q('SELECT rf.*, fd.label, fd.data_type FROM bid_recipe_field rf '
                     'LEFT JOIN bid_field_def fd ON fd.key=rf.field_key '
                     'WHERE rf.platform_id=%s AND rf.version=('
                     '  SELECT active_ver FROM bid_platform WHERE id=%s) '
                     'ORDER BY rf.source, rf.field_key', (pid, pid))
        return {'fields': _rows(rows)}

    # ---------------- 配置 CRUD ----------------
    @router.get('/keywords')
    def list_keywords(scope: str = '', q: str = '', enabled: str = '',
                      username: str = Depends(current_user)):
        return {'items': _list_entity('keyword', scope, q, enabled)}

    @router.post('/keywords')
    def create_keyword(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _create('keyword', body, username)

    @router.put('/keywords/{eid}')
    def update_keyword(eid: int, body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _update('keyword', eid, body, username)

    @router.delete('/keywords/{eid}')
    def delete_keyword(eid: int, username: str = Depends(current_user)):
        _need_write(need, username)
        return _delete('keyword', eid, username)

    @router.post('/keywords/bulk')
    def bulk_keywords(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        text = str(body.get('words') or '')
        words = [w.strip() for w in text.replace('，', '\n').replace(',', '\n').splitlines()
                 if w.strip()]
        scope = body.get('scope') or 'global'
        created = 0
        for w in dict.fromkeys(words):
            if len(w) > 32:
                continue
            bdb.x('INSERT INTO bid_keyword(word, scope, enabled, is_sample, note) '
                  'VALUES (%s,%s,%s,%s,%s) ON CONFLICT (word, scope) DO UPDATE '
                  'SET deleted_at=NULL, enabled=EXCLUDED.enabled, updated_at=now()',
                  (w, scope, bool(body.get('enabled', True)),
                   bool(body.get('is_sample')), body.get('note') or ''))
            created += 1
        bdb.commit()
        bcfg.invalidate()
        bcfg.audit('keyword', None, 'create', None,
                   {'bulk': created, 'scope': scope}, username)
        return {'created': created}

    @router.get('/buyers')
    def list_buyers(scope: str = '', q: str = '', enabled: str = '',
                    username: str = Depends(current_user)):
        return {'items': _list_entity('buyer', scope, q, enabled)}

    @router.post('/buyers')
    def create_buyer(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _create('buyer', body, username)

    @router.put('/buyers/{eid}')
    def update_buyer(eid: int, body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _update('buyer', eid, body, username)

    @router.delete('/buyers/{eid}')
    def delete_buyer(eid: int, username: str = Depends(current_user)):
        _need_write(need, username)
        return _delete('buyer', eid, username)

    @router.get('/rules')
    def list_rules(scope: str = '', q: str = '', enabled: str = '',
                   username: str = Depends(current_user)):
        return {'items': _list_entity('rule', scope, q, enabled)}

    @router.post('/rules')
    def create_rule(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _create('rule', body, username)

    @router.put('/rules/{eid}')
    def update_rule(eid: int, body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        return _update('rule', eid, body, username)

    @router.delete('/rules/{eid}')
    def delete_rule(eid: int, username: str = Depends(current_user)):
        _need_write(need, username)
        return _delete('rule', eid, username)

    @router.post('/rules/preview')
    def preview_rules(body: dict = Body(...), username: str = Depends(current_user)):
        pid = str(body.get('platform_id') or '').strip()
        if not pid:
            raise HTTPException(400, '缺少 platform_id')
        snap = bcfg.load_snapshot(pid, force=True)
        candidate = body.get('rules')
        if isinstance(candidate, list):
            from .bidcrawl.config import Snapshot
            snap = Snapshot(platform_id=pid, keywords=snap.keywords, buyers=snap.buyers,
                            rules=candidate, version=snap.version)
        rows = bdb.q('SELECT title, buyer, url, publish_at, raw FROM bid_item '
                     'WHERE platform_id=%s ORDER BY last_seen_at DESC LIMIT 500', (pid,))
        items = []
        for r in rows:
            raw = r.get('raw')
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            items.append({'title': r.get('title'), 'buyer': r.get('buyer'),
                          'url': r.get('url'), 'publish_at': r.get('publish_at'),
                          'raw': raw or {}})
        rec = brecipe.get_active(pid)
        return _jsonable(bfilter.preview(items, snap, pid,
                                         filters=bfilter.recipe_filters(rec)))

    @router.get('/config/audit')
    def config_audit(limit: int = 100, entity: str = '',
                     username: str = Depends(current_user)):
        sql = 'SELECT * FROM bid_config_audit'
        args = []
        if entity:
            sql += ' WHERE entity=%s'
            args.append(entity)
        sql += ' ORDER BY id DESC LIMIT %s'
        args.append(max(1, min(500, limit)))
        return {'logs': _rows(bdb.q(sql, tuple(args)))}

    @router.get('/fields')
    def field_defs(username: str = Depends(current_user)):
        return {'fields': _rows(bdb.q('SELECT * FROM bid_field_def ORDER BY sort_no, key'))}

    # ---------------- 异步任务 ----------------
    @router.post('/jobs')
    def submit_job(response: Response, body: dict = Body(...),
                   username: str = Depends(current_user)):
        _need_write(need, username)
        jtype = str(body.get('job_type') or '')
        if jtype not in ('run', 'rules_preview'):
            if jtype == 'analyze':
                raise HTTPException(400, '配方自动生成请调用 POST /api/bidcrawl/analyze '
                                         '（同步返回探测阶段与校验报告）')
            raise HTTPException(400, 'job_type 只能是 run / rules_preview')
        pid = body.get('platform_id') or None
        if jtype == 'rules_preview' and not pid:
            raise HTTPException(400, 'rules_preview 必须指定 platform_id')
        # platform_id 是权威来源；全平台任务也不能残留 params.platform，
        # 否则「全部启用平台」实际只跑一个站点
        params = bjobs.normalize_params(pid, body.get('params') or {})
        token = body.get('client_token')
        try:
            bjobs.assert_capacity()
        except bjobs.CapacityError as e:
            raise HTTPException(429, str(e))
        job, dedupe = bjobs.submit(jtype, pid, params, username, token)
        response.status_code = 200 if dedupe else 202
        return {'job_id': job['id'], 'dedupe': dedupe, 'status': job['status'],
                'job': job}

    @router.get('/jobs')
    def list_jobs(status: str = Query(default=''), limit: int = 50,
                  username: str = Depends(current_user)):
        return {'jobs': bjobs.list_jobs(status, limit)}

    @router.get('/jobs/{jid}')
    def get_job(jid: int, username: str = Depends(current_user)):
        job = bjobs.get_job(jid)
        if not job:
            raise HTTPException(404, '任务不存在')
        return job

    @router.get('/jobs/{jid}/events')
    def job_events(jid: int, after_seq: int = 0, username: str = Depends(current_user)):
        job = bjobs.get_job(jid)
        if not job:
            raise HTTPException(404, '任务不存在')
        return {'job': job, 'events': bjobs.events_after(jid, after_seq)}

    @router.post('/jobs/{jid}/cancel')
    def cancel_job(jid: int, username: str = Depends(current_user)):
        _need_write(need, username)
        if not bjobs.get_job(jid):
            raise HTTPException(404, '任务不存在')
        return {'cancelled': bjobs.request_cancel(jid)}

    # ---------------- 字段正则配置 ----------------
    # 普通员工维护「字段 → 匹配词」，由 regexcfg 拼成完整正则；
    # 优先级 配置中心 > 配方 fallback_regex > 内置默认，改完最多 60 秒在 worker 生效。
    def _regex_scope(scope: str) -> str:
        s = str(scope or 'global').strip() or 'global'
        if s == 'global':
            return s
        if s not in {p['id'] for p in brecipe.list_platforms()}:
            raise HTTPException(400, '平台不存在：%s' % s)
        return s

    def _regex_field_key(key: str) -> str:
        k = str(key or '').strip()
        if not bdb.one('SELECT 1 AS x FROM bid_regex_field WHERE field_key=%s', (k,)):
            raise HTTPException(400, '没有这个字段：%s' % (k or '（空）'))
        return k

    @router.get('/regex')
    def regex_list(platform_id: str = '', username: str = Depends(current_user)):
        from .bidcrawl import regexcfg
        fields, errors = regexcfg.describe(platform_id)
        return {'fields': _jsonable(fields), 'errors': errors}

    @router.post('/regex/aliases')
    def regex_alias_add(body: dict = Body(...), username: str = Depends(current_user)):
        _need_write(need, username)
        from .bidcrawl import regexcfg
        key = _regex_field_key(body.get('field_key'))
        alias = str(body.get('alias') or '').strip()
        if not alias:
            raise HTTPException(400, '匹配词不能为空')
        if len(alias) > 40 or '\n' in alias:
            raise HTTPException(400, '匹配词不超过 40 字，且不能含换行')
        scope = _regex_scope(body.get('scope'))
        nxt = bdb.one('SELECT COALESCE(max(sort_no), 0) + 10 AS n FROM bid_regex_alias '
                      'WHERE field_key=%s AND scope=%s', (key, scope))
        # 软删过的同一个词再加回来：恢复原行，避免唯一约束冲突
        row = bdb.one(
            'INSERT INTO bid_regex_alias(field_key, alias, scope, sort_no, enabled) '
            'VALUES (%s,%s,%s,%s,true) '
            'ON CONFLICT (field_key, alias, scope) DO UPDATE '
            'SET deleted_at=NULL, enabled=true, sort_no=EXCLUDED.sort_no, '
            '    updated_at=now() RETURNING *',
            (key, alias, scope, int((nxt or {}).get('n') or 10)))
        bdb.commit()
        regexcfg.invalidate()
        bcfg.audit('regex', row['id'], 'alias_add', None, _jsonable(dict(row)), username)
        return _jsonable(dict(row))

    @router.put('/regex/aliases/{aid}')
    def regex_alias_update(aid: int, body: dict = Body(...),
                           username: str = Depends(current_user)):
        _need_write(need, username)
        from .bidcrawl import regexcfg
        old = bdb.one('SELECT * FROM bid_regex_alias WHERE id=%s AND deleted_at IS NULL', (aid,))
        if not old:
            raise HTTPException(404, '匹配词不存在')
        alias = str(body.get('alias') or old['alias']).strip()
        if not alias or len(alias) > 40 or '\n' in alias:
            raise HTTPException(400, '匹配词不超过 40 字，且不能含换行')
        enabled = old['enabled'] if body.get('enabled') is None else bool(body.get('enabled'))
        sort_no = int(body.get('sort_no') if body.get('sort_no') is not None else old['sort_no'])
        try:
            row = bdb.one(
                'UPDATE bid_regex_alias SET alias=%s, enabled=%s, sort_no=%s, '
                'updated_at=now() WHERE id=%s RETURNING *',
                (alias, enabled, sort_no, aid))
            bdb.commit()
        except Exception as e:                     # noqa: BLE001
            bdb.rollback()
            raise HTTPException(400, '该字段下已有同名匹配词：%s' % alias)
        regexcfg.invalidate()
        bcfg.audit('regex', aid, 'alias_update', _jsonable(dict(old)),
                   _jsonable(dict(row)), username)
        return _jsonable(dict(row))

    @router.delete('/regex/aliases/{aid}')
    def regex_alias_delete(aid: int, username: str = Depends(current_user)):
        _need_write(need, username)
        from .bidcrawl import regexcfg
        old = bdb.one('SELECT * FROM bid_regex_alias WHERE id=%s AND deleted_at IS NULL', (aid,))
        if not old:
            raise HTTPException(404, '匹配词不存在')
        bdb.x('UPDATE bid_regex_alias SET deleted_at=now(), updated_at=now() WHERE id=%s', (aid,))
        bdb.commit()
        regexcfg.invalidate()
        bcfg.audit('regex', aid, 'alias_delete', _jsonable(dict(old)), None, username)
        return {'id': aid, 'deleted': True}

    @router.put('/regex/fields/{key}')
    def regex_field_update(key: str, body: dict = Body(...),
                           username: str = Depends(current_user)):
        """高级入口：改整条模板（含 {alts}）。普通员工用不到，出问题时才好动。"""
        _need_write(need, username)
        from .bidcrawl import regexcfg
        old = bdb.one('SELECT * FROM bid_regex_field WHERE field_key=%s', (key,))
        if not old:
            raise HTTPException(404, '字段不存在')
        tpl = str(body.get('pattern_tpl') or old['pattern_tpl']).strip()
        if '{alts}' in tpl:
            probe = tpl.replace('{alts}', '甲方')
        else:
            probe = tpl
        try:
            re.compile(probe)
        except re.error as e:
            raise HTTPException(400, '模板不是合法正则：%s' % e)
        label = str(body.get('label') or old['label'])[:40]
        enabled = old['enabled'] if body.get('enabled') is None else bool(body.get('enabled'))
        row = bdb.one(
            'UPDATE bid_regex_field SET pattern_tpl=%s, label=%s, enabled=%s, '
            'updated_at=now() WHERE field_key=%s RETURNING *', (tpl, label, enabled, key))
        bdb.commit()
        regexcfg.invalidate()
        bcfg.audit('regex', None, 'field_update', _jsonable(dict(old)),
                   _jsonable(dict(row)), username)
        return _jsonable(dict(row))

    @router.post('/regex/test')
    def regex_test(body: dict = Body(...), username: str = Depends(current_user)):
        """贴一段公告正文，看每个字段现在的配置能匹配到什么（忽略缓存，改完立刻可见）。"""
        from .bidcrawl import regexcfg
        text = str(body.get('text') or '')
        pid = str(body.get('platform_id') or '')
        if not text.strip():
            raise HTTPException(400, '请粘贴一段公告正文')
        rules = regexcfg.load_rules(pid, force=True)
        fields = {r['field_key']: r for r in bdb.q(
            'SELECT field_key, label FROM bid_regex_field')}
        out = []
        for k, pat in rules.items():
            val = ''
            try:
                m = re.search(pat, text, re.M)
                if m:
                    val = (m.group(1) if m.lastindex else m.group(0)).strip()
            except re.error:
                pass
            out.append({'field_key': k,
                        'label': (fields.get(k) or {}).get('label') or k,
                        'value': val, 'regex': pat})
        out.sort(key=lambda x: (0 if x['value'] else 1, x['field_key']))
        return {'matched': sum(1 for x in out if x['value']), 'total': len(out),
                'fields': out}

    # ---------------- 结果查询 ----------------
    @router.get('/items')
    def list_items(platform_id: str = '', status: str = '', q: str = '',
                   limit: int = 100, offset: int = 0, project: int = 1,
                   username: str = Depends(current_user)):
        where = ['1=1']
        args = []
        if platform_id:
            where.append('platform_id=%s')
            args.append(platform_id)
        if status:
            where.append('status=%s')
            args.append(status)
        if q:
            where.append('(title ILIKE %s OR buyer ILIKE %s)')
            args.extend(['%' + q + '%', '%' + q + '%'])
        cond = ' AND '.join(where)
        rows = bdb.q('SELECT * FROM bid_item WHERE ' + cond +
                     ' ORDER BY COALESCE(publish_at, first_seen_at) DESC '
                     'LIMIT %s OFFSET %s',
                     tuple(args + [max(1, min(500, limit)), max(0, offset)]))
        total = bdb.one('SELECT count(*) AS c FROM bid_item WHERE ' + cond, tuple(args))
        items = _rows(rows)
        if project:
            # 把每条公告翻译成台账那套字段（projectName/bidNo/owner/deadline…），
            # 取不到的留空。前端「报名」直接把这份 payload 交给台账。
            done = _enrolled_urls()
            for it in items:
                proj = enroll_mod.to_project(it)
                it['project'] = proj
                it['enrolled'] = bool(proj.get('noticeUrl')
                                      and proj['noticeUrl'] in done)
        return {'total': int((total or {}).get('c') or 0), 'items': items}

    @router.get('/items/{item_id}/project')
    def item_project(item_id: int, username: str = Depends(current_user)):
        """单条公告 → 台账字段（报名弹窗预览 / 手工核对用）。"""
        row = bdb.one('SELECT * FROM bid_item WHERE id=%s', (item_id,))
        if not row:
            raise HTTPException(404, '标讯不存在')
        it = _jsonable(dict(row))
        return {'project': enroll_mod.to_project(it),
                'source': {'id': item_id, 'title': it.get('title'),
                           'url': it.get('url')}}

    @router.post('/items/{item_id}/enroll')
    def enroll_item(item_id: int, body: dict = Body(default={}),
                    username: str = Depends(current_user)):
        """报名：把这条公告写进投标管理（台账）。

        body.overwrite=true 时，已存在则用最新解析结果覆盖（缺字段不动空值）。
        """
        need(username, 'bid.edit', '无台账新增权限')
        row = bdb.one('SELECT * FROM bid_item WHERE id=%s', (item_id,))
        if not row:
            raise HTTPException(404, '标讯不存在')
        it = _jsonable(dict(row))
        proj = enroll_mod.to_project(it)
        # 允许前端在弹窗里改过的字段覆盖解析值（与解析值同 key）
        for k in enroll_mod.PROJECT_FIELDS:
            v = (body.get('project') or {}).get(k)
            if isinstance(v, str) and v.strip():
                proj[k] = v.strip()
        if not proj.get('projectName'):
            raise HTTPException(400, '这条标讯没有项目名称，无法报名')
        out = ctx['enroll_project'](username, proj, bool(body.get('overwrite')))
        _enrolled_cache['at'] = 0          # 让下次列表立刻看到「已报名」
        out['source'] = {'id': item_id, 'url': it.get('url'),
                         'title': it.get('title')}
        return out

    return router


# ------------------------------------------------------------------ 定时任务
# 单独一个 router：不继承 /api/bidcrawl 的 view.bidcrawl 依赖，
# 定时任务由独立的 view.schedule / schedule.* 四个权限点控制，
# 未被授权的角色即使能进采集页也看不到这一块。
def build_schedule_router(current_user, ctx):
    from .bidcrawl import scheduler as bsched

    need = ctx['need']
    router = APIRouter(prefix='/api/bidcrawl', tags=['bidcrawl-schedule'])

    def _need(username, key, msg):
        need(username, key, msg)

    def _view(row) -> dict:
        d = _jsonable(dict(row))
        d['next_run_at'] = _jsonable(bsched.next_run_at(dict(row)))
        d['platforms'] = [str(x) for x in (row.get('platforms') or [])]
        d['keywords'] = [str(x) for x in (row.get('keywords') or [])]
        return d

    def _clean(body: dict) -> tuple:
        try:
            run_at = bsched.norm_hhmm(body.get('run_at'))
        except ValueError as e:
            raise HTTPException(400, str(e))
        name = str(body.get('name') or '').strip()[:60] or ('每日 %s' % run_at)
        plats = [str(x).strip() for x in (body.get('platforms') or [])
                 if str(x).strip()]
        if plats:
            valid = {p['id'] for p in brecipe.list_platforms()}
            bad = [x for x in plats if x not in valid]
            if bad:
                raise HTTPException(400, '平台不存在：%s' % '、'.join(bad))
        words = [str(x).strip() for x in (body.get('keywords') or [])
                 if str(x).strip()]
        enabled = body.get('enabled')
        enabled = True if enabled is None else bool(enabled)
        return name, run_at, plats, words, enabled

    @router.get('/schedules')
    def list_schedules(username: str = Depends(current_user)):
        _need(username, 'view.schedule', '无权查看定时任务')
        rows = bdb.q('SELECT * FROM bid_crawl_schedule ORDER BY run_at, id')
        return {'schedules': [_view(r) for r in rows],
                'server_time': _jsonable(datetime.now())}

    @router.post('/schedules')
    def create_schedule(body: dict = Body(...), username: str = Depends(current_user)):
        _need(username, 'schedule.create', '无权新建定时任务')
        name, run_at, plats, words, enabled = _clean(body)
        lrd = bsched.initial_last_run_date(run_at)
        row = bdb.one(
            'INSERT INTO bid_crawl_schedule(name, run_at, platforms, keywords, '
            'enabled, created_by, updated_by, last_run_date) '
            'VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s) RETURNING *',
            (name, run_at, json.dumps(plats), json.dumps(words), enabled,
             username, username, lrd))
        bdb.commit()
        bcfg.audit('schedule', row['id'], 'create', None, _jsonable(dict(row)), username)
        return _view(row)

    @router.put('/schedules/{sid}')
    def update_schedule(sid: int, body: dict = Body(...),
                        username: str = Depends(current_user)):
        _need(username, 'schedule.update', '无权修改定时任务')
        old = bdb.one('SELECT * FROM bid_crawl_schedule WHERE id=%s', (sid,))
        if not old:
            raise HTTPException(404, '定时任务不存在')
        name, run_at, plats, words, enabled = _clean(body)
        # 只有改了执行时间才重算「今天是否已跑」：改个名字不该清掉执行状态
        lrd = old.get('last_run_date')
        if str(old.get('run_at')) != run_at:
            lrd = bsched.initial_last_run_date(run_at)
        row = bdb.one(
            'UPDATE bid_crawl_schedule SET name=%s, run_at=%s, platforms=%s::jsonb, '
            'keywords=%s::jsonb, enabled=%s, updated_by=%s, updated_at=now(), '
            'last_run_date=%s WHERE id=%s RETURNING *',
            (name, run_at, json.dumps(plats), json.dumps(words), enabled,
             username, lrd, sid))
        bdb.commit()
        bcfg.audit('schedule', sid, 'update', _jsonable(dict(old)),
                   _jsonable(dict(row)), username)
        return _view(row)

    @router.delete('/schedules/{sid}')
    def delete_schedule(sid: int, username: str = Depends(current_user)):
        _need(username, 'schedule.delete', '无权删除定时任务')
        old = bdb.one('SELECT * FROM bid_crawl_schedule WHERE id=%s', (sid,))
        if not old:
            raise HTTPException(404, '定时任务不存在')
        bdb.x('DELETE FROM bid_crawl_schedule WHERE id=%s', (sid,))
        bdb.commit()
        bcfg.audit('schedule', sid, 'delete', _jsonable(dict(old)), None, username)
        return {'id': sid, 'deleted': True}

    @router.post('/schedules/{sid}/run')
    def run_schedule(sid: int, body: dict = Body(default=None),
                     username: str = Depends(current_user)):
        """立即执行一次（用于验证配置）。不影响当天定时执行。"""
        _need(username, 'schedule.update', '无权执行定时任务')
        plats = None
        if body and body.get('platforms'):
            plats = [str(x).strip() for x in body['platforms'] if str(x).strip()]
        res = bsched.run_now(sid, username, platforms=plats)
        if not res.get('ok'):
            raise HTTPException(400, res.get('error') or '提交失败')
        bcfg.audit('schedule', sid, 'run', None, res, username)
        return res

    return router
