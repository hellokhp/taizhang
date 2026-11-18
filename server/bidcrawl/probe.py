# -*- coding: utf-8 -*-
"""站点探测引擎：把「AI 猜页面」变成「先取证、再生成配方」。

背景：像金采网这类站点，入口 HTML 只有 6KB 骨架，列表数据、请求参数、
详情接口、详情页 URL 全在它引用的 JS 里。只把入口 HTML 前 8000 字丢给 LLM
必然生成一个 html_list 空壳配方。本模块负责把站点「问清楚」：

  1. GET 入口页，收集外链 JS + 内联脚本；
  2. 从 JS 里抽接口路径、请求参数名、字段引用、详情 URL 拼接线索；
     参数按来源脚本分组，避免把 public.js 的 searchText 串到 list.js 的接口上；
  3. 自动试请求，按「关键词是否生效 / 标题是否像公告 / 路径语义 /
     字段丰富度」打分，选出真正的列表接口；
  4. 用首条 id 打开详情页，从详情 JS 里找详情请求（如 id=xx&isDetail=1）
     与正文字段（noticeContent），并还原详情页 URL 模板；
  5. 输出结构化证据包 stages / evidence / errors，供 ai_analyze.py 生成配方。

本模块只做探测，不写数据库、不调用 LLM、不依赖任何站点分支。
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from urllib.parse import (urlparse, urlunparse, urljoin, parse_qsl,
                          unquote, quote, urlencode)

import httpx

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

FORM_HEADERS = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Accept': '*/*',
    'X-Requested-With': 'XMLHttpRequest',
    'User-Agent': UA,
}
JSON_HEADERS = {
    'Content-Type': 'application/json;charset=UTF-8',
    # 用通用 Accept：部分网关 WAF 会把 text/javascript...,q=0.01 误判为 XSS（如农行 gateway）
    'Accept': 'application/json, text/plain, */*',
    'X-Requested-With': 'XMLHttpRequest',
    'User-Agent': UA,
}
GET_HEADERS = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9',
               'Accept': 'text/html,application/json,*/*;q=0.8'}

PAGE_KEYS = ('pageNo', 'pageNum', 'pageNumber', 'pageIndex', 'page', 'current',
             'currentPage', 'pn', 'offset', 'start')
SIZE_KEYS = ('pageSize', 'page_size', 'size', 'limit', 'limitNum', 'rows',
             'perPage', 'pageCount', 'count')
# 顺序不是优先级：候选会逐个实测试探，由「关键词是否生效」决定胜负
KEYWORD_KEYS = ('briefContent', 'noticeContent', 'keyword', 'keywords',
                'keyWords', 'keyWord', 'searchKey', 'searchKeyWords',
                'searchWord', 'searchContent', 'searchValue', 'searchText',
                'noticeTitle', 'messageTitle', 'title', 'projectName', 'content', 'q')
ID_KEYS = ('id', 'noticeId', 'articleId', 'bidId', 'infoId', 'uuid', 'rowId',
           'bulletinId', 'projectId')
TITLE_HINT = re.compile(r'title|subject|name|标题|名称|project', re.I)
DATE_HINT = re.compile(r'time|date|publish|create|update|日期|时间', re.I)
ID_HINT = re.compile(r'^(id|.*Id|.*ID|uuid|guid)$', re.I)
CONTENT_GOOD = re.compile(r'noticeContent|content|detail|body|article|正文|详情|html', re.I)
CONTENT_BAD = re.compile(r'brief|summary|abstract|digest|remark|short|简介|摘要|title|name', re.I)
ANNOUNCE = re.compile(r'(公告|公示|招标|采购|中标|成交|项目|询价|竞价|磋商|谈判|结果|遴选)', re.I)
GOOD_PATH = re.compile(r'(notice|announce|bulletin|bid|tender|purchase|project|article|info|list|query|search|data)', re.I)
BAD_PATH = re.compile(r'(friend|link|advert|ad_|banner|nav|menu|dict|region|province|area|city|label|category|login|logout|captcha|verify|code|upload|download|static)', re.I)
BAD_EXT = re.compile(r'\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|html?|jsp|json)($|\?)', re.I)
SKIP_JS = re.compile(r'(jquery|bootstrap|popper|layer|layui|datepicker|vue|react|'
                     r'echarts|moment|axios|lodash|underscore|select2|swiper|'
                     r'jeeplus|common/js|pdf|chart)', re.I)

MAX_SCRIPTS = 14
MAX_SCRIPT_CHARS = 600_000
MAX_PROBE_REQUESTS = 64


# ------------------------------------------------------------------ 小工具
def _short(v, n: int = 120):
    s = '' if v is None else (v if isinstance(v, str) else str(v))
    s = re.sub(r'\s+', ' ', s)
    return s[:n]


def _literal(raw: str) -> str:
    """把 JS 右值缩成字符串字面量；变量/函数调用一律视为空值占位。"""
    s = (raw or '').strip().rstrip(',;').strip()
    m = re.match(r'^["\']([^"\']*)["\']$', s)
    if m:
        return m.group(1)
    if re.match(r'^-?\d+(\.\d+)?$', s):
        return s
    return ''


def _origin(url: str) -> str:
    u = urlparse(url)
    return '%s://%s' % (u.scheme, u.netloc)


def _ctx_prefix(url: str) -> str:
    """入口 URL 的第一段路径通常就是应用 ctx（金采网是 /jcw）。"""
    seg = [x for x in urlparse(url).path.split('/') if x]
    return seg[0] if seg else ''


def _abs(entry_url: str, path: str) -> list[str]:
    """接口路径在 JS 里往往是相对 ctx 的（/noticeinfo/...），要同时试
    origin+ctx+path 与 origin+path 两种拼法。"""
    origin = _origin(entry_url)
    ctx = _ctx_prefix(entry_url)
    if not path.startswith('/'):
        path = '/' + path
    out = []
    if ctx and not path.startswith('/%s/' % ctx):
        out.append('%s/%s%s' % (origin, ctx, path))
    out.append(origin + path)
    return list(dict.fromkeys(out))[:2]


def _render(payload, ctx: dict):
    """递归渲染 payload：支持 {page}/{size}/{keyword} 出现在任意嵌套层级。

    像邮储这类站点的请求体是 {"pageNo":1,"pageSize":10,"dto":{...}}，
    占位符在 dto 里面，只处理顶层会漏掉。
    """
    if isinstance(payload, dict):
        return {k: _render(v, ctx) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_render(v, ctx) for v in payload]
    if isinstance(payload, str):
        m = re.fullmatch(r'\{(\w+)\}', payload.strip())
        if m and m.group(1) in ctx:
            return ctx[m.group(1)]
        return re.sub(r'\{\w+\}', '', payload)
    return payload


def _deep_keys(obj, out: list | None = None) -> list[str]:
    """递归收集对象里所有 key（含嵌套），供参数语义识别。"""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _deep_keys(v, out)
    elif isinstance(obj, list):
        for v in obj[:2]:
            _deep_keys(v, out)
    return out


def _deep_value(obj, key: str):
    """按 key 名在嵌套对象里取值（第一个命中）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _deep_value(v, key)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj[:2]:
            got = _deep_value(v, key)
            if got is not None:
                return got
    return None


def _split_top(text: str, sep: str = ',') -> list[str]:
    """按顶层分隔符切分，忽略括号/引号内的分隔符。"""
    parts, buf, depth, quote_ch = [], [], 0, ''
    for ch in text:
        if quote_ch:
            buf.append(ch)
            if ch == quote_ch:
                quote_ch = ''
            continue
        if ch in '"\'':
            quote_ch = ch
            buf.append(ch)
            continue
        if ch in '{[(':
            depth += 1
        elif ch in '}])':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf))
    return parts


def _balanced(text: str, start: int, open_ch: str = '{', close_ch: str = '}') -> str:
    """从 start 处的开括号取出配对到闭括号的子串（含首尾括号）。"""
    if start >= len(text) or text[start] != open_ch:
        return ''
    depth, quote_ch = 0, ''
    for i in range(start, len(text)):
        ch = text[i]
        if quote_ch:
            if ch == quote_ch and text[i - 1] != '\\':
                quote_ch = ''
            continue
        if ch in '"\'':
            quote_ch = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ''


def parse_js_object(text: str):
    """把 JS 对象字面量解析成 dict（不要求是合法 JSON）。

    - key 可带引号或不带；值支持字符串/数字/布尔/嵌套对象/数组；
    - 变量值（如 'siteId': siteId）解析成空串，但保留 key，
      后续可用页面隐藏域里的默认值回填；
    - 不是对象时返回 None。
    """
    text = (text or '').strip()
    if not text.startswith('{'):
        return None
    body = _balanced(text, 0)
    if not body:
        return None
    out: dict = {}
    for chunk in _split_top(body[1:-1]):
        chunk = chunk.strip()
        if not chunk or ':' not in chunk:
            continue
        k, v = chunk.split(':', 1)
        k = k.strip().strip('"\'').strip()
        if not re.match(r'^[A-Za-z_$][\w$]*$', k):
            continue
        v = v.strip()
        if v.startswith('{'):
            sub = parse_js_object(v)
            out[k] = sub if sub is not None else {}
        elif v.startswith('['):
            arr = _balanced(v, 0, '[', ']')
            out[k] = [_literal(x) for x in _split_top(arr[1:-1])[:3]] if arr else []
        elif re.match(r'^["\']', v):
            out[k] = _literal(v)
        elif re.match(r'^-?\d+(\.\d+)?$', v):
            out[k] = v
        elif v in ('true', 'false', 'null'):
            out[k] = '' if v == 'null' else v
        else:
            out[k] = ''       # 变量，等隐藏域默认值回填
    return out


def iter_request_configs(text: str):
    """逐个产出 (接口路径, 请求配置对象文本)。

    不依赖 $.ajax(...) 的具体写法：很多站点写成
    `config = {url: server+cms+"/api/search/searchKeyWord", data: {...}}`
    再交给 EasyAjax.post_json(config)。围绕 url: 向上找对象边界最稳。
    """
    for m in re.finditer(r'\burl\s*:\s*([^,\n]{1,300})', text or ''):
        paths = re.findall(r'["\'](/[A-Za-z0-9_\-/.]{6,160})["\']', m.group(1))
        if not paths:
            continue
        start = text.rfind('{', max(0, m.start() - 2000), m.start())
        if start < 0:
            continue
        body = _balanced(text, start)
        if not body or len(body) > 5000:
            continue
        yield paths[-1], body


def template_from_config(path: str, body: str, source: str = '') -> dict | None:
    """从请求配置对象里取 method / contentType / data（支持 JSON.stringify）。"""
    dm = (re.search(r'data\s*:\s*JSON\.stringify\s*\(\s*(\{)', body)
          or re.search(r'\bdata\s*:\s*(\{)', body))
    if not dm:
        return None
    data_obj = parse_js_object(_balanced(body, dm.start(1)))
    if not isinstance(data_obj, dict):
        return None
    method = 'POST' if re.search(r'type\s*:\s*["\']post|method\s*:\s*["\']post',
                                 body, re.I) else \
             ('GET' if re.search(r'type\s*:\s*["\']get|method\s*:\s*["\']get',
                                 body, re.I) else 'POST')
    ctype = ('json' if re.search(r'application/json', body, re.I)
             else 'form' if re.search(r'x-www-form-urlencoded', body, re.I) else '')
    if not ctype:
        # 没写 contentType 时按形态推断：JSON.stringify / post_json / 嵌套 dto
        if (re.search(r'JSON\.stringify|post_json|postJson|postJsonData', body, re.I)
                or any(isinstance(v, dict) for v in data_obj.values())):
            ctype = 'json'
    return {'url': path, 'method': method, 'content_type': ctype,
            'data': data_obj, 'source': source}


