# -*- coding: utf-8 -*-
"""证据驱动的配方原料解析：curl + 真实响应样本 → 请求与结构证据。

与 probe.py 的分工：
- probe.py：只给入口页，主动「猜」接口（自动探测，遇到混淆 JS / 强网关容易失败）；
- 本模块：用户已经给出真实可用的 curl 与响应样本，只做**结构解析与规则反推**，
  不猜请求。因此对 SPA、WAF 加固、动态签名的站点更可靠。

产出全部是确定性的（jsonpath / css / 正则），LLM 只在 manual.py 里做字段语义映射。
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

# ------------------------------------------------------------------ curl 解析
_WRAP_RE = re.compile(r'\\\r?\n')
_ANSI_ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', "'": "'", '"': '"', '\\': '\\'}


def _tokenize(cmd: str) -> list[str]:
    """把 curl 命令切成参数 token，兼容 bash / cmd / PowerShell 三种引号写法。"""
    toks: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote: str | None = None
    ansi = False
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == '\\' and i + 1 < n:
                nxt = cmd[i + 1]
                if ansi and nxt in _ANSI_ESCAPES:
                    buf.append(_ANSI_ESCAPES[nxt])
                    i += 2
                    continue
                if quote == '"' and nxt in ('"', '\\'):
                    buf.append(nxt)
                    i += 2
                    continue
                buf.append(ch)
                i += 1
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == '$' and i + 1 < n and cmd[i + 1] == "'":
            # bash ANSI-C 引用：$'{\n "a": 1}'
            quote, ansi = "'", True
            i += 2
            continue
        if ch.isspace():
            if buf:
                toks.append(''.join(buf))
                buf = []
            i += 1
            continue
        # 续行符：\ + 换行 / cmd 的 ^ + 换行
        if ch == '\\' and i + 1 < n and cmd[i + 1] in ('\r', '\n'):
            i += 2
            continue
        if ch == '^' and i + 1 < n and cmd[i + 1] in ('\r', '\n'):
            i += 2
            continue
        buf.append(ch)
        i += 1
    if buf:
        toks.append(''.join(buf))
    return toks


def _kv_header(token: str) -> tuple[str, str]:
    if ':' not in token:
        return '', ''
    k, v = token.split(':', 1)
    return k.strip(), v.strip()


# 这些头由 httpx 自己算，照搬会导致请求异常（伪造的 Content-Length / Host）
_DROP_HEADERS = ('content-length', 'host', 'connection', ':authority', ':method',
                 ':path', ':scheme', 'accept-encoding')


def parse_curl(text: str) -> dict:
    """解析一条 curl 命令。

    返回 {ok, method, url, params, headers, body, body_kind, host, origin, error}。
    url 已去掉 query（query 进 params），便于把参数统一做成模板。
    """
    out = {'ok': False, 'method': '', 'url': '', 'params': {}, 'headers': {},
           'body': None, 'body_kind': '', 'host': '', 'origin': '',
           'path': '', 'raw_url': '', 'error': ''}
    cmd = _WRAP_RE.sub(' ', (text or '').strip())
    if not cmd:
        out['error'] = 'curl 为空'
        return out
    toks = _tokenize(cmd)
    if toks and toks[0].lower() in ('curl', 'curl.exe'):
        toks = toks[1:]
    if not toks:
        out['error'] = '未解析到任何参数'
        return out

    url, method, headers, datas = '', '', {}, []
    i = 0
    skip_value = ('-o', '--output', '-w', '--write-out', '--max-time', '--connect-timeout',
                  '--proxy', '-x', '--cert', '--cacert', '--resolve', '--interface',
                  '--retry', '--limit-rate', '--cookie-jar', '-c', '--form', '-F')
    while i < len(toks):
        a = toks[i]
        low = a.lower()
        if low in ('-x', '--request'):
            method = toks[i + 1] if i + 1 < len(toks) else ''
            i += 2
        elif low in ('-h', '--header'):
            k, v = _kv_header(toks[i + 1] if i + 1 < len(toks) else '')
            if k:
                headers[k] = v
            i += 2
        elif low in ('-d', '--data', '--data-raw', '--data-binary', '--data-urlencode',
                     '--json'):
            datas.append(toks[i + 1] if i + 1 < len(toks) else '')
            if low == '--json':
                headers.setdefault('Content-Type', 'application/json')
            i += 2
        elif low in ('-b', '--cookie'):
            val = toks[i + 1] if i + 1 < len(toks) else ''
            if '=' in val:
                headers['Cookie'] = val
            i += 2
        elif low in ('-a', '--user-agent'):
            headers['User-Agent'] = toks[i + 1] if i + 1 < len(toks) else ''
            i += 2
        elif low in ('-e', '--referer'):
            headers['Referer'] = toks[i + 1] if i + 1 < len(toks) else ''
            i += 2
        elif low == '--url':
            url = toks[i + 1] if i + 1 < len(toks) else ''
            i += 2
        elif low in ('-u', '--user'):
            cred = toks[i + 1] if i + 1 < len(toks) else ''
            headers['Authorization'] = 'Basic ' + base64.b64encode(
                cred.encode('utf-8')).decode()
            i += 2
        elif low in skip_value:
            i += 2
        elif a.startswith('-'):
            # --compressed / -sS / -k 等无值开关
            i += 1
        else:
            if not url and (a.startswith('http://') or a.startswith('https://')):
                url = a
            i += 1

    if not url:
        out['error'] = '未找到请求地址（需要 http(s):// 开头的 URL）'
        return out
    sp = urlsplit(url)
    params = {k: v[0] for k, v in parse_qs(sp.query, keep_blank_values=True).items()}
    base = urlunsplit((sp.scheme, sp.netloc, sp.path, '', ''))

    raw_body = ''
    if datas:
        if len(datas) == 1:
            raw_body = datas[0]
        else:
            # 多个 -d 以 & 连接（curl 的真实行为）
            raw_body = '&'.join(datas)

    ctype = ''
    for k, v in headers.items():
        if k.lower() == 'content-type':
            ctype = (v or '').lower()
            break

    body, kind = None, ''
    if raw_body:
        head = raw_body.lstrip()[:1]
        if 'json' in ctype or head in ('{', '['):
            try:
                body = json.loads(raw_body)
                kind = 'json'
            except Exception:
                body = raw_body
                kind = 'text'
        elif 'x-www-form-urlencoded' in ctype or (
                '=' in raw_body and 'json' not in ctype and 'multipart' not in ctype):
            body = {k: v[0] for k, v in
                    parse_qs(raw_body, keep_blank_values=True).items()}
            kind = 'form'
        else:
            body = raw_body
            kind = 'text'

    method = (method or ('POST' if body is not None else 'GET')).upper()
    headers = {k: v for k, v in headers.items() if k.lower() not in _DROP_HEADERS}
    out.update({
        'ok': True, 'method': method, 'url': base, 'params': params,
        'headers': headers, 'body': body, 'body_kind': kind,
        'host': sp.netloc, 'origin': '%s://%s' % (sp.scheme, sp.netloc),
        'path': sp.path, 'raw_url': url,
    })
    return out


# ------------------------------------------------------------------ 通用小工具
def sample_kind(text: str) -> str:
    s = (text or '').strip()
    if not s:
        return ''
    if s[0] in '{[' and s[-1] in '}]':
        try:
            json.loads(s)
            return 'json'
        except Exception:
            return 'html' if '<' in s[:400] else 'text'
    if s[0] == '<' or re.search(r'<(html|body|div|table|ul|li)\b', s[:2000], re.I):
        return 'html'
    return 'text'


def load_sample(text: str):
    """把样本文本解析成对象：json → dict/list；其它原样返回。"""
    kind = sample_kind(text)
    if kind == 'json':
        try:
            return json.loads(text)
        except Exception:
            return None
    return text


def flat_pairs(obj, prefix: str = '') -> list[tuple[str, object]]:
    """把嵌套 dict 摊平成 (点号路径, 值)。"""
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = '%s.%s' % (prefix, k) if prefix else str(k)
            if isinstance(v, dict):
                out.extend(flat_pairs(v, path))
            else:
                out.append((path, v))
    elif obj is not None and prefix:
        out.append((prefix, obj))
    return out


def set_path(obj: dict, path: str, value):
    """按点号路径写值（供分页参数嵌套在 dto/pageInfo 里时使用）。"""
    parts = str(path).split('.')
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return obj


def get_path(obj, path: str):
    cur = obj
    for p in str(path).split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def flatten_keys(obj, prefix: str = '', depth: int = 0, max_depth: int = 2) -> list[str]:
    """取字段名清单（含一层嵌套的点号名），供字段语义映射使用。"""
    out: list[str] = []
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        path = '%s.%s' % (prefix, k) if prefix else str(k)
        if isinstance(v, dict):
            if depth < max_depth:
                out.append(path)
                out.extend(flatten_keys(v, path, depth + 1, max_depth))
        else:
            out.append(path)
    return out


def _short(v, n: int = 160):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) \
        if isinstance(v, (dict, list)) else str(v)
    s = re.sub(r'\s+', ' ', s)
    return s[:n]


def sample_rows(items: list, limit: int = 3, keep: int = 8) -> list[dict]:
    """裁剪样本行：只保留前若干字段短值，避免喂给 LLM 的内容过长。"""
    out = []
    for row in (items or [])[:limit]:
        if not isinstance(row, dict):
            out.append({'value': _short(row)})
            continue
        slim = {}
        for k, v in list(row.items())[:keep]:
            slim[k] = _short(v, 120)
        out.append(slim)
    return out


# ------------------------------------------------------------------ JSON 结构
def infer_json_list(data) -> dict | None:
    """在列表接口响应里定位条目数组、总数与字段名。"""
    from .probe import _find_items, _find_total, _find_total_value
    got = _find_items(data)
    if not got:
        return None
    items_path, items, score = got
    rows = [x for x in items if isinstance(x, dict)]
    if not rows:
        return None
    keys = flatten_keys(rows[0])
    return {
        'items_path': items_path,
        'total_path': _find_total(data) or '',
        'total_value': _find_total_value(data),
        'keys': keys,
        'count': len(items),
        'rows_sample': sample_rows(rows),
        # 原始行（未裁剪）：详情模板要在「所有样本行」里找 id 字面量，
        # 用户复制的详情 curl 常来自另一条公告，只比第一行会漏。
        'sample_rows_raw': rows[:10],
        'first_row': rows[0],
        'score': score,
    }


def infer_json_detail(data) -> dict | None:
    """在详情接口响应里定位公告对象与正文长文本字段。"""
    from .probe import _find_content

    def _row_of(path: str):
        if not path or path == '$':
            return data if isinstance(data, dict) else {}
        try:
            import jsonpath_ng
            hits = jsonpath_ng.parse(path).find(data)
            return hits[0].value if hits and isinstance(hits[0].value, dict) else {}
        except Exception:
            return {}

    c = _find_content(data)
    if c:
        row = _row_of(c['row_path'])
        return {
            'row_path': c['row_path'], 'content_path': c['content_path'],
            'content_len': c['value_len'], 'content_encoding': c['encoding'],
            'keys': flatten_keys(row) if row else [],
            'row_sample': sample_rows([row])[0] if row else {},
        }
    row = data if isinstance(data, dict) else {}
    return {'row_path': '$', 'content_path': '', 'content_len': 0,
            'content_encoding': '', 'keys': flatten_keys(row),
            'row_sample': sample_rows([row])[0] if row else {}}


# ------------------------------------------------------------------ 字段值域校验
# 字段映射只靠「名字」会翻车：areaName 看着像标题（含 Name）其实是地区，
# createTime 看着像发布时间却整列为 null。样本就是真相——按值的分布复核一遍。
_CN_RE = re.compile(r'[\u4e00-\u9fa5]')
_DATE_RE = re.compile(r'\d{4}\s*[-/年.]\s*\d{1,2}(?:\s*[-/月.]\s*\d{1,2})?')
_TITLE_NAME_GOOD = re.compile(
    r'(title|hdln|headline|subject|标题|公告名|项目名|noticeName|bulletin|gonggao)',
    re.I)
_TIME_NAME = re.compile(r'(time|date|dt|dttm|datetime|日期|时间)', re.I)

# 驼峰/下划线切词后再判断：areaName 结尾是 Name，但词元里含 area → 不是标题。
# 只按结尾正则匹配会漏掉 areaName / orgName / buyerName 这一大片。
_TITLE_BAD_WORDS = {
    'area', 'region', 'province', 'city', 'district', 'county', 'org', 'company',
    'corp', 'buyer', 'supplier', 'purchas', 'user', 'agency', 'agent', 'dept',
    'address', 'link', 'url', 'href', 'email', 'phone', 'tel', 'contact',
    'person', 'status', 'state', 'type', 'source', 'bank', 'code', 'remark', 'memo',
}


def _key_words(key) -> list[str]:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(key))
    return [w.lower() for w in re.split(r'[^A-Za-z0-9]+', s) if w]


def title_name_bad(key) -> bool:
    if _TITLE_NAME_GOOD.search(str(key)):
        return False
    return any(w in _TITLE_BAD_WORDS for w in _key_words(key))

# 标准字段 → 样本里「非空率」低于这个值就认为映射错了，直接不要
_MIN_NONEMPTY = 0.5


def _col_stats(rows: list[dict], key: str, limit: int = 10) -> dict:
    rs = [r for r in (rows or [])[:limit] if isinstance(r, dict)]
    vals = [str(r.get(key)) for r in rs
            if isinstance(r.get(key), str) and str(r.get(key)).strip()]
    n = max(1, len(rs))
    lens = sorted(len(v) for v in vals)
    return {
        'nonempty': round(len(vals) / n, 2),
        'uniq': round(len(set(vals)) / max(1, len(vals)), 2),
        'med': lens[len(lens) // 2] if lens else 0,
        'cn': round(sum(1 for v in vals if _CN_RE.search(v)) / max(1, len(vals)), 2),
        'date': round(sum(1 for v in vals if _DATE_RE.search(v)) / max(1, len(vals)), 2),
    }


def _title_score(key: str, st: dict) -> float:
    if st['nonempty'] < 0.6:
        return -999
    s = 0.0
    if 8 <= st['med'] <= 120:
        s += 20
    elif st['med'] < 6:
        s -= 40
    if st['med'] > 200:
        s -= 40
    s += 25 * st['cn'] + 20 * st['uniq']
    if st['date'] > 0.6:          # 整列都是日期 → 是时间不是标题
        s -= 50
    if _TITLE_NAME_GOOD.search(key):
        s += 40
    if title_name_bad(key):
        s -= 60
    return s


def _time_score(key: str, st: dict) -> float:
    if st['nonempty'] < 0.6 or st['date'] < 0.7:
        return -999
    s = 60 * st['date']
    if _TIME_NAME.search(key):
        s += 20
    if 'create' in key.lower() or 'modify' in key.lower():
        s -= 10            # createTime 常为 null，真实发布时间多在业务字段上
    return s


def refine_field_map(lm: dict, keys: list[str], rows: list[dict]) -> dict:
    """按样本值复核字段映射，返回修正后的映射（不改原对象）。

    只处理「能靠值判断」的字段：title / publish_at / region / buyer /
    source_name / budget。判断不出更好的就保持原样，交给校验去报错。
    """
    out = dict(lm or {})
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows or not keys:
        return out
    plain = [str(k) for k in keys if '.' not in str(k)]
    if not plain:
        return out

    def _pick(best_of: list[tuple[float, str]], threshold: float) -> str:
        best_of = [x for x in best_of if x[0] >= threshold]
        if not best_of:
            return ''
        best_of.sort(key=lambda x: (-x[0], plain.index(x[1])))
        return best_of[0][1]

    # ---- title
    others = {str(v).lower() for k, v in out.items()
              if k != 'title' and isinstance(v, str)}
    cands = [(_title_score(k, _col_stats(rows, k)), k)
             for k in plain if k.lower() not in others]
    best = _pick(cands, 30)
    cur = out.get('title') if isinstance(out.get('title'), str) else ''
    cur_score = -999
    if cur in plain and cur.lower() not in others:
        cur_score = _title_score(cur, _col_stats(rows, cur))
    if best and best != cur and cur_score < 30:
        out['title'] = best

    # ---- publish_at：整列能解析成日期的才算
    if plain:
        tcands = [(_time_score(k, _col_stats(rows, k)), k) for k in plain]
        tbest = _pick(tcands, 30)
        cur = out.get('publish_at') if isinstance(out.get('publish_at'), str) else ''
        cur_ok = bool(cur) and cur in plain and \
            _time_score(cur, _col_stats(rows, cur)) >= 0
        if not cur_ok:
            if tbest:
                out['publish_at'] = tbest
            else:
                out.pop('publish_at', None)

    # ---- 其余：值为空就说明映射错了，宁可不写
    for std in ('region', 'buyer', 'source_name', 'budget'):
        k = out.get(std)
        if not isinstance(k, str) or not k:
            continue
        if k not in plain or _col_stats(rows, k)['nonempty'] < _MIN_NONEMPTY:
            out.pop(std, None)

    # ---- external_id 必须真实存在
    k = out.get('external_id')
    if isinstance(k, str) and k and k not in plain:
        out.pop('external_id', None)
    return out


# ------------------------------------------------------------------ 参数角色
_KW_NAME = re.compile(
    r'(keyword|key_word|keyWords|searchWord|searchText|searchKey|searchValue|'
    r'goSearch|searchVal|searchName|messageTitle|noticeTitle|noticeName|title|'
    r'briefContent|noticeContent|searchContent|projectName|name|q|wd|key)$', re.I)
# shardingKey / shardingType 是分片路由键，不是搜索词：塞关键词进去服务端会
# 直接忽略，导致「配方看着对、采回来却是全站数据」。签名/流水号/令牌同理。
_KW_BAD = re.compile(
    r'(page|size|limit|offset|id|type|state|status|column|sort|shard|sharding|'
    r'serNo|serialNo|globalSerNo|txnSerNo|requestTime|timestamp|token|sign|signature|'
    r'nonce|trace|cookie|session|channel|entity|branch|code|version|ver|flag|level)$',
    re.I)
_PAGE_NAME = re.compile(
    r'^(page|pageNo|pageNum|pageIndex|pageCurrent|current|curPage|cur_page|'
    r'page_no|pn|currentPage|pageIndexNo)$', re.I)
_SIZE_NAME = re.compile(
    r'^(pageSize|size|rows|limit|page_size|perPage|per_page|numPerPage|'
    r'pageNumSize|rowCount|pageCount)$', re.I)


def _kw_slots(body, params, keyword: str) -> list[dict]:
    """列出所有可能是「关键词」的槽位，按可信度降序。

    两个来源都收：值正好等于用户关键词的（最硬），以及参数名像搜索词的。
    单靠参数名会踩坑——银行类接口爱用缩写（ancmHdlnCntnt=公告标题内容），
    名字里根本没有 keyword/title；反过来 shardingKey 名字里有 Key 却是分片键。
    """
    kw = (keyword or '').strip()
    slots = []
    for container, obj in (('body', body), ('params', params)):
        if not isinstance(obj, dict):
            continue
        for path, val in flat_pairs(obj):
            if not isinstance(val, str):
                continue
            leaf = path.split('.')[-1]
            slots.append({'container': container, 'path': path, 'leaf': leaf,
                          'old': val})
    if not slots:
        return []

    def score(s: dict, by: str) -> int:
        v = 100 if by == 'value' else 0
        if _KW_NAME.search(s['leaf']):
            v += 60
        if _KW_BAD.search(s['leaf']):
            v -= 40 if by != 'value' else 0
        if not s['old'].strip():
            v += 5
        return v

    out = []
    if kw:
        for s in slots:
            if s['old'].strip() == kw:
                s = dict(s, by='value', score=score(s, 'value'))
                out.append(s)
    for s in slots:
        if _KW_NAME.search(s['leaf']) and not _KW_BAD.search(s['leaf']) \
                and (not kw or len(s['old']) <= 60):
            if any(o['container'] == s['container'] and o['path'] == s['path']
                   for o in out):
                continue
            out.append(dict(s, by='name', score=score(s, 'name')))
    out.sort(key=lambda x: -x['score'])
    return out


def find_keyword_slot(body, params, keyword: str) -> dict | None:
    """定位关键词参数：先看谁的值正好等于用户给的关键词，再看参数名像不像。"""
    got = _kw_slots(body, params, keyword)
    if not got:
        return None
    best = got[0]
    return {'container': best['container'], 'path': best['path'],
            'old': best['old'], 'by': best['by']}


def find_keyword_candidates(body, params, keyword: str, limit: int = 4,
                            row_keys: list[str] | None = None) -> list[dict]:
    """给在线甄别用的候选，按可信度排序。

    除了「值命中」「名字命中」，还收一类弱候选：请求体里的字段名和列表结果行
    的字段名同名（如 ancmHdlnCntnt，公告标题内容）。这类缩写命名靠名字永远
    猜不出来，但「同一个名字既在查询体里又在结果里」几乎必然是过滤条件。
    """
    got = _kw_slots(body, params, keyword)
    row_set = {str(k).lower() for k in (row_keys or []) if '.' not in str(k)}
    weak = []
    for container, obj in (('body', body), ('params', params)):
        if not isinstance(obj, dict):
            continue
        for path, val in flat_pairs(obj):
            if not isinstance(val, str) or len(val) > 80:
                continue
            leaf = path.split('.')[-1]
            if leaf.lower() not in row_set:
                continue
            # 只收「已经填了值」的：空值字段（orgType/noticeType）本质上没在过滤，
            # 拿它们当候选只会白白多发几次请求
            if not val.strip() or _KW_BAD.search(leaf):
                continue
            if any(g['container'] == container and g['path'] == path for g in got):
                continue
            weak.append({'container': container, 'path': path, 'leaf': leaf,
                         'old': val, 'by': 'sample',
                         'score': 90 - (40 if _KW_BAD.search(leaf) else 0)})
    got = got + weak
    got.sort(key=lambda x: -x['score'])
    out, seen = [], set()
    for s in got:
        if len(s['old']) > 80:
            continue
        if s['leaf'].lower() in seen:
            continue
        seen.add(s['leaf'].lower())
        out.append(s)
        if len(out) >= limit:
            break
    return out


def find_page_slot(body, params) -> dict:
    """定位页码 / 页大小参数，返回 {page_param, size_param, page, size}。"""
    out = {'page_param': '', 'size_param': '', 'page': 1, 'size': None}
    pairs = []
    for container, obj in (('body', body), ('params', params)):
        if isinstance(obj, dict):
            pairs.extend((container, p, v) for p, v in flat_pairs(obj))
    for container, path, val in pairs:
        leaf = path.split('.')[-1]
        if _SIZE_NAME.search(leaf) and not out['size_param']:
            try:
                out['size_param'] = path
                out['size'] = int(str(val))
            except (TypeError, ValueError):
                out['size_param'] = ''
            continue
    for container, path, val in pairs:
        leaf = path.split('.')[-1]
        if _PAGE_NAME.search(leaf) and not _SIZE_NAME.search(leaf):
            try:
                out['page_param'] = path
                out['page'] = int(str(val) or 1)
            except (TypeError, ValueError):
                out['page_param'] = ''
            break
    return out


def infer_detail_templates(req: dict, row: dict | None, list_url_field: str = '',
                          rows: list | None = None) -> dict:
    """从详情 curl 里把「样本 id 的字面值」换成占位符，得到请求模板。

    优先用列表行里真实存在的字段名（如 {messageId}）——runner 的详情 ctx
    会把原始行字段一并放进去，因此模板能直接用原始字段名。

    三条命中路径，可靠性从高到低：
    1. **键名对齐**：详情请求体的键在列表行里同名（`{"messageId": "..."}`）→
       直接换成 `{messageId}`，不管值是否等于某条样本（用户复制的常常是另一条公告）；
    2. **值命中**：把详情 curl 里出现的某个列表字段值换成 `{字段名}`；
    3. 兜底 `{id}`：URL/字符串体里的 ?id=xxx 之类。
    """
    row = row or {}
    row_list = [r for r in (rows or []) if isinstance(r, dict)] or [row]
    url = req.get('url') or ''
    body = req.get('body')
    if isinstance(body, dict):
        body = json.loads(json.dumps(body, ensure_ascii=False))
    params = dict(req.get('params') or {})
    used = ''

    def _flat_dict(obj) -> list[tuple[str, object]]:
        return flat_pairs(obj) if isinstance(obj, dict) else []

    def _skip(leaf: str) -> bool:
        return bool(_PAGE_NAME.search(leaf) or _SIZE_NAME.search(leaf))

    # ---- 1) 键名对齐（不依赖值）
    if isinstance(body, dict):
        for path, val in _flat_dict(body):
            leaf = path.split('.')[-1]
            if leaf not in row or _skip(leaf):
                continue
            if not isinstance(val, str) and not re.search(r'(id|no|code|guid)', leaf, re.I):
                continue
            set_path(body, path, '{%s}' % leaf)
            used = leaf
    if not used:
        for p, v in list(params.items()):
            if p in row and not _skip(p):
                params[p] = '{%s}' % p
                used = p

    # ---- 2) 值命中（在所有样本行里找，避免「详情 curl 是另一条公告」时匹配不到）
    if not used:
        cands = []
        for r in row_list:
            for k, v in r.items():
                if not isinstance(v, (str, int, float)):
                    continue
                sv = str(v)
                if len(sv) < 2 or len(sv) > 80:
                    continue
                is_id_key = bool(re.search(r'(id|no|code|guid)', str(k), re.I))
                # 太短的值容易误伤（如状态位 "1"）：只有 id 类字段才允许 2~3 字符
                if len(sv) < 4 and not is_id_key:
                    continue
                cands.append((len(sv) + (20 if is_id_key else 0), str(k), sv))
        cands.sort(reverse=True)
        for _s, k, sv in cands:
            ph = '{%s}' % k
            hit = False
            if sv in url:
                url = url.replace(sv, ph)
                hit = True
            if isinstance(body, str) and sv in body:
                body = body.replace(sv, ph)
                hit = True
            for path, val in _flat_dict(body):
                if str(val) == sv:
                    set_path(body, path, ph)
                    hit = True
            for p, v in list(params.items()):
                if str(v) == sv:
                    params[p] = ph
                    hit = True
            if hit:
                used = k
                break

    # ---- 3) 兜底 {id}
    if not used:
        for sv in _id_candidates(url):
            url = url.replace(sv, '{id}')
            used = 'id'
            break
        if not used and isinstance(body, str):
            for sv in _id_candidates(body):
                body = body.replace(sv, '{id}')
                used = 'id'
                break
        if not used and isinstance(body, dict):
            # 字典体：把「键名像 id 且值是长字符串」的那一项换成 {id}
            for path, val in _flat_dict(body):
                leaf = path.split('.')[-1]
                if re.search(r'(id|no|code|guid)$', leaf, re.I) and \
                        isinstance(val, str) and len(val) >= 4:
                    set_path(body, path, '{id}')
                    used = 'id'
                    break

    return {'url': url, 'body': body, 'params': params, 'id_placeholder': used}


def _id_candidates(text: str) -> list[str]:
    """从 URL/文本里挑出像「主键」的字面量。"""
    out = []
    for m in re.finditer(r'[?&](?:id|ids|fileId|noticeId|messageId|articleId|infoId|'
                         r'contentId|projectId|bidId)=([A-Za-z0-9_-]{4,64})', text, re.I):
        out.append(m.group(1))
    if not out:
        for m in re.finditer(r'/([0-9a-fA-F-]{8,64})(?:[/.?]|$)', text):
            out.append(m.group(1))
    return out


# ------------------------------------------------------------------ HTML 结构
_STRIP_RE = re.compile(
    r'<(script|style|noscript|template)\b[^>]*>.*?</\1>|<!--.*?-->', re.S | re.I)
_GOOD_CLS = re.compile(
    r'(list|news|item|row|result|data|gg|article|content|notice|info|record)', re.I)
_TITLE_CLS = re.compile(r'(title|tit\b|name|subject|biaoti|bt\b)', re.I)
_DETAIL_PATH = re.compile(
    r'(detail|content|info|notice|article|show|view|gg|zbgg|jyxx|\d{4,})', re.I)
_DATE_RE = re.compile(r'(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})')
# 日期被拆成多个标签（<div>2026</div><div>11/11</div>）时的宽松写法
_DATE_SPLIT_RE = re.compile(r'(20\d{2})[^\d]{1,40}?(\d{1,2})\s*[-/.月]\s*(\d{1,2})')
_BAD_CLS = re.compile(
    r'(nav|menu|footer|header|crumb|sidebar|side|search|login|toolbar|share|comment|'
    r'pager|paging|page-?nav|breadcrumb|topbar|copyright|qrcode)', re.I)


def _clean(html: str) -> str:
    return _STRIP_RE.sub(' ', html or '')


def _text_of(el) -> str:
    try:
        txt = el.text_content()
    except Exception:
        txt = ''
    return re.sub(r'\s+', ' ', txt or '').strip()


def _text_loose(el) -> str:
    """按文本节点拼，节点之间补空格：<div>2026</div><div>11/01</div> → 「2026 11/01」。

    lxml 的 text_content 会拼成 202611/01，跨标签的日期就再也匹配不到了。
    """
    try:
        parts = [t.strip() for t in el.itertext() if t and t.strip()]
    except Exception:
        parts = []
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def _depth(el) -> int:
    d = 0
    p = el.getparent()
    while p is not None:
        d += 1
        p = p.getparent()
    return d


def _classes(el) -> list[str]:
    return [c for c in (el.get('class') or '').split() if c]


# CSS 标识符只能这样写；class/id 里出现 `sm:flex`、`w-1/2`、`[&>*]` 这类
# 工具类（Tailwind/UnoCSS 很常见）直接拼进选择器会让 lxml 抛 Invalid selector。
_CSS_IDENT = re.compile(r'^[A-Za-z_][\w-]*$')


def _css_ident(s: str) -> str:
    """能安全写进 CSS 选择器的标识符，否则返回空串。"""
    s = str(s or '').strip()
    return s if _CSS_IDENT.match(s) else ''


def _css_classes(el) -> list[str]:
    return [c for c in _classes(el) if _CSS_IDENT.match(c)]


def _path_pattern(href: str) -> str:
    path = urlsplit(href or '').path
    segs = [x for x in path.split('/') if x]
    return '/'.join('*' if re.fullmatch(r'\d+|[0-9a-fA-F-]{8,}', x) else x
                    for x in segs[:4])


def _selector_ok(sel, cand: str, nodes: list, *, relaxed: bool = False) -> bool:
    try:
        got = sel.css(cand)
    except Exception:
        return False
    if not relaxed:
        if len(got) != len(nodes):
            return False
        return all(a.root is b for a, b in zip(got, nodes))
    # 宽松模式：选中的集合包含全部目标节点，且没有多出太多（容忍 1~2 条隐藏/额外行）
    if len(got) < len(nodes) or len(got) > len(nodes) + 2:
        return False
    roots = [a.root for a in got]
    return all(any(n is r for r in roots) for n in nodes)


def selector_for_nodes(sel, nodes: list) -> str:
    """为同一组节点反推一个「能重新精确选中它们」的 CSS 选择器。"""
    if not nodes:
        return ''
    node = nodes[0]
    tag = node.tag if isinstance(node.tag, str) else ''
    cls = _css_classes(node)
    parent = node.getparent()
    cands: list[str] = []
    nid = _css_ident(node.get('id') or '')
    if nid and not re.search(r'\d{4,}', nid):
        cands.append('%s#%s' % (tag, nid))
    if cls:
        cands.append(tag + ''.join('.%s' % c for c in cls[:3]))
        cands.append('%s.%s' % (tag, cls[0]))
        cands.append('%s[class*="%s"]' % (tag, cls[0]))
    if parent is not None and isinstance(parent.tag, str):
        pcls = _css_classes(parent)
        if pcls:
            cands.append('%s.%s > %s' % (parent.tag, pcls[0], tag))
            if cls:
                cands.append('%s.%s > %s.%s' % (parent.tag, pcls[0], tag, cls[0]))
        cands.append('%s > %s' % (parent.tag, tag))
        # 同一页常有多份结构完全相同的无 class 容器（如导航 ul、菜单 ul、公告 ul），
        # `ul > li` 会同时选中它们。用「父级在兄弟里的位次」锚定。
        _pp = parent.getparent()
        psibs = [x for x in (list(_pp) if _pp is not None else [])
                 if isinstance(x.tag, str) and x.tag == parent.tag]
        if len(psibs) > 1:
            pk = psibs.index(parent) + 1
            cands.append('%s:nth-of-type(%d) > %s' % (parent.tag, pk, tag))
            if cls:
                cands.append('%s:nth-of-type(%d) > %s.%s'
                             % (parent.tag, pk, tag, cls[0]))
        # 再往上找最近的带 class 的祖先：`div.List2 > ul > li` 这类锚点最稳
        anc = parent
        for _ in range(3):
            anc = anc.getparent() if anc is not None else None
            if anc is None or not isinstance(anc.tag, str):
                break
            acls = _css_classes(anc)
            if not acls:
                continue
            for take in (acls[:2], acls[:1]):
                cands.append('%s%s > %s%s' % (
                    anc.tag, ''.join('.%s' % c for c in take), tag,
                    ('.' + cls[0]) if cls else ''))
                if len(acls) > 1:
                    cands.append('%s%s > %s > %s' % (
                        anc.tag, ''.join('.%s' % c for c in take),
                        parent.tag, tag))
            break
    cands.append(tag)
    for cand in cands:
        if _selector_ok(sel, cand, nodes):
            return cand
    # 严格匹配失败时放宽一位：页面里常有一两条隐藏/广告行让节点数对不上，
    # 为此整个列表都放弃不值得。
    for cand in cands:
        if _selector_ok(sel, cand, nodes, relaxed=True):
            return cand
    # 兜底：用结构路径（父级若干层 + nth-of-type）
    parts = []
    cur = node
    for _ in range(3):
        if cur is None or not isinstance(cur.tag, str):
            break
        c = cur.tag
        ccls = _css_classes(cur)
        if ccls:
            c += '.' + ccls[0]
        _cp = cur.getparent()
        siblings = [x for x in (list(_cp) if _cp is not None else [])
                    if isinstance(x.tag, str) and x.tag == cur.tag]
        if len(siblings) > 1:
            c += ':nth-of-type(%d)' % (siblings.index(cur) + 1)
        parts.insert(0, c)
        cur = cur.getparent()
    cand = ' > '.join(parts)
    # 全部候选都没回测通过就如实返回空串：给一个"选不中"的选择器比没有更糟，
    # 下游会拿它去抽取，报出来的错误还指错方向。
    return cand if (cand and _selector_ok(sel, cand, nodes)) else ''


def _from_sel(row, spec: dict) -> str:
    from .recipe import resolve_field
    try:
        return resolve_field(spec, {}, selector=row, text=row.get() or '')
    except Exception:
        return ''


def _pick_spec(rows: list, cands: list[dict], valid, min_cov: float = 0.6) -> dict:
    """在候选规则里挑覆盖率最高的一条（必须过 min_cov）。"""
    best = None
    for spec in cands:
        vals = [_from_sel(r, spec) for r in rows]
        ok = sum(1 for v in vals if valid(v))
        cov = ok / max(1, len(rows))
        if cov < min_cov:
            continue
        avg = sum(len(v) for v in vals) / max(1, len(vals))
        score = cov * 100 + min(avg, 60)
        if best is None or score > best[0]:
            best = (score, cov, spec)
    return best[2] if best else {}


def _title_cands(row) -> list[dict]:
    cands: list[dict] = []
    try:
        for el in row.root.iter():
            if not isinstance(el.tag, str) or el.tag in ('html', 'body', 'script', 'style'):
                continue
            cls = ' '.join(_classes(el))
            if el.tag in ('h1', 'h2', 'h3', 'h4') or (cls and _TITLE_CLS.search(cls)):
                if el.tag == 'a':
                    cands.append({'css': 'a::text', 'strip': True})
                if _classes(el):
                    cands.append({'css': '%s.%s::text' % (el.tag, _classes(el)[0]),
                                  'strip': True})
                else:
                    cands.append({'css': '%s::text' % el.tag, 'strip': True})
    except Exception:
        pass
    for css in ('a::text', 'a[href]::text', '.title::text', '.tit::text', 'span::text',
                'p::text', 'div::text'):
        cands.append({'css': css, 'strip': True})
    # 标题往往是行内第 2/3 个链接（第 1 个是分类标签），逐个小角标都试一遍
    for i in range(1, 4):
        cands.append({'css': 'a::text', 'nth': i, 'strip': True})
    # 去重
    seen, out = set(), []
    for c in cands:
        key = json.dumps(c, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _url_cands(row) -> list[dict]:
    cands: list[dict] = []
    try:
        anchors = row.css('a[href]')
    except Exception:
        anchors = []
    for i in range(min(5, len(anchors))):
        cands.append({'css': 'a[href]', 'attr': 'href', 'nth': i})
    cands.append({'css': 'a', 'attr': 'href'})
    return cands


_DETAIL_LIKE_RE = re.compile(
    r'(detail|content|info|notice|article|show|zbgg|zbxx|jhtml|\.html?|\.jspx?|\d{3,})', re.I)


def _detail_like(href: str) -> bool:
    """像「一条公告」的链接：路径里有数字/详情词/页面后缀，而不是首页或栏目页。"""
    p = urlsplit(href or '').path
    if not p or p in ('/', ''):
        return False
    if re.search(r'/(index|list|search)(\.\w+)?$', p, re.I):
        return False
    return bool(_DETAIL_LIKE_RE.search(p))


def _pick_url_spec(rows: list) -> dict:
    """挑「每条都不一样、且像详情页」的链接，而不是盲取第一个 <a>。

    列表行里常有分类标签、收藏、栏目导航等链接：取错了列表能入库、详情全抓不到。
    """
    n = max(1, len(rows))
    best = None
    for spec in _url_cands(rows[0]):
        vals = [_from_sel(r, spec) for r in rows]
        ok = [v for v in vals if _valid_url(v)]
        if len(ok) / n < 0.6:
            continue
        distinct = len({v for v in ok}) / max(1, len(ok))
        detailish = sum(1 for v in ok if _detail_like(v)) / max(1, len(ok))
        score = (len(ok) / n) * 100 + distinct * 70 + detailish * 60
        if best is None or score > best[0]:
            best = (score, spec)
    return best[1] if best else {}


def _valid_title(v: str) -> bool:
    s = (v or '').strip()
    if not (6 <= len(s) <= 240):
        return False
    if re.fullmatch(r'[\d\s:/.\-年月日]+', s):
        return False
    return bool(re.search(r'[\u4e00-\u9fa5A-Za-z]', s))


def _valid_url(v: str) -> bool:
    s = (v or '').strip()
    if len(s) < 4 or s.startswith(('javascript:', '#', 'mailto:')):
        return False
    return bool(re.match(r'^(https?://|//|/|\.\.?/)', s)) or s.endswith(('.html', '.htm'))


def infer_html_list(html: str, base_url: str = '',
                    *, diag: dict | None = None) -> dict | None:
    """从列表页 HTML 反推 row_selector 与行内字段规则（含回测）。

    diag 传入一个 dict 时会填诊断信息（候选组数、行数、选择器、失败原因），
    供上层把「为什么没认出来」写进报错里。
    """
    from parsel import Selector

    sel = Selector(text=_clean(html))
    root = sel.root

    groups: list[tuple[float, list]] = []
    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        children = [c for c in parent if isinstance(c.tag, str)]
        if len(children) < 3:
            continue
        buckets: dict[tuple, list] = {}
        for c in children:
            key = (c.tag, tuple(sorted(_classes(c))))
            buckets.setdefault(key, []).append(c)
        for key, nodes in buckets.items():
            if len(nodes) < 3:
                continue
            good, hrefs = [], []
            for n in nodes:
                # 行元素本身就是 <a>（<ul><a class="item"> 这种写法很常见）时，
                # `.//a` 只找后代、找不到自己，直接取自身 href。
                href = ''
                if n.tag == 'a':
                    h = (n.get('href') or '').strip()
                    if h and not h.startswith(('javascript:', '#')):
                        href = h
                if not href:
                    for a in n.xpath('.//a[@href]'):
                        h = (a.get('href') or '').strip()
                        if h and not h.startswith(('javascript:', '#')):
                            href = h
                            break
                if not href:
                    continue
                txt = _text_of(n)
                if not (4 <= len(txt) <= 600):
                    continue
                good.append(n)
                hrefs.append(href)
            if len(good) < 3 or len(good) < len(nodes) * 0.6:
                continue
            score = min(len(good), 20) * 3.0
            cls = ' '.join(key[1])
            if cls and _GOOD_CLS.search(cls):
                score += 6
            if cls and _BAD_CLS.search(cls):
                score -= 8
            pats = [_path_pattern(h) for h in hrefs]
            top = max(set(pats), key=pats.count) if pats else ''
            if top and pats.count(top) >= len(pats) * 0.6 and \
                    (re.search(r'\d', top) or len(top) >= 6):
                score += 8
            date_ratio = sum(1 for n in good
                             if _DATE_SPLIT_RE.search(_text_loose(n))) / len(good)
            if date_ratio >= 0.5:
                score += 5
            # 链接全指向别的域名（省分公司/站点切换器、合作站点导航）：
            # 这明显不是本页的公告列表，重罚，避免把导航当成列表行。
            base_host = urlsplit(base_url).netloc.lower() if base_url else ''
            if base_host and hrefs:
                cross = sum(1 for h in hrefs
                            if h.startswith(('http://', 'https://'))
                            and urlsplit(h).netloc.lower() not in ('', base_host))
                if cross >= len(hrefs) * 0.8:
                    score -= 15
            if key[0] in ('li', 'tr', 'dd'):
                score += 3
            if score > 0:
                groups.append((score, good))

    if diag is not None:
        diag['groups'] = len(groups)
    if not groups:
        if diag is not None:
            diag['reason'] = ('没找到 3 条以上「结构相同、都带链接、文本长度合理」的兄弟节点；'
                              '可能样本只贴了一两条结果，或贴的是渲染后的片段')
        return None
    groups.sort(key=lambda x: x[0], reverse=True)
    _score, nodes = groups[0]
    row_selector = selector_for_nodes(sel, nodes)
    rows: list = []
    if row_selector:
        try:
            picked = sel.css(row_selector)
            if len(picked) >= 3:
                rows = list(picked)
        except Exception:
            # 选择器非法（类名带冒号/斜杠等）不能让异常冒到接口层
            rows = []
    if not rows:
        # 兜底：把候选节点包成 Selector，保证下游统一的 Selector 接口
        # （lxml 元素没有 .get()，直接混用会抛 TypeError → 500）
        try:
            from parsel import Selector as _Sel
            rows = [_Sel(root=n) for n in nodes]
            if not row_selector:
                row_selector = selector_for_nodes(sel, nodes)
        except Exception:
            return None
    if diag is not None:
        diag.update(rows=len(rows), selector=row_selector,
                    group_rows=len(nodes),
                    group_score=round(float(_score or 0), 1))
    if len(rows) < 3 or not row_selector:
        if diag is not None:
            diag['reason'] = ('找到 %d 行候选（最大一组 %d 行），但反推不出能稳定命中的 '
                              'CSS 选择器' % (len(rows), len(nodes)))
        return None

    row0 = rows[0]
    row_html0 = row0.get() or ''
    fields: dict = {}

    title_spec = _pick_spec(rows, _title_cands(row0), _valid_title, 0.6)
    if not title_spec:
        return None
    fields['title'] = title_spec

    # url：优先挑「同一条公告」的链接（每条不同、路径像详情页）
    url_spec = _pick_url_spec(rows)
    if url_spec:
        fields['url'] = url_spec
    else:
        fields['url'] = {'css': 'a', 'attr': 'href'}

    # 宽松选择器可能多选一两条（隐藏行、广告位）：把没有有效链接的剔掉，
    # 避免把空行当公告入库。
    if len(rows) > len(nodes):
        kept = [r for r in rows if _valid_url(_from_sel(r, fields['url']))]
        if len(kept) >= 3:
            rows = kept

    # 详情页 URL 路径模式 → 详情基地址（相对链接由 normalize 用 meta.base_url 补齐）
    href0 = _from_sel(row0, fields['url'])
    if href0 and not href0.startswith(('http://', 'https://', '//')):
        pass
    elif href0:
        base_url = '%s://%s' % urlsplit(href0)[:2]

    # 日期：整行正则，命中率 >= 60% 才写（含「年/月/日分处不同标签」的写法）
    def _date_spec(rows_):
        best = None
        for rx in (_DATE_RE, _DATE_SPLIT_RE):
            hits = 0
            for r in rows_:
                m = rx.search(_text_loose(r.root))
                if not m:
                    continue
                try:
                    mo, d = int(m.group(2)), int(m.group(3))
                except (TypeError, ValueError):
                    continue
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    hits += 1
            cov = hits / max(1, len(rows_))
            if cov >= 0.6 and (best is None or cov > best[0]):
                best = (cov, rx)
        if not best:
            return None
        return {'regex': best[1].pattern, 'template': '{1}-{2}-{3} 00:00',
                'type': 'datetime'}

    date_spec = _date_spec(rows)
    if date_spec:
        fields['publish_at'] = date_spec

    # 编号 / 类型：行内的短文本 span / li 子项
    short_cands = [{'css': 'li span::text', 'nth': i, 'strip': True} for i in range(3)]
    short_cands += [{'css': 'span::text', 'nth': i, 'strip': True} for i in range(2)]
    for key, valid in (
            ('bid_no', lambda v: bool(re.match(r'^[A-Za-z0-9_/（）()\-]{6,40}$', v or ''))),
            ('notice_type', lambda v: 2 <= len(v or '') <= 20
             and bool(re.search(r'(招标|磋商|询价|谈判|单一来源|竞价|采购|比选)', v or '')))):
        spec = _pick_spec(rows, short_cands, valid, 0.6)
        if spec:
            fields[key] = spec

    for key, pat in (('buyer', r'(buyer|purchas|tenderee|zhbr|caigou)'),
                     ('region', r'(area|region|province|city|diqu)')):
        cands = [{'css': '%s[class*="%s"]::text' % (tag, word), 'strip': True}
                 for word in (pat.split('|')[0],) for tag in ('span', 'div', 'td')]
        spec = _pick_spec(rows, cands, lambda v: 2 <= len(v or '') <= 60, 0.7)
        if spec:
            fields[key] = spec

    ds = []
    for r in rows[:3]:
        item = {k: _from_sel(r, v)[:120] for k, v in fields.items()}
        ds.append(item)
    return {
        'row_selector': row_selector, 'fields': fields, 'rows': len(rows),
        'sample': ds, 'first_url': _from_sel(rows[0], fields['url']),
        'base_url': base_url,
        'date_ratio': round(sum(1 for r in rows if _DATE_SPLIT_RE.search(_text_loose(r.root)))
                            / max(1, len(rows)), 2),
    }


def infer_html_detail(html: str) -> dict | None:
    """从详情页 HTML 反推正文容器（content_css）与标题选择器。"""
    from parsel import Selector

    cleaned = _clean(html)
    sel = Selector(text=cleaned)
    best = None
    for el in sel.root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag not in ('div', 'article', 'section', 'main', 'td', 'dd', 'p', 'pre'):
            continue
        cls = ' '.join(_classes(el)) + ' ' + (el.get('id') or '')
        if cls.strip() and _BAD_CLS.search(cls):
            continue
        txt = _text_of(el)
        n = len(txt)
        if n < 200:
            continue
        score = min(n, 20000)
        if re.search(r'(content|article|detail|main|txt|zoom|con\b|infor|notice|'
                     r'text|nr\b)', cls, re.I):
            score += 6000
        links = len(el.xpath('.//a'))
        score -= links * 120
        score -= _depth(el) * 12
        if best is None or score > best[0]:
            best = (score, n, el)

    if not best:
        return None
    _score, n, node = best
    cands: list[str] = []
    tag = node.tag
    cls = _classes(node)
    nid = node.get('id') or ''
    if nid and not re.search(r'\d{4,}', nid):
        cands.append('#%s' % nid)
    if cls:
        cands.append(tag + ''.join('.%s' % c for c in cls[:3]))
        cands.append('%s.%s' % (tag, cls[0]))
    parent = node.getparent()
    if parent is not None and isinstance(parent.tag, str) and _classes(parent):
        cands.append('%s.%s > %s' % (parent.tag, _classes(parent)[0], tag))
    css = ''
    for cand in cands:
        try:
            got = sel.css(cand)
        except Exception:
            continue
        if not got:
            continue
        first = _text_of(got[0].root)
        if abs(len(first) - n) <= max(50, n * 0.2):
            css = cand
            break
    if not css:
        return None

    title_css = ''
    for cand in ('h1::text', '.title::text', '.article-title::text', 'h2::text'):
        vals = [v.strip() for v in sel.css(cand).getall() if v and v.strip()]
        # <title> 里带站点后缀（「xx-中国邮政」），不能当公告标题用
        if vals and 4 <= len(vals[0]) <= 200 and not re.search(r'[|｜]\s*\S+$', vals[0]):
            title_css = cand
            break
    return {'content_css': css, 'content_len': n, 'title_css': title_css,
            'preview': _text_of(node)[:200]}


# ------------------------------------------------------------------ 站点标识
def host_of(req: dict) -> str:
    return req.get('host') or urlsplit(req.get('url') or '').netloc


def origin_of(req: dict) -> str:
    return req.get('origin') or '%s://%s' % urlsplit(req.get('url') or '')[:2]


def abs_url(base: str, href: str) -> str:
    if not href:
        return ''
    return urljoin(base, href)


_TPL_RE = re.compile(r'\{[A-Za-z_]\w*\}')
_PAGE_PARAM_RE = re.compile(
    r'([?&])(?:currentPage|pageNo|pageNum|pageIndex|pageCurrent|curPage|page|'
    r'pageSize|page_size)=[^&#]*', re.I)


def _strip_page_params(s: str) -> str:
    """去掉示例地址里残留的页码参数（&currentPage=3）：那是当时翻到的页，不是公告属性。"""
    out = _PAGE_PARAM_RE.sub('', s)
    out = re.sub(r'[?&]+(?=#|$)', '', out)
    out = out.replace('?&', '?').replace('#&', '#')
    return out


def page_url_template(example: str, row: dict | None) -> str:
    """把「某一条公告的详情页地址」变成可套用的 URL 模板。

    用户通常直接把浏览器地址栏里的那一条公告地址粘进来（含真实 id）。
    若原样当模板用，所有条目的 url 都会指向这一条公告，采集时被 URL 去重成 1 条；
    详情请求也会永远抓同一条。因此必须先用列表行里的真实字段值把它参数化：
        https://x/#/noticeDetail?messageId=M10001  →  ...?messageId={messageId}
    已经是 `{xxx}` 写法的模板则原样返回。
    """
    s = (example or '').strip()
    if not s:
        return ''
    if _TPL_RE.search(s):
        return _strip_page_params(s)
    row = row or {}
    t = infer_detail_templates({'url': s, 'params': {}, 'body': None}, row)
    if t.get('id_placeholder') and t['url'] != s:
        return _strip_page_params(t['url'])
    # 列表行里没有直接对应的字段：只对 query 里的 id 参数做通用替换
    #（不含路径，避免把 /2025/11/11/ 这种日期段当成 id）
    m = re.search(r'([?&](?:id|ids|\w*Id|\w*_id)=)([^&#]+)', s, re.I)
    if m and len(m.group(2)) >= 2:
        return _strip_page_params(s[:m.start(2)] + '{id}' + s[m.end(2):])
    return ''


def infer_path_pagination(html: str, list_url: str) -> dict | None:
    """识别「页码在路径里」的分页（如 search.jspx → search_2.jspx）。

    这类站点（中招联合 365trade 等）没有 page 查询参数，页码藏在文件名里，
    query 里只有关键词。只认「下一页/尾页」链接里唯一的数字段，且必须能还原出
    第 1 页地址，否则不动。
    """
    from parsel import Selector

    try:
        sel = Selector(text=_clean(html))
    except Exception:
        return None
    lp = urlsplit(list_url)
    nxt = ''
    for a in sel.css('a[href]'):
        href = (a.attrib.get('href') or '').strip()
        if not href or href.startswith(('javascript:', '#')):
            continue
        txt = ' '.join(x.strip() for x in a.xpath('.//text()').getall()).strip()
        if re.search(r'(下一页|下页|next|›|»)', txt, re.I):
            nxt = href
            break
    if not nxt:
        return None
    full = urljoin(list_url, nxt)
    fp = urlsplit(full)
    m = re.search(r'^(.*?)(\d+)([^\d]*)$', fp.path)
    if not m:
        return None
    prefix, num, suffix = m.groups()
    if not suffix or not (2 <= int(num) <= 500):
        return None
    # 第 1 页一般是「去掉数字」的路径；prefix 可能多带一个分隔符（search_ → search）
    for sep in ('_', '-', '', '/', '.'):
        base_prefix = prefix[:-len(sep)] if sep and prefix.endswith(sep) else prefix
        if (base_prefix + suffix).rstrip('/') == (lp.path or '').rstrip('/'):
            tpl_path = prefix + '{page}' + suffix
            tpl = urlunsplit((lp.scheme, lp.netloc, tpl_path, lp.query, ''))
            return {'type': 'page', 'page_in_url': True, 'url_template': tpl,
                    'first_url': list_url, 'start': 1,
                    'total_text': _total_text(sel), 'next_href': nxt}
    return None


def _total_text(sel) -> str:
    """列表页上的「共 N 条记录」文案，仅用于展示。"""
    for t in sel.css('body ::text').getall():
        m = re.search(r'共\s*([\d,]+)\s*条', t or '')
        if m:
            return m.group(0).strip()
    return ''


def _href_template_shape(tpl: str, info: dict) -> str:
    """`https://host{_href}` 遇到绝对链接会拼成 `https://hosthttps://…`（DNS 直接失败）。

    按样本里 href 的实际形态选择最合适的模板：
    - 多数是绝对地址 → 直接用 `{_href}`，不再前置域名；
    - 多数是协议相对 `//host/path` → 用 `https:{_href}`；
    - 其余（站内相对路径）→ 保持用户写的 `origin{_href}`。
    """
    if '{_href}' not in tpl:
        return tpl
    hrefs = [str((r or {}).get('url') or '').strip()
             for r in (info.get('sample') or [])]
    hrefs = [h for h in hrefs if h]
    if not hrefs:
        return tpl
    n = len(hrefs)
    absolute = sum(1 for h in hrefs if h.startswith(('http://', 'https://'))) / n
    proto = sum(1 for h in hrefs if h.startswith('//')) / n
    if absolute >= 0.6:
        return '{_href}'
    if proto >= 0.6:
        m = re.match(r'^(https?)://', tpl)
        return '%s:{_href}' % (m.group(1) if m else 'https')
    return tpl


def html_url_template(text: str, list_info: dict | None) -> str:
    """HTML 列表模式：把用户填的「详情页 URL 模板/示例」变成行内模板。

    行上下文里能用的占位符是 `{_href}`（行内第一个可用链接）与各字段名；
    用户习惯写 `{href}`，这里统一换成 `{_href}`。也接受一条真实详情页地址
    （如 `https://host/zgczb/897997.jhtml`），用行内 href 反推前缀。
    """
    s = (text or '').strip()
    if not s:
        return ''
    if '{href}' in s or '{_href}' in s:
        tpl = _strip_page_params(s.replace('{href}', '{_href}'))
        return _href_template_shape(tpl, list_info or {})
    if _TPL_RE.search(s):
        return _strip_page_params(s)
    info = list_info or {}
    hrefs = []
    first = str(info.get('first_url') or '')
    if first:
        hrefs.append(first)
    for row in (info.get('sample') or []):
        v = str((row or {}).get('url') or '')
        if v:
            hrefs.append(v)
    for href in hrefs:
        hp = urlsplit(href).path or href
        if not hp.startswith('/'):
            continue
        if s.rstrip('/').endswith(hp.rstrip('/')):
            return _href_template_shape(
                _strip_page_params(s[:len(s) - len(hp)] + '{_href}'), info)
        # 示例多半是「另一条公告」：目录相同即认为是同一个详情页路由
        d = hp.rsplit('/', 1)[0]
        sp = urlsplit(s)
        if d and len(d) > 1 and (sp.path or '').startswith(d + '/'):
            return _href_template_shape(
                _strip_page_params('%s://%s{_href}' % (sp.scheme, sp.netloc)), info)
    return ''


def match_url_prefix(row_url: str, detail_url: str) -> str:
    """列表里的相对链接常需要前缀才能打开详情页。

    例：列表给 `/1ywgg3/20250711/1128265293198524416.html`，而详情 curl 的
    path 是 `/cms/default/webfile/1ywgg3/.../xxx.html` → 前缀 `/cms/default/webfile`。
    这种情况下「直接拼域名」会拿到 200 但内容不对的页面，必须补前缀。
    """
    if not row_url or not detail_url.startswith('http'):
        return ''
    rp = row_url.strip()
    for q in ('?', '#'):
        if q in rp:
            rp = rp.split(q)[0]
    if not rp.startswith('/'):
        rp = '/' + rp
    dp = urlsplit(detail_url).path or '/'
    if dp == rp or not dp.endswith(rp):
        return ''
    prefix = dp[:-len(rp)]
    return '' if prefix in ('', '/') else prefix
