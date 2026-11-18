# -*- coding: utf-8 -*-
"""模式化配方生成：用户给 curl + 真实响应样本，系统反推结构并组装配方。

两种模式（对应网页上可切换的 Tab）：
- api_list：列表/详情接口都返回 JSON（接口型站点，最常见）；
- html_list：列表/详情返回 HTML（CMS 型站点，正文可能是 PDF iframe）。

与 ai_analyze.analyze（自动探测）的区别：请求参数 100% 来自用户粘贴的 curl，
不猜接口、不试参数，所以强网关 / SPA / 混淆 JS 的站点也能一次成型。
LLM 只做「原始字段名 → 标准字段」的语义映射，结构（jsonpath / css / 正则）
全部由 evidence.py 确定性反推，并在样本上回测通过后才写进配方。
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from . import evidence as evd
from . import recipe as recipe_mod
from .ai_analyze import (AI_FALLBACK_REGEX, AI_FIELDS, DEFAULT_KEYWORD,
                         DETAIL_FIELD_MAP, LIST_FIELD_MAP, _chat, _heuristic_map,
                         _llm_available, _loads, _merge_map, _platform_id,
                         verify_recipe)

VALID_MODES = ('api_list', 'html_list')

# 生成器版本：接口会把它带在返回里，用来确认线上进程是否已重启到新代码
GENERATOR_VERSION = '2026-11-17.5'


# ------------------------------------------------------------------ 公共小工具
def _stage(name: str, status: str, detail: str, t0: float) -> dict:
    return {'name': name, 'status': status, 'detail': detail,
            'ms': int((time.monotonic() - t0) * 1000)}


def _encoding(headers: dict, sample: str = '') -> str:
    for k, v in (headers or {}).items():
        if k.lower() == 'content-type':
            m = re.search(r'charset=([\w-]+)', str(v), re.I)
            if m:
                return m.group(1).lower()
    m = re.search(r'charset=["\']?([\w-]+)', (sample or '')[:2000], re.I)
    return (m.group(1).lower() if m else 'utf-8')


def _site_name(req: dict, name: str, sample: str = '') -> str:
    if name and name.strip():
        return name.strip()[:40]
    m = re.search(r'<title[^>]*>([^<]{2,80})</title>', sample or '', re.I)
    if m:
        t = re.split(r'[-_|｜，,·]', m.group(1).strip())[0].strip()
        if t and len(t) <= 20:
            return t
    return evd.host_of(req) or 'site'


_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def _base_transport(req: dict, sample: str, site_url: str = '') -> dict:
    headers = dict(req.get('headers') or {})
    low = {k.lower() for k in headers}
    # curl 里通常没有 UA / Accept（Copy as cURL 会省略默认值），
    # 但不少站点（如金采网）见到 python-httpx 的 UA 直接 403。
    if 'user-agent' not in low:
        headers['User-Agent'] = _UA
    if 'accept' not in low:
        headers['Accept'] = '*/*'
    if 'accept-language' not in low:
        headers['Accept-Language'] = 'zh-CN,zh;q=0.9'
    if 'origin' not in low and req.get('origin'):
        headers['Origin'] = req['origin']
    # 部分接口按 Referer 校验来源（金采网缺 Referer 直接 403）
    if 'referer' not in low:
        headers['Referer'] = site_url or (req.get('origin') or '') + '/'
    return {
        'kind': 'http', 'method': req.get('method') or 'POST', 'url': req.get('url') or '',
        'headers': headers, 'encoding': _encoding(headers, sample),
        # 与浏览器抓包一致：不走系统代理，避免 403/证书链问题
        'trust_env': False,
        'timeout_s': 25, 'total_timeout_s': 30, 'max_response_mb': 20,
        'rate_limit': {'qps': 0.2, 'concurrency': 1},
        'retry': {'times': 2, 'backoff_s': 5, 'retry_on': [429, 500, 502, 503, 504]},
    }


def _ai_detail_cfg() -> dict:
    return {'enabled': True, 'prompt_version': 'auto-detail-v1',
            'max_chars': 20000, 'timeout_s': 90, 'cache': True,
            'fields': list(AI_FIELDS),
            'fallback_regex': dict(AI_FALLBACK_REGEX)}


# ------------------------------------------------------------------ LLM 字段映射
SYS_MAP_MANUAL = (
    '你是招投标数据采集的字段映射助手。用户提供了某站点接口返回的原始字段名与样本值，'
    '请把原始字段映射到本系统的标准字段。\n'
    '铁律：\n'
    '1. 只能从给定的【候选字段名】里挑选，绝不编造。\n'
    '2. 只输出一个 JSON 对象：{"name":"站点中文名","list":{...},"detail":{...}}，'
    '不要解释、不要 ``` 包裹。\n'
    '3. 无法对应的标准字段直接省略。\n'
    '\n标准字段含义：\n'
    'list.title=公告标题；list.external_id=公告唯一ID；list.buyer=采购人/招标人；'
    'list.publish_at=发布时间；list.region=地区；list.source_name=信息来源；'
    'list.budget=预算金额；list.url=详情页链接。\n'
    'detail.bid_no=招标编号；detail.notice_type=采购方式；detail.agency=招标代理机构；'
    'detail.category=品类/标签；detail.contact=联系人。\n'
    'name 为站点中文简称，无法判断时省略。'
)


def _llm_map(keys: list[str], rows: list[dict], detail_keys: list[str],
             detail_row: dict, site_url: str) -> tuple[dict, dict, str]:
    ok, _ = _llm_available()
    if not ok or (not keys and not site_url):
        return {}, {}, ''
    payload = {'site': site_url, 'list_keys': keys[:80], 'list_sample': rows[:2],
               'detail_keys': detail_keys[:60], 'detail_sample': detail_row}
    try:
        raw = _chat(SYS_MAP_MANUAL,
                    '候选字段名与样本（JSON）：\n%s\n\n请输出字段映射 JSON。'
                    % json.dumps(payload, ensure_ascii=False)[:9000], timeout_s=60)
        data = _loads(raw)
        if not isinstance(data, dict):
            return {}, {}, ''
        lm = data.get('list') if isinstance(data.get('list'), dict) else {}
        dm = data.get('detail') if isinstance(data.get('detail'), dict) else {}
        return lm, dm, str(data.get('name') or '')[:30]
    except Exception:
        return {}, {}, ''


# ------------------------------------------------------------------ 列表字段装配
_URL_KEYS = ('url', 'detailUrl', 'link', 'href', 'contentUrl', 'htmlUrl', 'pageUrl',
             'noticeUrl', 'infoUrl', 'linkUrl', 'sourceUrl')
_TITLE_GUESS = ('title', 'noticeTitle', 'messageTitle', 'bulletinTitle', 'bidTitle',
                'projectName', 'name', 'subject', 'gongGaoMingCheng')
_URL_SOURCE_TXT = {'field': '列表自带链接', 'example': '用户提供的详情页示例',
                   'detail': '详情 curl 的页面地址',
                   'referer': '详情 curl 的 Referer', 'guess': '兜底推断'}


def _pick_url_key(keys: list[str], rows: list[dict]) -> str:
    """找「详情页链接」字段；接口地址（/gateway/、/api/）不算。"""
    low = {str(k).lower(): str(k) for k in keys}

    def _ok(val: str) -> bool:
        v = str(val or '').strip()
        if not v.startswith(('http://', 'https://', '/')):
            return False
        return not re.search(r'/(?:gateway|api|chunk|ajax|interface)(?:/|$)', v, re.I)

    for c in _URL_KEYS:
        real = low.get(c.lower())
        if real and any(_ok(r.get(real)) for r in rows[:5]):
            return real
    # 退一步：值本身像详情页地址
    for k in keys:
        vals = [str(r.get(k) or '') for r in rows[:5]]
        if vals and all(v.startswith('http') for v in vals if v) and \
                any(re.search(r'(detail|content|info|notice|article|show)', v, re.I)
                    for v in vals):
            return k
    return ''


def _assemble_json_fields(lm: dict, keys: list[str], rows: list[dict],
                          detail_page_tpl: str, origin: str,
                          referer_tpl: str = '', curl_page_tpl: str = '') -> tuple[dict, str]:
    """把字段映射落成 discovery.fields（path / template）。

    返回 (fields, url_source)，url_source 说明详情页链接从哪来：
    field=列表自带链接 / example=用户给的示例地址 / detail=详情 curl 本身就是页面
    / referer=详情 curl 的 Referer / guess=没证据只能兜底（需提醒补示例）。
    """
    fields: dict = {}
    title_key = lm.get('title') or next(
        (k for c in _TITLE_GUESS for k in keys if k.lower() == c.lower()), '')
    if not title_key:
        title_key = next(
            (k for k in keys
             if isinstance((rows[0] if rows else {}).get(k), str)
             and 6 <= len(str(rows[0].get(k))) <= 200
             and re.search(r'[\u4e00-\u9fa5]', str(rows[0].get(k)))), '')
    if not title_key:
        title_key = keys[0] if keys else 'title'
    fields['title'] = {'path': title_key, 'strip': True}

    # 用户给的可能是「模板」也可能是「某一条公告的真实地址」：统一参数化，
    # 否则所有条目 url 相同 → 采集时被去重成 1 条、详情永远抓同一条。
    page_tpl = evd.page_url_template(detail_page_tpl, rows[0] if rows else {})

    id_key = lm.get('external_id') or ''
    if not id_key and page_tpl:
        m = re.search(r'\{(\w+)\}', page_tpl)
        if m and m.group(1) in keys:
            id_key = m.group(1)
    if id_key:
        fields['external_id'] = {'path': id_key}

    url_key = _pick_url_key(keys, rows)
    url_source = ''
    if url_key:
        fields['url'] = {'path': url_key, 'strip': True,
                         'fallback': [{'path': 'url'}, {'path': 'link'}]}
        url_source = 'field'
    elif page_tpl:
        fields['url'] = {'template': page_tpl}
        url_source = 'example'
    elif curl_page_tpl:
        # 详情 curl 本身就是 GET 页面请求 → 它就是一手的详情页地址模板
        fields['url'] = {'template': curl_page_tpl}
        url_source = 'detail'
    elif referer_tpl:
        fields['url'] = {'template': referer_tpl}
        url_source = 'referer'
    else:
        # 没有任何页面地址证据：用站点根 + id 兜底，只保证 url 唯一可落库
        tpl_id = '{%s}' % (id_key or 'id')
        fields['url'] = {'template': '%s/detail?id=%s' % (origin, tpl_id)}
        url_source = 'guess'

    for std in ('buyer', 'publish_at', 'region', 'source_name', 'budget'):
        if lm.get(std):
            spec = {'path': lm[std], 'strip': True}
            if std == 'publish_at':
                spec['type'] = 'datetime'
            fields[std] = spec
    return fields, url_source


def _assemble_detail_fields(dm: dict, detail_keys: list[str]) -> dict:
    out = {}
    for std in ('bid_no', 'notice_type', 'agency', 'category', 'contact'):
        key = dm.get(std)
        if key and key in detail_keys:
            out[std] = {'path': key, 'strip': True}
    return out


def _same_path(a: str, b: str) -> bool:
    try:
        return urlsplit(a).path.rstrip('/') == urlsplit(b).path.rstrip('/')
    except Exception:
        return False


def _page_request(detail_req: dict | None, list_url: str) -> dict:
    """网页型详情的请求：GET 条目 url，沿用详情 curl 的头（cookie 等）再补 Referer。"""
    headers = {}
    if detail_req:
        headers = {k: v for k, v in (detail_req.get('headers') or {}).items()
                   if k.lower() != 'content-type'}
    if not any(k.lower() == 'referer' for k in headers):
        headers['Referer'] = list_url
    return {'url': '{url}', 'method': 'GET', 'headers': headers}


def _referer_template(detail_req: dict | None, row: dict) -> str:
    """详情 curl 的 Referer 往往就是「详情页地址」，把它变成 URL 模板。"""
    if not detail_req:
        return ''
    referer = next((v for k, v in (detail_req.get('headers') or {}).items()
                    if k.lower() == 'referer'), '')
    if not referer or referer.rstrip('/').count('/') < 3:
        return ''
    t = evd.infer_detail_templates({'url': referer, 'params': {}, 'body': None}, row)
    return t['url'] if t.get('id_placeholder') and t['url'] != referer else ''


def _curl_page_template(detail_req: dict | None, row: dict) -> str:
    """详情 curl 本身是 GET 页面请求时，它的地址就是详情页 URL 的一手模板。"""
    if not detail_req or (detail_req.get('method') or 'GET').upper() != 'GET':
        return ''
    if detail_req.get('body') is not None:
        return ''
    t = evd.infer_detail_templates(detail_req, row)
    src = detail_req.get('url') or ''
    return t['url'] if t.get('id_placeholder') and t['url'] != src else ''


# ------------------------------------------------------------------ 关键词甄别
def _copy_container(body, params):
    return (json.loads(json.dumps(body)) if body is not None else None,
            json.loads(json.dumps(params)) if params is not None else {})


def _send_list(list_req: dict, body, params, timeout_s: float = 15) -> dict:
    """按用户 curl 的原始方式发一次列表请求，只取「有多少条 / 前几条标题」。"""
    import httpx
    headers = {k: v for k, v in (list_req.get('headers') or {}).items()}
    headers.setdefault('User-Agent', (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'))
    method = str(list_req.get('method') or 'GET').upper()
    kw = {'headers': headers, 'timeout': timeout_s,
          'follow_redirects': True, 'trust_env': False}
    try:
        if method == 'GET':
            kw['params'] = params or None
            r = httpx.get(list_req['url'], **kw)
        elif list_req.get('body_kind') == 'json':
            r = httpx.request(method, list_req['url'], json=body, **kw)
        elif list_req.get('body_kind') == 'form':
            r = httpx.request(method, list_req['url'], data=body, **kw)
        elif isinstance(body, str):
            r = httpx.request(method, list_req['url'], content=body.encode('utf-8'),
                              **kw)
        else:
            r = httpx.request(method, list_req['url'], **kw)
        if r.status_code >= 400:
            return {'ok': False, 'status': r.status_code}
        data = r.json()
    except Exception as e:
        return {'ok': False, 'error': str(e)[:120]}
    from .probe import _find_items, _find_total_value
    got = _find_items(data)
    if not got:
        # 关键词没命中时接口返回的是空数组（total=0），不是「没找到条目数组」
        if _find_total_value(data) == 0:
            return {'ok': True, 'n': 0, 'titles': [], 'total': 0}
        return {'ok': False, 'error': '响应里没有条目数组'}
    items = [x for x in got[1] if isinstance(x, dict)]
    titles = []
    for row in items[:3]:
        t = ''
        for k in ('title', 'noticeTitle', 'ancmHdlnCntnt', 'name', 'subject'):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                t = v.strip()[:60]
                break
        titles.append(t)
    return {'ok': True, 'n': len(items), 'titles': titles,
            'total': _find_total_value(data)}


def _anchor_word(rows: list[dict], title_key: str = '') -> str:
    """从样本标题里挑一段中文，作为「这个站点肯定搜得到」的阳性对照词。

    用来排除「塞什么都返回 0 条」的字段（日期、状态这类）。只看标题：正文
    里的词不一定是搜索可命中的。
    """
    keys = [title_key] if title_key else []
    keys += ['title', 'noticeTitle', 'ancmHdlnCntnt', 'messageTitle', 'name']
    for r in (rows or [])[:3]:
        if not isinstance(r, dict):
            continue
        for k in keys:
            v = r.get(k)
            if not isinstance(v, str) or len(v) < 8:
                continue
            m = re.search(r'[\u4e00-\u9fa5]{6,}', v)
            if m:
                return m.group(0)[:6]
    return ''


def _pick_keyword_slot(list_req: dict, body, params, keyword: str,
                       row_keys: list[str] | None = None,
                       anchor: str = '') -> tuple[dict | None, str]:
    """在候选槽位里用「换值看结果变不变」的办法找出真正的搜索字段。

    名字推断会误判（shardingKey 有 Key 却是分片键、ancmHdlnCntnt 没半个关键字
    却是搜索框）。这里把候选逐个换成关键词实发一次；结果集变了还不算数——
    日期/状态字段塞任何词都会变成 0 条，必须再用「样本里的真实标题片段」
    搜一次，能搜回数据才算文本搜索字段。
    """
    cands = evd.find_keyword_candidates(body, params, keyword, row_keys=row_keys)
    if not cands or not keyword:
        return None, ''
    if len(cands) == 1 and cands[0]['by'] == 'value':
        # 值就是用户搜的词，且没有别的候选：没什么可争的，省几次请求
        return cands[0], ''
    cands = cands[:3]

    def _probe(c: dict, value: str):
        tb, tp = _copy_container(body, params)
        # 只留当前候选在过滤：其他候选原有的词（如页面上的「硬件」）会把结果
        # 拉回同一个集合，让 shardingKey 这种无效字段看起来"变化了"
        for o in cands:
            if o is c or not str(o.get('old') or '').strip():
                continue
            box = tb if o['container'] == 'body' else tp
            if isinstance(box, dict):
                evd.set_path(box, o['path'], '')
        box = tb if c['container'] == 'body' else tp
        if not isinstance(box, dict):
            return None
        evd.set_path(box, c['path'], value)
        got = _send_list(list_req, tb, tp)
        if not got.get('ok'):
            return None
        got['sig'] = (got.get('total'), tuple(got.get('titles') or []))
        return got

    # 基线：把「本来就有值的候选」置空（样本里原有的搜索词会干扰比较）。
    # 空值候选不动——orgType 这类必填项清空会让接口直接不返回数据。
    base_body, base_params = _copy_container(body, params)
    for c in cands:
        if not str(c.get('old') or '').strip():
            continue
        box = base_body if c['container'] == 'body' else base_params
        if isinstance(box, dict):
            evd.set_path(box, c['path'], '')
    base = _send_list(list_req, base_body, base_params)
    if not base.get('ok'):
        # 清空候选后接口不认（有些字段清空即报错）：退回原始请求当基线
        base = _send_list(list_req, body, params)
    if not base.get('ok'):
        best = cands[0]
        return {'container': best['container'], 'path': best['path'],
                'old': best['old'], 'by': best['by']}, ''
    base_sig = (base.get('total'), tuple(base.get('titles') or []))

    tried = []
    for c in cands:
        r1 = _probe(c, keyword)
        if r1 is None:
            tried.append('%s(请求失败)' % c['leaf'])
            continue
        if r1['sig'] == base_sig:
            tried.append('%s✗' % c['leaf'])
            continue
        if anchor:
            r2 = _probe(c, anchor)
            if r2 is None or r2['sig'] == r1['sig'] or \
                    not (r2.get('total') or r2.get('n')):
                # 换成样本里的真实标题片段依然没有结果 → 不是文本搜索字段
                tried.append('%s✗' % c['leaf'])
                continue
        tried.append('%s✓' % c['leaf'])
        return ({'container': c['container'], 'path': c['path'],
                 'old': c['old'], 'by': 'probe'},
                '关键词参数经在线验证生效：%s（换词后结果集随之变化）' % c['path'])
    return cands[0], (
        '试了 %s，都没能改变检索结果：该接口可能不支持关键词过滤，采集会抓到'
        '全站公告。请确认后手动把关键词参数改成 {keyword}。' % '、'.join(tried[:4]))


# ------------------------------------------------------------------ 配方装配
def _sample_hint(html: str) -> str:
    """详情样本抽不出正文时，判断是不是反爬/验证页，给出可操作提示。"""
    s = (html or '')[:200000]
    if re.search(r'(acw_sc__v2|cdn-cgi|__jsl_clearance|cf_chl_|验证码|滑动验证|请开启JS|'
                 r'访问验证|人机验证)', s, re.I):
        return '（样本像站点的反爬/验证页，只有脚本没有正文。请在浏览器里正常打开详情页后' \
               '「查看网页源代码」再复制，或改用详情接口的 JSON 响应）'
    plain = re.sub(r'<[^>]+>', ' ', s)
    n = len(re.sub(r'\s+', '', plain))
    if n < 800:
        return '（样本正文只有 %d 字，像是 SPA 空壳或未登录页。请在登录状态下重新复制，' \
               '或改贴详情接口的 JSON 响应）' % n
    return ''


def build_json_recipe(*, list_req: dict, list_info: dict, detail_req: dict | None,
                      detail_info: dict | None, pdf_info: dict | None = None,
                      detail_html: bool = False, keyword: str, name: str,
                      site_url: str, detail_page_tpl: str = '',
                      with_llm: bool = True) -> tuple[dict, str, dict]:
    """api_list 模式：JSON 列表 + JSON/HTML 详情。"""
    host = evd.host_of(list_req)
    origin = evd.origin_of(list_req)
    pid = _platform_id(host)
    keys = list_info['keys']
    rows_sample = list_info['rows_sample']
    first_row = list_info['first_row']

    llm_l, llm_d, llm_name = ({}, {}, '')
    if with_llm:
        llm_l, llm_d, llm_name = _llm_map(
            keys, rows_sample, (detail_info or {}).get('keys') or [],
            (detail_info or {}).get('row_sample') or {}, site_url or list_req['url'])
    heu_l, strong_l = _heuristic_map(keys, LIST_FIELD_MAP)
    lm = _merge_map(heu_l, strong_l, llm_l, keys)
    # 名字推断会指错列（areaName 含 name 被当成标题、createTime 整列为 null）。
    # 用样本值复核一遍，空列/日期列/枚举列都过不了。
    rows_raw = list_info.get('sample_rows_raw') or [first_row]
    lm = evd.refine_field_map(lm, keys, rows_raw)
    dkeys = (detail_info or {}).get('keys') or []
    heu_d, strong_d = _heuristic_map(dkeys, DETAIL_FIELD_MAP)
    dm = _merge_map(heu_d, strong_d, llm_d, dkeys)
    source = 'llm' if (llm_l or llm_d or llm_name) else 'heuristic'

    fields, url_source = _assemble_json_fields(
        lm, keys, rows_raw, detail_page_tpl, origin,
        referer_tpl=_referer_template(detail_req, first_row),
        curl_page_tpl=_curl_page_template(detail_req, first_row))
    # 列表里的相对链接可能需要前缀（如 /cms/default/webfile）才能打开详情页：
    # 用详情 curl 的路径反推，避免「拼域名后 200 但正文是空白页」。
    if detail_req and isinstance(fields.get('url'), dict) and 'path' in fields['url']:
        ukey = str(fields['url']['path'])
        if '.' not in ukey:
            rv = str(first_row.get(ukey) or '')
            prefix = evd.match_url_prefix(
                rv, detail_req.get('raw_url') or detail_req.get('url') or '')
            if prefix:
                joiner = '' if rv.startswith('/') else '/'
                fields['url'] = {'template': origin + prefix + joiner + '{%s}' % ukey,
                                 'strip': True}

    # 关键词 / 翻页参数
    kw_slot = evd.find_keyword_slot(list_req.get('body'), list_req.get('params'), keyword)
    kw_note = ''
    # 名字推断常常指到分片键/流水号上（服务端会忽略），实发一次确认它能改变结果
    probed, kw_note = _pick_keyword_slot(
        list_req, list_req.get('body'), list_req.get('params'), keyword,
        row_keys=keys,
        anchor=_anchor_word(rows_raw, (lm.get('title') or '') if isinstance(lm.get('title'), str) else ''))
    if probed:
        kw_slot = probed
    page = evd.find_page_slot(list_req.get('body'), list_req.get('params'))

    body = list_req.get('body')
    params = dict(list_req.get('params') or {})
    container = {'body': body, 'params': params}

    kw_used = False
    if kw_slot:
        target = container.get(kw_slot['container'])
        if isinstance(target, dict):
            evd.set_path(target, kw_slot['path'], '{keyword}')
            kw_used = True
        else:
            params[kw_slot['path']] = '{keyword}'
            kw_used = True
    body = container['body']

    # 页数上限要够覆盖全量：接口不认关键词时只能靠翻页，固定 20 页会只采到 200 条
    size = int(page['size'] or 20)
    total_v = list_info.get('total_value') or 0
    max_pages = 20
    try:
        if total_v and size:
            max_pages = min(300, max(20, -(-int(total_v) // size) + 1))
    except Exception:
        max_pages = 20
    pag = {
        'type': 'page' if page['page_param'] else 'none',
        'page_param': page['page_param'] or '',
        'start': int(page['page'] or 1),
        'size_param': page['size_param'] or '',
        'size': size,
        'total_path': list_info.get('total_path') or '',
        'max_pages': max_pages,
        'stop_when': ['empty_items', 'page_unchanged', 'reached_total'],
    }

    transport = _base_transport(list_req, '', site_url)
    discovery = {
        'mode': 'json_api',
        'request': {'url': list_req['url'], 'method': list_req['method'],
                    'params': params or None, 'body': body},
        'pagination': pag,
        'items_path': list_info['items_path'],
        'total_path': list_info.get('total_path') or '',
        'fields': fields,
    }

    detail: dict = {'enabled': False}
    content_css = (detail_info or {}).get('content_css') or ''
    has_json_content = bool((detail_info or {}).get('content_path'))
    has_pdf = bool((pdf_info or {}).get('pdf_url'))
    # 详情样本是网页（HTML）时，详情请求必须拿页面本身；哪怕用户贴的详情 curl
    # 指向的是接口（/gateway/、/api/），也不能用接口地址去取 .content 容器。
    page_mode = bool(content_css or has_pdf) and detail_html
    if (detail_req or content_css) and (content_css or has_json_content or has_pdf):
        if detail_req and not page_mode:
            tpl = evd.infer_detail_templates(
                detail_req, first_row,
                list_url_field=next((k for k, v in fields.items()
                                     if k == 'url' and 'path' in v), ''),
                rows=list_info.get('sample_rows_raw') or [first_row])
            dheaders = dict(detail_req.get('headers') or {})
            req = {'url': tpl['url'], 'method': detail_req['method'],
                   'headers': dheaders}
            if tpl['params']:
                req['params'] = tpl['params']
            if tpl['body'] is not None:
                req['body'] = tpl['body']
            # 详情本身是网页（CMS 详情页）：直接用列表里的 url，不再拼 id
            row_url = recipe_mod.resolve_field(fields.get('url') or {}, first_row)
            if content_css and row_url and _same_path(row_url, detail_req['url']):
                req = _page_request(detail_req, list_req['url'])
        else:
            # 网页型详情：详情页就是列表条目的 url（没有详情 curl 也一样）
            req = _page_request(detail_req, list_req['url'])

        if content_css:
            detail = {
                'enabled': True,
                'id_field': 'external_id' if 'external_id' in fields else 'url',
                'request': req,
                'content_css': content_css,
                'content_encoding': 'html',
                'extract': {'scope': 'html',
                            'fields': ({'title': {'css': detail_info['title_css'],
                                                  'strip': True}}
                                       if detail_info.get('title_css') else {})},
                'ai': _ai_detail_cfg(),
                'attachments': {'enabled': False},
            }
        elif has_json_content:
            dkeys = detail_info.get('keys') or []
            detail = {
                'enabled': True,
                'id_field': 'external_id' if 'external_id' in fields else 'id',
                'request': req,
                'row_path': detail_info.get('row_path') or '$',
                'content_path': detail_info.get('content_path') or '',
                'content_encoding': detail_info.get('content_encoding') or 'html',
                'extract': {'scope': 'json',
                            'fields': _assemble_detail_fields(dm, dkeys)},
                'ai': _ai_detail_cfg(),
                'attachments': {'enabled': False},
            }
        else:
            detail = {
                'enabled': True,
                'id_field': 'external_id' if 'external_id' in fields else 'url',
                'request': req,
                'extract': {'scope': 'html', 'fields': {}},
                'ai': _ai_detail_cfg(),
                'attachments': {'enabled': False},
            }
        if (pdf_info or {}).get('pdf_url'):
            detail['pdf'] = {
                'enabled': True,
                'file_id_regex': pdf_info.get('file_id_regex') or '',
                'url_template': pdf_info.get('url_template') or '',
                'iframe_css': pdf_info.get('iframe_css') or '',
                'max_bytes': 8 * 1024 * 1024, 'max_chars': 20000, 'timeout_s': 30,
            }
            detail['content_source'] = 'pdf'

    expected = {k: (0.6 if k == 'buyer' else 0.8)
                for k in ('buyer', 'publish_at', 'region', 'source_name') if k in fields}
    validation = {
        'min_items_per_keyword': 1,
        'page2_must_differ': False,
        'required_fields': {'title': 1.0, 'url': 1.0},
        'expected_nonempty': expected,
        'url_reachable_rate': 0.9,
        'sample_keyword': keyword,
    }
    if detail.get('enabled'):
        validation['detail_expected_nonempty'] = {
            'budget': 0.5, 'fee_note': 0.5, 'scope': 0.5,
            'doc_get_end': 0.5, 'open_at': 0.5}

    notes = ('由用户提供的接口证据生成：列表 %s %s（items_path %s，关键词参数 %s，'
             '翻页参数 %s，详情页链接来源 %s）→ 详情 %s（%s.%s）。'
             '请求头、请求体均取自用户粘贴的真实 curl，未做猜测；'
             '新配方默认 status=paused 且关闭全局采购人白名单，请审核后再启用。'
             % (list_req['method'], list_req['url'], list_info['items_path'],
                kw_slot['path'] if kw_slot else '未识别',
                page['page_param'] or '单页', _URL_SOURCE_TXT.get(url_source, url_source),
                (detail.get('request') or {}).get('url') or '-',
                (detail_info or {}).get('row_path') or '-',
                (detail_info or {}).get('content_path') or '-'))

    recipe = {
        'meta': {'id': pid, 'name': _site_name(list_req, name), 'host': host,
                 'entry_url': site_url or list_req['url'], 'version': 1,
                 'status': 'paused', 'created_by': 'ai', 'notes': notes,
                 'base_url': origin},
        'transport': transport,
        'discovery': discovery,
        'detail': detail,
        'normalize': {'filters': {'inherit': 'global', 'override': {},
                                  'buyer_whitelist': False},
                      'amount': {'target': 'budget', 'units': {'万元': 10000, '元': 1}},
                      'keep_url': True},
        'validation': validation,
    }
    return recipe, source, {'keyword_used': kw_used, 'kw_slot': kw_slot, 'page': page,
                            'kw_note': kw_note, 'total_value': total_v,
                            'url_source': url_source,
                            'page_tpl_input': detail_page_tpl,
                            'page_tpl_failed': bool(detail_page_tpl)
                            and url_source != 'example'}


def build_html_recipe(*, list_req: dict, list_info: dict, detail_req: dict | None,
                      detail_info: dict | None, pdf_info: dict | None,
                      keyword: str, name: str, site_url: str,
                      detail_page_tpl: str = '', page_hint: dict | None = None,
                      with_llm: bool = True) -> tuple[dict, str, dict]:
    """html_list 模式：HTML 列表 + HTML/PDF 详情。"""
    host = evd.host_of(list_req)
    origin = evd.origin_of(list_req)
    pid = _platform_id(host)
    fields = dict(list_info['fields'])

    # 用户给的详情页 URL 模板/示例优先：列表行里常有分类、导航等干扰链接，
    # 直接指定「host + href」最省事（支持 {href} 写法）。
    url_tpl = evd.html_url_template(detail_page_tpl, list_info)
    if url_tpl:
        fields['url'] = {'template': url_tpl}

    llm_name = ''
    source = 'heuristic'
    if with_llm:
        _l, _d, llm_name = _llm_map([], [], [], {}, site_url or list_req['url'])
        source = 'llm' if llm_name else 'heuristic'

    params = dict(list_req.get('params') or {})
    page = evd.find_page_slot(None, params)
    kw_slot = evd.find_keyword_slot(None, params, keyword)
    kw_used = False
    if kw_slot:
        params[kw_slot['path']] = '{keyword}'
        kw_used = True

    pagination = {
        'type': 'page' if page['page_param'] else 'none',
        'page_param': page['page_param'] or '',
        'start': int(page['page'] or 1),
        'max_pages': 20,
        'stop_when': ['empty_items', 'page_unchanged', 'reached_total'],
    }
    if page['size_param']:
        pagination['size_param'] = page['size_param']
        pagination['size'] = int(page['size'] or 20)
    req_url = list_req['url']
    if page_hint:
        # 页码在路径里（search.jspx → search_2.jspx）：url 模板带 {page}，
        # 第 1 页用 first_url 覆盖（paginate._inject 处理）。
        pagination = {
            'type': 'page', 'page_in_url': True, 'page_param': '',
            'start': 1, 'max_pages': 20,
            'first_url': page_hint['first_url'],
            'url_template': page_hint['url_template'],
            'stop_when': ['empty_items', 'page_unchanged', 'reached_total'],
        }
        req_url = page_hint['url_template']

    detail: dict = {'enabled': False}
    if detail_info or pdf_info:
        content_css = (detail_info or {}).get('content_css') or ''
        dfields = {}
        if (detail_info or {}).get('title_css'):
            dfields['title'] = {'css': detail_info['title_css'], 'strip': True}
        detail = {
            'enabled': True,
            'id_field': 'external_id' if 'external_id' in fields else 'url',
            'request': {'url': '{url}', 'method': 'GET',
                        'headers': {'Referer': list_req['url']}},
            'content_css': content_css,
            'content_encoding': 'html',
            'extract': {'scope': 'html', 'fields': dfields},
            'ai': _ai_detail_cfg(),
            'attachments': {'enabled': False},
        }
        if pdf_info and pdf_info.get('pdf_url'):
            detail['pdf'] = {
                'enabled': True,
                'file_id_regex': pdf_info.get('file_id_regex') or '',
                'url_template': pdf_info.get('url_template') or '',
                'iframe_css': pdf_info.get('iframe_css') or '',
                'max_bytes': 8 * 1024 * 1024, 'max_chars': 20000, 'timeout_s': 30,
            }
            detail['content_source'] = 'pdf'
            if not content_css:
                detail['content_encoding'] = 'html'
        elif not content_css:
            detail['enabled'] = False

    expected = {k: 0.8 for k in ('buyer', 'publish_at', 'region', 'source_name')
                if k in fields}
    validation = {
        'min_items_per_keyword': 1,
        'page2_must_differ': bool(pagination['type'] != 'none'),
        'required_fields': {'title': 1.0, 'url': 1.0},
        'expected_nonempty': expected,
        'url_reachable_rate': 0.9,
        'sample_keyword': keyword,
    }
    if detail.get('enabled'):
        validation['detail_expected_nonempty'] = {
            'buyer': 0.5, 'scope': 0.5, 'doc_get_end': 0.5, 'open_at': 0.5}

    notes = ('由用户提供的 HTML 证据生成：列表 %s %s（row_selector %s，%s 行/样本，'
             '关键词参数 %s，分页 %s，详情页链接 %s）→ 详情 %s。'
             '选择器均由样本回测命中后才写入；新配方默认 status=paused，请审核后再启用。'
             % (list_req['method'], list_req['url'], list_info['row_selector'],
                list_info.get('rows'), kw_slot['path'] if kw_slot else '未识别',
                page['page_param'] or '单页',
                ('用户模板 %s' % url_tpl) if url_tpl else '行内链接选择器',
                'PDF 正文' if (pdf_info or {}).get('pdf_url')
                else (detail.get('content_css') or '未配置')))

    recipe = {
        'meta': {'id': pid, 'name': _site_name(list_req, name, '') or host, 'host': host,
                 'entry_url': site_url or list_req['url'], 'version': 1,
                 'status': 'paused', 'created_by': 'ai', 'notes': notes,
                 'base_url': origin},
        'transport': _base_transport(list_req, '', site_url),
        'discovery': {
            'mode': 'html_list',
            'request': {'url': req_url, 'method': list_req['method'],
                        'params': params or None, 'body': list_req.get('body')},
            'pagination': pagination,
            'row_selector': list_info['row_selector'],
            'fields': fields,
        },
        'detail': detail,
        'normalize': {'filters': {'inherit': 'global', 'override': {},
                                  'buyer_whitelist': False},
                      'amount': {'target': 'budget', 'units': {'万元': 10000, '元': 1}},
                      'keep_url': True},
        'validation': validation,
    }
    return recipe, source, {'keyword_used': kw_used, 'kw_slot': kw_slot, 'page': page,
                            'url_source': 'example' if url_tpl else 'field',
                            'page_tpl_input': detail_page_tpl,
                            'page_tpl_failed': bool(detail_page_tpl) and not url_tpl}


# ------------------------------------------------------------------ 离线回测
def offline_verify(recipe: dict, list_sample: str, detail_sample: str = '') -> dict:
    """用用户给的样本跑一遍规则（不联网）：条目数 / 字段非空率 / 正文长度。"""
    out = {'valid': False, 'items': 0, 'total': None, 'field_coverage': {},
           'detail_len': 0, 'distinct_urls': 0, 'errors': []}
    ds = recipe.get('discovery') or {}
    mode = str(ds.get('mode') or '')
    fields = ds.get('fields') or {}

    if mode == 'json_api':
        try:
            data = json.loads(list_sample)
        except Exception as e:
            out['errors'].append('列表样本不是合法 JSON：%s' % str(e)[:100])
            return out
        import jsonpath_ng
        try:
            rows = [m.value for m in jsonpath_ng.parse(ds['items_path']).find(data)
                    if isinstance(m.value, dict)]
        except Exception as e:
            out['errors'].append('items_path 无效：%s' % str(e)[:100])
            return out
        if isinstance(data, dict) and ds.get('total_path'):
            try:
                t = jsonpath_ng.parse(ds['total_path']).find(data)
                out['total'] = t[0].value if t else None
            except Exception:
                out['total'] = None
        out['items'] = len(rows)
        cov = {}
        for name, spec in fields.items():
            hit = sum(1 for r in rows if recipe_mod.resolve_field(spec, r))
            cov[name] = round(hit / max(1, len(rows)), 2)
        out['field_coverage'] = cov
        out['distinct_urls'] = len({
            recipe_mod.resolve_field(fields.get('url') or {}, r) for r in rows})
        detail = recipe.get('detail') or {}
        if detail.get('enabled') and detail_sample and \
                detail.get('extract', {}).get('scope') in ('json', 'json_api'):
            try:
                dd = json.loads(detail_sample)
            except Exception:
                dd = None
            if isinstance(dd, dict):
                node = dd
                rp = str(detail.get('row_path') or '')
                if rp and rp != '$':
                    try:
                        m = jsonpath_ng.parse(rp).find(dd)
                        node = m[0].value if m else None
                    except Exception:
                        node = None
                if isinstance(node, dict):
                    val = node.get(detail.get('content_path') or '')
                    out['detail_len'] = len(str(val or ''))
                else:
                    out['errors'].append('详情 row_path %s 未定位到对象' % rp)
    else:
        from parsel import Selector
        from .extract.html_list import row_context
        from .recipe import resolve_field
        sel = Selector(text=evd._clean(list_sample))
        try:
            rows = list(sel.css(str(ds.get('row_selector') or '')))
        except Exception as e:
            out['errors'].append('row_selector 无效：%s' % str(e)[:100])
            return out
        out['items'] = len(rows)
        ctxs = [row_context(r) for r in rows]
        cov = {}
        for name, spec in fields.items():
            hit = sum(1 for r, c in zip(rows, ctxs)
                      if resolve_field(spec, c, text=c.get('_text') or '', selector=r))
            cov[name] = round(hit / max(1, len(rows)), 2)
        out['field_coverage'] = cov
        out['distinct_urls'] = len({
            resolve_field(fields.get('url') or {}, c, text=c.get('_text') or '', selector=r)
            for r, c in zip(rows, ctxs)})
        detail = recipe.get('detail') or {}
        if detail.get('enabled') and detail_sample:
            try:
                dsel = Selector(text=evd._clean(detail_sample))
                nodes = dsel.css(str(detail.get('content_css') or ''))
                text = max((' '.join(n.xpath('.//text()').getall()) for n in nodes[:5]),
                           key=len) if nodes else ''
                out['detail_len'] = len(re.sub(r'\s+', ' ', text).strip())
            except Exception:
                out['detail_len'] = 0

    if not out['items']:
        out['errors'].append('样本里 0 条记录：条目规则未命中')
    if out['field_coverage'].get('title', 0) < 0.5:
        out['errors'].append('title 非空率 %d%%' % round(
            out['field_coverage'].get('title', 0) * 100))
    if out['field_coverage'].get('url', 0) < 0.5:
        out['errors'].append('url 非空率 %d%%' % round(
            out['field_coverage'].get('url', 0) * 100))
    # 详情页模板没参数化时，所有条目会指向同一条公告：采集时被 URL 去重成 1 条，
    # 详情也永远抓同一条。样本里就能看出来，必须在这里拦住。
    if out['items'] > 1 and out.get('distinct_urls') == 1:
        out['errors'].append('所有条目 url 完全相同（详情页模板没有替换成 id 占位符）')
    out['valid'] = not out['errors']
    return out


# ------------------------------------------------------------------ 主入口
def analyze(payload: dict) -> dict:
    """按模式解析用户提供的 curl 与样本，产出可试跑的配方。

    payload:
      mode: api_list | html_list
      list_curl: 列表请求 curl（必填）
      list_sample: 列表返回样本（JSON 文本 / HTML）
      detail_curl / detail_sample: 详情（可选）
      keyword: 样本关键词（可选，用于定位关键词参数）
      name: 站点中文名（可选）
      site_url: 列表页 URL（可选，缺省用 curl 的地址）
      detail_page_tpl: 详情页 URL 模板（可选，如 https://x/#/detail?id={messageId}）
      with_llm / verify: 开关
    """
    t_all = time.monotonic()
    diag: dict = {}
    mode = str(payload.get('mode') or 'api_list').strip()
    list_curl = str(payload.get('list_curl') or '')
    list_sample = str(payload.get('list_sample') or '')
    detail_curl = str(payload.get('detail_curl') or '')
    detail_sample = str(payload.get('detail_sample') or '')
    keyword = str(payload.get('keyword') or '').strip() or DEFAULT_KEYWORD
    name = str(payload.get('name') or '').strip()
    site_url = str(payload.get('site_url') or '').strip()
    detail_page_tpl = str(payload.get('detail_page_tpl') or '').strip()
    with_llm = payload.get('with_llm', True) is not False
    do_verify = payload.get('verify', True) is not False
    stages: list[dict] = []

    if mode not in VALID_MODES:
        return {'ok': False, 'error': 'mode 只能是 %s' % ' / '.join(VALID_MODES),
                'stages': stages}
    if not list_curl.strip():
        return {'ok': False, 'error': '请粘贴列表请求的 curl', 'stages': stages}
    if not list_sample.strip():
        return {'ok': False, 'error': '请粘贴列表接口的返回样本', 'stages': stages}

    # ---- 1. 解析列表 curl
    t0 = time.monotonic()
    lreq = evd.parse_curl(list_curl)
    if not lreq['ok']:
        return {'ok': False, 'error': '列表 curl 解析失败：%s' % lreq['error'],
                'stages': stages + [_stage('解析列表 curl', 'error', lreq['error'], t0)]}
    stages.append(_stage(
        '解析列表 curl', 'ok',
        '%s %s · %s 体 · %d 个头' % (lreq['method'], lreq['url'],
                                    lreq['body_kind'] or '无请求体',
                                    len(lreq['headers'])), t0))

    # ---- 2. 解析列表样本
    t0 = time.monotonic()
    kind = evd.sample_kind(list_sample)
    if mode == 'api_list' and kind != 'json':
        return {'ok': False,
                'error': '列表样本不像 JSON（%s）。接口型站点请粘贴 JSON 响应；'
                         '如果是 HTML 页面请切换「HTML 列表」模式' % (kind or '空'),
                'stages': stages + [_stage('解析列表样本', 'error', '样本类型 %s' % kind, t0)]}
    if mode == 'html_list' and kind != 'html':
        return {'ok': False,
                'error': '列表样本不像 HTML（%s）。HTML 型站点请粘贴页面源码；'
                         '如果是 JSON 请切换「接口 JSON」模式' % (kind or '空'),
                'stages': stages + [_stage('解析列表样本', 'error', '样本类型 %s' % kind, t0)]}

    detail_req = evd.parse_curl(detail_curl) if detail_curl.strip() else None
    detail_info, pdf_info = None, None
    detail_html = False

    if mode == 'api_list':
        data = evd.load_sample(list_sample)
        info = evd.infer_json_list(data)
        if not info:
            return {'ok': False,
                    'error': '样本里没有找到「对象数组」（条目列表），请粘贴列表接口的完整响应',
                    'stages': stages + [_stage('解析列表样本', 'error',
                                               '未定位到条目数组', t0)]}
        stages.append(_stage('解析列表样本', 'ok',
                             'items_path %s · %d 条 · %d 个字段%s'
                             % (info['items_path'], info['count'], len(info['keys']),
                                ' · total_path %s' % info['total_path']
                                if info['total_path'] else ''), t0))
        if detail_sample.strip():
            t0 = time.monotonic()
            dkind = evd.sample_kind(detail_sample)
            if dkind == 'json':
                detail_info = evd.infer_json_detail(evd.load_sample(detail_sample))
                stages.append(_stage(
                    '解析详情样本', 'ok',
                    '%s%s' % (detail_info['row_path'],
                              '.' + detail_info['content_path']
                              if detail_info['content_path'] else '（未找到正文长文本）'),
                    t0))
            elif dkind == 'html':
                from .probe import find_pdf_source
                detail_html = True
                html_info = evd.infer_html_detail(detail_sample)
                pdf_info = find_pdf_source(
                    detail_sample, lreq['url'], list_html=list_sample,
                    content_css=(html_info or {}).get('content_css') or '')
                clen = int((html_info or {}).get('content_len') or 0)
                if pdf_info and not pdf_info.get('from_iframe') and clen >= 300:
                    pdf_info = None
                if pdf_info and pdf_info.get('from_iframe') and clen >= 1500:
                    pdf_info = None
                if html_info:
                    detail_info = {'row_path': '$', 'content_path': '',
                                   'content_encoding': 'html',
                                   'content_css': html_info['content_css'],
                                   'title_css': html_info.get('title_css') or '',
                                   'keys': [], 'row_sample': {}}
                    stages.append(_stage('解析详情样本', 'ok',
                                         'HTML 正文容器 %s（%d 字符）'
                                         % (html_info['content_css'],
                                            html_info['content_len']), t0))
                elif pdf_info:
                    stages.append(_stage('解析详情样本', 'ok',
                                         '详情正文为 PDF：%s'
                                         % pdf_info['pdf_url'][:120], t0))
                else:
                    stages.append(_stage('解析详情样本', 'warn',
                                         '未识别出正文容器' + _sample_hint(detail_sample),
                                         t0))
            else:
                stages.append(_stage('解析详情样本', 'warn',
                                     '样本类型 %s 不支持' % dkind, t0))

        recipe, source, meta_info = build_json_recipe(
            list_req=lreq, list_info=info, detail_req=detail_req,
            detail_info=detail_info, pdf_info=pdf_info, detail_html=detail_html,
            keyword=keyword, name=name,
            site_url=site_url, detail_page_tpl=detail_page_tpl, with_llm=with_llm)
        list_info = info
    else:
        try:
            info = evd.infer_html_list(list_sample,
                                       base_url=site_url or lreq['url'],
                                       diag=diag)
        except Exception as e:
            # 样本结构千奇百怪（工具类名、非法 CSS、截断的 HTML），
            # 解析器自身异常也要变成一句能照着改的提示，而不是 500。
            return {'ok': False,
                    'error': '列表样本解析失败：%s。请确认粘贴的是完整的列表结果 HTML'
                             '（可从浏览器「查看源代码」复制），或改用「接口 JSON」模式'
                             % str(e)[:160],
                    'stages': stages + [_stage('解析列表样本', 'error',
                                               '解析异常：%s' % str(e)[:160], t0)]}
        if not info:
            why = str(diag.get('reason') or '').strip()
            return {'ok': False,
                    'error': '没能从列表页 HTML 里识别出重复的公告行（row_selector）'
                             + ('：' + why if why else '') +
                             '。请确认粘贴的是「列表结果」所在的那段完整 HTML'
                             '（含更多条目），或改用「接口 JSON」模式',
                    'diag': diag,
                    'generator_version': GENERATOR_VERSION,
                    'stages': stages + [_stage('解析列表样本', 'error',
                                               '未识别出列表行' + ('：' + why if why else ''),
                                               t0)]}
        stages.append(_stage('解析列表样本', 'ok',
                             'row_selector %s · %d 行 · 字段 %s'
                             % (info['row_selector'], info['rows'],
                                '/'.join(info['fields'].keys())), t0))
        # 页码在路径里的站点（search.jspx → search_2.jspx）：query 里没有页码，
        # 只看参数会判成「单页」，结果永远只采第一页。
        page_hint = evd.infer_path_pagination(list_sample, lreq['url'])
        if page_hint:
            stages.append(_stage('识别路径分页', 'ok',
                                 '%s（第 1 页 %s）'
                                 % (page_hint['url_template'], page_hint['first_url']),
                                 time.monotonic()))
        if detail_sample.strip():
            t0 = time.monotonic()
            html_info = evd.infer_html_detail(detail_sample)
            from .probe import find_pdf_source
            pdf_info = find_pdf_source(detail_sample, lreq['url'],
                                       list_html=list_sample,
                                       content_css=(html_info or {}).get('content_css') or '')
            # 正文容器里有足够的文字，就以 HTML 为正文：门户页脚常年挂着
            # 《平台注册规则.pdf》之类文档，误当正文会把详情写成别人的附件。
            # 例外：页面本身就是 PDF 查看器（iframe 指向 pdfjs），此时 HTML 容器
            # 里只有"点击查看附件"几个字，正文必须以 PDF 为准。
            clen = int((html_info or {}).get('content_len') or 0)
            if pdf_info and not pdf_info.get('from_iframe') and clen >= 300:
                stages.append(_stage(
                    'PDF 直链', 'ok',
                    '页面另有 PDF（%s），但正文容器 %d 字符，本配方以 HTML 正文为准'
                    % (pdf_info['pdf_url'][:80], clen), t0))
                pdf_info = None
            if pdf_info and pdf_info.get('from_iframe') and clen >= 1500:
                # 查看器 + 大段正文同时存在：正文更可能是 HTML，PDF 只是附件
                pdf_info = None
            if html_info:
                detail_info = html_info
                stages.append(_stage('解析详情样本', 'ok',
                                     '正文容器 %s（%d 字符）'
                                     % (html_info['content_css'],
                                        html_info['content_len']), t0))
            elif pdf_info:
                detail_info = None
                stages.append(_stage('解析详情样本', 'ok',
                                     '详情正文为 PDF：%s' % pdf_info['pdf_url'][:120], t0))
            else:
                stages.append(_stage('解析详情样本', 'warn',
                                     '未识别出正文容器 / PDF 直链'
                                     + _sample_hint(detail_sample), t0))

        recipe, source, meta_info = build_html_recipe(
            list_req=lreq, list_info=info, detail_req=detail_req,
            detail_info=detail_info, pdf_info=pdf_info, keyword=keyword,
            name=name, site_url=site_url, detail_page_tpl=detail_page_tpl,
            page_hint=page_hint, with_llm=with_llm)

    # ---- 3. 结构校验
    list_info = info
    detail_missing = bool(detail_sample.strip()) and not \
        (recipe.get('detail') or {}).get('enabled')
    if detail_missing:
        stages.append(_stage(
            '详情装配', 'warn',
            '给了详情样本但没能装配出详情请求（接口型详情需要同时提供详情 curl），'
            '本次只生成列表配方', time.monotonic()))
    url_warn = ''
    if meta_info.get('page_tpl_failed'):
        url_warn = ('你填的详情页地址里找不到与列表字段对应的 id（或该地址不含 id），'
                    '无法参数化，已忽略。请改填某一条公告的完整真实地址，'
                    '系统会自动套成 {字段} 模板。')
    elif meta_info.get('url_source') == 'guess':
        url_warn = ('列表结果里没有详情页链接，跳转地址只能用站点根 + id 兜底，'
                    '点开可能 404。请在「详情页 URL 示例」里粘贴一条公告的真实地址，'
                    '系统会自动参数化成模板。')
    if url_warn:
        stages.append(_stage('详情页链接', 'warn', url_warn, time.monotonic()))
    t0 = time.monotonic()
    errors = recipe_mod.validate_recipe(recipe)
    stages.append(_stage('配方结构校验', 'error' if errors else 'ok',
                         '；'.join(errors[:3]) if errors else '结构完整', t0))
    if errors:
        return {'ok': False, 'recipe': recipe,
                'verify': {'valid': False, 'errors': errors},
                'stages': stages, 'mapping_source': source,
                'error': '生成的配方未通过结构校验：%s' % '；'.join(errors[:3])}

    # ---- 4. 离线回测（样本即真相）
    t0 = time.monotonic()
    off = offline_verify(recipe, list_sample, detail_sample)
    stages.append(_stage(
        '样本回测', 'ok' if off['valid'] else 'warn',
        '%d 条 · 字段非空率 %s%s' % (
            off['items'], json.dumps(off['field_coverage'], ensure_ascii=False),
            '' if not off['errors'] else '；' + '；'.join(off['errors'][:2])), t0))

    # ---- 5. 在线试跑
    vr = off
    if do_verify:
        t0 = time.monotonic()
        try:
            online = verify_recipe(recipe, keyword)
            if online.get('waf') == 'browser':
                stages.append(_stage(
                    '反爬过盾', 'ok',
                    '详情页是反爬 JS 挑战页（acw_sc__v2 / 瑞数），已用无头浏览器换取 cookie 后取到正文',
                    t0))
            detail_on = bool((recipe.get('detail') or {}).get('enabled'))
            if online.get('empty_by_keyword'):
                trial_txt = ('0 条：关键词「%s」在该站点检索不到公告，未做详情试跑；'
                             '配方结构已在样本上回测通过' % keyword)
            elif detail_on:
                trial_txt = '%d 条 · 详情正文 %d 字符%s' % (
                    online.get('items') or 0, online.get('detail_len') or 0,
                    '' if online.get('valid')
                    else '；' + '；'.join((online.get('errors') or [])[:2]))
            else:
                # 没配详情时不显示「正文 0 字符」，否则看起来像失败
                trial_txt = '%d 条 · 仅列表（未提供详情样本）' % (online.get('items') or 0)
            stages.append(_stage(
                '在线试跑', 'ok' if online.get('valid') else 'warn',
                trial_txt, t0))
            # 在线结果作为最终判定；样本回测失败时也一并暴露
            vr = dict(online)
            vr['offline'] = off
            if off['errors']:
                vr.setdefault('errors', []).extend(
                    ['样本回测：%s' % e for e in off['errors'][:2]])
                vr['valid'] = False
        except Exception as e:
            stages.append(_stage('在线试跑', 'warn', '试跑异常：%s' % str(e)[:120], t0))
            vr = dict(off)

    kw_warn = ''
    if not meta_info.get('keyword_used'):
        kw_warn = ('未识别出关键词参数：采集时会抓到全站公告，需在配方里把关键词参数'
                   '改成 {keyword}（可在 curl 里带上搜索词后重新生成）。')
    elif meta_info.get('kw_note'):
        kw_warn = str(meta_info['kw_note'])

    ok = bool(vr.get('valid'))
    if detail_missing:
        # 用户明确给了详情样本，说明他要详情；装不出来不能算成功，否则会存下
        # 一个「列表能采、详情永远为空」的配方。
        ok = False
        vr.setdefault('errors', []).append(
            '详情样本没能装配成详情配置：请确认详情样本是详情页源码/详情接口 JSON，'
            '接口型详情还需要同时粘贴详情 curl')
        vr['valid'] = False
    out = {
        'ok': ok, 'recipe': recipe, 'verify': vr, 'stages': stages,
        'mapping_source': source,
        'generator_version': GENERATOR_VERSION,
        'diag': diag,
        'evidence': {
            'mode': mode,
            'list_request': {k: lreq.get(k) for k in
                             ('method', 'url', 'body_kind', 'params')},
            'list_body': lreq.get('body'),
            'list_headers': list(lreq.get('headers') or {}),
            'items_path': recipe['discovery'].get('items_path'),
            'row_selector': recipe['discovery'].get('row_selector'),
            'keyword_param': (meta_info.get('kw_slot') or {}).get('path') or '',
            'keyword_by': (meta_info.get('kw_slot') or {}).get('by') or '',
            'page_param': (meta_info.get('page') or {}).get('page_param') or '',
            'size_param': (meta_info.get('page') or {}).get('size_param') or '',
            'detail_url': ((recipe.get('detail') or {}).get('request') or {}).get('url'),
            'url_field': recipe['discovery'].get('fields', {}).get('url'),
            'url_source': meta_info.get('url_source') or 'field',
            'url_template': (recipe['discovery'].get('fields', {}).get('url') or {}
                             ).get('template') or '',
            'content_path': (recipe.get('detail') or {}).get('content_path'),
            'content_css': (recipe.get('detail') or {}).get('content_css'),
            'pdf_url': (pdf_info or {}).get('pdf_url') or '',
            'items': list_info.get('count') or list_info.get('rows'),
            'elapsed_ms': int((time.monotonic() - t_all) * 1000),
        },
        'error': '' if ok else ('配方已生成，但校验未通过：%s。可在下方编辑后保存重试。'
                                % '；'.join((vr.get('errors') or [])[:2])),
        'warning': ('配方已按你提供的 curl + 样本生成，并通过样本回测'
                    if ok else '') or ('配方已生成，建议人工核对后再启用'),
    }
    if kw_warn:
        out['warning'] = (out['warning'] + ' ' + kw_warn).strip()
        # kw_warn 已经在顶部提示条里露出，这里只补「还没被在线试跑提示过」的
        if kw_warn not in (out['verify'].get('warnings') or []):
            out['verify'].setdefault('warnings', []).append(kw_warn)
    if url_warn:
        out['warning'] = (out['warning'] + ' ' + url_warn).strip()
        out['verify'].setdefault('warnings', []).append(url_warn)
    return out