# 页面隐藏域名 → 接口参数的语义别名（CMS 类站点常见 chunkSiteId/chunkId）
HIDDEN_ALIASES = {
    'chunksiteid': 'siteId', 'siteid': 'siteId', 'site_id': 'siteId',
    'chunkid': 'categoryId', 'categoryid': 'categoryId', 'category_id': 'categoryId',
    'channelid': 'channelId', 'columnid': 'columnId',
    'pageno': 'pageNo', 'pagesize': 'pageSize',
}
# 隐藏域里的路径前缀（cms=/cms）用于拼接口地址，不能塞进请求体
DEFAULT_SKIP = re.compile(r'^(cms|server|res|sitepthtemplocal|tenderbuyhref)$', re.I)


def parse_hidden_defaults(html_text: str) -> dict:
    """从列表页隐藏域提取接口默认参数（siteId/categoryId/pageSize 等）。

    这类 CMS 的接口参数不写在 JS 默认值里，而是由页面
    <input type="hidden" name="chunkSiteId" value="725"> 提供。
    """
    out: dict = {}
    for m in re.finditer(r'<input\b[^>]*>', html_text or '', re.I):
        tag = m.group(0)
        low = tag.lower()
        if 'type="hidden"' not in low.replace("'", '"'):
            continue

        def attr(name):
            am = re.search(r'\b%s\s*=\s*["\']([^"\']*)["\']' % name, tag, re.I)
            return (am.group(1) or '').strip() if am else ''

        raw_name = attr('name') or attr('id')
        if not raw_name:
            cls = attr('class').split()
            raw_name = cls[0] if cls else ''
        val = attr('value')
        if not raw_name or not val:
            continue
        key = HIDDEN_ALIASES.get(raw_name.lower(), raw_name)
        if re.match(r'^[A-Za-z_]\w*$', key) and key not in out:
            out[key] = val
    return out


LIST_TEXT = re.compile(r'(招标|采购|公告|中标|成交|公示|项目信息|交易信息|非招标)')
ARTICLE_URL = re.compile(r'/\d{8}/\d{8,}\.html?$', re.I)


def discover_list_pages(html_text: str, base_url: str, limit: int = 4) -> list[str]:
    """从入口页导航里找「招标/采购公告」列表页。

    入口首页通常只放几块推荐位，真正的列表页（带 siteId/categoryId 隐藏域）
    在导航链接后面，例如 /cms/default/webfile/1ywgg1/index.html。
    """
    pages: list[str] = []
    host = urlparse(base_url).netloc
    for m in re.finditer(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html_text or '', re.S | re.I):
        href = (m.group(1) or '').strip()
        txt = re.sub(r'<[^>]+>', ' ', m.group(2) or '')
        txt = re.sub(r'\s+', ' ', html_mod.unescape(txt)).strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:')):
            continue
        if not LIST_TEXT.search(txt):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc != host:
            continue
        if ARTICLE_URL.search(urlparse(full).path):
            continue
        if full.rstrip('/') == base_url.rstrip('/'):
            continue
        if full not in pages:
            pages.append(full)
        if len(pages) >= limit:
            break
    return pages


def _title_quality(rows: list) -> bool:
    for row in rows[:5]:
        for k, v in row.items():
            if TITLE_HINT.search(k) and isinstance(v, str) and len(v) >= 8:
                if ANNOUNCE.search(v):
                    return True
    return False


def _keyword_hit(rows: list, keyword: str) -> float:
    if not keyword or not rows:
        return 0.0
    hit = 0
    for row in rows[:8]:
        blob = ' '.join(str(v)[:400] for v in row.values()
                        if isinstance(v, (str, int, float)))
        if keyword in blob:
            hit += 1
    return hit / min(len(rows), 8)


# ------------------------------------------------------------------ JSON 结构分析
def _find_items(data, path='$', depth=0):
    """在响应 JSON 里找「条目数组」，返回 (jsonpath, items, score)。"""
    if depth > 6:
        return None
    best = None

    def take(cand):
        nonlocal best
        if cand and (best is None or cand[2] > best[2]):
            best = cand

    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:3]):
                keys = set(v[0].keys())
                s = 4 + min(6, len(v)) + min(9, len(keys))
                if any(ID_HINT.search(x) for x in keys):
                    s += 4
                if any(TITLE_HINT.search(x) for x in keys):
                    s += 3
                if any(DATE_HINT.search(x) for x in keys):
                    s += 2
                if _title_quality(v):
                    s += 6
                child = ('%s.%s[*]' % (path, k), v, s)
                take(child)
        for k, v in data.items():
            if isinstance(v, dict):
                take(_find_items(v, '%s.%s' % (path, k), depth + 1))
            elif isinstance(v, list):
                for i, x in enumerate(v[:2]):
                    take(_find_items(x, '%s.%s[%d]' % (path, k, i), depth + 1))
    elif isinstance(data, list):
        for i, x in enumerate(data[:2]):
            take(_find_items(x, '%s[%d]' % (path, i), depth + 1))
    return best


_TOTAL_NAME = (r'(total|totalCount|totalSize|totalNum|totalRecord|totalRows|totalCnt|'
               r'recordCount|recordsTotal|records|count|totalElements)')


def _find_total(data, path='$', depth=0) -> str:
    if depth > 5:
        return ''
    if isinstance(data, dict):
        for k, v in data.items():
            if re.fullmatch(_TOTAL_NAME, str(k), re.I):
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                    return '%s.%s' % (path, k)
        for k, v in data.items():
            if isinstance(v, dict):
                got = _find_total(v, '%s.%s' % (path, k), depth + 1)
                if got:
                    return got
    return ''


def _find_total_value(data, depth=0):
    """递归取总数（如 data.res.total），用于判断关键词是否被接口采纳。"""
    if depth > 5:
        return None
    if isinstance(data, dict):
        for k, v in data.items():
            if re.fullmatch(_TOTAL_NAME, str(k), re.I):
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, str) and v.isdigit():
                    return int(v)
        for v in data.values():
            if isinstance(v, (dict, list)):
                got = _find_total_value(v, depth + 1)
                if got is not None:
                    return got
    elif isinstance(data, list):
        for v in data[:2]:
            got = _find_total_value(v, depth + 1)
            if got is not None:
                return got
    return None


def _find_content(data, path='$', depth=0):
    """在详情响应 JSON 里找「公告正文」：长文本 + 中文/HTML 特征。

    返回 {row_path, content_path, value_len, encoding, preview, score}。
    row_path 必须指向「包含正文的那个对象」，因为配方里 content_path 是行内字段名。
    """
    if depth > 7:
        return None
    best = None

    def take(cand):
        nonlocal best
        if cand and (best is None or cand['score'] > best['score']):
            best = cand

    if isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(v, str) or len(v) < 200:
                continue
            head = v[:4000]
            html_like = bool(re.search(r'&lt;|<(?:div|p|table|span|br|h[1-6])\b|&nbsp;', head, re.I))
            cn = len(re.findall(r'[\u4e00-\u9fa5]', head))
            if not (html_like or cn >= 50):
                continue
            score = min(len(v), 30000)
            if CONTENT_GOOD.search(k):
                score += 30000
            if CONTENT_BAD.search(k):
                score -= 30000
            if html_like:
                score += 4000
            if cn >= 100:
                score += 2000
            escaped = ('&lt;' in head or '&amp;lt' in head)
            take({'row_path': path, 'content_path': k, 'value_len': len(v),
                  'encoding': 'html_escaped' if escaped else ('html' if html_like else 'text'),
                  'preview': _short(v, 400), 'score': score})
        for k, v in data.items():
            if isinstance(v, dict):
                take(_find_content(v, '%s.%s' % (path, k), depth + 1))
            elif isinstance(v, list):
                for i, x in enumerate(v[:2]):
                    take(_find_content(x, '%s.%s[%d]' % (path, k, i), depth + 1))
    elif isinstance(data, list):
        for i, x in enumerate(data[:2]):
            take(_find_content(x, '%s[%d]' % (path, i), depth + 1))
    return best


def _endpoint_rank(path: str, base: int) -> int:
    score = base
    if GOOD_PATH.search(path):
        score += 6
    if BAD_PATH.search(path):
        score -= 14
    if re.search(r'(notice|bulletin|announce|bid|tender|gonggao|gg)', path, re.I):
        score += 6
    if re.search(r'/(detail|content|info)$', path, re.I):
        score += 2
    return score


