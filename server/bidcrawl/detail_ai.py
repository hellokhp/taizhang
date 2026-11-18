# -*- coding: utf-8 -*-
"""详情页正文 AI 结构化抽取。

设计原则（与《标讯采集_平台自解析与配方复用方案》一致）：
- AI 只在「第一次遇到某条公告正文」时出现一次，结果按
  sha1(platform_id + prompt_ver + 清洗后正文) 落到 bid_detail_ai；
  同一公告被 21 个关键词重复命中、以及后续每轮跑批都直接读缓存，0 次模型调用。
- 无 API Key / 模型超时 / 返回不合法 JSON 时降级为确定性正则，绝不阻断采集主链路。
- 站点特有的字段只写在配方里（detail.ai.fallback_regex），本模块不含任何站点分支。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import re
from typing import Any

# 标准字段：直接对应 bid_item 的列 / normalize_item 的输入键
STANDARD_FIELDS = ('budget', 'fee_note', 'scope', 'doc_get_start', 'doc_get_end',
                   'open_at', 'region', 'buyer')
# 扩展字段：进入 bid_item.detail（jsonb），不改动主表结构
EXTRA_FIELDS = ('bid_no', 'agency', 'contact', 'qualification', 'project_period',
                'bidder_limit', 'project_name', 'funding', 'bid_place', 'notice_type',
                'deadline')
ALL_FIELDS = STANDARD_FIELDS + EXTRA_FIELDS

# 老配方（detail.ai.fields 写死）里没有、但报名进台账必须有的字段：
# 抽取时无条件补上，免得「配方能采、台账的投标截止时间永远空」。
ENRICH_FIELDS = ('deadline',)

PROMPT_VER = 'bid-detail-v2'

SYS_PROMPT = (
    '你是招投标公告信息抽取助手。从给定的招标公告正文中抽取结构化字段。\n'
    '工作铁律：\n'
    '1. 只依据正文实际出现的信息作答，原文没有的字段填空字符串 ""，严禁编造或推测。\n'
    '2. 只输出一个合法 JSON 对象，不以 ``` 开头，不含任何解释文字。\n'
    '3. 日期统一写 "YYYY-MM-DD HH:MM" 24 小时制；原文写「下午14:00」按 14:00，'
    '「下午2:00」按 14:00，「上午9:00」按 09:00；没有具体时刻就写 "YYYY-MM-DD 00:00"。\n'
    '4. 金额类字段保留原文（含单位），不要自行换算。\n'
    '\n'
    '字段定义：\n'
    '  budget          项目规模/预算/合同估算价/最高限价，带单位原文\n'
    '  fee_note        报名费/标书费/招标文件费用（含「售后不退」等备注）\n'
    '  scope           采购范围/项目内容/标的简述\n'
    '  doc_get_start   招标文件获取开始时间\n'
    '  doc_get_end     招标文件获取截止时间 / 报名截止时间\n'
    '  open_at         开标时间（若公告只写了一个截止时间，就与 deadline 相同）\n'
    '  deadline        投标文件递交截止时间（台账的「投标截止时间」，不是开标时间）\n'
    '  region          项目所在地区\n'
    '  buyer           采购人 / 招标人\n'
    '  bid_no          招标编号 / 项目编号\n'
    '  agency          招标代理机构\n'
    '  contact         联系人与联系电话\n'
    '  qualification   核心资质与业绩门槛（一句话概括）\n'
    '  project_period  工期 / 服务期\n'
    '  bidder_limit    中标人数量\n'
    '  project_name    项目全称\n'
    '  funding         资金来源\n'
    '  bid_place       开标及投标文件递交地点\n'
    '  notice_type     采购方式（公开招标/竞争性磋商/询价等）\n'
)

USER_TMPL = (
    '公告标题：%s\n'
    '\n公告正文（已去除 HTML 标签）：\n---BEGIN---\n%s\n---END---\n\n'
    '请按字段定义输出 JSON 对象。'
)

# 无模型时的确定性兜底：通用中文招标表述，可被配方 detail.ai.fallback_regex 覆盖
DEFAULT_FALLBACK_REGEX = {
    'budget': r'(?:合同估算价|项目预算|采购预算|预算金额|最高限价|预算)[：:\s]*([^\n，。；]{1,40})',
    'fee_note': r'(?:费用|标书费|文件费|招标文件费)[：:]\s*([^\n]{1,60})',
    'doc_get_start': r'获取时间[：:]\s*从\s*([^\n到]{4,40}?)\s*到',
    'doc_get_end': r'获取时间[：:]\s*从\s*[^\n到]{4,40}?\s*到\s*([^\n]{4,40})',
    'open_at': r'(?:递交截止时间|开标时间)[：:]\s*([^\n，。（]{4,45})',
    'deadline': r'(?:投标文件递交截止时间|递交截止时间|投标截止时间|响应文件递交截止时间)[：:]\s*([^\n，。（]{4,45})',
    'bid_no': r'(?:招标编号|项目编号)[：:]\s*([A-Za-z0-9\-_（）()]{4,40})',
    'agency': r'([\u4e00-\u9fa5]{4,30}(?:有限公司|有限责任公司))[（(]招标代理机构',
    'contact': r'联系人[：:]\s*([^\n，。]{1,40})',
    'project_period': r'工期[：:]\s*([^\n，。]{1,20})',
    'bidder_limit': r'中标人数量[：:]\s*([^\n，。名]{1,10})',
}

_BLOCK_END = re.compile(r'(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>|</table>|</section>')
_CELL_END = re.compile(r'(?i)</t[dh]>')
_TAG = re.compile(r'(?i)<[^>]+>')
_CN_DT = re.compile(
    r'(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?'
    # 11185 的 PDF 正文写「2026年12月04日09点30分」，故时刻分隔符也认「点」
    r'\s*(?:([上下]午|中午)\s*)?(?:(\d{1,2})\s*[时:：点]\s*(\d{1,2})\s*分?)?')


def html_to_text(raw: str, max_chars: int = 20000) -> str:
    """把站点返回的「转义过的 Word HTML」清洗成可读纯文本。

    JSON 里的 noticeContent 是 &lt;p ...&gt; 形式（HTML 实体套 HTML），
    因此先反转义一次拿回真标签，去标签后再反转义一次处理 &amp;nbsp; 等二次实体。
    行内标签直接删除（不能替换成空格，否则「2026」会被拆成「202 6」），
    块级标签换成换行，保留段落结构。
    """
    if not raw:
        return ''
    s = html_mod.unescape(str(raw))
    s = _BLOCK_END.sub('\n', s)
    s = _CELL_END.sub(' | ', s)
    s = _TAG.sub('', s)
    s = html_mod.unescape(s).replace('\xa0', ' ').replace('\u3000', ' ')
    s = re.sub(r'[ \t\r]+', ' ', s)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    text = '\n'.join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text


def cn_dt_to_iso(value: str) -> str:
    """把「2026年11月14日09时00分 / 下午2:00 / 2026-11-14 09:00」统一成 ISO 串。

    normalize.parse_datetime 只认冒号时刻，中文「09时00分」会丢掉时分，
    故在交给归一化之前先做一次转换。
    """
    if not value:
        return ''
    m = _CN_DT.search(str(value))
    if not m:
        return str(value)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(5) or 0)
    mm = int(m.group(6) or 0)
    meridiem = m.group(4) or ''
    if ('下' in meridiem or '晚' in meridiem) and hh < 12:
        hh += 12
    try:
        return '%04d-%02d-%02d %02d:%02d' % (y, mo, d, hh, mm)
    except (ValueError, TypeError):
        return str(value)


# ------------------------------------------------------------------ 缓存
def _cache_key(platform_id: str, prompt_ver: str, text: str) -> str:
    payload = '\x1f'.join((platform_id, prompt_ver, text))
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()


def cache_get(key: str):
    """命中返回 {result, model, source}；缓存表不存在或读失败返回 None。"""
    try:
        from . import db
        row = db.one('SELECT result, model, source FROM bid_detail_ai WHERE cache_key=%s',
                     (key,))
        if not row:
            return None
        result = row.get('result')
        if isinstance(result, str):
            result = json.loads(result)
        return {'result': result or {}, 'model': row.get('model') or '',
                'source': row.get('source') or 'llm'}
    except Exception:
        return None


def cache_put(key: str, platform_id: str, prompt_ver: str, model: str,
              result: dict, source: str = 'llm'):
    try:
        from . import db
        db.x('INSERT INTO bid_detail_ai(cache_key, platform_id, prompt_ver, model, '
             'result, source) VALUES (%s,%s,%s,%s,%s::jsonb,%s) '
             'ON CONFLICT (cache_key) DO UPDATE SET result=EXCLUDED.result, '
             'source=EXCLUDED.source, created_at=now()',
             (key, platform_id, prompt_ver, model,
              json.dumps(result, ensure_ascii=False, default=str), source))
        db.commit()
        return True
    except Exception:
        try:
            from . import db
            db.rollback()
        except Exception:
            pass
        return False


# ------------------------------------------------------------------ 模型调用
def _llm_client():
    """复用 ai_analyze 的 .env 加载与 OpenAI 兼容客户端，避免配置第二套。"""
    from . import ai_analyze
    return ai_analyze


def _model_name() -> str:
    return os.environ.get('LLM_TEXT_MODEL') or 'qwen-plus'


def _safe_error(msg: str) -> str:
    """模型异常可能把 API Key 带进 message，落库前统一脱敏。"""
    s = str(msg or '')
    for key in ('LLM_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY'):
        secret = os.environ.get(key) or ''
        if secret and len(secret) > 4:
            s = s.replace(secret, '***')
    return s[:200]


def call_llm(title: str, text: str, timeout_s: float = 90,
             fields: list | None = None) -> tuple[dict, str]:
    """返回 (抽取结果, 模型名)；任何异常都向上抛，由 extract() 决定降级。"""
    mod = _llm_client()
    ok, msg = mod._llm_available()
    if not ok:
        raise RuntimeError(msg)
    answer = mod._chat(SYS_PROMPT, USER_TMPL % (title or '', text), timeout_s=timeout_s)
    data = mod._loads(answer)
    if not isinstance(data, dict):
        raise ValueError('模型返回不是 JSON 对象')
    wanted = set(fields or ALL_FIELDS)
    out = {}
    for k, v in data.items():
        key = str(k).strip()
        if key not in wanted or v is None:
            continue
        val = str(v).strip() if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
        if val and val.lower() not in ('null', 'none', 'n/a', '未知', '无'):
            out[key] = val[:600]
    return out, _model_name()


def regex_fallback(text: str, patterns: dict | None = None,
                   platform_id: str = '') -> dict:
    """确定性兜底：正则从纯文本里抓关键字段。

    三层来源，后者覆盖前者：
      内置 DEFAULT_FALLBACK_REGEX → 配方 detail.ai.fallback_regex → 配置中心
    配置中心（bid_regex_field/alias）放在最后，是为了「页面上加一个匹配词，
    全平台立即生效」，不必回头改每个平台的配方。
    """
    specs = dict(DEFAULT_FALLBACK_REGEX)
    specs.update(patterns or {})
    try:
        from . import regexcfg
        specs.update(regexcfg.load_rules(platform_id))
    except Exception:
        pass
    out = {}
    for key, pat in specs.items():
        try:
            m = re.search(pat, text, re.M)
        except re.error:
            continue
        if not m:
            continue
        val = (m.group(1) if m.lastindex else m.group(0)).strip()
        if val:
            out[key] = val[:600]
    return out


# ------------------------------------------------------------------ 主入口
def detail_config(recipe) -> dict:
    d = recipe.detail if hasattr(recipe, 'detail') else (recipe.get('detail') or {})
    return d if isinstance(d, dict) else {}


def ai_config(recipe) -> dict:
    cfg = detail_config(recipe).get('ai') or {}
    return cfg if isinstance(cfg, dict) else {}


def ai_enabled(recipe) -> bool:
    cfg = ai_config(recipe)
    return bool(cfg.get('enabled')) and bool(detail_config(recipe).get('enabled'))


def content_from_result(recipe, result) -> str:
    """按配方声明从详情响应里取出正文，并清洗成纯文本。"""
    from .recipe import jsonpath_get

    d = detail_config(recipe)
    # PDF 型站点（如 cg.11185.cn）：正文不在响应 JSON 里，而在详情页
    # pdfjs iframe 指向的文件中，交给 pdf_text 下载并按 URL 缓存。
    if (d.get('pdf') or {}).get('enabled'):
        from .pdf_text import text_from_result
        return text_from_result(recipe, result)[0]
    # HTML 详情页形态（如 cg.psbc.com）：正文在页面的 .content 容器里
    css = str(d.get('content_css') or '').strip()
    if css and (getattr(result, 'text', '') or ''):
        try:
            from parsel import Selector
            nodes = Selector(text=result.text).css(css)
            if nodes:
                # 同一选择器可能命中多个容器，取文本最长的那个（与探测一致）
                best = max((' '.join(n.xpath('.//text()').getall()) for n in nodes[:5]),
                           key=len)
                raw = re.sub(r'\s+', ' ', best).strip()
                if raw:
                    max_chars = int(ai_config(recipe).get('max_chars') or 20000)
                    return raw[:max_chars]
        except Exception:
            pass
    data = result.data
    if not isinstance(data, dict):
        try:
            data = json.loads(result.text or '{}')
        except Exception:
            return ''
    row_path = d.get('row_path')
    if row_path:
        got = jsonpath_get(data, str(row_path)) if str(row_path).startswith('$') else None
        if isinstance(got, dict):
            data = got
        else:
            # 兼容 "rows.0" 这类点号路径
            from .recipe import dotted_get
            got = dotted_get(data, str(row_path))
            data = got if isinstance(got, dict) else data
    field = str(d.get('content_path') or 'noticeContent')
    from .recipe import dotted_get
    raw = dotted_get(data, field)
    if raw is None and field not in data:
        # 兜底：正文可能在 briefContent
        raw = dotted_get(data, 'briefContent')
    if not isinstance(raw, str) or not raw.strip():
        return ''
    max_chars = int(ai_config(recipe).get('max_chars') or 20000)
    encoding = str(d.get('content_encoding') or 'html_escaped')
    if encoding == 'text':
        return raw[:max_chars]
    return html_to_text(raw, max_chars)


def extract(recipe, result, item: dict) -> dict:
    """返回要合并进条目的字段 dict（含标准字段 + detail 扩展 + _ai_meta）。

    永不抛异常：模型不可用时返回正则结果，连正文都没有时返回空 dict。
    """
    if not ai_enabled(recipe):
        return {}
    platform_id = (getattr(recipe, 'platform_id', '') or
                   (recipe.get('meta', {}) or {}).get('id') or '')
    cfg = ai_config(recipe)
    text = ''
    try:
        text = content_from_result(recipe, result)
    except Exception:
        text = ''
    if not text:
        return {}

    title = str(item.get('title') or '')[:200]
    # 缓存 key 里带上全局 PROMPT_VER：配方里写死的 prompt_version 不会随代码升级，
    # 只用它会让「改了 prompt 却继续读旧缓存」，抽出来的字段永远是上一版口径。
    prompt_ver = PROMPT_VER + '|' + str(cfg.get('prompt_version') or '')
    fields = [str(x) for x in (cfg.get('fields') or ALL_FIELDS)]
    for k in ENRICH_FIELDS:            # 老配方漏掉的台账字段在这里补上
        if k not in fields:
            fields.append(k)
    key = _cache_key(platform_id, prompt_ver, text)
    use_cache = bool(cfg.get('cache', True))
    hit = cache_get(key) if use_cache else None

    source, model, error, parsed = 'llm', _model_name(), '', {}
    if hit:
        parsed = dict(hit.get('result') or {})
        source, model = 'cache', hit.get('model') or ''
    else:
        try:
            parsed, model = call_llm(title, text,
                                     timeout_s=float(cfg.get('timeout_s') or 90),
                                     fields=fields)
            if use_cache and parsed:
                cache_put(key, platform_id, prompt_ver, model, parsed, 'llm')
        except Exception as e:
            error = '%s: %s' % (type(e).__name__, _safe_error(e))
            source = 'fallback'
            parsed = regex_fallback(text, cfg.get('fallback_regex'), platform_id)

    if not parsed:
        return {'_ai_meta': {'source': source, 'model': model, 'error': error,
                             'prompt_ver': prompt_ver, 'chars': len(text),
                             'cache_key': key}}

    # 日期字段统一成 ISO，保证 normalize.parse_datetime 能解析出时分
    out = {}
    for k, v in parsed.items():
        out[k] = cn_dt_to_iso(v) if k in ('doc_get_start', 'doc_get_end', 'open_at') else v

    detail = {}
    for k in EXTRA_FIELDS:
        if out.get(k):
            detail[k] = out[k]
    detail['ai'] = {'source': source, 'model': model, 'prompt_ver': prompt_ver,
                    'chars': len(text), 'cache_key': key, 'error': error}
    out['detail'] = detail
    out['_ai_meta'] = detail['ai']
    return out
