# -*- coding: utf-8 -*-
"""Recipe（采集配方）：校验、编译、加载、版本保存。

配方只描述「站点怎么取数」，不写任何站点分支；关键词 / 白名单 / 剔除规则
一律放在配置表（config.py），改口径不需要重新解析站点。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import jsonpath_ng

from . import db

VALID_MODES = ('json_api', 'html_list')
VALID_PAGINATION = ('page', 'size_offset', 'cursor', 'none')
VALID_CAP = ('ok', 'truncate', 'ignore', 'error')
# 平台状态：paused 是前端/生成器/AI 配方统一使用的「停用」；
# disabled 是历史数据里的同义词，保留兼容；error 由 runner 写入。
PLATFORM_STATUS = ('active', 'degraded', 'paused', 'disabled', 'draft', 'error')

STANDARD_FIELDS = ('title', 'url', 'buyer', 'budget', 'fee_note', 'scope',
                   'doc_get_start', 'doc_get_end', 'open_at', 'publish_at',
                   'region', 'source_name', 'status')


class RecipeError(ValueError):
    """配方结构非法。"""


class _SafeDict(dict):
    def __missing__(self, key):
        return ''


# ------------------------------------------------------------------ 取值原语
def dotted_get(obj: Any, path: str):
    """支持 data.list / items.0.title 这种点号路径。"""
    cur = obj
    for part in str(path).split('.'):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def jsonpath_get(data: Any, expr: str):
    try:
        matches = jsonpath_ng.parse(expr).find(data)
    except Exception:
        return None
    if not matches:
        return None
    val = matches[0].value
    return val


def _text(v) -> str:
    if v is None:
        return ''
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _clean(text: str, spec: dict) -> str:
    s = _text(text).replace('\u3000', ' ').strip()
    if spec.get('strip'):
        s = re.sub(r'\s+', ' ', s)
    max_len = spec.get('max_len')
    if max_len and len(s) > int(max_len):
        s = s[:int(max_len)]
    return s


def compile_jsonpath(expr: str):
    try:
        return jsonpath_ng.parse(expr)
    except Exception as e:
        raise RecipeError('JSONPath 非法：%s（%s）' % (expr, e))


# ------------------------------------------------------------------ 编译结果
@dataclass
class CompiledRecipe:
    raw: dict
    meta: dict
    transport: dict
    discovery: dict
    detail: dict
    normalize: dict
    validation: dict
    items_path: Any = None
    fields: dict = field(default_factory=dict)

    # ---- 常用属性 ----
    @property
    def platform_id(self) -> str:
        return str(self.meta.get('id') or '')

    @property
    def version(self) -> int:
        return int(self.meta.get('version') or 1)

    @property
    def pagination(self) -> dict:
        return self.discovery.get('pagination') or {}

    @property
    def mode(self) -> str:
        return str(self.discovery.get('mode') or 'json_api')

    def effective_size(self) -> int | None:
        """运行时优先用实测上限；服务端忽略页宽时退回声明值。"""
        p = self.pagination
        if not p.get('size_param'):
            return None
        behavior = p.get('size_cap_behavior') or 'ok'
        if behavior in ('ok', 'truncate') and p.get('size_max'):
            return int(p['size_max'])
        return int(p['size'] or p.get('size_max') or 20)

    def request_spec(self) -> dict:
        return dict(self.discovery.get('request') or {})

    def detail_enabled(self) -> bool:
        return bool(self.detail.get('enabled'))

    def validation_cfg(self) -> dict:
        return dict(self.validation or {})


def compile_recipe(raw: dict) -> CompiledRecipe:
    errors = validate_recipe(raw)
    if errors:
        raise RecipeError('；'.join(errors))
    discovery = dict(raw.get('discovery') or {})
    fields = dict(discovery.get('fields') or {})
    items_path = discovery.get('items_path')
    return CompiledRecipe(
        raw=raw,
        meta=dict(raw.get('meta') or {}),
        transport=dict(raw.get('transport') or {}),
        discovery=discovery,
        detail=dict(raw.get('detail') or {}),
        normalize=dict(raw.get('normalize') or {}),
        validation=dict(raw.get('validation') or {}),
        items_path=compile_jsonpath(items_path) if items_path else None,
        fields=fields,
    )


# ------------------------------------------------------------------ 校验
def validate_recipe(raw: dict) -> list[str]:
    errs = []
    if not isinstance(raw, dict):
        return ['配方必须是 JSON 对象']
    meta = raw.get('meta') or {}
    if not str(meta.get('id') or '').strip():
        errs.append('meta.id 不能为空')
    if not str(meta.get('name') or '').strip():
        errs.append('meta.name 不能为空')
    if not str(meta.get('host') or '').strip():
        errs.append('meta.host 不能为空')

    transport = raw.get('transport') or {}
    discovery = raw.get('discovery') or {}
    request = discovery.get('request') or {}
    url = str(request.get('url') or transport.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        errs.append('discovery.request.url / transport.url 必须是 http(s) 地址')

    mode = str(discovery.get('mode') or '')
    if mode not in VALID_MODES:
        errs.append('discovery.mode 只能是 %s' % ' / '.join(VALID_MODES))
    if mode == 'json_api' and not discovery.get('items_path'):
        errs.append('json_api 模式必须提供 items_path')
    if mode == 'html_list' and not discovery.get('row_selector'):
        errs.append('html_list 模式必须提供 row_selector')
    if discovery.get('items_path'):
        try:
            compile_jsonpath(discovery['items_path'])
        except RecipeError as e:
            errs.append(str(e))

    fields = discovery.get('fields') or {}
    if not fields:
        errs.append('discovery.fields 不能为空')
    for key in ('title', 'url'):
        if key not in fields:
            errs.append('discovery.fields 缺少必需字段：%s' % key)
    for name, spec in (fields or {}).items():
        if not isinstance(spec, dict):
            errs.append('字段 %s 的取值规则必须是对象' % name)
            continue
        if not any(k in spec for k in ('path', 'css', 'xpath', 'regex', 'template', 'const')):
            errs.append('字段 %s 缺少 path/css/xpath/regex/template/const' % name)
        if 'regex' in spec:
            try:
                re.compile(str(spec['regex']))
            except re.error as e:
                errs.append('字段 %s 正则非法：%s' % (name, e))

    pag = discovery.get('pagination') or {}
    ptype = str(pag.get('type') or 'none')
    if ptype not in VALID_PAGINATION:
        errs.append('pagination.type 只能是 %s' % ' / '.join(VALID_PAGINATION))
    if ptype != 'none' and ptype != 'cursor' and not pag.get('page_param') \
            and not pag.get('page_in_url'):
        errs.append('分页类型 %s 必须提供 page_param（页码在 URL 路径里时可设 page_in_url=true）'
                    % ptype)
    if pag.get('page_in_url'):
        if '{page}' not in str(request.get('url') or ''):
            errs.append('page_in_url=true 时 discovery.request.url 必须包含 {page} 占位符')
        if not str(pag.get('first_url') or '').startswith(('http://', 'https://')):
            errs.append('page_in_url=true 时必须提供 first_url（第 1 页地址）')
    if pag.get('size_cap_behavior') and pag['size_cap_behavior'] not in VALID_CAP:
        errs.append('pagination.size_cap_behavior 只能是 %s' % ' / '.join(VALID_CAP))
    probe = pag.get('size_probe')
    if probe is not None:
        if not isinstance(probe, list) or not probe or \
                any(not isinstance(x, int) or x <= 0 for x in probe):
            errs.append('pagination.size_probe 必须是正整数数组')
    if pag.get('total_path'):
        try:
            compile_jsonpath(pag['total_path'])
        except RecipeError as e:
            errs.append('pagination.total_path 非法：%s' % e)

    detail = raw.get('detail') or {}
    if detail.get('enabled'):
        dreq = detail.get('request') or {}
        has_deterministic = bool((detail.get('extract') or {}).get('fields'))
        has_ai = bool((detail.get('ai') or {}).get('enabled'))
        has_pdf = bool((detail.get('pdf') or {}).get('enabled'))
        if not str(dreq.get('url') or '').strip():
            errs.append('detail.enabled=true 时必须提供 detail.request.url')
        if not has_deterministic and not has_ai and not has_pdf:
            errs.append('detail.enabled=true 时必须提供 detail.extract.fields / detail.ai.enabled / detail.pdf.enabled')
        if (has_ai and not has_pdf
                and not str(detail.get('content_path') or '').strip()
                and not str(detail.get('content_css') or '').strip()):
            errs.append('detail.ai.enabled=true 时必须提供 detail.content_path（JSON 正文字段）、'
                        'detail.content_css（HTML 正文选择器），或启用 detail.pdf.enabled 从 PDF 取正文')
        if dreq.get('body') is not None and not isinstance(dreq['body'], (dict, str)):
            errs.append('detail.request.body 必须是对象或字符串模板')
        scope = str((detail.get('extract') or {}).get('scope') or 'html')
        if scope not in ('html', 'html_page', 'json', 'json_api'):
            errs.append('detail.extract.scope 只能是 html/html_page/json/json_api')
        row_path = str(detail.get('row_path') or '')
        if row_path.startswith('$'):
            try:
                compile_jsonpath(row_path)
            except RecipeError as e:
                errs.append(str(e))
        elif row_path and not re.match(r'^[A-Za-z_][\w]*(\.[A-Za-z_0-9]+)*$', row_path):
            errs.append('detail.row_path 必须是 JSONPath($. 开头) 或点号路径(rows.0)')
    return errs


# ------------------------------------------------------------------ 字段取值
def _from_selector(sel, spec: dict) -> str:
    """从 parsel Selector（列表行或详情页）上按 css/xpath 取值。

    nth: 一组重复结构里取第 n 个（0 起），如列表行内多个 li > span；
    比 nth-child 更稳，因为行本身也可能是一个 li 而打乱编号。
    """
    node = None
    if spec.get('css'):
        node = sel.css(str(spec['css']))
    elif spec.get('xpath'):
        node = sel.xpath(str(spec['xpath']))
    if node is None:
        return ''
    if spec.get('nth') is not None:
        try:
            i = int(spec['nth'])
            node = node[i] if 0 <= i < len(node) else None
        except (TypeError, ValueError, IndexError):
            node = None
        if node is None:
            return ''
    attr = spec.get('attr')
    if attr:
        val = node.attrib.get(attr) or ''
        return _clean(val, spec)
    txt = node.get()
    if isinstance(txt, list):
        txt = ' '.join(t for t in txt if t)
    return _clean(txt, spec)


def resolve_field(spec: dict, row: dict, *, text: str = '', selector=None,
                  base_url: str = '') -> str:
    """按字段规则从一个条目里取一个值；取不到返回空串。"""
    if not isinstance(spec, dict):
        return ''
    val = ''

    if 'const' in spec:
        val = _text(spec['const'])
    elif 'regex' in spec:
        # 放在 template 之前：{regex, template} 是「先抽分组再拼接」的组合规则，
        # 若先走 template 会把 {1}/{2} 当成 row 字段而取到空串。
        hay = text if text else (_text(row) if row else '')
        try:
            m = re.search(str(spec['regex']), hay)
        except re.error:
            m = None
        if m:
            if spec.get('template'):
                # 例：<div>2026</div><div>11/11</div> → 2026-11-11 00:00
                # 占位符必须是「命名」的：str.format 会把裸 {1} 当成位置参数
                # 而不是字典键，导致 IndexError。这里同时兼容 {1} 与 {g1} 写法。
                ctx = {'g0': m.group(0)}
                for i, g in enumerate(m.groups(), 1):
                    ctx['g%d' % i] = g or ''
                tpl = re.sub(r'\{(\d+)\}', r'{g\1}', str(spec['template']))
                try:
                    val = tpl.format_map(_SafeDict(ctx))
                except Exception:
                    val = ''
            else:
                g = int(spec.get('group') or 0)
                val = m.group(g) if g <= (m.lastindex or 0) else m.group(0)
    elif 'template' in spec:
        try:
            val = str(spec['template']).format_map(_SafeDict(row))
        except Exception:
            val = ''
    elif 'path' in spec:
        path = str(spec['path'])
        v = jsonpath_get(row, path) if path.startswith('$') else dotted_get(row, path)
        val = _text(v)
    elif 'css' in spec or 'xpath' in spec:
        if selector is not None:
            val = _from_selector(selector, spec)
    val = _clean(val, spec)

    if not val:
        for fb in (spec.get('fallback') or []):
            if not isinstance(fb, dict):
                continue
            val = resolve_field(dict(fb, strip=spec.get('strip'), max_len=spec.get('max_len')),
                                row, text=text, selector=selector, base_url=base_url)
            if val:
                break
    return val


# ------------------------------------------------------------------ 数据库读写
def get_active(platform_id: str) -> CompiledRecipe | None:
    r = db.one('SELECT p.id, p.active_ver, v.recipe FROM bid_platform p '
               'JOIN bid_recipe_version v '
               'ON v.platform_id=p.id AND v.version=p.active_ver WHERE p.id=%s',
               (platform_id,))
    if not r or not r.get('recipe'):
        return None
    raw = r['recipe'] if isinstance(r['recipe'], dict) else json.loads(r['recipe'])
    raw.setdefault('meta', {})['version'] = int(r['active_ver'] or 1)
    return compile_recipe(raw)


def list_platforms(statuses=None) -> list[dict]:
    sql = ('SELECT p.id, p.name, p.host, p.entry_url, p.status, p.active_ver, '
           'p.last_ok_at, p.last_err, p.created_at, '
           '(v.recipe->\'discovery\'->\'pagination\') AS pagination '
           'FROM bid_platform p LEFT JOIN bid_recipe_version v '
           'ON v.platform_id=p.id AND v.version=p.active_ver')
    args: tuple = ()
    if statuses:
        sql += ' WHERE p.status = ANY(%s)'
        args = (list(statuses),)
    sql += ' ORDER BY p.id'
    return db.q(sql, args)


def save_version(raw: dict, *, created_by='cli', activate=True, verify=None) -> dict:
    """保存新版本（版本号自增），可选立即启用，并重建字段投影。"""
    errors = validate_recipe(raw)
    if errors:
        raise RecipeError('；'.join(errors))
    pid = str(raw['meta']['id'])
    meta = raw['meta']
    host = str(meta.get('host') or '')
    name = str(meta.get('name') or pid)
    with db.conn() as c:          # psycopg2 连接支持 with 事务
        cur = c.cursor()
        try:
            cur.execute(
                'INSERT INTO bid_platform(id, name, host, entry_url, status) '
                "VALUES (%s,%s,%s,%s,'draft') "
                'ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, host=EXCLUDED.host, '
                'entry_url=COALESCE(EXCLUDED.entry_url, bid_platform.entry_url)',
                (pid, name, host, meta.get('entry_url')))
            cur.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM bid_recipe_version '
                        'WHERE platform_id=%s', (pid,))
            version = int(cur.fetchone()[0])
            raw.setdefault('meta', {})['version'] = version
            verify = verify or {}
            cur.execute(
                'INSERT INTO bid_recipe_version(platform_id, version, recipe, verify, created_by) '
                'VALUES (%s,%s,%s,%s,%s)',
                (pid, version, json.dumps(raw, ensure_ascii=False),
                 json.dumps(verify, ensure_ascii=False), created_by))
            if activate:
                cur.execute('UPDATE bid_platform SET active_ver=%s, status=%s, '
                            'last_err=NULL WHERE id=%s',
                            (version, 'active', pid))
            _rebuild_projection(cur, pid, version, raw)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()
    return {'platform_id': pid, 'version': version, 'activated': bool(activate)}


def _selector_desc(spec: dict):
    if 'const' in spec:
        return 'const', _text(spec['const']), None
    if 'template' in spec:
        return 'template', str(spec['template']), None
    if 'path' in spec:
        return 'path', str(spec['path']), None
    if 'css' in spec:
        return 'css', str(spec['css']), None
    if 'xpath' in spec:
        return 'xpath', str(spec['xpath']), None
    if 'regex' in spec:
        return 'regex', str(spec['regex']), None
    return 'unknown', '', None


def _rebuild_projection(cur, pid: str, version: int, raw: dict):
    cur.execute('DELETE FROM bid_recipe_field WHERE platform_id=%s AND version=%s',
                (pid, version))
    sources = [('list', (raw.get('discovery') or {}).get('fields') or {}),
               ('detail', ((raw.get('detail') or {}).get('extract') or {}).get('fields') or {})]
    for source, fields in sources:
        for key, spec in fields.items():
            if not isinstance(spec, dict):
                continue
            kind, selector, _ = _selector_desc(spec)
            cur.execute(
                'INSERT INTO bid_recipe_field(platform_id, version, field_key, source, '
                'selector_kind, selector, type_hint, has_fallback) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (platform_id, version, field_key, source) DO UPDATE '
                'SET selector_kind=EXCLUDED.selector_kind, selector=EXCLUDED.selector, '
                'type_hint=EXCLUDED.type_hint, has_fallback=EXCLUDED.has_fallback',
                (pid, version, key, source, kind, selector, spec.get('type'),
                 bool(spec.get('fallback'))))


def activate(platform_id: str, version: int | None = None) -> dict:
    with db.conn() as c:
        cur = c.cursor()
        try:
            if version is None:
                cur.execute('SELECT COALESCE(MAX(version),0) AS v FROM bid_recipe_version '
                            'WHERE platform_id=%s', (platform_id,))
                version = int(cur.fetchone()[0])
            cur.execute('SELECT 1 FROM bid_recipe_version WHERE platform_id=%s AND version=%s',
                        (platform_id, version))
            if not cur.fetchone():
                raise RecipeError('配方版本不存在：%s v%s' % (platform_id, version))
            cur.execute("UPDATE bid_platform SET active_ver=%s, status='active', last_err=NULL "
                        'WHERE id=%s', (version, platform_id))
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()
    return {'platform_id': platform_id, 'version': version, 'status': 'active'}


def set_status(platform_id: str, status: str, error: str = '') -> bool:
    if status not in PLATFORM_STATUS:
        raise RecipeError('非法平台状态：%s（可用：%s）'
                          % (status, '/'.join(PLATFORM_STATUS)))
    cur = db.x('UPDATE bid_platform SET status=%s, last_err=%s WHERE id=%s',
               (status, (error or '')[:1000] or None, platform_id))
    db.commit()
    return cur.rowcount > 0


def delete_recipe(platform_id: str) -> dict:
    """删除平台的全部配方版本与字段索引，平台保留但停用。

    历史采集结果 / 任务记录不删除，便于事后追溯；重新生成或粘贴配方后
    版本号从 1 开始。平台本身保留，避免误删连带关键词、规则等配置。
    """
    with db.conn() as c:
        cur = c.cursor()
        try:
            cur.execute('SELECT COUNT(*) FROM bid_recipe_version WHERE platform_id=%s',
                        (platform_id,))
            versions = int(cur.fetchone()[0] or 0)
            cur.execute('DELETE FROM bid_recipe_field WHERE platform_id=%s', (platform_id,))
            fields_deleted = cur.rowcount
            cur.execute('DELETE FROM bid_recipe_version WHERE platform_id=%s', (platform_id,))
            cur.execute("UPDATE bid_platform SET active_ver=NULL, status='paused', "
                        'last_err=NULL WHERE id=%s', (platform_id,))
            platform_found = cur.rowcount > 0
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()
    if not platform_found:
        raise RecipeError('平台不存在：%s' % platform_id)
    return {'platform_id': platform_id, 'versions_deleted': versions,
            'fields_deleted': int(fields_deleted or 0), 'status': 'paused'}


def delete_platform(platform_id: str, *, force: bool = False) -> dict:
    """彻底删除平台及其全部关联数据（含历史采集结果与任务）。

    与 delete_recipe 的区别：这里连平台本身一起删，且不做「保留历史」的假设，
    因为用户明确要删除平台时，残留的孤立 bid_item / run 没有意义。
    bid_config_audit 是全局审计日志，不带 platform_id 外键，予以保留。

    默认拒绝删除有 queued/running 任务的平台（worker 仍在跑会产生孤儿数据）；
    force=True 时连同任务一起删，用于清理卡死任务。
    """
    with db.conn() as c:
        cur = c.cursor()
        try:
            cur.execute('SELECT id FROM bid_platform WHERE id=%s', (platform_id,))
            if not cur.fetchone():
                raise RecipeError('平台不存在：%s' % platform_id)

            cur.execute("SELECT COUNT(*) FROM bid_crawl_job WHERE platform_id=%s "
                        "AND status IN ('queued','running')", (platform_id,))
            active_jobs = int(cur.fetchone()[0] or 0)
            if active_jobs and not force:
                raise RecipeError('平台仍有 %d 个排队/运行中的任务，请先取消或稍后重试；'
                                  '确需强制删除可调用 force 接口' % active_jobs)

            counts: dict[str, int] = {}
            # 顺序：先删子表 / 无外键引用的明细，最后删平台；job 与 item 的子表靠级联。
            cur.execute('DELETE FROM bid_recipe_field WHERE platform_id=%s', (platform_id,))
            counts['recipe_fields'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_recipe_version WHERE platform_id=%s', (platform_id,))
            counts['recipes'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_platform_credential WHERE platform_id=%s', (platform_id,))
            counts['credentials'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_detail_ai WHERE platform_id=%s', (platform_id,))
            counts['ai_cache'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_item WHERE platform_id=%s', (platform_id,))
            counts['items'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_crawl_run WHERE platform_id=%s', (platform_id,))
            counts['runs'] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_crawl_job WHERE platform_id=%s', (platform_id,))
            counts['jobs'] = int(cur.rowcount or 0)
            for table in ('bid_keyword', 'bid_buyer_pattern', 'bid_exclude_rule'):
                cur.execute('DELETE FROM %s WHERE scope=%%s' % table, (platform_id,))
                counts[table] = int(cur.rowcount or 0)
            cur.execute('DELETE FROM bid_platform WHERE id=%s', (platform_id,))
            if cur.rowcount != 1:
                raise RecipeError('平台删除失败：%s' % platform_id)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()
    return {'platform_id': platform_id, 'deleted': counts, 'force': bool(force)}


def field_coverage(platform_id: str, sample_items: list[dict]) -> dict:
    """按配方 validation.expected_nonempty 统计字段非空率（运行期健康检查用）。"""
    rec = get_active(platform_id)
    if not rec:
        return {}
    expected = (rec.validation.get('expected_nonempty') or {})
    out = {}
    for key in STANDARD_FIELDS:
        need = float(expected.get(key) or 0)
        if need <= 0 and key not in ('title', 'url'):
            continue
        hit = sum(1 for it in sample_items if str(it.get(key) or '').strip())
        total = len(sample_items) or 1
        out[key] = round(hit / total, 3)
    return out


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')