# ------------------------------------------------------------------ JS 信号
def parse_js_signals(scripts: list[dict]) -> dict:
    """从 JS 源码里抽接口路径 / 请求参数 / 字段引用 / URL 拼接行。

    关键点：参数与接口按「来源脚本」分组。金采网 public.js 里有一个返回 rows
    的友情链接接口和 searchText 参数，若与 list.js 混在一起就会选错接口。
    """
    per_script: list[dict] = []
    merged: dict[str, dict] = {}
    url_lines: list[str] = []
    state_defaults: dict[str, str] = {}

    for s in scripts:
        t = s.get('text') or ''
        if not t:
            continue
        local_eps: dict[str, int] = {}
        local_pairs: list[tuple[str, str]] = []
        local_method = ''
        local_tpl: dict[str, dict] = {}

        # $.ajax / $.post / fetch / axios 附近的窗口，接口路径最可信
        windows: list[str] = []
        for m in re.finditer(r'(\$\.ajax\s*\(|\$\.(?:post|get)\s*\(|'
                             r'axios\.(?:post|get)\s*\(|fetch\s*\(|'
                             r'EasyAjax\.\w+\s*\(|\.(?:post_json|postJson|request)\s*\()', t):
            windows.append(t[m.start(): m.start() + 1800])
        # 很多站点把请求配置写在变量里（config={url:...,data:{...}}），
        # 再由 EasyAjax.post_json(config) 发送；围绕 url: 定义取窗口才能拿到请求体。
        for m in re.finditer(r'\burl\s*:\s*[^,\n]{1,300}', t):
            windows.append(t[max(0, m.start() - 1100): m.end() + 1600])
        for w in windows[:80]:
            w_post = bool(re.search(r'type\s*:\s*["\']post|method\s*:\s*["\']post|'
                                    r'\$\.post\(|\.post\(', w, re.I))
            if w_post:
                local_method = 'POST'
            for p in re.findall(r'["\'](/[A-Za-z0-9_\-/.]{6,160})["\']', w):
                if not BAD_EXT.search(p) and '/static/' not in p:
                    local_eps[p] = local_eps.get(p, 0) + 5
            # 请求模板：url + type + contentType + data（可能是嵌套对象）
            tpl = None
            url_m = re.search(r'\burl\s*:\s*([^,\n]{1,300})', w)
            url_paths = re.findall(r'["\'](/[A-Za-z0-9_\-/.]{6,160})["\']',
                                   url_m.group(1) if url_m else '')
            if url_paths:
                obj_m = re.search(r'data\s*:\s*JSON\.stringify\s*\(\s*(\{)', w)
                if not obj_m:
                    obj_m = re.search(r'\bdata\s*:\s*(\{)', w)
                data_obj = None
                if obj_m:
                    data_obj = parse_js_object(_balanced(w, obj_m.start(1)))
                tpl = {
                    'url': url_paths[-1],
                    'method': 'POST' if w_post else
                              ('GET' if re.search(r'type\s*:\s*["\']get', w, re.I) else 'POST'),
                    'content_type': ('json' if re.search(r'application/json', w, re.I)
                                     else 'form' if re.search(r'x-www-form-urlencoded', w, re.I)
                                     else ''),
                    'data': data_obj if isinstance(data_obj, dict) else None,
                    'source': s.get('url') or '',
                }
                old = local_tpl.get(tpl['url'])
                # 同一接口多次调用时，保留带请求体的那个
                if old is None or (tpl.get('data') and not old.get('data')):
                    local_tpl[tpl['url']] = tpl
        # 独立于调用形式的请求配置解析：覆盖 config={url:...,data:{...}} 写法
        for path, body in iter_request_configs(t):
            if BAD_EXT.search(path) or '/static/' in path:
                continue
            tpl = template_from_config(path, body, s.get('url') or '')
            if not tpl:
                continue
            old = local_tpl.get(path)
            if old is None or (tpl.get('data') and not old.get('data')):
                local_tpl[path] = tpl
            local_eps[path] = local_eps.get(path, 0) + 5

        # 全文里的绝对路径补充线索（权重低）
        for p in re.findall(r'["\'](/[A-Za-z0-9_\-/.]{6,160})["\']', t):
            if not BAD_EXT.search(p) and '/static/' not in p and not GOOD_PATH.search(p):
                continue
            if BAD_EXT.search(p) or '/static/' in p:
                continue
            local_eps[p] = local_eps.get(p, 0) + 1

        # param.xxx = value / data: { xxx: value } / var param = {...}
        # 注意：searchMap.xxx 是页面查询状态（如 searchText），不是发给接口的 body 参数，
        # 混进来会污染关键词候选，所以单独收集、不进 param_keys。
        for m in re.finditer(r'\b(?:param|params|data)\.([A-Za-z_]\w*)\s*=\s*([^;\n]{0,80})', t):
            local_pairs.append((m.group(1), _literal(m.group(2))))
        # 页面查询状态里的字面量默认值（如 searchMap.searchNoticeType = "1"），
        # 供生成器把空的 noticeType 补成站点默认栏目，避免抓成全站公告。
        for m in re.finditer(r'\b(?:searchMap|state|queryState)\.([A-Za-z_]\w*)\s*=\s*'
                             r'(["\'][^"\']{0,30}["\']|-?\d+)', t):
            v = _literal(m.group(2))
            if v:
                state_defaults[m.group(1)] = v
        for m in re.finditer(r'\bvar\s+(?:param|params|searchMap)\s*=\s*\{([^}]{0,700})\}', t, re.S):
            for kv in re.finditer(r'([A-Za-z_]\w*)\s*:\s*([^,\n]{0,80})', m.group(1)):
                local_pairs.append((kv.group(1), _literal(kv.group(2))))
        for m in re.finditer(r'\bdata\s*:\s*\{([^}]{0,700})\}', t, re.S):
            for kv in re.finditer(r'["\']?([A-Za-z_]\w*)["\']?\s*:\s*([^,\n]{0,80})', m.group(1)):
                local_pairs.append((kv.group(1), _literal(kv.group(2))))

        # row.xxx 字段引用，帮助做字段映射
        refs = sorted(set(re.findall(r'\brow\.([A-Za-z_]\w*)', t)))[:60]

        # URL 拼接行（goUrl / searchVal / noticeDetail）
        for line in t.splitlines():
            if re.search(r'(searchVal|goUrl|noticeDetail|detail\s*\(|window\.open)', line, re.I):
                if re.search(r'(goUrl|searchVal|detail)', line, re.I) and len(line) < 400:
                    url_lines.append(line.strip())

        pmap: dict[str, str] = {}
        for k, v in local_pairs:
            pmap[k] = v
        per_script.append({'url': s.get('url') or '', 'tag': s.get('tag') or '',
                           'chars': len(t), 'endpoints': local_eps,
                           'param_keys': pmap, 'field_refs': refs,
                           'templates': list(local_tpl.values())[:10],
                           'method_hint': local_method})
        for p, sc in local_eps.items():
            cur = merged.get(p)
            if cur is None or sc > cur['base']:
                merged[p] = {'path': p, 'base': sc,
                             'score': _endpoint_rank(p, sc), 'source': s.get('url') or '',
                             'param_keys': dict(pmap), 'method': local_method,
                             'template': local_tpl.get(p)}

    ranked = sorted(merged.values(), key=lambda x: (-x['score'], len(x['path'])))[:14]
    templates = [x['template'] for x in ranked if x.get('template')]
    return {
        'endpoints': ranked,
        'per_script': per_script,
        'templates': templates[:10],
        'url_lines': list(dict.fromkeys(url_lines))[:40],
        'state_defaults': state_defaults,
        'method_hint': 'POST' if any(x['method_hint'] == 'POST' for x in per_script) else 'GET',
    }


# ------------------------------------------------------------------ HTML 列表
DETAIL_PATH = re.compile(
    r'/(?:biddingBulletin|bulletin|notice|announcement|article|content|detail|'
    r'zbgg|cggg|info)/', re.I)
KEYWORD_PARAM = re.compile(
    r'^(?:goSearch|keyword|keywords|searchKey|searchWord|searchText|'
    r'searchValue|word|words|q|key|keyWord|projectName)$', re.I)
SEARCH_URL = re.compile(r'(search|bulletin|notice|list|query)', re.I)
DATE_IN_ROW = re.compile(r'\b(20\d{2})[^\d]{1,40}?(\d{1,2})[/-](\d{1,2})\b')


def _path_pattern(path: str) -> str:
    """把 /biddingBulletin/2026-11-11/142770.html 归一成 /biddingBulletin/{date}/{id}.html，
    用于把「指向详情页的链接」聚成一组，避免把导航链接也算进列表行。"""
    out = []
    for seg in str(path).split('/'):
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', seg):
            out.append('{date}')
        elif re.fullmatch(r'\d+', seg):
            out.append('{id}')
        elif re.fullmatch(r'[A-Za-z0-9_-]*\d+\.(?:html?|shtml)', seg):
            out.append('{id}.html')
        else:
            out.append(seg)
    return '/'.join(out)


def find_search_template(html_text: str, entry_url: str):
    """从入口页（含内联 JS）里找「关键词 + 分页」的搜索 URL 模板。

    例：首页内联脚本
      var url = "https://cg.11185.cn/zgyzcms/category/searchBulletinList.html"
                + "?categoryId=88&goSearch=" + goSearch;
    会得到 {url, params:{categoryId:88, goSearch:{keyword}, page:{page}}, ...}
    """
    cands: list[str] = []
    for m in re.finditer(r'["\']((?:https?://[^"\']+|/[^"\']*)\?[^"\']*)["\']',
                         html_text):
        cands.append(m.group(1))
    for m in re.finditer(r'href\s*=\s*["\']([^"\']+\?[^"\']*)["\']', html_text, re.I):
        cands.append(m.group(1))
    if urlparse(entry_url).query:
        cands.insert(0, entry_url)

    best = None
    for raw in cands:
        u = html_mod.unescape(raw).strip()
        if '?' not in u:
            continue
        if u.startswith('//'):
            u = 'https:' + u
        p = urlparse(u if '://' in u else urljoin(entry_url, u))
        if not SEARCH_URL.search(p.path):
            continue
        pairs = parse_qsl(p.query, keep_blank_values=True)
        kw_key = ''
        for k, _v in pairs:
            if KEYWORD_PARAM.match(k) or (
                    re.search(r'(search|keyword|word|key)', k, re.I)
                    and not re.search(r'(date|time|type|id|page|tab)', k, re.I)):
                kw_key = k
                break
        if not kw_key:
            continue
        params = dict(pairs)
        params[kw_key] = '{keyword}'
        page_key = next((k for k in params
                         if k.lower() in ('page', 'pageno', 'pagenum', 'pageindex')), '')
        if not page_key and re.search(r'[?&]page\s*=|turnPage\s*\(|&page=',
                                      html_text, re.I):
            page_key = 'page'
        if page_key:
            params[page_key] = '{page}'
        cand = {'url': '%s://%s%s' % (p.scheme, p.netloc, p.path),
                'params': params, 'keyword_param': kw_key,
                'page_param': page_key, 'raw': u}
        score = ((2 if 'search' in p.path.lower() else 0) +
                 (1 if page_key else 0) +
                 (1 if kw_key.lower() in ('gosearch', 'keyword') else 0))
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


def parse_html_list(html_text: str, base_url: str = ''):
    """从 HTML 列表页推断 row_selector 与字段规则（无站点分支）。"""
    try:
        from parsel import Selector
    except ImportError:
        return None
    sel = Selector(text=html_text)
    anchors = []
    for a in sel.css('a[href]'):
        href = (a.attrib.get('href') or '').strip()
        if not href or href.startswith(('javascript:', '#')):
            continue
        full = urljoin(base_url, href) if base_url else href
        txt = ' '.join(x.strip() for x in a.xpath('.//text()').getall() if x.strip())
        txt = re.sub(r'\s+', ' ', txt)
        if len(txt) < 8 or not re.search(r'[\u4e00-\u9fa5]', txt):
            continue
        path = urlparse(full).path
        if not (DETAIL_PATH.search(path)
                or re.search(r'/\d{4}-\d{2}-\d{2}/\d+\.html', path)):
            continue
        anchors.append((a, href, full, txt))
    if len(anchors) < 3:
        return None

    groups: dict[str, list] = {}
    for x in anchors:
        groups.setdefault(_path_pattern(urlparse(x[2]).path), []).append(x)
    members = max(groups.values(), key=len)
    if len(members) < 3:
        return None

    a0 = members[0][0]
    li = a0.xpath('ancestor::li[1]')
    if li:
        ul = li.xpath('ancestor::ul[1]')
        ul_cls = ((ul.attrib.get('class') or '').split() if ul else [])
        li_cls = (li.attrib.get('class') or '').split()
        if ul_cls:
            row_selector = 'ul.%s > li' % ul_cls[0]
        elif li_cls:
            row_selector = 'li.%s' % li_cls[0]
        else:
            row_selector = 'ul > li'
    else:
        seg = [x for x in urlparse(members[0][2]).path.split('/') if x]
        row_selector = 'a[href*="%s"]' % (seg[0] if seg else 'detail')
    rows = sel.css(row_selector)
    if len(rows) < 3:
        return None
    row = rows[0]
    row_html = row.get() or ''

    fields = {}
    for css in ('h1::text', 'h2::text', 'h3::text', '.title::text', 'a::text'):
        vals = [v.strip() for v in row.css(css).getall() if v and v.strip()]
        if vals and 8 <= len(vals[0]) <= 200:
            fields['title'] = {'css': css, 'strip': True}
            break
    if not fields.get('title'):
        return None
    fields['url'] = {'css': 'a', 'attr': 'href'}
    for rx in (r'/(\d{3,})\.html',
               r'[?&](?:id|noticeId|articleId)=([A-Za-z0-9_-]{6,})'):
        if re.search(rx, row_html):
            fields['external_id'] = {'regex': rx, 'group': 1}
            break
    dm = DATE_IN_ROW.search(row_html)
    if dm and 2000 <= int(dm.group(1)) <= 2100 and 1 <= int(dm.group(2)) <= 12 \
            and 1 <= int(dm.group(3)) <= 31:
        # regex + template：把分散在两个标签里的年 / 月日拼成 ISO 日期
        fields['publish_at'] = {'regex': DATE_IN_ROW.pattern,
                                'template': '{1}-{2}-{3} 00:00'}
    vals = [v.strip() for v in row.css('li span::text').getall()
            if v and v.strip() and len(v.strip()) <= 60][:2]
    for i, val in enumerate(vals):
        key = 'bid_no' if re.match(r'^[A-Za-z0-9_/-]{6,}$', val) else 'notice_type'
        if key not in fields:
            fields[key] = {'css': 'li span::text', 'nth': i, 'strip': True}

    return {
        'row_selector': row_selector, 'fields': fields, 'rows': len(rows),
        'sample': [{'title': x[3][:100], 'url': x[2]} for x in members[:3]],
        'first_url': members[0][2],
        'detail_path': urlparse(members[0][2]).path,
    }


