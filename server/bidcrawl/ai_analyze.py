# -*- coding: utf-8 -*-
"""AI 配方生成：输入站点 URL，自动产出可直接试跑的采集配方 JSON。

设计原则（与《标讯采集_平台自解析与配方复用方案》一致）：
- **结构确定性**：接口、参数、翻页、详情请求、正文路径、URL 模板全部由
  probe.py 实际探测取证，不交给 LLM 猜；
- **AI 只做语义**：LLM 负责把站点原始字段名（noticeTitle / userName / bidsNo …）
  映射到标准字段（title / buyer / bid_no …），以及给出站点中文名；
- **自动校验**：生成后用真实请求试跑一次，统计字段非空率、验证详情正文可取，
  失败时换下一个接口候选重试；
- **无 Key 可降级**：没有 LLM API Key 时全部走启发式字段映射，仍产出完整配方。

detail_ai.py 复用本模块的 _llm_available / _chat / _loads，请保持函数签名兼容。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import jsonpath_ng

from . import recipe as recipe_mod
from .probe import SiteProbe

# bidsummary 已在 server/ai_worker/.env 配好 DASHSCOPE_API_KEY；
# bidcrawl 运行在 FastAPI 进程，需显式加载同一份 .env。
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / 'ai_worker' / '.env'
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except Exception:
    pass

DEFAULT_KEYWORD = '招标'

# ---------------------------------------------------------------- 字段字典
LIST_FIELD_MAP = {
    'title': ('noticeTitle', 'bulletinTitle', 'bidTitle', 'projectTitle', 'title',
              'messageTitle', 'titleName', 'subject', 'projectName', 'name',
              'gongGaoMingCheng'),
    'external_id': ('id', 'noticeId', 'articleId', 'bidId', 'bulletinId',
                    'messageId', 'infoId', 'projectId', 'uuid', 'guid', 'rowId'),
    'buyer': ('userName', 'purchaseName', 'purchaserName', 'buyerName', 'buyer',
              'purchaser', 'orgName', 'companyName', 'purchaseOrgName', 'tenderee'),
    'publish_at': ('publishTime', 'publishDate', 'releaseTime', 'noticeTime',
                   'createTime', 'pubTime', 'issueTime', 'addTime', 'startTime'),
    'region': ('areaName', 'area', 'regionName', 'region', 'provinceName',
               'cityName', 'areaProvince', 'district'),
    'source_name': ('noticeSource', 'sourceName', 'dataSource', 'source',
                    'infoSource', 'siteName'),
    'budget': ('budget', 'budgetAmount', 'projectBudget', 'budgetMoney'),
    'status': ('noticeState', 'state', 'status', 'bidStatus'),
    'url': ('detailUrl', 'url', 'link', 'href', 'linkUrl', 'contentUrl'),
}

DETAIL_FIELD_MAP = {
    'bid_no': ('bidsNo', 'bidNo', 'projectNo', 'tenderNo', 'bidCode',
               'projectCode', 'noticeNo'),
    'notice_type': ('purchaseTypeName', 'noticeTypeName', 'purchaseTypeLable',
                    'bidType', 'purchaseMethod', 'tenderType'),
    'agency': ('createUserName', 'agencyName', 'agency', 'agentName', 'orgName'),
    'category': ('labelAllId', 'categoryName', 'category', 'yxCategoryNames',
                 'categoryLabName'),
    'contact': ('contact', 'contactName', 'linkMan', 'linkman'),
}

# 与 detail_ai.ALL_FIELDS 保持一致；正文语义字段全部交给 AI 抽取
AI_FIELDS = [
    'budget', 'fee_note', 'scope', 'doc_get_start', 'doc_get_end', 'open_at',
    'deadline', 'region', 'buyer', 'bid_no', 'agency', 'contact', 'qualification',
    'project_period', 'bidder_limit', 'project_name', 'funding',
    'bid_place', 'notice_type',
]

# 无模型 / 模型失败时的确定性兜底（通用中文招标表述，不含站点分支）
AI_FALLBACK_REGEX = {
    'budget': r'(?:合同估算价|项目预算|采购预算|预算金额|最高限价|预算)[：:\s]*([^\n，。；]{1,40})',
    'fee_note': r'(?:标书费|文件费|招标文件费|.*?费用)[：:]\s*([^\n]{1,60})',
    'doc_get_start': r'获取时间[：:]\s*从\s*([^\n到]{4,40}?)\s*到',
    'doc_get_end': r'获取时间[：:]\s*从\s*[^\n到]{4,40}?\s*到\s*([^\n]{4,40})',
    'open_at': r'(?:投标文件递交截止时间|开标时间|响应文件递交截止时间)[：:]\s*([^\n，。（]{4,45})',
    'deadline': r'(?:投标文件递交截止时间|递交截止时间|投标截止时间)[：:]\s*([^\n，。（]{4,45})',
    'bid_no': r'(?:招标编号|项目编号|采购编号)[：:]\s*([A-Za-z0-9\-_（）()]{4,40})',
    'agency': r'([\u4e00-\u9fa5]{4,30}(?:有限公司|有限责任公司))（招标代理机构）',
    'contact': r'联系人[：:]\s*([^\n，。]{1,40})',
    'project_period': r'(?:工期|服务期)[：:]\s*([^\n，。]{1,20})',
    'bidder_limit': r'中标人数量[：:]\s*([^\n，。名]{1,10})',
}


# ---------------------------------------------------------------- LLM 基础设施
def _llm_available() -> tuple[bool, str]:
    key = (os.environ.get('LLM_API_KEY') or os.environ.get('DASHSCOPE_API_KEY')
           or os.environ.get('DEEPSEEK_API_KEY') or '')
    if not key:
        return False, ('未配置 LLM API Key。探测器仍可自动生成配方（字段用启发式映射），'
                       '如需 AI 语义映射请在 server/ai_worker/.env 设置 DASHSCOPE_API_KEY。')
    return True, ''


def _chat(system: str, user: str, timeout_s: float = 120) -> str:
    from openai import OpenAI
    base = (os.environ.get('LLM_BASE_URL')
            or os.environ.get('DASHSCOPE_BASE_URL')
            or 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    key = (os.environ.get('LLM_API_KEY') or os.environ.get('DASHSCOPE_API_KEY')
           or os.environ.get('DEEPSEEK_API_KEY'))
    model = os.environ.get('LLM_TEXT_MODEL') or 'qwen-plus'
    client = OpenAI(api_key=key, base_url=base.rstrip('/'), timeout=timeout_s, max_retries=0)
    resp = client.chat.completions.create(
        model=model,
        messages=[{'role': 'system', 'content': system},
                  {'role': 'user', 'content': user}],
        temperature=0.1,
    )
    return resp.choices[0].message.content or ''


def _strip_fence(text: str) -> str:
    text = (text or '').strip()
    if text.startswith('```'):
        parts = text.split('```')
        text = parts[1] if len(parts) > 1 else text
        text = re.sub(r'^\s*json\s*', '', text, flags=re.I).strip()
    return text


def _loads(text: str) -> dict:
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find('{'), text.rfind('}')
        if i >= 0 and j > i:
            return json.loads(text[i: j + 1])
        raise


def fetch_page(url: str, timeout_s: float = 20) -> dict:
    """抓取目标 URL（仅 HTML 列表兜底路径使用）。"""
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
        'Accept': 'text/html,application/json,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_s,
                         follow_redirects=True, trust_env=False)
        return {'ok': True, 'url': str(resp.url), 'status': resp.status_code,
                'content_type': resp.headers.get('content-type', ''),
                'text': (resp.text or '')[:200_000]}
    except Exception as e:
        return {'ok': False, 'url': url, 'status': 0, 'content_type': '',
                'text': '', 'error': str(e)[:300]}


# ---------------------------------------------------------------- 字段映射
# 语义护栏：字段名必须「长得像」这个标准字段，防止模型把 userName
# 同时塞给 agency / contact 这类明显串味的映射。
SEMANTIC_GUARD = {
    'title': r'(title|subject|标题|name)',
    'external_id': r'(id|uuid|guid|code|编号)',
    'buyer': r'(buyer|purchas|user|org|company|tenderee|采购人)',
    'publish_at': r'(time|date|publish|create|release|日期|时间)',
    'region': r'(area|region|province|city|district|地区)',
    'budget': r'(budget|amount|money|price|金额|价)',
    'bid_no': r'(no|code|num|number|编号)',
    'notice_type': r'(type|method|lable|label|方式|类型)',
    'agency': r'(agency|agent|create|org|代理|机构)',
    'category': r'(category|label|品类|类目|分类)',
    'contact': r'(contact|link|phone|tel|person|联系人)',
}


_TITLE_GOOD = re.compile(r'(title|hdln|headline|subject|标题)', re.I)
_TITLE_BAD_WORDS = {
    'area', 'region', 'province', 'city', 'district', 'county', 'org', 'company',
    'corp', 'buyer', 'supplier', 'purchas', 'user', 'agency', 'agent', 'dept',
    'address', 'link', 'url', 'href', 'email', 'phone', 'tel', 'contact',
    'person', 'status', 'state', 'type', 'source', 'bank', 'code', 'remark', 'memo',
}


def _title_name_bad(key) -> bool:
    """驼峰切词后判断：areaName 词元里有 area，不是标题。"""
    if _TITLE_GOOD.search(str(key)):
        return False
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(key))
    return any(w.lower() in _TITLE_BAD_WORDS
               for w in re.split(r'[^A-Za-z0-9]+', s) if w)


def _guard_ok(std: str, key: str) -> bool:
    if std == 'title' and _title_name_bad(key):
        # areaName / orgName / buyerName 结尾都是 Name，护栏只写 name 会全部放行
        return False
    pat = SEMANTIC_GUARD.get(std)
    return (not pat) or bool(re.search(pat, str(key), re.I))


def _pick(keys: list[str], cands: tuple) -> tuple[str, bool]:
    """在真实字段名里找最匹配的候选：先精确（忽略大小写），再子串。

    返回 (字段名, 是否精确匹配)。精确匹配可信度最高，不允许 LLM 覆盖。
    """
    low = {str(k).lower(): str(k) for k in keys}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()], True
    for k in keys:
        kl = str(k).lower()
        for c in cands:
            if len(c) >= 4 and c.lower() in kl:
                return str(k), False
    return '', False


def _heuristic_map(keys: list[str], table: dict) -> tuple[dict, set]:
    out, strong = {}, set()
    for std, cands in table.items():
        k, exact = _pick(keys, cands)
        if not k:
            continue
        if exact or _guard_ok(std, k):
            out[std] = k
            if exact:
                strong.add(std)
    return out, strong


SYS_MAP = (
    '你是招投标数据采集的字段映射助手。任务是：把某站点接口返回的「原始字段名」'
    '映射到本系统的标准字段名。\n'
    '铁律：\n'
    '1. 只能从给定的【候选字段名】里挑选，绝不编造不存在的字段。\n'
    '2. 只输出一个 JSON 对象，格式 {"name":"站点中文名","list":{...},"detail":{...}}，'
    '不含解释文字、不以 ``` 开头。\n'
    '3. 无法对应的标准字段直接省略，不要填 null 或空字符串。\n'
    '\n标准字段含义：\n'
    'list.title=公告标题；list.external_id=公告唯一ID；list.buyer=采购人/招标人；'
    'list.publish_at=发布时间；list.region=地区；list.source_name=信息来源；'
    'list.budget=预算金额；list.status=公告状态；list.url=详情链接。\n'
    'detail.bid_no=招标编号；detail.notice_type=采购方式；detail.agency=招标代理机构；'
    'detail.category=品类/标签；detail.contact=联系人。\n'
    'name 为站点中文简称（如「金采网」）；无法判断时省略该键。'
)


def _llm_map(ev: dict) -> tuple[dict, dict, str]:
    """返回 (list 映射, detail 映射, 站点中文名)。失败返回空 dict。"""
    ok, _ = _llm_available()
    if not ok:
        return {}, {}, ''
    lst = ev.get('list') or {}
    det = ev.get('detail') or {}
    sample = (lst.get('rows_sample') or [])[:2]
    payload = {
        'entry_title': (ev.get('entry') or {}).get('title', ''),
        'list_keys': lst.get('keys') or [],
        'list_sample': sample,
        'detail_keys': det.get('row_keys') or [],
    }
    user = ('站点入口：%s\n候选字段名与样本值（JSON）：\n%s\n\n'
            '请输出字段映射 JSON。' %
            (ev.get('entry_url') or '',
             json.dumps(payload, ensure_ascii=False)[:9000]))
    try:
        data = _loads(_chat(SYS_MAP, user, timeout_s=60))
        if not isinstance(data, dict):
            return {}, {}, ''
        lm = data.get('list') if isinstance(data.get('list'), dict) else {}
        dm = data.get('detail') if isinstance(data.get('detail'), dict) else {}
        return lm, dm, str(data.get('name') or '')[:30]
    except Exception:
        return {}, {}, ''


def _merge_map(heu: dict, strong: set, llm: dict, keys: list[str]) -> dict:
    """启发式精确匹配为最高优先级；LLM 只用于补充空缺 / 修正模糊匹配。"""
    out = dict(heu)
    valid = {str(k).lower(): str(k) for k in keys}
    allowed = set(LIST_FIELD_MAP) | set(DETAIL_FIELD_MAP)
    used = {str(v).lower() for v in out.values()}
    for std, key in (llm or {}).items():
        if std not in allowed or std in strong or not isinstance(key, str):
            continue
        real = valid.get(key.lower())
        if not real or not _guard_ok(std, real):
            continue
        # 同一个原始字段不能同时映射给两个标准字段。占用判断必须在「覆盖」
        # 之前：否则 LLM 把 region 的 areaName 再塞给 title 时，两个字段会同时
        # 指向地区列，title 整列为空且没人发现。
        if real.lower() in used and str(out.get(std, '')).lower() != real.lower():
            continue
        out[std] = real
        used.add(real.lower())
    return out


# ---------------------------------------------------------------- 配方构建
def _referer_of(url: str) -> str:
    """入口 URL → 可用作 Referer 的前端地址（去掉 hash 与查询）。

    SPA 的入口常是 /puc/#/biddingPage：hash 部分不会发给服务端，
    但不少网关会校验 Referer 的完整字符串，带 # 会被判「来源非法」。
    """
    p = urlparse(url)
    path = p.path or '/'
    if not path.endswith('/'):
        path = path.rsplit('/', 1)[0] + '/'
    return '%s://%s%s' % (p.scheme, p.netloc, path)


def _platform_id(host: str) -> str:
    labels = [x for x in str(host).split('.') if x]
    if labels and labels[0].lower() in ('www', 'm', 'wap'):
        labels = labels[1:]
    base = labels[0] if labels else 'site'
    # cg / ec / portal 之类通用子域没有辨识度，拼上第二段（cg.11185.cn → cg_11185）
    if len(labels) >= 2 and base.lower() in (
            'cg', 'ec', 'ebid', 'portal', 'mall', 'buy', 'zhaobiao', 'sso', 'e') \
            and re.fullmatch(r'[a-z0-9_-]+', labels[1], re.I):
        base = base + '_' + labels[1]
    base = re.sub(r'[^a-z0-9_]', '', base.lower())
    return base or 'site'


def _site_name(ev: dict, llm_name: str, host: str) -> str:
    if llm_name and re.search(r'[\u4e00-\u9fa5]', llm_name):
        return llm_name
    title = str((ev.get('entry') or {}).get('title') or '')
    title = re.split(r'[-_|｜，,·]', title)[0].strip()
    if title and re.search(r'[\u4e00-\u9fa5]', title) and len(title) <= 20:
        return title
    return host


def _apply_state_defaults(payload: dict, state: dict) -> dict:
    """把 JS 查询状态里的字面量默认值补回请求参数。

    例：list.js 里 `searchMap.searchNoticeType = "1"`，而 `param.noticeType = type`
    是变量，探测时为空；补成 "1" 才能对齐站点默认栏目，避免抓成全站公告。
    """
    for k, v in list(payload.items()):
        if v not in ('', None) or str(v).startswith('{'):
            continue
        kl = k.lower()
        for sk, sv in (state or {}).items():
            sl = str(sk).lower()
            if sl == kl or sl == 'search' + kl or sl.endswith(kl):
                payload[k] = str(sv)
                break
    return payload


def _legacy_templates(ev: dict) -> list[str]:
    out = []
    for line in (ev.get('js_signals') or {}).get('url_lines') or []:
        for m in re.finditer(r'["\']([^"\']*noticeDetail\?id=[^"\']*)["\']', line):
            t = m.group(1)
            if t.startswith('/'):
                t = '%s%s' % (_origin(ev['entry_url']), t)
            out.append(t.replace('{id}', '{id}') if '{id}' in t else t)
    return list(dict.fromkeys(out))[:3]


def _origin(url: str) -> str:
    u = urlparse(url)
    return '%s://%s' % (u.scheme, u.netloc)


def build_recipe(ev: dict, url: str, keyword: str, *, with_llm: bool = True) -> tuple[dict, str]:
    """由证据包生成完整配方，返回 (recipe, 映射来源)。"""
    lst = ev['list']
    det = ev.get('detail')
    tpl = ev.get('detail_url_template') or ''
    host = urlparse(url).netloc
    pid = _platform_id(host)
    keys = lst.get('keys') or []
    det_keys = (det or {}).get('row_keys') or []

    llm_list, llm_detail, llm_name = ({}, {}, '')
    source = 'heuristic'
    if with_llm:
        llm_list, llm_detail, llm_name = _llm_map(ev)
        source = 'llm' if (llm_list or llm_detail or llm_name) else 'llm_empty'
    heu_l, strong_l = _heuristic_map(keys, LIST_FIELD_MAP)
    heu_d, strong_d = _heuristic_map(det_keys, DETAIL_FIELD_MAP)
    lm = _merge_map(heu_l, strong_l, llm_list, keys)
    dm = _merge_map(heu_d, strong_d, llm_detail, det_keys)

    id_key = lm.get('external_id') or ''
    id_placeholder = '{%s}' % (id_key or 'id')
    url_tpl = tpl.replace('{id}', id_placeholder) if tpl else ''

    payload = _apply_state_defaults(dict(lst['payload']),
                                    (ev.get('js_signals') or {}).get('state_defaults'))
    roles = lst.get('roles') or {}
    pag = {
        'type': 'page' if roles.get('page') else 'none',
        'page_param': roles.get('page') or '',
        'start': 1,
        'size_param': roles.get('size') or '',
        'size': 50,
        'size_probe': None,
        'total_path': lst.get('total_path') or '',
        'max_pages': 20,
        'stop_when': ['empty_items', 'page_unchanged', 'reached_total'],
    }

    fields: dict = {}
    if lm.get('title'):
        fields['title'] = {'path': lm['title'], 'strip': True}
    else:
        # 保底：至少要有一个 title 规则，取第一个像标题的字段
        guess = _pick(keys, LIST_FIELD_MAP['title'])[0] or (keys[0] if keys else 'title')
        fields['title'] = {'path': guess, 'strip': True}
    if id_key:
        fields['external_id'] = {'path': id_key}
    if url_tpl:
        fields['url'] = {'template': url_tpl,
                         'fallback': [{'path': 'url'}, {'path': 'link'},
                                      {'path': 'detailUrl'}]}
    elif lm.get('url'):
        fields['url'] = {'path': lm['url']}
    else:
        fields['url'] = {'template': '%s/detail?%s=%s' % (_origin(url), id_key or 'id',
                                                           id_placeholder),
                         'fallback': [{'path': 'url'}, {'path': 'link'}]}
    # 注意：不自动映射 status。站点里的 noticeState 多为 "1" 这类内部编码，
    # 直接写进标准 status 会被 normalize 当成业务状态，含义不对。
    for std in ('buyer', 'publish_at', 'region', 'source_name', 'budget'):
        if lm.get(std):
            spec = {'path': lm[std], 'strip': True}
            if std == 'publish_at':
                spec['type'] = 'datetime'
            fields[std] = spec

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
                        if lst.get('payload_kind') != 'json'
                        else 'application/json;charset=UTF-8',
        'Accept': '*/*',
        'X-Requested-With': 'XMLHttpRequest',
        # 入口若是 hash 路由（/#/biddingPage），Referer 不能带 hash：
        # 网关会按完整 URL 校验来源，带 # 一律 0011 拒绝。取去掉 hash 的目录。
        'Referer': _referer_of(lst.get('referer') or url),
        'Origin': _origin(url),
    }
    # 入口直探 / 浏览器嗅探记录过真实可用的 header（如农行 gateway 要求
    # Referer=/puc/、Accept 不能含 text/javascript,q=0.01 否则被 WAF 当 XSS），
    # 直接沿用，避免「探测能取数、试跑/生产 0 条」。
    if lst.get('payload_style') in ('entry_api', 'browser') and lst.get('headers'):
        real = {k: v for k, v in lst['headers'].items()
                if k.lower() in ('content-type', 'accept', 'referer', 'origin',
                                 'x-requested-with', 'cookie')}
        headers.update(real)
    # Referer 一律规范化（去掉 hash），覆盖候选里的原始值
    headers['Referer'] = _referer_of(headers.get('Referer') or url)
    transport = {
        'kind': 'http', 'method': lst.get('method') or 'POST', 'url': lst['url'],
        'headers': headers, 'encoding': 'utf-8',
        # 探测与试跑都用直连（trust_env=False）；若环境里配了 HTTP_PROXY，
        # Fetcher 默认 trust_env=True 会改走代理，站点常返回 403，导致
        # 「试跑通过但实际采集失败」。生成配方必须与探测条件一致。
        'trust_env': False,
        'timeout_s': 25, 'total_timeout_s': 30, 'max_response_mb': 5,
        'rate_limit': {'qps': 0.1, 'concurrency': 1},
        'retry': {'times': 2, 'backoff_s': 5,
                  'retry_on': [429, 500, 502, 503, 504]},
    }

    ai_detail_cfg = {
        'enabled': True, 'prompt_version': 'auto-detail-v1',
        'max_chars': 20000, 'timeout_s': 90, 'cache': True,
        'fields': list(AI_FIELDS),
        'fallback_regex': dict(AI_FALLBACK_REGEX),
    }
    detail: dict = {'enabled': False}
    if det and det.get('kind') == 'html':
        # 详情页本身就是正文（CMS 静态页）：GET 列表里的 url，正文在 content_css
        detail = {
            'enabled': True,
            'request': {'url': '{url}', 'method': 'GET'},
            'content_css': det.get('content_css') or '',
            'content_encoding': 'html',
            'extract': {'scope': 'html', 'fields': {}},
            'ai': ai_detail_cfg,
            'attachments': {'enabled': False},
        }
    elif det:
        dfields = {}
        for std, key in dm.items():
            dfields[std] = {'path': key}
        if url_tpl:
            dfields['detail_url'] = {
                'template': tpl.replace('{id}', '{external_id}')}
        detail = {
            'enabled': True,
            'id_field': 'external_id' if id_key else 'id',
            'request': {
                'url': det['url'], 'method': det.get('method') or 'POST',
                'headers': {
                    'Referer': tpl.replace('{id}', '{id}') if tpl else url,
                    'X-Requested-With': 'XMLHttpRequest', 'Accept': '*/*',
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
                                    if det.get('payload_kind') != 'json'
                                    else 'application/json;charset=UTF-8',
                },
                'body': det['body'],
            },
            'row_path': det['row_path'],
            'content_path': det['content_path'],
            'content_encoding': det['content_encoding'],
            'extract': {'scope': 'json', 'fields': dfields},
            'ai': ai_detail_cfg,
            'attachments': {'enabled': False},
        }

    expected = {}
    for std in ('buyer', 'publish_at', 'region', 'source_name'):
        if std in fields:
            expected[std] = 0.6 if std == 'buyer' else 0.8
    validation = {
        'min_items_per_keyword': 1,
        'page2_must_differ': bool(lst.get('page2_differs')),
        'required_fields': {'title': 1.0, 'url': 1.0},
        'expected_nonempty': expected,
        'url_reachable_rate': 0.9,
        'sample_keyword': keyword,
    }
    if det:
        validation['detail_expected_nonempty'] = {
            'budget': 0.5, 'fee_note': 0.5, 'scope': 0.5,
            'doc_get_end': 0.5, 'open_at': 0.5,
        }

    legacy = _legacy_templates(ev)
    normalize = {
        'filters': {'inherit': 'global', 'override': {},
                    'buyer_whitelist': False},
        'amount': {'target': 'budget', 'units': {'万元': 10000, '元': 1}},
        'keep_url': bool(url_tpl),
    }
    if legacy:
        normalize['legacy_url_templates'] = legacy

    notes = ('由站点探测器自动生成：入口页 JS → 列表接口 %s（%s，关键词参数 %s）'
             '→ 详情正文 %s.%s → 详情页 URL 模板 %s。'
             '新配方默认关闭全局采购人白名单，避免未知站点被业务规则误杀，请审核后再启用。'
             % (lst['endpoint_path'], lst['items_path'], lst['keyword_key'] or '-',
                (det or {}).get('row_path', '-'), (det or {}).get('content_path', '-'),
                '已还原' if url_tpl else '未识别'))

    recipe = {
        'meta': {
            'id': pid, 'name': _site_name(ev, llm_name, host), 'host': host,
            'entry_url': url, 'version': 1, 'status': 'paused',
            'created_by': 'ai', 'notes': notes,
        },
        'transport': transport,
        'discovery': {
            'mode': 'json_api',
            'request': {'url': lst['url'], 'method': lst.get('method') or 'POST',
                        'params': None, 'body': payload},
            'pagination': pag,
            'items_path': lst['items_path'],
            'total_path': lst.get('total_path') or '',
            'fields': fields,
        },
        'detail': detail,
        'normalize': normalize,
        'validation': validation,
    }
    return recipe, source


def build_html_recipe(ev: dict, url: str, keyword: str,
                      *, with_llm: bool = True) -> tuple[dict, str]:
    """由 HTML 探测证据组装 html_list 配方（含 PDF 正文型详情）。"""
    h = ev['html_list']
    sea = h['search']
    pdf = h.get('pdf') or {}
    host = urlparse(url).netloc
    pid = _platform_id(host)

    llm_name = ''
    source = 'heuristic'
    if with_llm:
        # HTML 站点的字段是 CSS/正则，已由探测确定；LLM 只用于站点中文名
        _l, _d, llm_name = _llm_map(ev)
        source = 'llm' if llm_name else 'heuristic'

    params = dict(sea['params'])
    if sea.get('page_param') and sea['page_param'] not in params:
        params[sea['page_param']] = '{page}'
    fields = dict(h['fields'])
    # 保底：title / url 必须有
    fields.setdefault('title', {'css': 'a::text', 'strip': True})
    fields.setdefault('url', {'css': 'a', 'attr': 'href'})

    total_pages = int(h.get('total_pages') or 10)
    pagination = {
        'type': 'page' if sea.get('page_param') else 'none',
        'page_param': sea.get('page_param') or '',
        'start': 1,
        'max_pages': max(1, min(total_pages, 20)),
        'stop_when': ['empty_items', 'page_unchanged'],
    }

    detail: dict = {'enabled': False}
    pdf_cfg = None
    if h.get('first_url'):
        pdf_cfg = {
            'enabled': bool(pdf.get('pdf_url')),
            'file_id_regex': pdf.get('file_id_regex') or '',
            'url_template': pdf.get('url_template') or '',
            'iframe_css': pdf.get('iframe_css') or '',
            'max_bytes': 8 * 1024 * 1024,
            'max_chars': 20000,
            'timeout_s': 30,
        }
        detail = {
            'enabled': True,
            'id_field': 'external_id' if 'external_id' in fields else 'url',
            'request': {'url': '{url}', 'method': 'GET',
                        'headers': {'Referer': sea['url']}},
            'extract': {
                'scope': 'html',
                'fields': {
                    'title': {'css': 'h1::text', 'strip': True,
                              'fallback': [{'css': 'h2::text', 'strip': True},
                                           {'css': 'h3::text', 'strip': True},
                                           {'css': '.topTitle::text', 'strip': True}]},
                },
            },
            'attachments': {'enabled': False},
        }
        if pdf_cfg['enabled']:
            detail['pdf'] = pdf_cfg
            detail['content_source'] = 'pdf'
        detail['ai'] = {
            'enabled': True, 'prompt_version': 'auto-detail-v1',
            'max_chars': 20000, 'timeout_s': 90, 'cache': True,
            'fields': list(AI_FIELDS),
            'fallback_regex': dict(AI_FALLBACK_REGEX),
        }

    expected = {k: 0.8 for k in ('buyer', 'publish_at', 'region', 'source_name')
                if k in fields}
    validation = {
        'min_items_per_keyword': 1,
        'page2_must_differ': bool(h.get('page2_differs')),
        'required_fields': {'title': 1.0, 'url': 1.0},
        'expected_nonempty': expected,
        'url_reachable_rate': 0.9,
        'sample_keyword': keyword,
    }
    if detail.get('ai', {}).get('enabled'):
        validation['detail_expected_nonempty'] = {
            'buyer': 0.5, 'scope': 0.5, 'doc_get_end': 0.5, 'open_at': 0.5}

    notes = ('由站点探测器自动生成：HTML 列表 %s（%s，关键词参数 %s，分页 %s，'
             '共 %s 页）→ 详情页 %s → %s。'
             '列表字段为探测出的 CSS/正则规则，正文来自 PDF，已接入 AI 结构化抽取。'
             '新配方默认关闭全局采购人白名单，请审核后再启用。'
             % (sea['url'], h['row_selector'], sea['keyword_param'],
                sea.get('page_param') or '-', h.get('total_pages') or '?',
                h.get('detail_path') or '-',
                'pdfjs PDF 直链' if pdf_cfg and pdf_cfg['enabled'] else 'HTML 正文'))

    recipe = {
        'meta': {
            'id': pid, 'name': _site_name(ev, llm_name, host), 'host': host,
            'entry_url': url, 'version': 1, 'status': 'paused',
            'created_by': 'ai', 'notes': notes,
        },
        'transport': {
            'kind': 'http', 'method': 'GET', 'url': sea['url'],
            'headers': {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                      'AppleWebKit/537.36 Chrome/120 Safari/537.36'),
                        'Accept-Language': 'zh-CN,zh;q=0.9',
                        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'},
            'encoding': 'utf-8',
            'trust_env': False,
            'timeout_s': 25, 'total_timeout_s': 30, 'max_response_mb': 5,
            'rate_limit': {'qps': 0.2, 'concurrency': 1},
            'retry': {'times': 2, 'backoff_s': 5,
                      'retry_on': [429, 500, 502, 503, 504]},
        },
        'discovery': {
            'mode': 'html_list',
            'request': {'url': sea['url'], 'method': 'GET',
                        'params': params, 'body': None},
            'pagination': pagination,
            'row_selector': h['row_selector'],
            'fields': fields,
        },
        'detail': detail,
        'normalize': {
            'filters': {'inherit': 'global', 'override': {},
                        'buyer_whitelist': False},
            'amount': {'target': 'budget', 'units': {'万元': 10000, '元': 1}},
            'keep_url': True,
        },
        'validation': validation,
    }
    return recipe, source


# ---------------------------------------------------------------- 试跑校验
def _render(v, ctx: dict):
    if isinstance(v, str):
        def sub(m):
            return str(ctx.get(m.group(1), ''))
        return re.sub(r'\{(\w+)\}', sub, v)
    if isinstance(v, dict):
        return {k: _render(x, ctx) for k, x in v.items()}
    return v


def _request(spec: dict, ctx: dict, timeout: float = 15):
    # Content-Type 通常声明在 transport.headers，spec 里往往没有；
    # 只查 spec 会把表单错发成 JSON，站点会忽略全部筛选条件。
    headers = _render(dict(spec.get('headers') or {}), ctx)
    ctype = next((str(v) for k, v in headers.items()
                  if k.lower() == 'content-type'), '')
    url = _render(spec.get('url') or '', ctx)
    method = str(spec.get('method') or 'POST').upper()
    body = _render(spec.get('body') or {}, ctx)
    params = _render(spec.get('params') or {}, ctx)
    kwargs = {'headers': headers, 'timeout': timeout,
              'follow_redirects': True, 'trust_env': False}
    if isinstance(body, dict) and body:
        if 'form-urlencoded' in ctype:
            kwargs['data'] = body
        else:
            kwargs['json'] = body
    elif body:
        kwargs['content'] = str(body).encode('utf-8')
    if params:
        kwargs['params'] = params
    r = httpx.request(method, url, **kwargs)
    return r


_NOTICE_WORDS = ('招标', '采购', '投标', '中标', '成交', '公告', '供应商',
                 '响应', '项目', '合同')


def _pdf_looks_like_notice(text: str, title: str = '') -> tuple[bool, str]:
    """PDF 正文是不是「这条公告」：标题核心词命中，或出现足够多的招标用语。

    门户页脚常有《平台注册规则.pdf》《供应商入驻指南.pdf》这类文档，内容和
    公告毫无关系，却是页面上唯一的 PDF；只校验"抽到 200 字"会把它当成功。
    """
    body = (text or '')[:6000]
    flat = re.sub(r'\s+', '', body)
    if len(flat) < 80:
        return False, '正文过短（%d 字）' % len(flat)
    core = re.sub(r'\s+', '', title or '')
    if len(core) >= 6 and core[:6] in flat:
        return True, ''
    hits = sum(1 for w in _NOTICE_WORDS if w in body)
    if hits >= 2:
        return True, ''
    return False, '未出现招标/采购/公告等用语（命中 %d 个）' % hits


class _TextResp:
    """浏览器取回的页面，按 httpx.Response 的最小接口提供给校验逻辑。"""

    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def json(self):
        return json.loads(self.text)


def _waf_rescue(spec: dict, ctx: dict, resp):
    """响应是反爬中间页时，用无头浏览器取回真实页面。

    返回 (resp, reason)：救回时 reason='browser'；失败时 reason 说明原因。
    """
    from .browser_probe import fetch_page, is_waf_page
    text = getattr(resp, 'text', '') or ''
    if not is_waf_page(text):
        return resp, ''
    if str(spec.get('method') or 'GET').upper() != 'GET':
        return resp, '非 GET 请求无法用浏览器过盾'
    url = _render(spec.get('url') or '', ctx)
    # 浏览器只能导航：params 必须拼进 URL，否则拿到的可能是没带关键词的落地页
    params = _render(spec.get('params') or {}, ctx)
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    referer = ''
    for k, v in (spec.get('headers') or {}).items():
        if str(k).lower() == 'referer':
            referer = _render(str(v), ctx)
            break
    got = fetch_page(url, referer=referer)
    if got.get('ok') and got.get('html'):
        return _TextResp(got['html']), 'browser'
    return resp, (got.get('reason') or '浏览器未能取到正文')


def verify_recipe(recipe: dict, keyword: str) -> dict:
    """真实发一次请求，验证列表能取到条目、字段非空率、详情正文可取。

    不写数据库、不调 LLM、不受配方 qps 限速影响。
    """
    t0 = time.monotonic()
    out: dict = {'valid': False, 'items': 0, 'total': None,
                 'field_coverage': {}, 'detail_ok': False, 'detail_len': 0,
                 'errors': []}
    ds = recipe['discovery']
    ctx = {'keyword': keyword, 'page': 1, 'size': 50, 'id': '', 'external_id': ''}
    # 第 1 页的请求可能与模板不同（页码在路径里的站点：search.jspx vs search_2.jspx），
    # 必须复用运行期的注入逻辑，否则试跑会去请求 search_1.jspx。
    from .paginate import _inject as _inject_page
    try:
        _first = _inject_page(dict(ds['request']),
                              int((ds.get('pagination') or {}).get('start') or 1),
                              None, recipe_mod.compile_recipe(recipe))
    except Exception:
        _first = dict(ds['request'])
    spec = dict(_first)
    merged = dict(recipe.get('transport', {}).get('headers') or {})
    merged.update(spec.get('headers') or {})
    spec['headers'] = merged
    mode = str(ds.get('mode') or 'json_api')
    from .extract.html_list import row_context as _row_ctx
    rows: list = []
    html_rows: list = []
    try:
        r = _request(spec, ctx)
        r, _waf = _waf_rescue(spec, ctx, r)
        if _waf:
            out['waf'] = _waf
        if r.status_code >= 400:
            out['errors'].append('列表请求 HTTP %d' % r.status_code)
            return out
        if mode == 'html_list':
            from parsel import Selector
            sel = Selector(text=r.text)
            html_rows = list(sel.css(str(ds.get('row_selector') or '')))[:8]
            rows = [{} for _ in html_rows]
        else:
            data = r.json()
            matches = jsonpath_ng.parse(ds['items_path']).find(data)
            rows = [m.value for m in matches if isinstance(m.value, dict)][:8]
            if isinstance(data, dict):
                total = next((data[k] for k in ('total', 'totalCount', 'count')
                              if isinstance(data.get(k), (int, float))), None)
                # 嵌套 total（如 $.value.total）：优先用探测器记录的 total_path
                if total is None and ds.get('total_path'):
                    tm = jsonpath_ng.parse(ds['total_path']).find(data)
                    if tm and isinstance(tm[0].value, (int, float)):
                        total = tm[0].value
                out['total'] = total
        out['items'] = len(rows)
    except Exception as e:
        out['errors'].append('列表请求失败：%s' % str(e)[:160])
        return out
    if not rows:
        if out.get('total') == 0:
            # 接口正常返回但总数为 0：说明关键词确实被采纳了，只是这个站点没有
            # 匹配的公告。配方本身没问题，不能判成「参数没生效」让人白改一遍。
            out['empty_by_keyword'] = True
            out.setdefault('warnings', []).append(
                '关键词「%s」在该站点没有检索结果（接口返回 0 条），配方本身可用；'
                '换成有数据的关键词再试跑即可' % keyword)
        else:
            out['errors'].append(
                '列表返回 0 条，检查关键词参数是否被服务端采纳、row_selector 是否匹配')
        out['valid'] = not out['errors']
        out['elapsed_ms'] = int((time.monotonic() - t0) * 1000)
        return out

    cov = {}
    for name, fspec in ds['fields'].items():
        if mode == 'html_list':
            hit = 0
            for hr in html_rows:
                ctx_h = _row_ctx(hr)
                if recipe_mod.resolve_field(fspec, ctx_h, text=ctx_h.get('_text') or '',
                                            selector=hr):
                    hit += 1
        else:
            hit = sum(1 for row in rows if recipe_mod.resolve_field(fspec, row))
        cov[name] = round(hit / len(rows), 2)
    out['field_coverage'] = cov
    if cov.get('title', 0) < 0.5:
        out['errors'].append('title 字段非空率 %.0f%%，字段映射可能有误' % (cov['title'] * 100))
    if cov.get('url', 0) < 0.5:
        out['errors'].append('url 字段非空率 %.0f%%，详情页 URL 模板可能拼错' % (cov['url'] * 100))

    detail = recipe.get('detail') or {}
    pdf_cfg = detail.get('pdf') or {}
    if detail.get('enabled') and (detail.get('content_path')
                                  or detail.get('content_css')
                                  or pdf_cfg.get('enabled')):
        url_spec = ds['fields'].get('url') or {}
        title_spec = ds['fields'].get('title') or {}
        if mode == 'html_list':
            ctx0 = _row_ctx(html_rows[0])
            first_url = recipe_mod.resolve_field(
                url_spec, ctx0, text=ctx0.get('_text') or '', selector=html_rows[0])
            first_title = recipe_mod.resolve_field(
                title_spec, ctx0, text=ctx0.get('_text') or '', selector=html_rows[0])
        else:
            first_url = recipe_mod.resolve_field(url_spec, rows[0])
            first_title = recipe_mod.resolve_field(title_spec, rows[0])
        # 列表里的 url 常是站内相对路径（/1ywgg3/...html），runner 会用
        # meta.base_url 补成绝对地址；试跑必须做同样处理，否则详情请求直接失败。
        if first_url and not first_url.startswith(('http://', 'https://')):
            base = str((recipe.get('meta') or {}).get('base_url') or '')
            if not base:
                u = urlparse(ds.get('request', {}).get('url')
                             or recipe.get('transport', {}).get('url') or '')
                base = '%s://%s' % (u.scheme, u.netloc)
            first_url = urljoin(base.rstrip('/') + '/', first_url.lstrip('/')) \
                if base else first_url
        id_spec = ds['fields'].get('external_id')
        did = ''
        for i, row in enumerate(rows):
            if mode == 'html_list':
                ctx_i = _row_ctx(html_rows[i])
                did = recipe_mod.resolve_field(id_spec or {}, ctx_i,
                                               text=ctx_i.get('_text') or '',
                                               selector=html_rows[i])
            else:
                did = recipe_mod.resolve_field(id_spec, row) if id_spec else ''
                if not did:
                    did = str(row.get('id') or row.get('noticeId') or '')
            if did:
                break
        dctx = dict(ctx, id=did, external_id=did, url=first_url)
        # 详情模板可能引用列表原始字段名（如 {messageId}）：runner 在 _detail_ctx
        # 里会把这些字段一并放进模板上下文，试跑必须保持一致，否则「试跑失败、
        # 实际采集成功」或反之。
        if mode != 'html_list' and isinstance(rows[0], dict):
            for k, v in rows[0].items():
                if isinstance(v, (str, int, float)) and k not in dctx:
                    dctx[k] = str(v)
        for k, v in (ds.get('fields') or {}).items():
            if isinstance(v, dict) and k not in dctx:
                got = (recipe_mod.resolve_field(v, rows[0]) if mode != 'html_list'
                       else '')
                if got:
                    dctx[k] = got
        try:
            dspec = dict(detail['request'])
            dh = dict(merged)
            dh.update(dspec.get('headers') or {})
            dspec['headers'] = dh
            dr = _request(dspec, dctx)
            dr, _waf = _waf_rescue(dspec, dctx, dr)
            if _waf:
                out['waf'] = _waf
            if dr.status_code >= 400:
                out['errors'].append('详情请求 HTTP %d' % dr.status_code)
            elif pdf_cfg.get('enabled'):
                from .pdf_text import pdf_url_from_html, download_and_extract
                purl = pdf_url_from_html(pdf_cfg, dr.text, first_url)
                if purl:
                    out['pdf_url'] = purl
                    text, pmeta = download_and_extract(purl, pdf_cfg)
                    out['pdf_meta'] = pmeta
                    out['detail_len'] = len(text)
                    out['detail_ok'] = len(text) >= 200
                    if out['detail_ok']:
                        good, why = _pdf_looks_like_notice(text, first_title)
                        if not good:
                            # PDF 打开了、也有字，但内容是《平台注册规则》这类
                            # 页脚文档——比"0 字符"更隐蔽，必须拦下来。
                            out['detail_ok'] = False
                            out['errors'].append(
                                'PDF 正文与公告不符（疑似页脚/帮助文档）：%s；%s'
                                % (purl[:100], why))
                        else:
                            out['pdf_backend'] = pmeta.get('backend') or ''
                    else:
                        reason = pmeta.get('reason') or '未知原因'
                        if text:
                            reason = '仅抽出 %d 字符（<200）：%s' % (len(text), reason)
                        out['errors'].append(
                            'PDF 正文为空或过短：%s → %s' % (purl[:100], reason))
                else:
                    out['errors'].append('详情页未解析出 PDF 直链')
            elif detail.get('content_css'):
                # HTML 详情页：正文在页面容器里（如 cg.psbc.com 的 .content）
                try:
                    from parsel import Selector
                    nodes = Selector(text=dr.text).css(str(detail['content_css']))
                    text = (max((' '.join(n.xpath('.//text()').getall())
                                for n in nodes[:5]), key=len) if nodes else '')
                except Exception:
                    text = ''
                out['detail_len'] = len(re.sub(r'\s+', ' ', text).strip())
                out['detail_ok'] = out['detail_len'] >= 200
                if not out['detail_ok']:
                    from .browser_probe import is_waf_page
                    if is_waf_page(getattr(dr, 'text', '') or ''):
                        out['errors'].append(
                            '详情页被反爬中间页拦截（反爬 JS 挑战：acw_sc__v2 / 瑞数 $_ts），未取到正文：%s。'
                            '请确认本机装有 Edge/Chrome（可用 BIDCRAWL_BROWSER 指定路径），'
                            '或从浏览器重新复制最新 Cookie 后重新生成配方'
                            % (out.get('waf') or '浏览器不可用'))
                    else:
                        out['errors'].append(
                            'HTML 正文 %s 为空或过短（%d 字符）'
                            % (detail['content_css'], out['detail_len']))
            else:
                dd = dr.json() if dr.text.lstrip()[:1] in ('{', '[') else {}
                rp = str(detail.get('row_path') or '')
                node = dd
                if rp.startswith('$'):
                    m = jsonpath_ng.parse(rp).find(dd)
                    node = m[0].value if m else None
                elif rp:
                    node = recipe_mod.dotted_get(dd, rp)
                if isinstance(node, dict):
                    content = node.get(detail['content_path']) or ''
                    out['detail_len'] = len(str(content))
                    out['detail_ok'] = out['detail_len'] >= 200
                    if not out['detail_ok']:
                        out['errors'].append('详情正文 %s 为空或过短'
                                             % detail['content_path'])
                else:
                    out['errors'].append('详情 row_path %s 未定位到对象' % rp)
        except Exception as e:
            out['errors'].append('详情请求失败：%s' % str(e)[:160])

    out['valid'] = not out['errors']
    out['elapsed_ms'] = int((time.monotonic() - t0) * 1000)
    return out


# ---------------------------------------------------------------- HTML 兜底
SYS_HTML = (
    '你是招投标信息采集配方生成专家。根据给定的公告列表页 HTML，生成结构化采集配方 JSON。\n'
    '铁律：\n'
    '1. 只依据页面里实际出现的标签/class/id 作答，不编造选择器。\n'
    '2. 严格遵循示例的字段名与层级，只输出一个合法 JSON 对象，不含解释文字。\n'
    '3. 找不到翻页机制就设 pagination.type=none；详情页结构无法推断，detail.enabled=false。\n'
    '\n示例：\n'
    '{"meta":{"id":"站点ID简写","name":"站点中文名","host":"域名","entry_url":"入口URL",'
    '"status":"paused"},'
    '"transport":{"kind":"http","timeout_s":25,"total_timeout_s":30,"max_response_mb":5,'
    '"rate_limit":{"qps":0.2},"retry":{"times":2,"backoff_s":5,'
    '"retry_on":[429,500,502,503,504]}},'
    '"discovery":{"mode":"html_list","request":{"url":"列表页URL","method":"GET",'
    '"params":{"keyword":"{keyword}","page":"{page}"}},"row_selector":"每条公告行的CSS选择器",'
    '"pagination":{"type":"none"},'
    '"fields":{"title":{"css":"行内标题选择器","strip":true},'
    '"url":{"css":"行内链接选择器","attr":"href"}}},'
    '"detail":{"enabled":false},"normalize":{"filters":{"inherit":"global"}},'
    '"validation":{"min_items_per_keyword":0}}'
)


def _html_fallback(url: str, html: str, ev: dict, stages: list) -> dict:
    """探测不到 JSON 接口时，退回「入口 HTML + LLM 生成 html_list」的旧路径。"""
    ok, msg = _llm_available()
    if not ok:
        return {'ok': False,
                'error': ('站点里未探测到可用的 JSON 取数接口；%s' % msg),
                'stages': stages, 'evidence': _trim_evidence(ev),
                'manual_steps': MANUAL_STEPS}
    t0 = time.monotonic()
    try:
        raw = _chat(SYS_HTML, '目标站点：%s\n页面 HTML（前 12000 字符）：\n---\n%s\n---\n'
                    '请生成 html_list 配方 JSON。' % (url, (html or '')[:12000]),
                    timeout_s=90)
        recipe = _loads(raw)
    except Exception as e:
        return {'ok': False, 'error': 'HTML 配方生成失败：%s' % str(e)[:200],
                'stages': stages, 'evidence': _trim_evidence(ev),
                'manual_steps': MANUAL_STEPS}
    meta = recipe.setdefault('meta', {})
    meta.setdefault('host', urlparse(url).netloc)
    meta.setdefault('entry_url', url)
    meta.setdefault('id', _platform_id(meta['host']))
    meta.setdefault('name', (ev.get('entry') or {}).get('title') or meta['host'])
    meta['status'] = 'paused'
    meta['created_by'] = 'ai'
    recipe.setdefault('detail', {'enabled': False})
    recipe.setdefault('normalize', {'filters': {'inherit': 'global'}})
    recipe.setdefault('validation', {'min_items_per_keyword': 0})
    errors = recipe_mod.validate_recipe(recipe)
    stages.append({'name': 'HTML 列表兜底生成', 'status': 'warn' if errors else 'ok',
                   'detail': '未探测到 JSON 接口，已退回 html_list 模式',
                   'ms': int((time.monotonic() - t0) * 1000)})
    if errors:
        return {'ok': False, 'recipe': recipe,
                'verify': {'errors': errors, 'valid': False},
                'error': 'HTML 配方校验未通过：%s' % '；'.join(errors[:3]),
                'stages': stages, 'evidence': _trim_evidence(ev),
                'manual_steps': MANUAL_STEPS}
    return {'ok': True, 'recipe': recipe,
            'verify': {'errors': [], 'valid': True, 'mode': 'html_list',
                       'note': 'html_list 模式未做真实试跑，请手动验证选择器'},
            'stages': stages, 'evidence': _trim_evidence(ev),
            'warning': '未找到 JSON 接口，已生成 html_list 骨架配方，需人工核对选择器。'}


# ---------------------------------------------------------------- 证据裁剪
def _trim_evidence(ev: dict) -> dict:
    lst = ev.get('list') or {}
    det = ev.get('detail') or {}
    return {
        'entry': ev.get('entry') or {},
        'keyword': ev.get('keyword'),
        'requests': ev.get('requests'),
        'elapsed_ms': ev.get('elapsed_ms'),
        'scripts': ev.get('scripts') or [],
        'endpoints': (ev.get('js_signals') or {}).get('endpoints', [])[:6],
        'state_defaults': (ev.get('js_signals') or {}).get('state_defaults', {}),
        'list': {k: lst.get(k) for k in
                 ('url', 'method', 'items_path', 'total_path', 'count', 'total',
                  'keyword_key', 'keyword_hit', 'page2_differs', 'page2_count',
                  'payload', 'roles', 'keys')} if lst else None,
        'list_candidates': [{'url': c.get('url'), 'items_path': c.get('items_path'),
                             'count': c.get('count'), 'rank': c.get('rank'),
                             'keyword_key': c.get('keyword_key')}
                            for c in (ev.get('list_candidates') or [])[:4]],
        'detail_url_template': ev.get('detail_url_template'),
        'detail': {k: det.get(k) for k in
                   ('url', 'body', 'row_path', 'content_path', 'content_encoding',
                    'content_len')} if det else None,
        'html_list': {k: (ev['html_list'] or {}).get(k) for k in
                      ('search', 'search_url', 'row_selector', 'fields', 'rows',
                       'sample', 'total_pages', 'page2_differs', 'first_url',
                       'keyword_hit')} if ev.get('html_list') else None,
        'pdf': {k: v for k, v in ((ev.get('html_list') or {}).get('pdf') or {}).items()
                if k in ('pdf_url', 'viewer_url', 'url_template',
                         'file_id_regex', 'iframe_css')} or None,
        'detail_page': ev.get('detail_page'),
        'browser': ev.get('browser') or None,
        'errors': (ev.get('errors') or [])[:10],
    }


# ---------------------------------------------------------------- 主入口
def analyze(url: str, keyword: str = '', *, with_llm: bool = True,
            verify: bool = True) -> dict:
    """输入 URL，返回 {ok, recipe, verify, stages, evidence, error, manual_steps, warning}。

    keyword 为样本关键词，用于验证接口筛选是否生效（默认「招标」）。
    """
    url = (url or '').strip()
    if not url.startswith(('http://', 'https://')):
        return {'ok': False, 'error': '请输入 http(s):// 开头的完整网址',
                'manual_steps': MANUAL_STEPS}
    kw = (keyword or '').strip() or DEFAULT_KEYWORD

    probe = SiteProbe(url, kw)
    try:
        ev = probe.run()
        entry_html = probe.entry_html
    finally:
        probe.close()
    stages = list(ev.get('stages') or [])

    if not ev.get('list'):
        if not ev.get('html_list'):
            return _html_fallback(url, entry_html, ev, stages)
        # HTML 列表站点（goSearch + page）：用探测证据组装完整配方
        t0 = time.monotonic()
        recipe, source = build_html_recipe(ev, url, kw, with_llm=with_llm)
        errors = recipe_mod.validate_recipe(recipe)
        h = ev['html_list']
        stages.append({
            'name': '生成配方骨架',
            'status': 'error' if errors else 'ok',
            'detail': ('；'.join(errors[:2]) if errors else
                       'html_list + %s + %s（字段规则由探测器给出，映射 %s）' %
                       (h['search']['keyword_param'],
                        'PDF 正文 AI' if (h.get('pdf') or {}).get('pdf_url') else 'HTML 正文',
                        source)),
            'ms': int((time.monotonic() - t0) * 1000)})
        if errors:
            return {'ok': False, 'recipe': recipe,
                    'verify': {'valid': False, 'errors': errors},
                    'stages': stages, 'evidence': _trim_evidence(ev),
                    'mapping_source': source,
                    'error': '生成的配方校验未通过：%s' % '；'.join(errors[:3]),
                    'manual_steps': MANUAL_STEPS}
        t1 = time.monotonic()
        vr = verify_recipe(recipe, kw) if verify else {'valid': True, 'skipped': True}
        if verify:
            stages.append({
                'name': '试跑校验', 'status': 'ok' if vr['valid'] else 'warn',
                'detail': ('%d 条样本，字段非空率 %s，%s %d 字符%s' %
                           (vr['items'],
                            json.dumps(vr['field_coverage'], ensure_ascii=False),
                            'PDF 正文' if vr.get('pdf_url') else '详情正文',
                            vr['detail_len'],
                            '' if vr['valid'] else '；' + '；'.join(vr['errors'][:2]))),
                'ms': int((time.monotonic() - t1) * 1000)})
        good = bool(vr.get('valid'))
        return {'ok': good, 'recipe': recipe, 'verify': vr,
                'stages': stages, 'evidence': _trim_evidence(ev),
                'mapping_source': source,
                'error': '' if good else '自动试跑未通过：%s' %
                                       '；'.join(vr.get('errors', [])[:2]),
                'warning': _warning(source)}

    # 生成 + 校验；列表接口失败时换下一个候选重试
    candidates = [ev['list']]
    seen = {(ev['list'].get('url'), ev['list'].get('keyword_key'))}
    for c in (ev.get('list_candidates') or [])[1:]:
        key = (c.get('url'), c.get('keyword_key'))
        if key not in seen and c.get('payload'):
            cand = dict(c)
            cand['first_row'] = (c.get('rows_sample') or [{}])[0]
            candidates.append(cand)
            seen.add(key)
        if len(candidates) >= 3:
            break

    last_recipe = None
    last_vr = None
    source = 'heuristic'
    for idx, cand in enumerate(candidates):
        trial_ev = dict(ev, list=cand)
        t0 = time.monotonic()
        recipe, source = build_recipe(trial_ev, url, kw, with_llm=with_llm)
        errors = recipe_mod.validate_recipe(recipe)
        if errors:
            stages.append({'name': '生成配方骨架', 'status': 'error',
                           'detail': '；'.join(errors[:2]),
                           'ms': int((time.monotonic() - t0) * 1000)})
            last_recipe, last_vr = recipe, {'valid': False, 'errors': errors}
            continue
        stages.append({'name': '生成配方骨架', 'status': 'ok',
                       'detail': 'json_api + %s + %s（字段映射 %s）' %
                                 ('详情两段式' if recipe['detail']['enabled'] else '仅列表',
                                  'URL 模板已还原' if ev.get('detail_url_template') else 'URL 兜底',
                                  source),
                       'ms': int((time.monotonic() - t0) * 1000)})

        if not verify:
            return {'ok': True, 'recipe': recipe,
                    'verify': {'valid': True, 'skipped': True},
                    'stages': stages, 'evidence': _trim_evidence(ev),
                    'warning': _warning(source)}
        t1 = time.monotonic()
        vr = verify_recipe(recipe, kw)
        stages.append({'name': '试跑校验', 'status': 'ok' if vr['valid'] else 'warn',
                       'detail': ('%d 条样本，字段非空率 %s，详情正文 %d 字符%s' %
                                  (vr['items'], json.dumps(vr['field_coverage'],
                                                           ensure_ascii=False),
                                   vr['detail_len'],
                                   '' if vr['valid'] else '；' + '；'.join(vr['errors'][:2]))),
                       'ms': int((time.monotonic() - t1) * 1000)})
        last_recipe, last_vr = recipe, vr
        if vr['valid']:
            return {'ok': True, 'recipe': recipe, 'verify': vr,
                    'stages': stages, 'evidence': _trim_evidence(ev),
                    'mapping_source': source,
                    'warning': _warning(source)}

    return {
        'ok': False, 'recipe': last_recipe, 'verify': last_vr or {},
        'stages': stages, 'evidence': _trim_evidence(ev),
        'mapping_source': source,
        'error': ('配方已生成，但自动试跑未通过：%s。可在下方编辑后手动保存重试。' %
                  '；'.join((last_vr or {}).get('errors', [])[:2])),
        'manual_steps': MANUAL_STEPS,
        'warning': _warning(source),
    }


def _warning(source: str) -> str:
    return ('配方已按探测证据生成并试跑通过（字段映射：%s）。默认 status=paused，'
            '且已关闭全局采购人白名单，请在「配方」里审核后再启用。' %
            ('AI 语义映射' if source == 'llm' else '启发式规则'))


# ---------------------------------------------------------------- 手动生成步骤
MANUAL_STEPS = (
    '=== 手动生成配方 JSON 步骤 ===\n'
    '\n'
    '1. 打开目标站点的招标公告列表页，打开浏览器 F12 → Network 面板。\n'
    '2. 在列表页搜索框输入一个关键词（如「门头」），点搜索，观察请求：\n'
    '   - 若请求返回 JSON（Response 是 {...}），记 mode=json_api；\n'
    '     记下 URL、Method、Request Payload（body）、分页参数名（page/pageSize）。\n'
    '   - 若请求返回 HTML，记 mode=html_list；记下列表行的 CSS 选择器。\n'
    '3. 解析第一条数据，确定各字段的取值规则：\n'
    '   - json_api：用 JSONPath，如 $.rows[*].noticeTitle\n'
    '   - html_list：用 CSS 选择器，如 .item .title\n'
    '   - url 字段若列表里只有相对路径，用 template 拼接域名。\n'
    '4. 若详情需要二次请求，记下详情 URL、请求体（如 id=xx&isDetail=1）、'
    '正文字段名（如 noticeContent），并填写 detail 区块。\n'
    '5. 参照 server/bidcrawl/recipes/cfcpn.json 写出完整 JSON。\n'
    '6. 放到「平台与配方 → 配方」里粘贴保存，status 先设 paused，\n'
    '   用「试跑预览」验证能取到数据后再启用。\n'
    '\n'
    '提示：新版生成器会先抓取页面引用的 JS、自动试探接口并校验，'
    '多数站点无需手工完成上述步骤。'
)


if __name__ == '__main__':
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else (
        'http://www.cfcpn.com/jcw/sys/index/goUrl?url=modules/sys/login/list&column=cggg')
    kw = sys.argv[2] if len(sys.argv) > 2 else '门头'
    out = analyze(u, kw)
    print('ok=%s error=%s' % (out.get('ok'), out.get('error', '')))
    print('--- stages ---')
    for s in out.get('stages') or []:
        print('[%s] %s | %s (%dms)' % (s['status'], s['name'], s['detail'][:200], s['ms']))
    print('--- verify ---')
    print(json.dumps(out.get('verify'), ensure_ascii=False, indent=2)[:3000])
    print('--- recipe ---')
    print(json.dumps(out.get('recipe'), ensure_ascii=False, indent=2)[:14000])
