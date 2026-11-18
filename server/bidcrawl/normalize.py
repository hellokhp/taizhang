# -*- coding: utf-8 -*-
"""归一化：日期、金额、状态、URL 规范化与去重指纹。

清洗规则集中在这里，配方只负责「取」，便于统一测试。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urljoin, urlencode, urlsplit, urlunsplit

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))

_DATE_PATTERNS = (
    (re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?'),
     'ymd'),
    (re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?'),
     'ymd'),
    (re.compile(r'^(\d{4})(\d{2})(\d{2})$'), 'ymd8'),
)


def parse_datetime(v):
    """宽松解析常见中文/英文日期；失败返回 None。"""
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=LOCAL_TZ)
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        try:
            n = int(float(v))
            if n > 10 ** 12:
                n //= 1000
            if 10 ** 9 < n < 10 ** 11:
                return datetime.fromtimestamp(n, LOCAL_TZ)
        except Exception:
            pass
    s = str(v).strip().replace('\u3000', ' ')
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)
    except Exception:
        pass
    # Java Date.toString()：Tue Apr 01 09:17:32 UTC 2025（邮储等 CMS 的 publishDate）
    m = re.match(r'^[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+'
                 r'(\d{2}):(\d{2}):(\d{2})\s+(\S+)\s+(\d{4})$', s)
    if m:
        try:
            mon = datetime.strptime(m.group(1), '%b').month
            return datetime(int(m.group(7)), mon, int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), int(m.group(5)),
                            tzinfo=LOCAL_TZ)
        except Exception:
            pass
    for pat, kind in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        g = list(m.groups())
        if kind == 'ymd8':
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
            hh = mm = ss = 0
        else:
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
            hh = int(g[3] or 0)
            mm = int(g[4] or 0)
            ss = int(g[5] or 0)
        try:
            return datetime(y, mo, d, hh, mm, ss, tzinfo=LOCAL_TZ)
        except ValueError:
            return None
    return None


def parse_amount(v) -> Decimal | None:
    """金额统一为元。支持 万元 / 亿 / 元、千分位、中英文单位。"""
    if v is None or v == '':
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).replace(',', '').replace('，', '')
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if not m:
        return None
    try:
        num = Decimal(m.group(1))
    except InvalidOperation:
        return None
    if '亿' in s:
        num *= Decimal(100000000)
    elif '万' in s:
        num *= Decimal(10000)
    return num.quantize(Decimal('0.01'))


def compute_status(deadline, now=None) -> str:
    """按截止时间给出 open / soon / expired / unknown。"""
    dt = parse_datetime(deadline) if not isinstance(deadline, datetime) else deadline
    if not dt:
        return 'unknown'
    now = now or datetime.now(LOCAL_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    delta = dt - now
    if delta.total_seconds() < 0:
        return 'expired'
    if delta <= timedelta(days=3):
        return 'soon'
    return 'open'


_TRACKING = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
             'spm', 'from', 'share_token')


def canonical_url(url: str, base_url: str = '') -> str:
    """绝对化 + 去跟踪参数 + 参数排序，保证同一详情只有一个 hash。

    **hash 路由（SPA）不能丢 fragment**：`https://x/puc/#/noticeDetail?messageId=abc`
    的公告 id 全在 `#` 之后，丢掉 fragment 会让同站所有详情收敛成 `https://x/puc/`，
    入库时被去重成一条（农银e采就是这个坑）。只有纯锚点（`#top`）才丢弃。
    """
    if not url:
        return ''
    full = urljoin(base_url or '', url.strip())
    try:
        sp = urlsplit(full)
    except Exception:
        return full
    if not sp.scheme or not sp.netloc:
        return full
    query = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
             if k.lower() not in _TRACKING]
    query.sort()
    path = re.sub(r'/+', '/', sp.path or '/')
    if len(path) > 1:
        path = path.rstrip('/') or '/'
    frag = sp.fragment or ''
    keep_frag = frag if (frag.startswith('/') or '?' in frag or '=' in frag) else ''
    return urlunsplit((sp.scheme.lower(), sp.netloc.lower(), path,
                       urlencode(query), keep_frag))


def url_hash(url: str, base_url: str = '') -> str:
    return hashlib.sha1(canonical_url(url, base_url).encode('utf-8')).hexdigest()


def item_fingerprint(title: str, buyer: str, publish_at) -> str:
    dt = parse_datetime(publish_at) if not isinstance(publish_at, datetime) else publish_at
    day = dt.strftime('%Y-%m-%d') if dt else ''
    raw = '|'.join([str(title or '').strip().lower(),
                    str(buyer or '').strip().lower(), day])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


DATETIME_FIELDS = ('doc_get_start', 'doc_get_end', 'open_at', 'publish_at')
TEXT_FIELDS = ('title', 'buyer', 'fee_note', 'scope', 'region', 'source_name')


def normalize_item(item: dict, *, base_url: str = '', source_default: str = '',
                   keep_url: bool = False) -> dict:
    """把抽取出的字符串条目转成可直接入库的规范化 dict。

    keep_url=True 时保存配方拼接出的原始详情 URL（站点参数顺序敏感时用），
    url_hash 仍由 canonical_url 计算，去重指纹不受影响。
    """
    out = {}
    for key in TEXT_FIELDS:
        val = item.get(key)
        out[key] = re.sub(r'\s+', ' ', str(val)).strip() if val else None
    out['title'] = out.get('title') or '（无标题）'
    out['source_name'] = out.get('source_name') or (source_default or None)
    canon = canonical_url(item.get('url') or '', base_url)
    raw_url = str(item.get('url') or '').strip()
    out['url'] = (urljoin(base_url or '', raw_url) if keep_url and raw_url else canon)
    out['url_hash'] = hashlib.sha1(canon.encode('utf-8')).hexdigest() if canon else ''
    out['external_id'] = item.get('external_id') or None
    out['budget_yuan'] = parse_amount(item.get('budget'))
    for key in DATETIME_FIELDS:
        out[key] = parse_datetime(item.get(key))
    out['status'] = compute_status(out.get('doc_get_end') or out.get('open_at'))
    out['raw'] = item.get('_raw') or {k: v for k, v in item.items() if k != '_raw'}
    # 详情 AI 解析结果单独存 jsonb，不污染原始行快照
    detail = item.get('detail')
    out['detail'] = detail if isinstance(detail, dict) and detail else None
    return out