# 页脚/帮助中心的固定 PDF：出现在每一页，不能当公告正文
_PDF_BOILER = re.compile(
    r'(规则|制度|办法|指南|说明|须知|帮助|注册|入驻|政策|协议|条款|隐私|声明|'
    r'手册|常见问题|操作指引|流程|指引)', re.I)
# 附件/招标文件类：才是公告自带的正文
_PDF_ATTACH = re.compile(
    r'(附件|招标文件|采购文件|投标文件|响应文件|公告|中标|成交|结果|下载|'
    r'attachment|attach|upload)', re.I)


def _pdf_key(url: str) -> str:
    """PDF 的身份：只取路径（忽略域名/协议/查询串），用于判断"同一份文档"。"""
    try:
        return unquote(urlsplit(url).path or url).strip().lower()
    except Exception:
        return str(url or '').strip().lower()


def _pdf_from_viewer(viewer: str, base_url: str, html_text: str = ''):
    """把 pdfjs 之类的查看器地址还原成可下载的 PDF 直链。"""
    if not viewer:
        return None
    q = parse_qsl(urlparse(viewer).query, keep_blank_values=True)
    fp = next((v for k, v in q if k.lower() == 'file'), '')
    if fp:
        pdf_url = unquote(fp)
    elif '.pdf' in viewer.lower():
        pdf_url = viewer
    else:
        return None
    if not pdf_url.startswith('http'):
        pdf_url = urljoin(base_url, pdf_url)
    tpl, rx = pdf_url, ''
    m = re.search(r'([?&](?:id|fileId|fileid)=)([A-Za-z0-9_-]{6,})', pdf_url)
    if m:
        tpl = pdf_url[:m.start(2)] + '{file_id}' + pdf_url[m.end(2):]
        rx = r'openFileById(?:%26|&)(?:id|fileId)(?:%3D|=)([A-Za-z0-9_-]+)'
    return {'pdf_url': pdf_url, 'viewer_url': viewer,
            'url_template': tpl, 'file_id_regex': rx,
            'iframe_css': ('iframe#pdfContainer' if 'pdfContainer' in html_text
                           else 'iframe[src*="pdf"]'),
            'from_iframe': True, 'score': 100}


