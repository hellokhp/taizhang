# -*- coding: utf-8 -*-
"""开标汇总 AI · 后端接口模块（Python 3.8 / FastAPI）。

职责边界：本模块只做「鉴权 + 写 PENDING 任务 + 读结果 + 人工复核」，
绝不在这里做任何 PDF 解析 / 模型调用 —— 重活全部在独立 Worker 进程。

注入方式（参照 fs_api.build_router）：
    app.include_router(ai_summary_api.build_router(current_user, ctx))
ctx 由 main.py 在 SPA 兜底之前构造，包含 db/lock/need/perm_of/... 等依赖，
避免本模块反向 import main 造成循环引用。

SQL 一律用 `?` 占位符（PgDB 会转成 %s）。Worker 侧独立用 %s，两套不混用。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import ai_signal
from . import ai_project_index

# ------------------------------------------------------------------ DDL
# 统一 DDL 唯一事实源：后端 3.8 与 Worker 3.10 共用 server/ai_schema.sql。
# 两个进程不能共享 Python 模块，但纯 SQL 可以共用，避免「Worker 先启动就没表」。
AI_DDL = (Path(__file__).with_name('ai_schema.sql')).read_text(encoding='utf-8')

# 老库升级要补的列（PG 无 ADD COLUMN IF NOT EXISTS，逐条 try）
AI_ALTER_COLS = (
    ('ai_summary_task', 'prompt_version', "TEXT DEFAULT ''"),
    ('ai_summary_task', 'summary_id', 'BIGINT'),
    ('ai_summary', 'raw_result', "JSONB DEFAULT '{}'"),
    ('ai_summary', 'prompt_version', "TEXT DEFAULT ''"),
    ('ai_prompt_fewshot', 'is_regression', 'SMALLINT DEFAULT 0'),
)

DEFAULT_TEMPLATES = {
    'PROJECT': [
        {'key': 'project_name', 'label': '项目名称', 'type': 'text', 'required': True},
        {'key': 'project_code', 'label': '项目编号', 'type': 'text', 'required': True},
        {'key': 'tenderer', 'label': '招标人（采购人）', 'type': 'text'},
        {'key': 'agency', 'label': '招标代理机构', 'type': 'text'},
        {'key': 'open_time', 'label': '开标时间', 'type': 'text'},
        {'key': 'open_place', 'label': '开标地点', 'type': 'text'},
        {'key': 'budget', 'label': '预算/最高限价（元）', 'type': 'money', 'ledgerField': 'budget'},
        {'key': 'eval_method', 'label': '评标办法', 'type': 'text'},
    ],
    'BIDDER': [
        {'key': 'bidder_name', 'label': '投标人名称', 'type': 'text', 'required': True},
        {'key': 'bid_price', 'label': '投标报价（元）', 'type': 'money', 'required': True, 'ledgerField': 'ourPrice'},
        {'key': 'price_in_words', 'label': '报价大写', 'type': 'text'},
        {'key': 'duration', 'label': '工期/服务期', 'type': 'text'},
        {'key': 'quality', 'label': '质量标准', 'type': 'text'},
        {'key': 'deposit', 'label': '投标保证金（元）', 'type': 'money', 'ledgerField': 'bondAmount'},
        {'key': 'legal_person', 'label': '法定代表人', 'type': 'text'},
        {'key': 'authorized_rep', 'label': '授权代表/联系人', 'type': 'text'},
        {'key': 'contact', 'label': '联系方式', 'type': 'text'},
        {'key': 'qualification', 'label': '资格响应', 'type': 'text'},
        {'key': 'reject_reason', 'label': '废标/否决项', 'type': 'text'},
        {'key': 'remark', 'label': '备注', 'type': 'text'},
    ],
}

# AI 字段 -> 台账字段四档映射（价格类列要跟随台账的 hidden/masked）
LEDGER_FIELD_MAP = {
    'budget': 'budget', 'bid_price': 'ourPrice', 'incl_total': 'ourPrice',
    'total_net': 'ourPrice', 'deposit': 'bondAmount', 'price_score': 'result',
}

TASK_FIELDS = ('id', 'rid', 'status', 'stage', 'stage_label', 'progress',
               'total_files', 'parsed_files', 'failed_files', 'skipped_files',
               'current_file', 'mode', 'summary_id', 'prompt_version', 'error_msg',
               'created_at', 'started_at', 'finished_at', 'updated_at', 'created_by')


# ------------------------------------------------------------------ 字段体系 v2：布局合并
# 约定（docs/开标价格汇总字段体系_优化方案.md v2 §5）：
#   · pref 只存 diff，绝不整份拷 spec；系统布局升版后用户覆盖自动继承新列。
#   · 锚点列 locked，任何用户偏好都不能隐藏，否则「开标结果」失去意义。
#   · validate 与写入共用同一套规则，避免「存得进去、读不出来」。
#   · 校验失败一律回退基线 + layoutWarning，绝不 500 / 白屏。
LAYOUT_BASELINE = 'result.ranking.v1'
ANCHOR_KEYS = ('rank', 'bidder_name')
KEY_RE = re.compile(r'^[A-Za-z0-9_:.{}()\-]+$')
DYNAMIC_KEY_RE = re.compile(r'^(cat|part):\d+:[A-Za-z0-9_]+$')
EXPR_OPS = frozenset(('add', 'sub', 'mul', 'div', 'round', 'min', 'max',
                      'ifnull', 'row_ref', 'field'))
MAX_SPEC_COLUMNS = 80
CONTRACT_PERM_TO_LEDGER = {
    'ourPrice': 'ourPrice', 'result': 'result', 'budget': 'budget',
    'bondAmount': 'bondAmount',
}


def _as_json(v, default):
    """PG JSONB 经 psycopg2 返回 dict/list；老库或手工插入可能是字符串。"""
    if v is None:
        return copy.deepcopy(default)
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return copy.deepcopy(default)


def validate_spec(spec):
    """系统布局 spec 校验：返回 (ok, reason)。只验结构，不验数值。"""
    if not isinstance(spec, dict):
        return False, 'spec 必须是对象'
    groups = spec.get('column_groups')
    if not isinstance(groups, list) or not groups:
        return False, 'column_groups 必须是非空数组'
    seen, total = set(), 0
    for g in groups:
        if not isinstance(g, dict):
            return False, 'column_groups 元素必须是对象'
        cols = g.get('columns')
        if cols is not None:
            if not isinstance(cols, list):
                return False, 'columns 必须是数组'
            for c in cols:
                if not isinstance(c, dict) or not c.get('key'):
                    return False, '列缺少 key'
                k = str(c['key'])
                if not KEY_RE.match(k):
                    return False, '非法列 key：' + k
                if k in seen:
                    return False, '重复列 key：' + k
                if len(str(c.get('label', ''))) > 80:
                    return False, '列 label 过长：' + k
                seen.add(k)
                total += 1
        rep = g.get('repeat')
        if rep is not None:
            if not isinstance(rep, dict):
                return False, 'repeat 必须是对象'
            if rep.get('axis') not in ('category', 'part', 'metric'):
                return False, 'repeat.axis 非法'
            if not str(rep.get('from', '')).startswith('$'):
                return False, 'repeat.from 必须是 JSONPath'
            if not isinstance(rep.get('columns'), list):
                return False, 'repeat.columns 必须是数组'
            for m in rep['columns']:
                if not isinstance(m, str) or not KEY_RE.match(m):
                    return False, 'repeat.columns 含非法 metric'
            total += len(rep['columns']) * 8   # 动态列按最多 8 组估算
    if total > MAX_SPEC_COLUMNS:
        return False, '列数超过上限 %d' % MAX_SPEC_COLUMNS
    if not (set(ANCHOR_KEYS) & seen):
        return False, '缺少锚点列 rank / bidder_name'
    return True, ''


def _valid_key(k, contracts):
    return (isinstance(k, str) and KEY_RE.match(k)
            and (k in contracts or DYNAMIC_KEY_RE.match(k) or k.startswith('u:')))


def _validate_expr(e, depth=0):
    """C 类自造列：白名单 AST，禁止 eval / 任意代码字符串。"""
    if depth > 8:
        return False, '表达式嵌套过深'
    if e is None or isinstance(e, (int, float, bool)):
        return True, ''
    if isinstance(e, str):
        return (len(e) <= 200, '表达式操作数过长')
    if isinstance(e, list):
        for x in e:
            ok, why = _validate_expr(x, depth + 1)
            if not ok:
                return False, why
        return True, ''
    if isinstance(e, dict):
        op = e.get('op')
        if op not in EXPR_OPS:
            return False, '不允许的算子：' + str(op)
        for k, v in e.items():
            if k == 'op':
                continue
            if k not in ('a', 'b', 'args', 'key', 'rank', 'value', 'ndigits', 'path'):
                return False, '不允许的表达式字段：' + str(k)
            ok, why = _validate_expr(v, depth + 1)
            if not ok:
                return False, why
        return True, ''
    return False, '表达式含非法类型'


def validate_overrides(ov, contracts):
    """用户/项目 diff 校验：返回 (清洗后的 ov, warnings)。宽松但不放行脏数据。"""
    if not isinstance(ov, dict):
        return {}, ['overrides 必须是对象']
    out, warn = {}, []
    hidden = [k for k in (ov.get('hidden') or []) if _valid_key(k, contracts)]
    dropped = [k for k in hidden if k in ANCHOR_KEYS]
    if dropped:
        warn.append('锚点列不可隐藏，已忽略：' + ', '.join(dropped))
    hidden = [k for k in hidden if k not in ANCHOR_KEYS]
    invalid = [k for k in (ov.get('hidden') or []) if not _valid_key(k, contracts)]
    if invalid:
        warn.append('已丢弃未知列：' + ', '.join(map(str, invalid[:8])))
    if hidden:
        out['hidden'] = list(dict.fromkeys(hidden))
    for key, caster, checker in (
        ('order', list, lambda v: all(_valid_key(k, contracts) for k in v)),
        ('rules_panel', list, lambda v: all(isinstance(x, str) and len(x) <= 80 for x in v)),
    ):
        v = ov.get(key)
        if isinstance(v, caster) and checker(v):
            out[key] = v[:MAX_SPEC_COLUMNS]
    for key in ('labels', 'widths', 'groups', 'axis_filter', 'side_tables'):
        v = ov.get(key)
        if isinstance(v, dict):
            if key == 'labels':
                v = {k: str(x)[:80] for k, x in v.items() if _valid_key(k, contracts)}
            elif key == 'widths':
                v = {k: max(40, min(600, int(x))) for k, x in v.items()
                     if _valid_key(k, contracts) and str(x).isdigit()}
            elif key == 'groups':
                v = {k: str(x)[:80] for k, x in v.items() if _valid_key(k, contracts)}
            if v:
                out[key] = v
    return out, warn


def validate_custom_columns(cols, contracts):
    out, warn = [], []
    if not isinstance(cols, list):
        return out, ['custom_columns 必须是数组']
    for c in cols[:20]:
        if not isinstance(c, dict):
            warn.append('自造列格式错误，已跳过')
            continue
        k = str(c.get('key') or '')
        if not k.startswith('u:') or not KEY_RE.match(k):
            warn.append('自造列 key 必须以 u: 开头：' + k)
            continue
        ok, why = _validate_expr(c.get('expr'))
        if not ok:
            warn.append('自造列 %s 表达式不合法：%s' % (k, why))
            continue
        out.append({'key': k, 'label': str(c.get('label') or k)[:60],
                    'dtype': str(c.get('dtype') or 'num')[:16],
                    'expr': c.get('expr'),
                    'perm_group': str(c.get('perm_group') or '')[:32]})
    return out, warn


def merge_overrides(base_ov, add_ov):
    """高优先级 diff 叠加；hidden 取并集，其余按 key 覆盖。"""
    out = dict(base_ov or {})
    for k, v in (add_ov or {}).items():
        if k == 'hidden':
            out[k] = list(dict.fromkeys((out.get(k) or []) + list(v)))
        elif k in ('labels', 'widths', 'groups', 'side_tables', 'axis_filter') and isinstance(v, dict):
            out[k] = {**(out.get(k) or {}), **v}
        else:
            out[k] = v
    return out


def flatten_columns(spec, contracts, ov):
    """把 column_groups 展平成前端可直接渲染的列描述（repeat 保留为动态组）。"""
    cols, dynamic = [], []
    for g in spec.get('column_groups') or []:
        label = str(g.get('label') or '')
        for c in g.get('columns') or []:
            k = str(c.get('key'))
            if k in (ov.get('hidden') or []) and not c.get('locked'):
                continue
            ct = contracts.get(str(c.get('contract') or k), {})
            cols.append({
                'key': k, 'label': (ov.get('labels') or {}).get(k) or c.get('label') or ct.get('label') or k,
                'dtype': ct.get('dtype') or 'text', 'formatter': ct.get('formatter') or '',
                'contract': str(c.get('contract') or k),
                'permGroup': ct.get('perm_group') or '', 'group': (ov.get('groups') or {}).get(k) or label,
                'locked': bool(c.get('locked')) or k in ANCHOR_KEYS,
                'width': (ov.get('widths') or {}).get(k),
            })
        if g.get('repeat'):
            dynamic.append(dict(g['repeat'], group=label, id=g.get('id') or 'dynamic'))
    order = ov.get('order') or []
    if order:
        pos = {k: i for i, k in enumerate(order)}
        cols.sort(key=lambda c: pos.get(c['key'], 10 ** 6))
    return cols, dynamic


def init_tables(dbobj):
    """幂等建表 + 老库补列 + 默认字段字典。由 main.py 在 fs 初始化之后调用。"""
    dbobj.executescript(AI_DDL)
    for table, col, typ in AI_ALTER_COLS:
        try:
            dbobj.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, typ))
            dbobj.commit()
        except Exception:
            dbobj.rollback()
    for scope, fields in DEFAULT_TEMPLATES.items():
        try:
            dbobj.execute('INSERT INTO ai_field_template(scope, fields, is_default, updated_by, updated_at) '
                          "VALUES(?,?,1,'system',?) ON CONFLICT(scope) DO NOTHING",
                          (scope, json.dumps(fields, ensure_ascii=False), time.time()))
            dbobj.commit()
        except Exception:
            dbobj.rollback()
    return True


# ------------------------------------------------------------------ 共享 service（D5 接缝）
def _collect_source_nodes(db, tender_node_id, include_bid=False):
    """从 fs_node 收集「招标文件」子树里的实体文件，算出物化清单与内容指纹。

    返回 (nodes, fingerprint)；nodes 每项含 node_id/rel_path/name/ext/size/sha256，
    Worker 凭这份快照即可物化，无需再 import 台账的 fs 模块。
    """
    if not tender_node_id:
        return [], ''
    allnodes = db.execute('SELECT id, parent_id, name, kind, deleted, size FROM fs_node '
                          'WHERE id=?', (tender_node_id,)).fetchall()
    if not allnodes:
        return [], ''
    # 一次性取全树，避免递归查询
    rows = db.execute(
        'WITH RECURSIVE t AS ('
        '  SELECT id, parent_id, name, kind, deleted, size, 1 AS lvl FROM fs_node WHERE id=? '
        '  UNION ALL '
        '  SELECT n.id, n.parent_id, n.name, n.kind, n.deleted, n.size, t.lvl+1 '
        '  FROM fs_node n JOIN t ON n.parent_id=t.id WHERE t.lvl < 12'
        ') SELECT * FROM t', (tender_node_id,)).fetchall()
    by_id = {r['id']: r for r in rows}
    root_path = by_id[tender_node_id]['name'] if tender_node_id in by_id else ''
    out = []
    for r in rows:
        if r['kind'] != 'file' or r['deleted']:
            continue
        if r['id'] == tender_node_id:
            continue
        f = db.execute('SELECT blob_sha, size FROM fs_file WHERE node_id=?', (r['id'],)).fetchone()
        if not f or not f['blob_sha']:
            continue
        # 相对路径：从当前节点回溯到根，跳过根本身
        parts, cur, guard = [], r, 0
        while cur and guard < 12:
            parts.append(cur['name'])
            pid = cur['parent_id']
            if not pid or pid == tender_node_id:
                break
            cur = by_id.get(pid)
            guard += 1
        parts.reverse()
        rel = '/'.join(p for p in parts if p)
        name = r['name']
        ext = os.path.splitext(name)[1].lower()
        out.append({'node_id': r['id'], 'rel_path': rel, 'name': name, 'ext': ext,
                    'size': int(f['size'] or 0), 'sha256': f['blob_sha']})
    out.sort(key=lambda x: x['rel_path'])
    raw = '|'.join('%s|%s' % (x['sha256'], x['rel_path']) for x in out)
    fp = hashlib.md5(raw.encode('utf-8')).hexdigest() if raw else ''
    return out, fp


def create_summary_task(db, lock, rid, *, force_refresh=False, created_by='',
                        include_bid=False, source='manual', wake=None):
    """【共享接缝】建（或复用）一条开标汇总任务。路由与将来的定时器都只调这里。

    只做轻量 DB 读写 + 一次 Redis 唤醒，绝不等待 Worker、绝不解析文件。
    返回 {task_id, reused, status, summary_id, fingerprint, total_files}。
    """
    if not rid:
        raise ValueError('缺少项目 rid')
    with lock:
        m = db.execute('SELECT tender_node_id, bid_node_id FROM bid_fs_dir WHERE rid=?', (rid,)).fetchone()
        if not m or not m['tender_node_id']:
            raise ValueError('该项目尚未建立「招标文件」目录，请先在投标管理中打开该项目或上传招标文件')
        nodes, fp = _collect_source_nodes(db, m['tender_node_id'], include_bid)
        if not nodes:
            raise ValueError('「招标文件」目录里没有可解析的文件，请先上传招标文件')
        if not force_refresh:
            old = db.execute(
                "SELECT id, summary_id FROM ai_summary_task WHERE rid=? AND fingerprint=? "
                "AND status IN ('DONE','PARTIAL') ORDER BY id DESC LIMIT 1",
                (rid, fp)).fetchone()
            if old:
                return {'task_id': int(old['id']), 'reused': True, 'status': 'DONE',
                        'summary_id': int(old['summary_id']) if old['summary_id'] else None,
                        'fingerprint': fp, 'total_files': len(nodes)}
        now = time.time()
        cur = db.execute(
            'INSERT INTO ai_summary_task(rid, source_nodes, source_kind, fingerprint, '
            'force_refresh, status, stage, stage_label, progress, total_files, '
            'updated_at, created_by, created_at) '
            "VALUES(?,?,'tender',?,?, 'PENDING','QUEUED','等待 Worker 领取',0,?, ?,?,?) RETURNING id",
            (rid, json.dumps(nodes, ensure_ascii=False), fp, 1 if force_refresh else 0,
             len(nodes), now, created_by or source, now))
        row = cur.fetchone()
        task_id = int(row['id'])
        db.execute('INSERT INTO ai_task_event(task_id, seq, stage, progress, payload, created_at) '
                   "VALUES(?,1,'QUEUED',0,?,?)",
                   (task_id, json.dumps({'total_files': len(nodes)}, ensure_ascii=False), now))
        db.commit()
    if wake is None:
        wake = ai_signal.wake
    try:
        wake(task_id)          # 失败由 ai_signal 内部静默降级
    except Exception:
        pass
    return {'task_id': task_id, 'reused': False, 'status': 'PENDING',
            'summary_id': None, 'fingerprint': fp, 'total_files': len(nodes)}


# ------------------------------------------------------------------ 路由
class GenerateIn(BaseModel):
    rid: str
    force_refresh: bool = False
    # v1 固定 False，前端不暴露；预留给未来"包含标书"
    include_bid: bool = False


class CellIn(BaseModel):
    entity: str = 'BIDDER'          # BIDDER / PROJECT
    entity_id: int = 0
    field_key: str
    value: str = ''
    reason: str = ''


class ConfirmIn(BaseModel):
    write_back: bool = False


class ToggleIn(BaseModel):
    enabled: bool = True


def build_router(current_user, ctx):
    """ctx: dict(db, lock, need, perm_of, row_in_scope, self_names, audit_write,
                 field_level, mask_value, fs)"""
    db = ctx['db']
    lock = ctx['lock']
    need = ctx['need']
    perm_of = ctx['perm_of']
    row_in_scope = ctx['row_in_scope']
    self_names = ctx['self_names']
    audit_write = ctx['audit_write']
    field_level = ctx['field_level']
    mask_value = ctx['mask_value']
    router = APIRouter(prefix='/api/ai-summary', tags=['ai-summary'])

    # 项目选择器索引：进程级单例（supervisor --workers 1），指纹检测台账/目录变化后重建。
    # 只缓存搜索必需字段（rid/项目名/人员/目录标记），不缓存完整 payload。
    # 人员字段沿用 main.PERSON_FIELDS，避免 SELF 行级过滤口径漂移。
    project_index = ai_project_index.ProjectIndex(
        db, lock, person_fields=ctx.get('person_fields', ('lead', 'copilot', 'reviewer')))

    def _payload(rid):
        r = db.execute('SELECT payload FROM rows WHERE rid=?', (rid,)).fetchone()
        if not r:
            raise HTTPException(404, '台账中没有该项目')
        try:
            return json.loads(r['payload'] or '{}')
        except Exception:
            return {}

    def _check_rid(username, rid, write=False):
        p = perm_of(username)
        if write and p['dataScope'] != 'ALL':
            if not row_in_scope(_payload(rid), p['dataScope'], self_names(username)):
                raise HTTPException(403, '该项目不在你的数据范围内')
        elif not row_in_scope(_payload(rid), p['dataScope'], self_names(username)):
            raise HTTPException(403, '该项目不在你的数据范围内')
        return p

    def _task_or_404(task_id):
        r = db.execute('SELECT * FROM ai_summary_task WHERE id=?', (task_id,)).fetchone()
        if not r:
            raise HTTPException(404, '任务不存在')
        return r

    def _task_view(r):
        return {k: (float(r[k]) if k in ('created_at', 'started_at', 'finished_at', 'updated_at')
                    and r[k] is not None else r[k]) for k in TASK_FIELDS}

    def _visible(username, rid):
        """SSE / 结果读取统一走这里：既校验页面权限，也校验 dataScope。"""
        need(username, 'view.aiSummary')
        return _check_rid(username, rid)

    def _safe_col(username, key, val):
        """价格类列跟随台账字段四档：hidden 整列不返回，masked 返回脱敏值。"""
        lk = LEDGER_FIELD_MAP.get(key)
        if not lk:
            return True, val
        mode = field_level(perm_of(username)['fieldPerms'], lk)
        if mode == 'hidden':
            return False, None
        if mode == 'masked':
            rule = perm_of(username)['fieldPerms'].get(lk) or {}
            return True, mask_value(val, rule.get('mask', 'full'))
        return True, val

    # ---------------- 生成 ----------------
    @router.post('/generate')
    def generate(body: GenerateIn, username: str = Depends(current_user)):
        need(username, 'ai.generate')
        _check_rid(username, body.rid, write=True)
        try:
            out = create_summary_task(db, lock, body.rid,
                                      force_refresh=body.force_refresh,
                                      created_by=username,
                                      include_bid=False)   # v1 固定只取招标文件
        except ValueError as e:
            raise HTTPException(400, str(e))
        with lock:
            audit_write([(username, body.rid, 'ai.generate', 'openingSummary',
                          '', 'task:%s' % out['task_id'], True)])
            db.commit()
        return out

    # ---------------- 任务 ----------------
    @router.get('/tasks')
    def list_tasks(rid: str = '', status: str = '', limit: int = Query(30, le=200),
                   username: str = Depends(current_user)):
        need(username, 'view.aiSummary')
        sql, args = 'SELECT * FROM ai_summary_task WHERE 1=1', []
        if rid:
            sql += ' AND rid=?'
            args.append(rid)
        if status:
            sql += ' AND status=?'
            args.append(status)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        with lock:
            rows = db.execute(sql, args).fetchall()
        return {'tasks': [_task_view(r) for r in rows
                          if row_in_scope(_payload(r['rid']), perm_of(username)['dataScope'],
                                          self_names(username))]}

    @router.get('/tasks/{task_id}')
    def get_task(task_id: int, username: str = Depends(current_user)):
        with lock:
            r = _task_or_404(task_id)
        _visible(username, r['rid'])
        return _task_view(r)

    @router.get('/tasks/{task_id}/events')
    def task_events(task_id: int, last_seq: int = 0,
                    username: str = Depends(current_user)):
        """SSE 断线续传：从 PG 补事件（PG 是事实源，Redis 只做快通知）。"""
        with lock:
            t = _task_or_404(task_id)
            rows = db.execute('SELECT seq, stage, progress, payload, created_at FROM ai_task_event '
                              'WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200',
                              (task_id, last_seq)).fetchall()
        _visible(username, t['rid'])
        return {'events': [dict(r) for r in rows]}

    def _sse(data, event='message', eid=None):
        head = ('id: %s\n' % eid) if eid else ''
        return head + 'event: %s\ndata: %s\n\n' % (event, json.dumps(data, ensure_ascii=False, default=str))

    @router.get('/tasks/{task_id}/stream')
    async def task_stream(task_id: int, token: str = '', last_seq: int = 0):
        """SSE 实时进度。EventSource 不能带 Authorization 头，故用 ?token= 传会话令牌。

        - Redis 可用：async PubSub 订阅（绝不阻塞 event loop），PG 事件做断线续传。
        - Redis 不可用：推 degraded，退化为服务端 3 秒轮询读 ai_summary_task。
        """
        # 令牌校验：与 current_user 同源，但不走 Header（EventSource 限制）
        if not token:
            raise HTTPException(401, '未登录')
        with lock:
            s = db.execute('SELECT username FROM sessions WHERE token=?', (token,)).fetchone()
            t = db.execute('SELECT rid FROM ai_summary_task WHERE id=?', (task_id,)).fetchone()
        if not s:
            raise HTTPException(401, '登录已过期，请重新登录')
        if not t:
            raise HTTPException(404, '任务不存在')
        username = s['username']
        need(username, 'view.aiSummary')
        _check_rid(username, t['rid'])

        async def _load_state():
            def _q():
                with lock:
                    r = db.execute('SELECT * FROM ai_summary_task WHERE id=?', (task_id,)).fetchone()
                    evs = db.execute('SELECT seq, stage, progress, payload FROM ai_task_event '
                                     'WHERE task_id=? AND seq>? ORDER BY seq LIMIT 50',
                                     (task_id, last_seq)).fetchall() if last_seq else []
                return (dict(r) if r else None, [dict(e) for e in evs])
            return await run_in_threadpool(_q)

        async def gen():
            redis_ok = await run_in_threadpool(ai_signal.is_available)
            if not redis_ok:
                yield _sse({'type': 'degraded', 'reason': 'redis_unavailable'})
            seq = last_seq
            state, evs = await _load_state()
            for e in evs:
                seq = max(seq, e['seq'])
                yield _sse({'type': 'progress', **e}, eid=e['seq'])
            if state:
                yield _sse({'type': 'state', **_task_view(state)})

            ps = None
            if redis_ok:
                sub = await ai_signal.subscribe(task_id)
                if sub:
                    ps = sub[0]
            deadline = time.time() + 1800          # 30 分钟硬上限，防止连接悬挂
            last_ping = time.time()
            terminal = ('DONE', 'PARTIAL', 'FAILED', 'CANCELED')
            try:
                while time.time() < deadline:
                    if ps:
                        msg = await ai_signal.next_message(ps, timeout=1.0)
                        if msg:
                            seq = max(seq, int(msg.get('seq') or seq))
                            yield _sse({'type': 'progress', **msg}, eid=seq)
                    else:
                        await asyncio.sleep(3)
                    # 每轮都回查 PG：Redis 事件可丢，PG 才是事实源
                    state, _ = await _load_state()
                    if state:
                        yield _sse({'type': 'state', **_task_view(state)})
                        if state['status'] in terminal:
                            break
                    if time.time() - last_ping >= 15:
                        yield ': ping\n\n'
                        last_ping = time.time()
            finally:
                if ps:
                    await ai_signal.unsubscribe(ps)

        return StreamingResponse(gen(), media_type='text/event-stream', headers={
            'Cache-Control': 'no-cache', 'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        })

    @router.post('/tasks/{task_id}/cancel')
    def cancel_task(task_id: int, username: str = Depends(current_user)):
        need(username, 'ai.generate')
        with lock:
            r = _task_or_404(task_id)
            if r['status'] in ('DONE', 'PARTIAL', 'FAILED', 'CANCELED'):
                return {'ok': True, 'status': r['status']}
            db.execute('UPDATE ai_summary_task SET cancel_requested=1, updated_at=? WHERE id=?',
                       (time.time(), task_id))
            db.commit()
        ai_signal.publish_progress(task_id, {'stage': 'CANCEL_REQUESTED', 'progress': r['progress']})
        return {'ok': True, 'status': 'CANCEL_REQUESTED'}

    @router.post('/tasks/{task_id}/retry')
    def retry_task(task_id: int, body: dict = None, username: str = Depends(current_user)):
        """失败文件重跑：复用同一 rid 重新建任务（P0 全量重跑，续跑由 Worker 侧 normalized_md 优化）。"""
        need(username, 'ai.generate')
        with lock:
            r = _task_or_404(task_id)
        try:
            return create_summary_task(db, lock, r['rid'], force_refresh=True, created_by=username)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---------------- 结果 ----------------
    @router.get('/summaries/{summary_id}')
    def get_summary(summary_id: int, username: str = Depends(current_user)):
        with lock:
            s = db.execute('SELECT * FROM ai_summary WHERE id=?', (summary_id,)).fetchone()
            if not s:
                raise HTTPException(404, '汇总不存在')
            bidders = db.execute('SELECT * FROM ai_bidder WHERE summary_id=? ORDER BY seq, id',
                                 (summary_id,)).fetchall()
            evs = db.execute('SELECT * FROM ai_field_evidence WHERE summary_id=? ORDER BY id',
                             (summary_id,)).fetchall()
            files = db.execute('SELECT f.* FROM ai_file f JOIN ai_summary_task t '
                               'ON t.id=f.task_id WHERE t.summary_id=? ORDER BY f.id',
                               (summary_id,)).fetchall()
            tpl = db.execute('SELECT scope, fields FROM ai_field_template').fetchall()
        _visible(username, s['rid'])
        # 模式 D 读时兜底：早期版本 db_writer 未把 price_score 写进 ai_bidder 类型化列，
        # 历史汇总（重跑前的任务）这里从 raw_result.discount 补齐，无需重新解析、不烧 token。
        # 补在 _safe_col 之前，保证价格列仍受字段四档权限约束。
        disc_map = {}
        if s['mode'] == 'discount':
            raw = s.get('raw_result')
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = None
            if isinstance(raw, dict):
                for x in (raw.get('discount') or {}).get('bidders') or []:
                    if isinstance(x, dict) and x.get('name'):
                        disc_map[str(x['name'])] = x
        bd = []
        for b in bidders:
            row = dict(b)
            m = disc_map.get(row.get('bidder_name'))
            if m:
                if row.get('price_score') is None:
                    row['price_score'] = m.get('price_score')
                # 附加模式 D 明细（不落库，供前端扩展列使用）
                row['discount_rate'] = m.get('rate')
                row['discount_factor'] = m.get('discount')
                row['in_benchmark'] = m.get('in_benchmark')
            for k, v in list(row.items()):
                if k in LEDGER_FIELD_MAP:
                    ok, nv = _safe_col(username, k, v)
                    if not ok:
                        row[k] = None
                    else:
                        row[k] = nv
            bd.append(row)
        ev_out = []
        for e in evs:
            ok, _ = _safe_col(username, e['field_key'], e['value_text'])
            if not ok:
                continue
            ev_out.append(dict(e))
        return {
            'summary': {k: (str(v) if isinstance(v, datetime) else v) for k, v in dict(s).items()},
            'bidders': [{k: (str(v) if isinstance(v, datetime) else v) for k, v in r.items()} for r in bd],
            'evidence': [{k: (str(v) if isinstance(v, datetime) else v) for k, v in e.items()} for e in ev_out],
            'files': [dict(f) for f in files],
            'templates': {r['scope']: r['fields'] for r in tpl},
        }

    @router.patch('/summaries/{summary_id}/cells')
    def patch_cell(summary_id: int, body: CellIn, username: str = Depends(current_user)):
        need(username, 'ai.generate')
        with lock:
            s = db.execute('SELECT * FROM ai_summary WHERE id=?', (summary_id,)).fetchone()
            if not s:
                raise HTTPException(404, '汇总不存在')
            if s['status'] != 'DRAFT':
                raise HTTPException(400, '已定稿的汇总不可修改')
            p = perm_of(username)
            lk = LEDGER_FIELD_MAP.get(body.field_key)
            if lk and field_level(p['fieldPerms'], lk) in ('hidden', 'masked', 'readonly'):
                raise HTTPException(403, '该字段你没有编辑权限')
            # 核心列
            colmap = {'bid_price': 'bid_price', 'deposit': 'deposit', 'duration': 'duration',
                      'quality': 'remark', 'remark': 'remark', 'qualification': 'qualification',
                      'reject_reason': 'reject_reason', 'bidder_name': 'bidder_name',
                      'budget': 'budget', 'project_name': 'project_name',
                      'project_code': 'project_code', 'tenderer': 'tenderer',
                      'agency': 'agency', 'open_time_raw': 'open_time_raw',
                      'open_place': 'open_place', 'eval_method': 'eval_method'}
            col = colmap.get(body.field_key)
            table = 'ai_bidder' if body.entity == 'BIDDER' else 'ai_summary'
            where_id = body.entity_id if body.entity == 'BIDDER' else summary_id
            old = ''
            if col:
                r = db.execute('SELECT %s AS v FROM %s WHERE id=?' % (col, table),
                               (where_id,)).fetchone()
                old = '' if not r or r['v'] is None else str(r['v'])
                val = body.value
                if col in ('bid_price', 'deposit', 'budget', 'incl_total', 'total_net'):
                    try:
                        val = float(str(val).replace(',', '')) if str(val).strip() else None
                    except ValueError:
                        raise HTTPException(400, '金额必须是数字（单位：元）')
                db.execute('UPDATE %s SET %s=? WHERE id=?' % (table, col), (val, where_id))
            # 证据表：写一条 manual=1 覆盖记录
            bidder_id = body.entity_id if body.entity == 'BIDDER' else None
            scope = 'BIDDER' if body.entity == 'BIDDER' else 'PROJECT'
            ev = db.execute(
                'SELECT id FROM ai_field_evidence WHERE summary_id=? AND scope=? '
                'AND field_key=? AND bidder_id IS NOT DISTINCT FROM ? ORDER BY id DESC LIMIT 1',
                (summary_id, scope, body.field_key, bidder_id)).fetchone()
            now = time.time()
            if ev:
                db.execute('UPDATE ai_field_evidence SET value_text=?, confidence=1, '
                           "level='green', manual=1, note=?, created_at=? WHERE id=?",
                           (body.value, 'manual:' + username, now, ev['id']))
                ev_id = ev['id']
            else:
                cur = db.execute(
                    'INSERT INTO ai_field_evidence(summary_id, bidder_id, scope, field_key, '
                    'value_text, confidence, level, manual, note, created_at) '
                    "VALUES(?,?,?,?,?,1,'green',1,?,?) RETURNING id",
                    (summary_id, bidder_id, scope, body.field_key, body.value,
                     'manual:' + username, now))
                ev_id = cur.fetchone()['id']
            db.execute('INSERT INTO ai_change_log(summary_id, entity, entity_id, field_key, '
                       'old_value, new_value, actor, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (summary_id, body.entity, where_id, body.field_key, old, body.value,
                        username, body.reason, now))
            audit_write([(username, s['rid'], 'ai.review', body.field_key, old, body.value, True)])
            db.commit()
        # 复核即标注：把人工修正沉淀为 few-shot / 回归用例 / 别名（§14 数据飞轮）
        try:
            _feedback(summary_id, ev_id, body.field_key, body.value, username)
        except Exception:
            pass
        return {'ok': True, 'evidence_id': ev_id}

    def _feedback(summary_id, ev_id, field_key, value, username):
        """人工修正 -> 一条脱敏 few-shot + 回归用例。原文片段只取 source_quote(≤200字)。"""
        with lock:
            ev = db.execute('SELECT * FROM ai_field_evidence WHERE id=?', (ev_id,)).fetchone()
            if not ev or not ev['source_quote']:
                return
            t = db.execute('SELECT fingerprint FROM ai_summary_task WHERE summary_id=? '
                           'ORDER BY id DESC LIMIT 1', (summary_id,)).fetchone()
        sig = (t['fingerprint'][:16] if t and t['fingerprint'] else '0' * 16)
        excerpt = str(ev['source_quote'] or '')[:200]
        expected = {field_key: value}
        with lock:
            dup = db.execute('SELECT 1 FROM ai_prompt_fewshot WHERE source_evidence_id=?',
                             (ev_id,)).fetchone()
            if dup:
                return
            db.execute('INSERT INTO ai_prompt_fewshot(scene, format_sig, input_excerpt, '
                       'expected_json, source_task_id, source_evidence_id, is_regression, '
                       'created_by, created_at) VALUES(?,?,?,?,NULL,?,1,?,?)',
                       ('extractor', sig, excerpt, json.dumps(expected, ensure_ascii=False),
                        ev_id, username, time.time()))
            db.commit()

    @router.post('/summaries/{summary_id}/confirm')
    def confirm(summary_id: int, body: ConfirmIn = None,
                username: str = Depends(current_user)):
        need(username, 'ai.confirm')
        with lock:
            s = db.execute('SELECT * FROM ai_summary WHERE id=?', (summary_id,)).fetchone()
            if not s:
                raise HTTPException(404, '汇总不存在')
            if s['status'] == 'CONFIRMED':
                return {'ok': True, 'status': 'CONFIRMED'}
            bidders = db.execute('SELECT * FROM ai_bidder WHERE summary_id=? ORDER BY seq, id',
                                 (summary_id,)).fetchall()
            evs = db.execute('SELECT * FROM ai_field_evidence WHERE summary_id=?',
                             (summary_id,)).fetchall()
            snap = {'summary': dict(s), 'bidders': [dict(b) for b in bidders],
                    'evidence': [dict(e) for e in evs]}
            db.execute('UPDATE ai_summary SET status=?, confirmed_snapshot=?, '
                       'confirmed_by=?, confirmed_at=? WHERE id=?',
                       ('CONFIRMED', json.dumps(snap, ensure_ascii=False, default=str),
                        username, time.time(), summary_id))
            audit_write([(username, s['rid'], 'ai.confirm', 'openingSummary',
                          s['status'], 'CONFIRMED', True)])
            db.commit()
        return {'ok': True, 'status': 'CONFIRMED', 'bidder_count': len(bidders)}

    # ---------------- 字段字典 / 提示词版本（§14 P1-P2） ----------------
    @router.get('/field-templates')
    def get_templates(username: str = Depends(current_user)):
        need(username, 'view.aiSummary')
        with lock:
            rows = db.execute('SELECT * FROM ai_field_template').fetchall()
        return {'templates': {r['scope']: r['fields'] for r in rows},
                'aliases': _aliases()}

    def _aliases():
        rows = db.execute('SELECT scope, field_key, alias, source FROM ai_field_alias '
                          'WHERE enabled=1 ORDER BY id').fetchall()
        out = {}
        for r in rows:
            out.setdefault(r['scope'], {}).setdefault(r['field_key'], []).append(r['alias'])
        return out

    @router.put('/field-templates')
    def put_templates(body: dict, username: str = Depends(current_user)):
        need(username, 'file.admin')
        scope = body.get('scope')
        fields = body.get('fields')
        if scope not in ('PROJECT', 'BIDDER') or not isinstance(fields, list):
            raise HTTPException(400, 'scope 必须是 PROJECT/BIDDER，fields 必须是数组')
        with lock:
            db.execute('INSERT INTO ai_field_template(scope, fields, is_default, updated_by, updated_at) '
                       'VALUES(?,?,0,?,?) ON CONFLICT(scope) DO UPDATE SET '
                       'fields=EXCLUDED.fields, updated_by=EXCLUDED.updated_by, '
                       'updated_at=EXCLUDED.updated_at',
                       (scope, json.dumps(fields, ensure_ascii=False), username, time.time()))
            db.commit()
        return {'ok': True}

    @router.get('/prompt-versions')
    def list_prompts(scene: str = '', username: str = Depends(current_user)):
        need(username, 'view.aiSummary')
        with lock:
            rows = db.execute('SELECT id, scene, version, status, note, metrics, created_at, '
                              'activated_at FROM ai_prompt_version ' +
                              ('WHERE scene=? ' if scene else '') + 'ORDER BY id DESC LIMIT 100',
                              (scene,) if scene else ()).fetchall()
        return {'versions': [dict(r) for r in rows]}

    @router.get('/fewshots')
    def list_fewshots(username: str = Depends(current_user)):
        need(username, 'view.aiSummary')
        with lock:
            rows = db.execute('SELECT id, scene, format_sig, hits, enabled, is_regression, '
                              'created_by, created_at FROM ai_prompt_fewshot '
                              'ORDER BY id DESC LIMIT 100').fetchall()
        return {'fewshots': [dict(r) for r in rows]}

    @router.post('/fewshots/{shot_id}/toggle')
    def toggle_fewshot(shot_id: int, body: ToggleIn,
                       username: str = Depends(current_user)):
        need(username, 'file.admin')
        with lock:
            db.execute('UPDATE ai_prompt_fewshot SET enabled=? WHERE id=?',
                       (1 if body.enabled else 0, shot_id))
            db.commit()
        return {'ok': True}

    # ---------------- 项目选择器数据（服务端模糊搜索 + 分页） ----------------
    @router.get('/projects')
    def list_projects(q: str = '', limit: int = 50, offset: int = 0,
                      username: str = Depends(current_user)):
        """返回可搜索、可分页的项目子集。

        - q：多关键词空格分隔的模糊搜索（与前端 lib/fuzzy.js 同口径），空则按最近更新排序；
        - limit/offset：服务端分页，默认 50 / 0，limit 上限 200。
        - 返回 projects（含 nameParts/ridParts 高亮区间）+ total/hasMore/indexed，
          前端不再一次性下载全量项目，彻底去掉原 LIMIT 500 的硬截断。
        - dataScope=SELF 时按项目负责人做行级过滤（与 main.row_in_scope 同口径）。
        """
        need(username, 'view.aiSummary')
        p = perm_of(username)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        names = self_names(username) if p['dataScope'] == 'SELF' else None
        return project_index.search(q, offset, limit,
                                     scope=p['dataScope'], names=names)

    # ================ 字段体系 v2：布局表 / 自定义列 / 排名主视图 ================
    def _contracts_map():
        with lock:
            rows = db.execute('SELECT * FROM ai_field_contract WHERE enabled=1').fetchall()
        return {r['canonical_key']: dict(r) for r in rows}

    def _contract_perm(username, group):
        """契约下发的四档过滤：hidden 整列不给，masked 标记但不泄值。"""
        lk = CONTRACT_PERM_TO_LEDGER.get(group or '')
        if not lk:
            return 'writable'
        return field_level(perm_of(username)['fieldPerms'], lk)

    def _pref_scope_or_403(username, scope, scope_id):
        """个人偏好免 ai.layout；项目级 / 角色级 / 系统布局须持有 ai.layout。"""
        if scope not in ('user', 'project', 'role'):
            raise HTTPException(400, 'scope 必须是 user/project/role')
        if not scope_id:
            raise HTTPException(400, 'scope_id 不能为空')
        if scope == 'user' and scope_id == username:
            need(username, 'view.aiSummary')
        else:
            need(username, 'ai.layout')
        return scope_id

    @router.get('/field-contracts')
    def get_field_contracts(username: str = Depends(current_user)):
        need(username, 'view.aiSummary')
        rows = _contracts_map()
        out = []
        for k, r in sorted(rows.items(), key=lambda x: (x[1].get('sort_hint') or 0, x[0])):
            mode = _contract_perm(username, r.get('perm_group'))
            if mode == 'hidden':
                continue
            out.append({
                'key': k, 'label': r.get('label'), 'dtype': r.get('dtype'),
                'unit': r.get('unit'), 'moneyUnit': r.get('money_unit'),
                'valueScope': r.get('value_scope'), 'permGroup': r.get('perm_group'),
                'anchor': bool(r.get('is_anchor')), 'formatter': r.get('formatter'),
                'appliesTo': _as_json(r.get('applies_to'), []),
                'aliases': _as_json(r.get('aliases'), []),
                'caliberKey': r.get('caliber_key'), 'permMode': mode,
                'version': r.get('version'), 'sortHint': r.get('sort_hint'),
            })
        return {'contracts': out, 'anchors': list(ANCHOR_KEYS)}

    @router.get('/layouts/resolve')
    def resolve_layout(rid: str = '', mode: str = '', variant: str = '',
                       format_sig: str = '', username: str = Depends(current_user)):
        """读时合并：active 布局 → project/role/user diff → 校验 → 失败回退基线。"""
        need(username, 'view.aiSummary')
        if rid:
            _check_rid(username, rid)
        p = perm_of(username)
        with lock:
            layouts = db.execute(
                "SELECT * FROM ai_view_layout WHERE status='active' AND owner_scope='system'"
            ).fetchall()
        # format_sig 是 CHAR(16)，PG 会把空值补齐成 16 个空格；必须 strip，
        # 否则 (mode, variant, '') 永远匹配不到任何专用布局，全站退回基线。
        by_key = {((r['mode'] or '').strip(), (r['variant'] or '').strip(),
                   (r['format_sig'] or '').strip()): r for r in layouts}
        chain = [(mode, variant, format_sig), (mode, variant, ''),
                 (mode, '', ''), ('', '', '')]
        base = None
        for k in chain:
            if k in by_key:
                base = by_key[k]
                break
        if base is None:
            base = next((r for r in layouts if r['layout_id'] == LAYOUT_BASELINE), None)
        if base is None:
            raise HTTPException(500, '系统基线布局缺失，请重启服务触发 init_tables')
        spec0 = _as_json(base['spec'], {})
        ok, why = validate_spec(spec0)
        if not ok:
            # 基线损坏也要给一张能用的表
            spec0 = {'layout_id': LAYOUT_BASELINE, 'title': '开标结果 · 价格评审排名',
                     'column_groups': [{'id': 'anchor', 'label': '开标结果', 'columns': [
                         {'key': k, 'contract': k, 'locked': True} for k in ANCHOR_KEYS] +
                         [{'key': k, 'contract': k} for k in
                          ('quote_primary', 'deviation', 'total_score', 'status')]}]}
            degraded, warning = True, '系统布局校验失败，已回退内置基线：' + why
        else:
            degraded, warning = False, ''

        specs_scope = []
        if rid:
            specs_scope.append(('project', rid))
        specs_scope.extend([('role', p.get('roleKey') or ''), ('user', username)])
        applied, all_warn, custom = [], [], []
        ov = {}
        contracts = _contracts_map()
        with lock:
            for scope, sid in specs_scope:
                if not sid:
                    continue
                rows = db.execute(
                    'SELECT * FROM ai_view_pref WHERE scope=? AND scope_id=? AND layout_id=?',
                    (scope, sid, base['layout_id'])).fetchall()
                for r in rows:
                    clean, w = validate_overrides(_as_json(r['overrides'], {}), contracts)
                    cc, cw = validate_custom_columns(_as_json(r['custom_columns'], []), contracts)
                    ov = merge_overrides(ov, clean)
                    custom.extend(cc)
                    all_warn.extend(w + cw)
                    applied.append({'scope': scope, 'scopeId': sid,
                                    'baseVersion': r['base_version'] or '',
                                    'updatedAt': r['updated_at']})
                    if r['base_version'] and r['base_version'] != base['version']:
                        all_warn.append('布局已升级至 %s，个人设置以 diff 方式自动继承新列' % base['version'])

        merged = copy.deepcopy(spec0)
        merged['overrides'] = ov
        merged['custom_columns'] = custom
        if not validate_spec(merged)[0]:
            merged = copy.deepcopy(spec0)
            merged['overrides'] = {}
            ov = {}
            degraded = True
            all_warn.append('合并后布局校验失败，已忽略全部个人定制')
        cols, dynamic = flatten_columns(merged, contracts, ov)
        if not any(c['locked'] and c['key'] in ANCHOR_KEYS for c in cols):
            degraded = True
            all_warn.append('锚点列缺失，已回退基线')
            cols, dynamic = flatten_columns(spec0, contracts, {})
            merged['overrides'] = {}
            ov = {}
        if warning:
            all_warn.insert(0, warning)
        return {
            'layout_id': base['layout_id'],
            'version': base['version'],
            'base_version': base['version'],
            'title': merged.get('title') or base['title'] or '开标结果 · 价格评审排名',
            'spec': merged,
            'overrides': ov,
            'custom_columns': custom,
            'columns': cols,
            'dynamic_groups': dynamic,
            'rules_panel': ov.get('rules_panel') or merged.get('rules_panel') or [],
            'footnotes': merged.get('footnotes') or [],
            'appliedPrefs': applied,
            'degraded': degraded,
            'layoutWarning': '；'.join(dict.fromkeys(w for w in all_warn if w))[:500],
            'meta': {'baseVersion': base['version'], 'mode': mode or base['mode'] or '',
                     'variant': variant or '', 'degraded': degraded,
                     'appliedPrefs': len(applied), 'columnCount': len(cols)},
        }

    @router.get('/layout-prefs')
    def get_layout_prefs(scope: str = 'user', rid: str = '',
                         layout_id: str = '', username: str = Depends(current_user)):
        scope_id = (username if scope == 'user' else rid if scope == 'project'
                    else perm_of(username).get('roleKey') or '')
        _pref_scope_or_403(username, scope, scope_id)
        with lock:
            rows = db.execute(
                'SELECT * FROM ai_view_pref WHERE scope=? AND scope_id=? ' +
                ('AND layout_id=? ' if layout_id else '') + 'ORDER BY id',
                (scope, scope_id, layout_id) if layout_id else (scope, scope_id)).fetchall()
        return {'prefs': [{'id': r['id'], 'scope': r['scope'], 'scopeId': r['scope_id'],
                           'layoutId': r['layout_id'], 'baseVersion': r['base_version'],
                           'overrides': _as_json(r['overrides'], {}),
                           'customColumns': _as_json(r['custom_columns'], []),
                           'updatedBy': r['updated_by'], 'updatedAt': r['updated_at']}
                          for r in rows]}

    @router.put('/layout-prefs')
    def put_layout_prefs(body: dict, username: str = Depends(current_user)):
        """只存 diff；锚点列与未知列写入前就被 validate 清洗掉。"""
        scope = body.get('scope') or 'user'
        rid = str(body.get('rid') or '')
        scope_id = (username if scope == 'user' else rid if scope == 'project'
                    else perm_of(username).get('roleKey') or '')
        _pref_scope_or_403(username, scope, scope_id)
        layout_id = str(body.get('layout_id') or LAYOUT_BASELINE)[:64]
        contracts = _contracts_map()
        ov, w1 = validate_overrides(body.get('overrides') or {}, contracts)
        cc, w2 = validate_custom_columns(body.get('customColumns') or [], contracts)
        with lock:
            base = db.execute(
                "SELECT version FROM ai_view_layout WHERE layout_id=? AND owner_scope='system' "
                "AND status='active' ORDER BY id DESC LIMIT 1", (layout_id,)).fetchone()
        base_version = base['version'] if base else ''
        payload = json.dumps(ov, ensure_ascii=False)
        customs = json.dumps(cc, ensure_ascii=False)
        with lock:
            db.execute(
                'INSERT INTO ai_view_pref(scope, scope_id, layout_id, base_version, overrides, '
                'custom_columns, updated_by, updated_at) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(scope, scope_id, layout_id) DO UPDATE SET '
                'base_version=EXCLUDED.base_version, overrides=EXCLUDED.overrides, '
                'custom_columns=EXCLUDED.custom_columns, updated_by=EXCLUDED.updated_by, '
                'updated_at=EXCLUDED.updated_at',
                (scope, scope_id, layout_id, base_version, payload, customs,
                 username, time.time()))
            try:
                db.execute('INSERT INTO ai_change_log(summary_id, entity, entity_id, field_key, '
                           'old_value, new_value, actor, reason, created_at) '
                           'VALUES(0,?,0,?,?,?,?,?,?)',
                           ('layout_pref', layout_id, '', payload[:1000], username,
                            scope + ':' + scope_id, time.time()))
            except Exception:
                pass
            db.commit()
        return {'ok': True, 'scope': scope, 'scopeId': scope_id,
                'layoutId': layout_id, 'baseVersion': base_version,
                'overrides': ov, 'customColumns': cc,
                'warnings': w1 + w2}

    @router.get('/layouts/definitions')
    def list_layout_definitions(username: str = Depends(current_user)):
        need(username, 'ai.layout')
        with lock:
            rows = db.execute(
                'SELECT id, layout_id, version, mode, variant, format_sig, is_baseline, '
                "title, status, owner_scope, metrics, note, created_by, created_at, activated_at "
                'FROM ai_view_layout ORDER BY layout_id, version DESC, id DESC LIMIT 200'
            ).fetchall()
        return {'layouts': [dict(r) for r in rows]}

    @router.put('/layouts/{layout_id}')
    def put_layout_definition(layout_id: str, body: dict,
                              username: str = Depends(current_user)):
        need(username, 'ai.layout')
        spec = body.get('spec')
        ok, why = validate_spec(spec)
        if not ok:
            raise HTTPException(400, '布局 spec 非法：' + why)
        version = str(body.get('version') or ('v' + str(int(time.time()))))[:16]
        status = str(body.get('status') or 'draft')[:12]
        if status not in ('draft', 'active', 'archived', 'rollback'):
            raise HTTPException(400, '非法 status')
        mode = str(body.get('mode') or spec.get('mode') or '')[:24]
        variant = str(body.get('variant') or '')[:24]
        format_sig = str(body.get('format_sig') or '')[:16]
        payload = json.dumps(spec, ensure_ascii=False)
        with lock:
            if status == 'active':
                # active 唯一索引：同键先归档，避免整站布局冲突
                db.execute("UPDATE ai_view_layout SET status='archived' "
                           'WHERE owner_scope=? AND mode=? AND variant=? AND format_sig=? '
                           "AND status='active'", ('system', mode, variant, format_sig))
            db.execute(
                'INSERT INTO ai_view_layout(layout_id, version, mode, variant, format_sig, '
                'is_baseline, title, spec, status, owner_scope, note, created_by, created_at, '
                'activated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(layout_id, version, owner_scope) DO UPDATE SET '
                'spec=EXCLUDED.spec, status=EXCLUDED.status, title=EXCLUDED.title, '
                'note=EXCLUDED.note, activated_at=EXCLUDED.activated_at',
                (layout_id, version, mode, variant, format_sig,
                 1 if layout_id == LAYOUT_BASELINE else 0,
                 str(body.get('title') or layout_id)[:200], payload, status, 'system',
                 str(body.get('note') or '')[:500], username, time.time(),
                 time.time() if status == 'active' else None))
            try:
                db.execute('INSERT INTO ai_change_log(summary_id, entity, entity_id, field_key, '
                           'old_value, new_value, actor, reason, created_at) '
                           'VALUES(0,?,0,?,?,?,?,?,?)',
                           ('layout', layout_id, '', payload[:1000], username,
                            version + '/' + status, time.time()))
            except Exception:
                pass
            db.commit()
        return {'ok': True, 'layoutId': layout_id, 'version': version, 'status': status}

    @router.post('/layouts/{layout_id}/rollback')
    def rollback_layout(layout_id: str, version: str = '',
                        username: str = Depends(current_user)):
        need(username, 'ai.layout')
        with lock:
            target = db.execute(
                "SELECT * FROM ai_view_layout WHERE layout_id=? AND owner_scope='system' " +
                ('AND version=? ' if version else '') + 'ORDER BY id DESC LIMIT 1',
                (layout_id, version) if version else (layout_id,)).fetchone()
            if not target:
                raise HTTPException(404, '未找到可回滚的布局版本')
            db.execute("UPDATE ai_view_layout SET status='archived' "
                       "WHERE owner_scope='system' AND mode=? AND variant=? AND format_sig=? "
                       "AND status='active'", (target['mode'] or '', target['variant'] or '',
                                               target['format_sig'] or ''))
            db.execute("UPDATE ai_view_layout SET status='active', activated_at=? WHERE id=?",
                       (time.time(), target['id']))
            try:
                db.execute('INSERT INTO ai_change_log(summary_id, entity, entity_id, field_key, '
                           'old_value, new_value, actor, reason, created_at) '
                           'VALUES(0,?,0,?,?,?,?,?,?)',
                           ('layout', layout_id, '', 'rollback', username,
                            target['version'], time.time()))
            except Exception:
                pass
            db.commit()
        return {'ok': True, 'layoutId': layout_id, 'version': target['version'],
                'status': 'active'}

    @router.get('/layouts/{layout_id}/export')
    def export_layout(layout_id: str, version: str = '',
                      username: str = Depends(current_user)):
        need(username, 'ai.layout')
        with lock:
            r = db.execute(
                "SELECT * FROM ai_view_layout WHERE layout_id=? AND owner_scope='system' " +
                ('AND version=? ' if version else '') + 'ORDER BY id DESC LIMIT 1',
                (layout_id, version) if version else (layout_id,)).fetchone()
        if not r:
            raise HTTPException(404, '布局不存在')
        return {'layoutId': r['layout_id'], 'version': r['version'],
                'mode': r['mode'], 'variant': r['variant'],
                'title': r['title'], 'spec': _as_json(r['spec'], {})}

    return router