def find_pdf_source(html_text: str, base_url: str = '', *,
                    list_html: str = '', content_css: str = ''):
    """从详情页 HTML 还原「这条公告的」PDF 直链。

    只看"页面里有 .pdf"是不够的：门户页脚常年挂着《平台注册规则.pdf》这类文档，
    而且**列表页也有同样几个链接**。这里的判据是：

    1. 页面本身是 PDF 查看器（iframe/embed 指向 pdfjs/viewer）→ 最强，直接采用；
    2. 链接在正文容器内 + 锚文本像附件（附件/招标文件/公告…）→ 加分；
    3. 文档名像规则/指南/注册说明，或同一份文档**也出现在列表页** → 重罚，
       基本可以断定是页脚样板；
    4. 权重不够（< 25）就认定「没有 PDF 正文」，交给 HTML 正文，宁可不用也不误用。
    """
    from parsel import Selector

    html = html_text or ''
    sel = Selector(text=html)

    # 1) PDF 查看器：页面本身就是渲染 PDF 的壳子（如 11185 的 pdfContainer）
    viewer = ''
    m = re.search(r'<iframe[^>]+src=["\']([^"\']*(?:pdfjs|viewer|\.pdf|openFile)[^"\']*)',
                  html, re.I)
    if m:
        viewer = html_mod.unescape(m.group(1))
    if viewer:
        info = _pdf_from_viewer(viewer, base_url, html)
        if info:
            return info

    # 2) 普通 <a href="*.pdf">：按证据打分
    list_keys = set()
    if list_html:
        for u in re.findall(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', list_html, re.I):
            list_keys.add(_pdf_key(urljoin(base_url, html_mod.unescape(u))))
    in_content = set()
    if content_css:
        try:
            for a in sel.css(str(content_css)).css('a[href]'):
                href = (a.attrib.get('href') or '').strip()
                if href:
                    in_content.add(_pdf_key(urljoin(base_url, html_mod.unescape(href))))
        except Exception:
            in_content = set()

    best: dict | None = None
    for a in sel.css('a[href]'):
        href = (a.attrib.get('href') or '').strip()
        if not href:
            continue
        low = href.lower()
        if '.pdf' not in low and not re.search(r'(download|attach|file)', low):
            continue
        url = urljoin(base_url, html_mod.unescape(href))
        key = _pdf_key(url)
        text = ' '.join(a.xpath('.//text()').getall()).strip()
        title_attr = (a.attrib.get('title') or '').strip()
        label = '%s %s' % (text, title_attr)

        score = 0
        if _PDF_BOILER.search(label) or _PDF_BOILER.search(unquote(url)):
            score -= 70
        if _PDF_ATTACH.search(label):
            score += 30
        if key in in_content:
            score += 40
        else:
            score -= 15
        if list_keys and key in list_keys:
            # 列表页也有同一份 PDF → 页脚/导航样板，绝不是本条公告的附件
            score -= 80
        if low.endswith('.pdf'):
            score += 5
        if best is None or score > best['score']:
            tpl, rx = url, ''
            best = {'pdf_url': url, 'viewer_url': '', 'url_template': tpl,
                    'file_id_regex': rx,
                    'iframe_css': 'iframe[src*="pdf"]',
                    'from_iframe': False, 'score': score,
                    'label': label[:60]}

    if not best or best['score'] < 25:
        return None
    return best


# ------------------------------------------------------------------ 探测器
class SiteProbe:
    def __init__(self, url: str, keyword: str = '', *, timeout_s: float = 12,
                 budget_s: float = 90):
        self.url = url
        self.keyword = (keyword or '').strip()
        self.timeout_s = timeout_s
        self.budget_s = budget_s
        self.started = time.monotonic()
        self.origin = _origin(url)
        self.ctx = _ctx_prefix(url)
        self.entry_html = ''
        self.requests = 0
        self.errors: list[str] = []
        self.stages: list[dict] = []
        self.evidence: dict = {}
        self.defaults: dict = {}      # 列表页隐藏域提供的接口默认参数
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=True,
                                    trust_env=False, headers=GET_HEADERS)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 基础设施 ----
    def _left(self) -> bool:
        return (time.monotonic() - self.started) < self.budget_s and self.requests < MAX_PROBE_REQUESTS

    def _stage(self, name: str, status: str, detail: str, t0: float):
        self.stages.append({'name': name, 'status': status, 'detail': detail,
                            'ms': int((time.monotonic() - t0) * 1000)})

    def _get(self, url: str, headers: dict | None = None) -> httpx.Response:
        self.requests += 1
        return self._client.get(url, headers=headers or GET_HEADERS)

    def _endpoint_urls(self, path: str, defaults: dict) -> list[str]:
        """接口绝对地址；站点的全局前缀（如 cms=/cms）不在 path 里时补上。"""
        urls = _abs(self.url, path)
        prefix = str((defaults or {}).get('cms') or '').strip('/')
        if prefix and not path.startswith('/' + prefix + '/'):
            # 带全局前缀的地址更可能是真实接口，优先试
            urls.insert(0, self.origin + '/' + prefix + path)
        return list(dict.fromkeys(urls))[:2]

    def _post(self, url: str, payload: dict, headers: dict, as_json: bool) -> httpx.Response:
        self.requests += 1
        time.sleep(0.08)
        # 注意：client 默认带 GET_HEADERS（Accept 含 q=0.8），httpx 会与传入头合并，
        # 部分网关 WAF 会因此判 XSS（0011 来源非法）。这里用独立请求，不带默认头。
        import httpx as _httpx
        with _httpx.Client(timeout=self.timeout_s, follow_redirects=True,
                           trust_env=False) as c:
            if as_json:
                return c.post(url, json=payload, headers=headers)
            return c.post(url, data=payload, headers=headers)

    # ---- 1. 入口 ----
    def _fetch_entry(self) -> dict:
        t0 = time.monotonic()
        entry = {'status': 0, 'chars': 0, 'content_type': '', 'final_url': self.url,
                 'json': None, 'title': '', 'fetch_error': ''}
        try:
            r = self._get(self.url)
            self.entry_html = r.text or ''
            title = re.findall(r'<title[^>]*>(.*?)</title>', self.entry_html, re.I | re.S)
            entry.update({'status': r.status_code, 'chars': len(self.entry_html),
                          'content_type': r.headers.get('content-type', ''),
                          'final_url': str(r.url),
                          'title': re.sub(r'\s+', ' ', title[0]).strip()[:80] if title else ''})
            try:
                if 'json' in entry['content_type'].lower() or self.entry_html.lstrip()[:1] in ('{', '['):
                    entry['json'] = json.loads(self.entry_html)
            except Exception:
                pass
        except Exception as e:
            # 接口型入口对 GET 常直接断开（如农行 gateway）；不中断，交给接口直探
            entry['fetch_error'] = _short(str(e), 140)
            self.errors.append('入口 GET 失败：%s' % entry['fetch_error'])
        self.evidence['entry'] = entry
        self._stage('抓取入口页', 'error' if entry.get('fetch_error') or entry['status'] >= 400
                    else 'ok',
                    ('HTTP %d，%d 字符，%s' % (entry['status'], entry['chars'],
                                              entry['content_type'] or '-'))
                    if not entry.get('fetch_error') else ('GET 失败：%s' % entry['fetch_error']), t0)
        return entry

    # ---- 2. 脚本 ----
    def _collect_scripts(self, html_text: str, base_url: str, tag: str) -> list[dict]:
        out: list[dict] = []
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)', html_text, re.I)
        for src in srcs:
            if len(out) >= MAX_SCRIPTS:
                break
            if SKIP_JS.search(src):
                continue
            if src.startswith('//'):
                src = 'http:' + src
            elif src.startswith('/'):
                src = self.origin + src
            elif not src.startswith(('http://', 'https://')):
                p = urlparse(base_url)
                src = '%s://%s%s' % (p.scheme, p.netloc,
                                     (p.path.rsplit('/', 1)[0] or '') + '/' + src)
            if urlparse(src).netloc and urlparse(src).netloc != urlparse(self.url).netloc:
                continue
            try:
                r = self._get(src)
                txt = r.text if r.status_code < 400 else ''
            except Exception as e:
                self.errors.append('脚本抓取失败 %s：%s' % (src, _short(str(e), 80)))
                continue
            if txt:
                out.append({'url': src, 'text': txt[:MAX_SCRIPT_CHARS], 'tag': tag})
        for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                             html_text, re.I | re.S):
            t = (m.group(1) or '').strip()
            if len(t) > 20 and len(out) < MAX_SCRIPTS + 4:
                out.append({'url': base_url + '#inline', 'text': t[:MAX_SCRIPT_CHARS],
                            'tag': tag})
        return out

    # ---- 3. 请求模板 ----
    @staticmethod
    def _payload_from_keys(keys: dict, keyword_key: str) -> tuple[dict, dict]:
        payload = {k: (v if v else '') for k, v in (keys or {}).items()
                   if re.match(r'^[A-Za-z_]\w*$', k) and len(k) < 40
                   and not k.startswith('_')}

        def pick(cands, default=''):
            for c in cands:
                if c in payload:
                    return c
            return default

        pk = pick(PAGE_KEYS) or 'pageNo'
        sk = pick(SIZE_KEYS) or 'pageSize'
        payload[pk] = '{page}'
        payload[sk] = '{size}'
        if keyword_key:
            payload[keyword_key] = '{keyword}'
        return payload, {'page': pk, 'size': sk, 'keyword': keyword_key}

    @staticmethod
    def _payload_variants(ep: dict, defaults: dict,
                          keyword_key: str) -> list[tuple[dict, dict, str]]:
        """生成多种请求体形态：JS 模板 / 平铺 / 嵌套 dto。

        邮储 `queryContentPage` 的请求体是
        {"pageNo":1,"pageSize":10,"dto":{"siteId":"725","categoryId":"223"}}，
        只试平铺参数永远 get 不到数据。
        """
        out: list[tuple[dict, dict, str]] = []
        defaults = {k: str(v) for k, v in (defaults or {}).items()
                    if str(v or '') and not DEFAULT_SKIP.match(k)}

        def fill(obj):
            if isinstance(obj, dict):
                return {k: fill(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [fill(v) for v in obj]
            return obj

        def fill_defaults(obj):
            if isinstance(obj, dict):
                for k in list(obj.keys()):
                    v = obj[k]
                    if isinstance(v, (dict, list)):
                        fill_defaults(v)
                    elif v in ('', None) and k in defaults:
                        obj[k] = defaults[k]
            elif isinstance(obj, list):
                for v in obj:
                    fill_defaults(v)

        def set_first(obj, key, val, done=None):
            done = done if done is not None else [False]
            if done[0]:
                return
            if isinstance(obj, dict):
                if key in obj:
                    obj[key] = val
                    done[0] = True
                    return
                for v in obj.values():
                    set_first(v, key, val, done)
            elif isinstance(obj, list):
                for v in obj:
                    set_first(v, key, val, done)

        def prepare(obj, kw_key):
            """填分页占位符与关键词，返回 roles。"""
            keys = _deep_keys(obj)
            pk = next((k for k in keys if k in PAGE_KEYS), '')
            sk = next((k for k in keys if k in SIZE_KEYS), '')
            if pk:
                set_first(obj, pk, '{page}')
            else:
                pk = 'pageNo'
                obj[pk] = '{page}'
            if sk:
                set_first(obj, sk, '{size}')
            else:
                sk = 'pageSize'
                obj[sk] = '{size}'
            # 同义分页字段（pageSize/limitNum）一起填，接口可能只认其中一个
            for k in keys:
                if k in SIZE_KEYS and k not in ('pageCount', 'count'):
                    set_first(obj, k, '{size}')
            if kw_key:
                if kw_key in keys:
                    set_first(obj, kw_key, '{keyword}')
                else:
                    # 关键词字段不在模板里：优先放进 dto 子对象
                    target = obj.get('dto')
                    if not isinstance(target, dict):
                        target = obj
                    target[kw_key] = '{keyword}'
            return {'page': pk, 'size': sk, 'keyword': kw_key}

        # 分类/地区等过滤条件：搜索接口带上「资格预审」分类会搜不到「门头」，
        # 所以除完整模板外，再试一版只保留站点 + 关键词的最小参数集。
        FILTER_KEY = re.compile(
            r'^(categoryId|category|bidType|purchaseMode|province|city|county|'
            r'publishDays|qualMode|publishOrganization|channelId|columnId)$', re.I)

        tpl = (ep.get('template') or {}).get('data')
        if isinstance(tpl, dict) and tpl:
            payload = fill(tpl)
            fill_defaults(payload)
            out.append((payload, prepare(payload, keyword_key), 'template'))

            minimal = fill(tpl)
            fill_defaults(minimal)
            nodes = [minimal] + [v for v in minimal.values() if isinstance(v, dict)]
            for obj in nodes:
                for k in list(obj.keys()):
                    if FILTER_KEY.match(str(k)):
                        obj.pop(k, None)
            if (json.dumps(minimal, ensure_ascii=False, sort_keys=True) !=
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)):
                out.append((minimal, prepare(minimal, keyword_key), 'minimal'))

        keys = ep.get('param_keys') or {}
        flat = {k: (v if v else '') for k, v in keys.items()
                if re.match(r'^[A-Za-z_]\w*$', k) and len(k) < 40 and not k.startswith('_')}
        pk = next((c for c in PAGE_KEYS if c in flat), 'pageNo')
        sk = next((c for c in SIZE_KEYS if c in flat), 'pageSize')
        flat[pk] = '{page}'
        flat[sk] = '{size}'
        if keyword_key:
            flat[keyword_key] = '{keyword}'
        out.append((flat, {'page': pk, 'size': sk, 'keyword': keyword_key}, 'flat'))

        if defaults:
            dto = dict(defaults)
            if keyword_key:
                dto[keyword_key] = '{keyword}'
            out.append(({'pageNo': '{page}', 'pageSize': '{size}', 'dto': dto},
                        {'page': 'pageNo', 'size': 'pageSize', 'keyword': keyword_key},
                        'dto'))
        # 去重：同样的 payload 只试一次
        seen, uniq = set(), []
        for p, r, style in out:
            sig = json.dumps(p, ensure_ascii=False, sort_keys=True)
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append((p, r, style))
        return uniq[:3]

    @staticmethod
    def _keyword_candidates(keys: dict) -> list[str]:
        cands = []
        for k in KEYWORD_KEYS:
            if k in keys:
                cands.append(k)
        # 站点自定义的、但语义像搜索框的参数
        for k in keys:
            if k in cands:
                continue
            if re.search(r'(content|text|word|key|title|name|query)', k, re.I) \
                    and not re.search(r'(page|size|time|date|type|state|valid|order|id|area|label|sort)', k, re.I):
                cands.append(k)
        return cands[:4]

    # ---- 4. 列表接口 ----
    def _probe_list(self, sig: dict, defaults: dict | None = None,
                    referer_url: str = '') -> dict | None:
        t0 = time.monotonic()
        eps = sig['endpoints']
        if not eps:
            self._stage('探测取数接口', 'skip',
                        'JS 中未发现 JSON 接口路径，转入 HTML 列表探测', t0)
            return None

        base_defaults = dict(self.defaults or {})
        base_defaults.update(defaults or {})
        referer = referer_url or self.url
        found: list[dict] = []
        tried: set[tuple] = set()

        def done() -> bool:
            """找到「关键词确实生效」的高分接口才提前收敛。

            邮储的 queryContentPage 路径分很高、能返回 15 条，但完全不认关键词；
            若按 rank 提前停，就永远试不到同站的 searchKeyWord 接口。
            """
            return any(c['rank'] >= 40 and (not self.keyword or c['keyword_hit'] >= 0.5)
                       for c in found)
        for ep in eps[:5]:
            if not self._left():
                break
            ep_budget = 0
            keys = dict(ep.get('param_keys') or {})
            # 模板里的嵌套参数（如 dto.keyWords）也要参与关键词识别
            for k in _deep_keys((ep.get('template') or {}).get('data')):
                keys.setdefault(k, '')
            for kw_key in (self._keyword_candidates(keys) or ['keyword'])[:2]:
                if not self._left():
                    break
                for payload, roles, style in self._payload_variants(
                        ep, base_defaults, kw_key)[:2]:
                    ctx = {'page': 1, 'size': 50, 'keyword': self.keyword}
                    ctype = (ep.get('template') or {}).get('content_type')
                    if ctype == 'json' or style == 'dto':
                        encodings = (True,)
                    elif ctype == 'form':
                        encodings = (False,)
                    else:
                        encodings = (False, True)
                    for as_json in encodings:
                        if ep_budget >= 4 or not self._left():
                            break
                        for full in self._endpoint_urls(ep['path'], base_defaults):
                            if (full, as_json, kw_key, style) in tried or not self._left():
                                continue
                            tried.add((full, as_json, kw_key, style))
                            ep_budget += 1
                            headers = dict(JSON_HEADERS if as_json else FORM_HEADERS)
                            headers['Referer'] = referer
                            headers['Origin'] = self.origin
                            try:
                                r = self._post(full, _render(payload, ctx), headers, as_json)
                                if r.status_code >= 400:
                                    continue
                                data = r.json()
                            except Exception as e:
                                self.errors.append('%s 请求失败：%s' % (full, _short(str(e), 90)))
                                continue
                            got = _find_items(data)
                            if not got:
                                continue
                            ipath, items, base = got
                            hit = _keyword_hit(items, self.keyword)
                            total = _find_total_value(data)
                            rank = base + ep['score']
                            if hit >= 0.5:
                                rank += 18
                            elif self.keyword and hit > 0:
                                rank += 6
                            if _title_quality(items):
                                rank += 10
                            if isinstance(total, int) and total > 2000:
                                rank -= 16      # 关键词没被接口采纳，返回了全站数据
                            if any(ID_HINT.search(k) for k in items[0].keys()):
                                rank += 4
                            cand = {'url': full, 'method': 'POST', 'payload': payload,
                                    'payload_kind': 'json' if as_json else 'form',
                                    'payload_style': style,
                                    'items_path': ipath,
                                    'total_path': _find_total(data) or '',
                                    'count': len(items), 'total': total, 'rank': rank,
                                    'keyword_hit': round(hit, 2),
                                    'keyword_key': kw_key, 'roles': roles,
                                    'endpoint_path': ep['path'], 'source': ep['source'],
                                    'keys': list(items[0].keys()),
                                    'rows_sample': [{k: _short(v, 140) for k, v in row.items()}
                                                    for row in items[:3]],
                                    'first_row': items[0]}
                            found.append(cand)
                            if done():
                                break
                        if done():
                            break
                    if done():
                        break
                if done():
                    break
            if done():
                break

        if not found:
            self._stage('探测取数接口', 'warn',
                        '试了 %d 组 URL/编码/关键词组合，均未拿到条目数组' % len(tried), t0)
            return None
        # 关键词命中优先：搜索接口 > 返回全站数据的列表接口
        found.sort(key=lambda c: (c['keyword_hit'] >= 0.5, c['rank']), reverse=True)
        best = found[0]
        self.evidence['list_candidates'] = [
            {k: v for k, v in c.items() if k not in ('first_row',)} for c in found[:5]]
        self._stage('探测取数接口', 'ok',
                    '%s [%s/%s] → %s（%d 条，total=%s，关键词命中率 %.0f%%，参数 %s）' %
                    (best['url'], best['payload_kind'], best['payload_style'],
                     best['items_path'], best['count'], best['total'],
                     best['keyword_hit'] * 100, best['keyword_key'] or '-'), t0)
        return best

    def _verify_page(self, best: dict):
        t0 = time.monotonic()
        roles = best['roles']
        best['page2_differs'] = False
        best['page2_count'] = 0
        base = dict(FORM_HEADERS if best['payload_kind'] == 'form' else JSON_HEADERS)
        # 用候选自带的真实 header（浏览器嗅探/入口直探拿到的）
        base.update({k: v for k, v in (best.get('headers') or {}).items()
                     if k.lower() in ('referer', 'origin', 'accept', 'content-type',
                                      'x-requested-with', 'cookie')})
        base.setdefault('Referer', best.get('referer') or self.url)
        # 网关普遍校验 Origin（缺了直接 0011 来源非法），浏览器嗅探头里未必带
        base.setdefault('Origin', self.origin)

        # 先验证关键词是否真的生效：带关键词 total 应明显小于不带
        if self.keyword and roles.get('keyword') and self._left():
            try:
                r0 = self._post(best['url'],
                                _render(best['payload'], {'page': 1, 'size': 10,
                                                          'keyword': ''}),
                                base, best['payload_kind'] == 'json')
                d0 = r0.json() if r0.status_code < 400 else {}
                tot0 = _find_total_value(d0)
                r1 = self._post(best['url'],
                                _render(best['payload'], {'page': 1, 'size': 10,
                                                          'keyword': self.keyword}),
                                base, best['payload_kind'] == 'json')
                d1 = r1.json() if r1.status_code < 400 else {}
                tot1 = _find_total_value(d1)
                got1 = _find_items(d1)
                hit1 = _keyword_hit(got1[1], self.keyword) if got1 else 0
                best['keyword_hit'] = round(max(hit1, best.get('keyword_hit') or 0), 2)
                if isinstance(tot1, int):
                    best['total'] = tot1
                if isinstance(tot0, int) and isinstance(tot1, int) and tot1 < tot0:
                    self._stage('验证关键词生效', 'ok',
                                '带关键词 total %s < 不带 %s' % (tot1, tot0), t0)
                elif hit1 >= 0.5:
                    self._stage('验证关键词生效', 'ok',
                                '前 10 条命中率 %.0f%%' % (hit1 * 100), t0)
                else:
                    self._stage('验证关键词生效', 'warn',
                                '关键词字段 %s 可能未被采纳（total %s→%s）' %
                                (roles.get('keyword'), tot0, tot1), t0)
            except Exception as e:
                self.errors.append('关键词验证失败：%s' % _short(str(e), 80))

        if not roles.get('page') or not self._left():
            self._stage('验证翻页', 'skip', '未识别页码参数', t0)
            return
        try:
            # 页大小固定 10：总条数不足大页时（如 40 条），第 2 页会空，
            # 会被误判成「只有一页」，从而低估分页是否可用。
            r = self._post(best['url'],
                           _render(best['payload'], {'page': 2, 'size': 10,
                                                     'keyword': self.keyword}),
                           base, best['payload_kind'] == 'json')
            got = _find_items(r.json()) if r.status_code < 400 else None
            items2 = got[1] if got else []
            best['page2_count'] = len(items2)
            # 用行内实际存在的 id/标题键做指纹，避免写死 id/noticeId/title
            row = best.get('first_row') or {}
            sig_keys = [k for k in row.keys()
                        if ID_HINT.search(str(k)) or TITLE_HINT.search(str(k))][:2] or \
                       list(row.keys())[:2]
            sig1 = [str(row.get(k) or '') for k in sig_keys]
            row2 = items2[0] if items2 else {}
            sig2 = [str(row2.get(k) or '') for k in sig_keys]
            best['page2_differs'] = bool(items2 and sig1 and sig1 != sig2)
        except Exception as e:
            self.errors.append('翻页验证失败：%s' % _short(str(e), 90))
        self._stage('验证翻页', 'ok' if best['page2_differs'] else 'warn',
                    '第 2 页 %d 条，%s' % (best['page2_count'],
                                        '首条与上页不同' if best['page2_differs']
                                        else '与上页相同或为空（可能只有一页）'), t0)

    # ---- 5. 详情 URL 模板 ----
    def _detail_module(self, sig: dict) -> str:
        joined = '\n'.join(sig['url_lines']) + '\n' + '\n'.join(
            (s.get('text') or '')[:20000] for s in self.evidence.get('scripts', []))
        m = re.search(r'(?:noticeDetail|yunDetail|goUrlSearch|detailUrl)\s*\(\s*'
                      r'["\']([^"\']*(?:detail|content)[^"\']*)["\']', joined, re.I)
        if m:
            return m.group(1)
        q = dict(parse_qsl(urlparse(self.url).query, keep_blank_values=True))
        cur = q.get('url') or ''
        if cur:
            return re.sub(r'(list|index)$', 'detail', cur)
        return ''

    def _detail_url_template(self, sig: dict) -> str:
        t0 = time.monotonic()
        # 1. 列表行自带相对详情路径（CMS 静态页形态）：
        #    row.url=/1ywgg2/2026...html，页面隐藏域 sitePthTempLocal=/cms/default/webfile
        row = (self.evidence.get('list') or {}).get('first_row') or {}
        sample = row.get('url')
        prefix = str((self.defaults or {}).get('sitePthTempLocal') or '').strip('/')
        if isinstance(sample, str) and sample.startswith('/') and prefix:
            tpl = self.origin + '/' + prefix + '{url}'
            self.evidence['detail_url_template'] = tpl
            self._stage('还原详情页 URL', 'ok',
                        tpl.replace('{url}', '<url>')[:180], t0)
            return tpl
        p = urlparse(self.url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        module = self._detail_module(sig)
        # 入口本身是 API 接口且无真实详情线索时，不要臆造 ?column=undefined&searchVal= 假 URL
        style = (self.evidence.get('list') or {}).get('payload_style')
        if not module and not q.get('searchVal') and (
                style in ('browser', 'entry_api') or self._looks_like_api_url(self.url)):
            self.evidence['detail_url_template'] = ''
            return ''
        if module:
            q['url'] = module
        q['column'] = 'undefined'
        q['searchVal'] = '{id}'
        # 手工拼 query：urlencode 会把 modules/sys/login/detail 里的斜杠转义掉
        query = '&'.join('%s=%s' % (k, v) for k, v in q.items())
        tpl = urlunparse((p.scheme, p.netloc, p.path, '', query, ''))
        self.evidence['detail_url_template'] = tpl
        self._stage('还原详情页 URL', 'ok',
                    tpl.replace('{id}', '<id>')[:180], t0)
        return tpl

    @staticmethod
    def _html_detail_content(html_text: str, url: str) -> dict | None:
        """详情页 HTML 里的正文容器（.content 等），给配方 content_css。"""
        try:
            from parsel import Selector
        except ImportError:
            return None
        sel = Selector(text=html_text)
        cands = ('.content', '.article-content', '.detail-content', '.news-content',
                 '.infolist-main', '.article', '.detail', '#content', 'article',
                 '.box-content', '.notice-content', '.m-detail .content')
        best = None
        for css in cands:
            nodes = sel.css(css)
            if not nodes:
                continue
            for node in nodes[:2]:
                txt = re.sub(r'\s+', ' ',
                             ' '.join(node.xpath('.//text()').getall())).strip()
                if len(txt) >= 200 and (best is None or len(txt) > best[1]):
                    best = (css, len(txt), txt)
        if not best:
            return None
        title = ''
        for css in ('h1::text', '.title-text::text', 'title::text'):
            vals = [v.strip() for v in sel.css(css).getall() if v and v.strip()]
            if vals:
                title = vals[0][:200]
                break
        return {'kind': 'html', 'url': url, 'content_css': best[0],
                'content_len': best[1], 'content_preview': best[2][:200],
                'title': title, 'row_keys': []}

    # ---- 6. 详情接口 ----
    def _probe_detail(self, best: dict, sig: dict, tpl: str) -> dict | None:
        t0 = time.monotonic()
        row = best['first_row'] or {}
        id_key = next((k for k in ID_KEYS if row.get(k)), '')
        if not id_key:
            id_key = next((k for k in row if ID_HINT.search(k) and row.get(k)), '')
        sample_id = str(row.get(id_key) or '')
        if not sample_id:
            self._stage('探测详情接口', 'warn', '列表行里没有可用的 id', t0)
            return None
        self.evidence['sample_id'] = sample_id
        self.evidence['id_key'] = id_key

        # 打开详情页，收集它自己的 JS（金采网的 detail.js 里才有 isDetail）
        detail_scripts: list[dict] = []
        detail_html = ''
        detail_url = (tpl.replace('{id}', sample_id)
                        .replace('{url}', str(row.get('url') or '')))
        try:
            r = self._get(detail_url)
            detail_html = r.text or ''
            self.evidence['detail_page'] = {'url': detail_url,
                                            'status': r.status_code,
                                            'chars': len(detail_html)}
            if r.status_code < 400:
                detail_scripts = self._collect_scripts(detail_html, detail_url, 'detail')
        except Exception as e:
            self.evidence['detail_page'] = {'url': detail_url, 'status': 0,
                                            'error': _short(str(e), 100)}
        djs = parse_js_signals(detail_scripts) if detail_scripts else {'endpoints': [], 'per_script': []}
        self.evidence['detail_scripts'] = [
            {'url': s['url'], 'chars': len(s['text']), 'tag': 'detail'}
            for s in detail_scripts]

        # 详情页本身就是正文（CMS 静态页 /cms/default/webfile/...）：不必再猜详情接口
        if '{url}' in tpl and detail_html:
            got_html = self._html_detail_content(detail_html, detail_url)
            if got_html:
                self._stage('探测详情接口', 'ok',
                            '%s → %s（HTML 正文 %d 字符）' %
                            (detail_url, got_html['content_css'],
                             got_html['content_len']), t0)
                return got_html

        # 候选详情接口：列表接口放首位（同接口带 isDetail 是最常见的形态），
        # 其余只保留看起来像数据接口的，避免把 /FileUp/ 之类的资源路径也试一遍。
        list_path = urlparse(best['url']).path
        paths = [list_path]
        if self.ctx and list_path.startswith('/' + self.ctx + '/'):
            paths.insert(0, list_path[len(self.ctx) + 1:])
        for e in djs['endpoints']:
            p = e['path']
            if len(p) >= 12 and (GOOD_PATH.search(p) or 'notice' in p.lower()):
                paths.append(p)
        paths = list(dict.fromkeys(paths))[:5]

        # 详情请求体：优先用详情 JS 里 var param={...} 的键，再兜通用组合
        bodies: list[dict] = []
        for ps in djs['per_script']:
            keys = ps.get('param_keys') or {}
            if any(k.lower() == 'id' for k in keys):
                body = {k: ('{id}' if k.lower() == 'id' else (v or ''))
                        for k, v in keys.items()
                        if re.match(r'^[A-Za-z_]\w*$', k)}
                if len(body) <= 8:
                    bodies.append(body)
        bodies.append({'id': '{id}', 'isDetail': '1'})
        bodies.append({'id': '{id}'})
        seen: set[str] = set()
        bodies = [b for b in bodies if not (json.dumps(b, sort_keys=True) in seen
                                           or seen.add(json.dumps(b, sort_keys=True)))][:4]

        ctx = {'id': sample_id, 'keyword': '', 'page': 1, 'size': 50}
        found = None
        for path in paths:
            if not self._left():
                break
            for full in _abs(self.url, path):
                if not self._left():
                    break
                for body in bodies:
                    if not self._left():
                        break
                    for as_json in (False, True):
                        if not self._left():
                            break
                        headers = dict(JSON_HEADERS if as_json else FORM_HEADERS)
                        headers['Referer'] = detail_url
                        headers['Origin'] = self.origin
                        try:
                            r = self._post(full, _render(body, ctx), headers, as_json)
                            if r.status_code >= 400:
                                continue
                            data = r.json()
                        except Exception:
                            continue
                        got = _find_content(data)
                        if not got or got['score'] < 0:
                            continue
                        cand = {'url': full, 'method': 'POST', 'body': body,
                                'payload_kind': 'json' if as_json else 'form',
                                'row_path': got['row_path'],
                                'content_path': got['content_path'],
                                'content_encoding': got['encoding'],
                                'content_len': got['value_len'],
                                'content_preview': got['preview'],
                                'endpoint_path': path,
                                'row_keys': sorted(set(
                                    k for k in _row_at(data, got['row_path']).keys()))
                                if isinstance(_row_at(data, got['row_path']), dict) else [],
                                'score': got['score']}
                        if found is None or cand['score'] > found['score']:
                            found = cand
                        if got['score'] >= 30000:
                            break
                    if found and found['score'] >= 30000:
                        break
                if found and found['score'] >= 30000:
                    break
            if found and found['score'] >= 30000:
                break

        if not found and detail_html:
            # 详情页本身就是正文（CMS 静态页），不需要二次 JSON 请求
            got_html = self._html_detail_content(detail_html, detail_url)
            if got_html:
                self._stage('探测详情接口', 'ok',
                            '%s → %s（HTML 正文 %d 字符）' %
                            (detail_url, got_html['content_css'],
                             got_html['content_len']), t0)
                return got_html
        if not found:
            self._stage('探测详情接口', 'warn',
                        '未找到长文本正文字段，配方将只保留列表字段', t0)
            return None
        found.pop('score', None)
        self._stage('探测详情接口', 'ok',
                    '%s body=%s → %s.%s（%d 字符，%s）' %
                    (found['url'], json.dumps(found['body'], ensure_ascii=False)[:80],
                     found['row_path'], found['content_path'], found['content_len'],
                     found['content_encoding']), t0)
        return found

    # ---- 6b. HTML 列表站点（goSearch + page）与 PDF 详情 ----
    def _probe_html_list(self, sig: dict) -> dict | None:
        t0 = time.monotonic()
        tpl = find_search_template(self.entry_html, self.url)
        if not tpl:
            self._stage('探测 HTML 列表', 'warn',
                        '入口页未找到「关键词 + 分页」的搜索 URL 模板', t0)
            return None

        def url_for(page: int) -> str:
            q = {}
            for k, v in tpl['params'].items():
                q[k] = str(v).replace('{keyword}', self.keyword) \
                              .replace('{page}', str(page))
            return '%s?%s' % (tpl['url'], urlencode(q, encoding='utf-8'))

        u1 = url_for(1)
        r = None
        parsed = None
        try:
            r = self._get(u1)
            if r.status_code < 400:
                parsed = parse_html_list(r.text, tpl['url'])
        except Exception as e:
            self.errors.append('搜索页抓取失败：%s' % _short(str(e), 90))
        if not parsed:
            self._stage('探测 HTML 列表', 'warn',
                        '%s 未识别出重复列表行' % u1[:140], t0)
            return None

        page2_differs, p2_url = False, None
        if tpl.get('page_param') and self._left():
            try:
                r2 = self._get(url_for(2))
                p2 = parse_html_list(r2.text, tpl['url']) if r2.status_code < 400 else None
                if p2:
                    p2_url = p2['first_url']
                    page2_differs = p2_url != parsed['first_url']
            except Exception as e:
                self.errors.append('HTML 翻页验证失败：%s' % _short(str(e), 90))
        m = re.search(r'共\s*<label>\s*(\d+)\s*</label>\s*页|共\s*(\d+)\s*页',
                      r.text if r is not None else '')
        total_pages = int(m.group(1) or m.group(2)) if m else None

        pdf = None
        du = parsed['first_url']
        if self._left():
            try:
                dr = self._get(du)
                self.evidence['detail_page'] = {'url': du, 'status': dr.status_code,
                                                'chars': len(dr.text or '')}
                if dr.status_code < 400:
                    pdf = find_pdf_source(dr.text, du)
            except Exception as e:
                self.errors.append('详情页抓取失败：%s' % _short(str(e), 90))

        ev = {
            'mode': 'html_list', 'search': tpl, 'search_url': u1,
            'row_selector': parsed['row_selector'], 'fields': parsed['fields'],
            'rows': parsed['rows'], 'sample': parsed['sample'],
            'first_url': du, 'detail_path': parsed['detail_path'],
            'page2_differs': page2_differs, 'page2_first_url': p2_url,
            'total_pages': total_pages, 'pdf': pdf,
            'keyword_hit': _keyword_hit([{'title': x['title']} for x in parsed['sample']],
                                        self.keyword),
        }
        self._stage('探测 HTML 列表', 'ok',
                    '%s → %s（%d 行，共 %s 页，翻页%s，关键词参数 %s）' %
                    (tpl['url'], parsed['row_selector'], parsed['rows'],
                     total_pages or '?', '有效' if page2_differs else '无效',
                     tpl['keyword_param']), t0)
        if pdf:
            self._stage('探测 PDF 正文', 'ok',
                        '%s（%s）' % (pdf['pdf_url'][:150], pdf['iframe_css']), t0)
        else:
            self._stage('探测 PDF 正文', 'warn',
                        '详情页未发现 PDF 链接，正文按 HTML 处理', t0)
        return ev

    # ---- 编排 ----
    def _probe_browser(self) -> dict | None:
        """无头浏览器嗅探：真实渲染 SPA，拦截站点自己发出的 XHR。

        适用：入口 HTML 是空壳（SPA）、主包混淆导致静态解析不出接口。
        返回的 payload 会保留站点真实业务参数（如 publishColumn），
        只把关键词/页码/页大小替换成占位符，因此成功率远高于盲猜。
        """
        t0 = time.monotonic()
        try:
            from . import browser_probe
        except Exception as e:
            self._stage('浏览器嗅探', 'skip', '模块不可用：%s' % _short(str(e), 60), t0)
            return None
        if not browser_probe.find_browser():
            self._stage('浏览器嗅探', 'skip', '未找到 Edge/Chrome，跳过', t0)
            return None
        try:
            res = browser_probe.sniff(self.url, self.keyword,
                                      timeout_s=min(40, max(15, self.budget_s)),
                                      wait_ms=4000)
        except Exception as e:
            self.errors.append('浏览器嗅探失败：%s' % _short(str(e), 120))
            self._stage('浏览器嗅探', 'warn', _short(str(e), 120), t0)
            return None
        self.evidence['browser'] = {'ok': res.get('ok'), 'browser': res.get('browser'),
                                    'requests': len(res.get('requests') or []),
                                    'reason': res.get('reason') or ''}
        if not res.get('ok'):
            self._stage('浏览器嗅探', 'warn', res.get('reason') or '未捕获到接口', t0)
            return None

        best = None
        for req in res['requests']:
            path, items, data = browser_probe.json_items(req)
            if not path or not items:
                continue
            keys = list(items[0].keys()) if isinstance(items[0], dict) else []
            if not any(ID_HINT.search(k) for k in keys) and not _title_quality(items):
                continue
            payload, roles = self._browser_payload(req, items)
            hit = _keyword_hit(items, self.keyword)
            total = _find_total_value(data)
            rank = 40 + (18 if hit >= 0.5 else 0) + (10 if _title_quality(items) else 0)
            cand = {'url': req['url'], 'method': req.get('method') or 'POST',
                    'payload': payload, 'payload_kind': 'json',
                    'payload_style': 'browser', 'items_path': path,
                    'total_path': _find_total(data) or '',
                    'count': len(items), 'total': total, 'rank': rank,
                    'keyword_hit': round(hit, 2), 'keyword_key': roles.get('keyword') or '',
                    'roles': roles, 'endpoint_path': urlparse(req['url']).path,
                    'source': 'browser', 'keys': keys,
                    'referer': self.url, 'headers': dict(req.get('headers') or {}),
                    'rows_sample': [{k: _short(v, 140) for k, v in row.items()}
                                    for row in items[:3] if isinstance(row, dict)],
                    'first_row': items[0] if isinstance(items[0], dict) else {}}
            if best is None or (cand['keyword_hit'] >= 0.5, cand['rank']) > \
                    (best['keyword_hit'] >= 0.5, best['rank']):
                best = cand
        if not best:
            self._stage('浏览器嗅探', 'warn',
                        '捕获 %d 个请求，但响应里没有条目数组' %
                        len(res.get('requests') or []), t0)
            return None
        self._stage('浏览器嗅探', 'ok',
                    '%s → %s（%d 条，total=%s，关键词命中率 %.0f%%）' %
                    (best['url'], best['items_path'], best['count'],
                     best['total'], best['keyword_hit'] * 100), t0)
        return best

    def _browser_payload(self, req: dict, items: list) -> tuple[dict, dict]:
        """把嗅探到的真实请求体参数化：页码/页大小/关键词换成占位符。"""
        raw = req.get('post_data') or ''
        payload: dict = {}
        if raw.strip().startswith('{'):
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
        if not isinstance(payload, dict) or not payload:
            payload = {'page': '{page}', 'size': '{size}'}
        roles: dict[str, str] = {'page': '', 'size': '', 'keyword': ''}

        def walk(obj, path='') -> None:
            if isinstance(obj, dict):
                for k in list(obj.keys()):
                    v = obj[k]
                    kl = str(k).lower()
                    if isinstance(v, (dict, list)):
                        walk(v, path + '.' + str(k))
                        continue
                    if kl in ('size', 'pagesize', 'limit', 'limitnum', 'rows'):
                        obj[k] = '{size}'
                        roles['size'] = roles['size'] or str(k)
                    elif kl in ('current', 'pageno', 'pagenum', 'page', 'pageindex',
                                'currentpage', 'pagenumber'):
                        obj[k] = '{page}'
                        roles['page'] = roles['page'] or str(k)

        walk(payload)

        # 关键词字段推断，按可靠性排序：
        # 1) 请求体里已有的空字符串文本字段，且名字像标题/关键词；
        # 2) 列表行里存在 title 类字段 → 同名请求参数（站点多用同一 DTO 查询）；
        # 3) 请求体里有任何「像搜索」的字符串键；
        # 4) 兜底 messageTitle（国内网关最常见的标题检索字段）。
        def _is_texty(v) -> bool:
            return isinstance(v, str) and len(v) <= 60

        body_txt = [k for k in _deep_keys(payload)
                    if _is_texty(_deep_value(payload, k))]
        search_like = [k for k in body_txt
                       if re.search(r'(title|keyword|key|word|content|name|query)', str(k), re.I)]
        kw_key = ''
        if search_like:
            kw_key = sorted(search_like, key=lambda k: (
                0 if re.search(r'title', str(k), re.I) else 1, len(str(k))))[0]
        if not kw_key:
            row_title = next((k for k in (items[0].keys() if items and
                                          isinstance(items[0], dict) else ())
                              if re.search(r'title', str(k), re.I)), '')
            if row_title:
                kw_key = row_title
        if not kw_key:
            kw_key = 'messageTitle'

        target = payload
        if kw_key not in payload:
            # 优先塞进已有子对象（如 pageInfo / dto），否则放顶层
            holder = next((v for v in payload.values()
                           if isinstance(v, dict) and kw_key in v), None)
            if holder is None:
                for v in payload.values():
                    if isinstance(v, dict) and any(
                            re.search(r'(page|query|search|param|dto)', str(x), re.I)
                            for x in v.keys()):
                        holder = v
                        break
            target = holder if holder is not None else payload
        target[kw_key] = '{keyword}'
        roles['keyword'] = kw_key
        return payload, roles

    @staticmethod
    def _looks_like_api_url(url: str) -> bool:
        """入口本身就是接口地址（用户直接贴 API URL）而非 HTML 页面。"""
        path = urlparse(url).path.lower()
        return bool(GOOD_PATH.search(path)) and bool(re.search(
            r'(/api/|/gateway/|/portalmessage/|query\w*$|list\w*$|search\w*$)', path))

    def _probe_entry_as_api(self, entry: dict, sig: dict) -> dict | None:
        """入口 URL 本身就是 JSON 接口：把它当接口候选直接 POST 探测。

        典型场景：用户直接把 F12 里看到的接口地址贴进「AI 生成配方」，
        例如 jc.abchina.com.cn/gateway/puc/portalMessage/queryInviteMessageForEie。
        该地址 GET 返回 405、页面无 JS，旧逻辑会直接退回 html_list 空壳。
        这里为它生成通用嵌套分页模板（pageInfo / dto / 平铺）逐个试探。
        """
        t0 = time.monotonic()
        api_like = (entry.get('status', 200) >= 400
                    or 'json' in str(entry.get('content_type', '')).lower()
                    or entry.get('json') is not None
                    or self._looks_like_api_url(self.url))
        if not api_like:
            return None
        path = urlparse(self.url).path
        # 关键词字段优先试「标题类」（网关接口多以 messageTitle/title 做全文检索），
        # 再试通用 keyword；避免 briefContent 先返回全站数据误导打分。
        kw_priority = ('messageTitle', 'noticeTitle', 'title', 'projectName',
                       'briefContent', 'keyword', 'keywords', 'keyWords', 'keyWord',
                       'searchKey', 'searchText', 'searchWord', 'searchContent',
                       'searchValue', 'noticeContent', 'content', 'q')
        keys: dict[str, str] = {}
        for k in kw_priority + PAGE_KEYS + SIZE_KEYS + ID_KEYS:
            keys[k] = ''
        ep = {'path': path, 'score': 24, 'source': self.url,
              'param_keys': keys, 'method': 'POST',
              'template': {'url': path, 'method': 'POST', 'content_type': 'json',
                           'data': {'pageInfo': {'current': '{page}', 'size': '{size}'}}}}
        # 多个嵌套分页模板轮流试：pageInfo / 平铺 / dto
        templates = [
            {'pageInfo': {'current': '{page}', 'size': '{size}'}},
            {'pageNo': '{page}', 'pageSize': '{size}'},
            {'page': '{page}', 'size': '{size}'},
            {'pageNum': '{page}', 'limit': '{size}'},
        ]
        found: list[dict] = []
        for tpl in templates:
            if not self._left():
                break
            local = dict(ep)
            local['template'] = {'url': path, 'method': 'POST',
                                 'content_type': 'json', 'data': dict(tpl)}
            # 试完所有关键词候选再选命中率最高的：第一个能返回条目的字段
            # 可能根本不认关键词（如 briefContent 返回全站数据），必须比较后择优
            for kw_key in [k for k in kw_priority if k in keys][:8]:
                if not self._left():
                    break
                for payload, roles, style in self._payload_variants(local, {}, kw_key)[:1]:
                    if not self._left():
                        break
                    headers = dict(JSON_HEADERS)
                    # 网关型接口常校验 Referer 必须来自其前端页面（如 /puc/），
                    # 直接贴接口地址时 self.ctx 已提取出这段前缀。
                    ref = self.url
                    seg = [x for x in urlparse(self.url).path.split('/') if x]
                    if len(seg) >= 2 and seg[0].lower() == 'gateway':
                        # /gateway/puc/... → 前端在 /puc/
                        ref = '%s/%s/' % (self.origin, seg[1])
                    elif self.ctx:
                        ref = '%s/%s/' % (self.origin, self.ctx)
                    headers['Referer'] = ref
                    headers['Origin'] = self.origin
                    try:
                        r = self._post(self.url, _render(payload, {
                            'page': 1, 'size': 10, 'keyword': self.keyword}), headers, True)
                        if r.status_code >= 400:
                            continue
                        data = r.json()
                    except Exception as e:
                        self.errors.append('入口接口探测失败：%s' % _short(str(e), 90))
                        continue
                    got = _find_items(data)
                    if not got:
                        continue
                    ipath, items, base = got
                    hit = _keyword_hit(items, self.keyword)
                    total = _find_total_value(data)
                    rank = base + 24 + (18 if hit >= 0.5 else 6 if hit > 0 else 0)
                    if _title_quality(items):
                        rank += 10
                    if isinstance(total, int) and total > 2000:
                        rank -= 16
                    cand = {'url': self.url, 'method': 'POST', 'payload': payload,
                            'payload_kind': 'json', 'payload_style': 'entry_api',
                            'items_path': ipath, 'total_path': _find_total(data) or '',
                            'count': len(items), 'total': total, 'rank': rank,
                            'keyword_hit': round(hit, 2), 'keyword_key': kw_key,
                            'roles': roles, 'endpoint_path': path, 'source': self.url,
                            'referer': ref, 'headers': dict(headers),
                            'keys': list(items[0].keys()),
                            'rows_sample': [{k: _short(v, 140) for k, v in row.items()}
                                            for row in items[:3]],
                            'first_row': items[0]}
                    found.append(cand)
                    # 命中率高就停止试其它关键词（已找到真正生效的搜索字段）
                    if hit >= 0.5:
                        break
            # 该模板已找到高命中候选，无需再换分页模板
            if any(c['keyword_hit'] >= 0.5 for c in found):
                break
        if not found:
            self._stage('入口接口直探', 'warn',
                        '入口像接口，但通用分页/关键词模板未取到条目数组', t0)
            return None
        found.sort(key=lambda c: (c['keyword_hit'] >= 0.5, c['rank']), reverse=True)
        return found[0]

    def run(self) -> dict:
        t_all = time.monotonic()
        try:
            entry = self._fetch_entry()
            scripts = self._collect_scripts(self.entry_html, self.url, 'list')
            self.evidence['scripts'] = [
                {'url': s['url'], 'chars': len(s['text']), 'tag': s['tag']}
                for s in scripts]
            t0 = time.monotonic()
            sig = parse_js_signals(scripts)
            self.evidence['js_signals'] = {
                'endpoints': [{'path': e['path'], 'score': e['score'],
                               'source': e['source']} for e in sig['endpoints']],
                'url_lines': sig['url_lines'][:12],
                'state_defaults': sig['state_defaults'],
                'method_hint': sig['method_hint']}
            self._stage('解析站点 JS', 'ok',
                        '%d 个脚本，识别出 %d 个接口候选（首选 %s）' %
                        (len(scripts), len(sig['endpoints']),
                         sig['endpoints'][0]['path'] if sig['endpoints'] else '-'), t0)

            # 入口 URL 本身是接口（GET 405 / JSON / 接口路径）时，先直接 POST 探测入口
            best = self._probe_entry_as_api(entry, sig)
            if best:
                sig['endpoints'].insert(0, {
                    'path': best['endpoint_path'], 'score': best['rank'],
                    'source': best['source'], 'template': {
                        'url': best['endpoint_path'], 'method': 'POST',
                        'content_type': best['payload_kind'],
                        'data': best['payload']},
                    'param_keys': {k: '' for k in _deep_keys(best['payload'])},
                    'method': 'POST'})

            if not best:
                best = self._probe_list(sig)
            if not best:
                # 入口页只有推荐位，接口参数（siteId/categoryId）在列表页隐藏域里。
                # 从导航找列表页 → 读隐藏域 → 带默认参数再探一次。
                for lp in discover_list_pages(self.entry_html, self.url)[:3]:
                    if not self._left():
                        break
                    try:
                        lp_html = self._get(lp).text
                    except Exception as e:
                        self.errors.append('列表页 %s 抓取失败：%s' % (lp, _short(str(e), 80)))
                        continue
                    defaults = parse_hidden_defaults(lp_html)
                    if not defaults:
                        continue
                    self.defaults.update(defaults)
                    self.evidence['list_page'] = {'url': lp, 'defaults': defaults}
                    t1 = time.monotonic()
                    self._stage('发现列表页参数', 'ok',
                                '%s → %s' % (lp, ', '.join(
                                    '%s=%s' % kv for kv in list(defaults.items())[:8])), t1)
                    best = self._probe_list(sig, defaults=defaults, referer_url=lp)
                    if best:
                        best['list_page'] = lp
                        break
            if not best:
                # SPA 站（入口仅空壳 + 主包混淆）静态拿不到接口，
                # 用无头浏览器真实渲染并拦截 XHR，拿到站点自己发出的请求
                best = self._probe_browser()
            self.evidence['list'] = best
            if best:
                self._verify_page(best)
                tpl = self._detail_url_template(sig)
                detail = self._probe_detail(best, sig, tpl)
                self.evidence['detail'] = detail
            else:
                self.evidence['detail'] = None
                self.evidence['html_list'] = self._probe_html_list(sig)
        except Exception as e:
            self.errors.append('探测异常：%s' % _short(repr(e), 200))
            self._stage('探测流程', 'error', _short(str(e), 160), t_all)
        self.evidence['keyword'] = self.keyword
        self.evidence['entry_url'] = self.url
        self.evidence['stages'] = self.stages
        self.evidence['errors'] = self.errors[:20]
        self.evidence['requests'] = self.requests
        self.evidence['elapsed_ms'] = int((time.monotonic() - t_all) * 1000)
        return self.evidence


def _row_at(data, path: str):
    """按 $.rows[0] / data.list.0 这类路径取对象，用于拿详情行的字段名。"""
    cur = data
    for part in str(path).replace('[*]', '').lstrip('$.').replace('[', '.').replace(']', '').split('.'):
        if not part:
            continue
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


if __name__ == '__main__':
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else (
        'http://www.cfcpn.com/jcw/sys/index/goUrl?url=modules/sys/login/list&column=cggg')
    kw = sys.argv[2] if len(sys.argv) > 2 else '门头'
    p = SiteProbe(u, kw)
    try:
        ev = p.run()
        print(json.dumps({k: v for k, v in ev.items() if k != 'scripts'},
                         ensure_ascii=False, indent=2)[:16000])
        print('\n--- stages ---')
        for s in ev['stages']:
            print('[%s] %s | %s (%dms)' % (s['status'], s['name'], s['detail'], s['ms']))
    finally:
        p.close()
