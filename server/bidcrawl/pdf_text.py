# -*- coding: utf-8 -*-
"""PDF 正文抽取：详情页只给 PDF 链接时，把文件文本喂给 AI / 正则。

适用形态（如 cg.11185.cn）：详情页 HTML 里只有标题和一个 pdfjs iframe，
真实公告正文是 `viewer.html?file=<URL>` 指向的 PDF，其中 file 参数把
`&` / `=` 编码成 `%26` / `%3D`，需要反转义一次才能拿到可下载地址。
这类直链通常是 `Content-Disposition: attachment` 的下载接口，因此必须按
二进制下载再解析，不能当网页解析。

设计原则：
- 只做「取文本」，不改采集主链路：正文按 URL hash 缓存到 bid_fetch_cache，
  同一公告被多个关键词命中、以及后续跑批都不会重复下载；
- 解析库自动降级：pypdf → PyMuPDF → pdfplumber → pdfminer → PyPDF2，
  任意装了一个就能工作；全都没装时返回明确原因，不再静默给空串；
- 下载失败 / 不是 PDF / 扫描件无文本层都给出 reason，供试跑校验展示；
- 不含任何站点分支，直链必须由配方的 pdf 段描述。
"""
from __future__ import annotations

import html as html_mod
import io
import re
import time
from urllib.parse import urljoin, unquote

import httpx

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
MAX_BYTES = 8 * 1024 * 1024
CACHE_DAYS = 7
MIN_TEXT_CHARS = 20          # 少于这个长度认为「没抽到文本」，继续换后端


def detail_config(recipe) -> dict:
    d = recipe.detail if hasattr(recipe, 'detail') else (recipe.get('detail') or {})
    return d if isinstance(d, dict) else {}


def pdf_config(recipe) -> dict:
    cfg = detail_config(recipe).get('pdf') or {}
    return cfg if isinstance(cfg, dict) else {}


def pdf_url_from_html(cfg: dict, html_text: str, base_url: str = '') -> str:
    """按配方声明 / 通用 pdfjs 形态还原 PDF 直链。"""
    if not html_text:
        return ''
    # 1. 配方声明：正则抓 fileId，模板拼回直链（最可靠）
    rx = cfg.get('file_id_regex') or cfg.get('url_regex')
    if rx:
        try:
            m = re.search(str(rx), html_text)
        except re.error:
            m = None
        if m:
            fid = m.group(1) if m.lastindex else m.group(0)
            tpl = cfg.get('url_template')
            if tpl:
                try:
                    return str(tpl).format(file_id=fid, id=fid)
                except Exception:
                    pass
            if fid.startswith('http'):
                return html_mod.unescape(fid)
    # 2. 通用：pdfjs/web/viewer.html?file=<被编码过的 PDF URL>
    m = re.search(r'viewer\.html\?file=([^"\'\s>&]+)', html_text, re.I)
    if m:
        u = unquote(html_mod.unescape(m.group(1)))
        if u.startswith('//'):
            u = 'https:' + u
        if not u.startswith('http') and base_url:
            u = urljoin(base_url, u)
        return u
    # 3. 通用：页面里直接给出 .pdf 链接
    m = re.search(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html_text, re.I)
    if m:
        u = html_mod.unescape(m.group(1))
        if u.startswith('//'):
            u = 'https:' + u
        if not u.startswith('http') and base_url:
            u = urljoin(base_url, u)
        return u
    return ''


def _cache_get(url: str) -> str:
    try:
        from . import db
        from .normalize import url_hash
        row = db.one(
            "SELECT body FROM bid_fetch_cache WHERE url_hash=%s "
            "AND fetched_at > now() - (%s * interval '1 day')",
            (url_hash(url), CACHE_DAYS))
        return (row or {}).get('body') or ''
    except Exception:
        return ''


def _cache_put(url: str, text: str):
    try:
        from . import db
        from .normalize import url_hash
        db.x('INSERT INTO bid_fetch_cache(url_hash, url, body, content_type, fetched_at) '
             "VALUES (%s,%s,%s,'text/pdf-extract',now()) "
             'ON CONFLICT (url_hash) DO UPDATE SET body=EXCLUDED.body, '
             'content_type=EXCLUDED.content_type, fetched_at=now()',
             (url_hash(url), url, text[:2_000_000]))
        db.commit()
    except Exception:
        try:
            from . import db
            db.rollback()
        except Exception:
            pass


def _download_meta(url: str, cfg: dict, timeout: float = 30) -> tuple[bytes, dict]:
    """下载 PDF 二进制，返回 (raw, 诊断信息)。异常转成 reason，不抛。"""
    meta = {'url': url, 'status': 0, 'bytes': 0, 'content_type': '',
            'filename': '', 'reason': ''}
    headers = {'User-Agent': UA, 'Accept': 'application/pdf,*/*'}
    headers.update(cfg.get('headers') or {})
    try:
        r = httpx.get(url, headers=headers, timeout=timeout,
                      follow_redirects=True, trust_env=False)
    except Exception as e:
        meta['reason'] = 'PDF 下载失败：%s' % str(e)[:160]
        return b'', meta
    meta['status'] = r.status_code
    meta['content_type'] = r.headers.get('content-type', '')
    disp = r.headers.get('content-disposition', '')
    fm = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp, re.I)
    if fm:
        meta['filename'] = unquote(fm.group(1))
    if r.status_code >= 400:
        meta['reason'] = 'PDF 下载 HTTP %d' % r.status_code
        return b'', meta
    raw = r.content or b''
    meta['bytes'] = len(raw)
    if not raw:
        meta['reason'] = 'PDF 响应体为空'
        return b'', meta
    if len(raw) > int(cfg.get('max_bytes') or MAX_BYTES):
        meta['reason'] = 'PDF 超过体积上限 %d 字节' % len(raw)
        return b'', meta
    # 部分网关会在 PDF 前加 BOM / 空白，容忍前 64 字节
    if b'%PDF' not in raw[:64]:
        meta['reason'] = ('响应不是 PDF（可能是登录页/错误页），content-type=%s，'
                          '开头=%r' % (meta['content_type'][:60], raw[:24]))
        return b'', meta
    return raw, meta


def _download(url: str, cfg: dict, timeout: float = 30) -> bytes:
    """兼容旧调用：只返回二进制；失败返回空串字节。"""
    return _download_meta(url, cfg, timeout)[0]


def _clean_pdf_text(text: str, max_chars: int = 20000) -> str:
    text = re.sub(r'[ \t\r]+', ' ', text or '')
    # PDF 常在汉字中间硬换行，合并后「2026」之类数字才不会被拆开；
    # 句末标点后保留换行，让段落边界仍然可辨认（正则兜底依赖 \n）。
    text = re.sub(r'(?<=[\u4e00-\u9fa5，、（）])\n(?=[\u4e00-\u9fa5])', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:max_chars]


def _extractor(name: str):
    """返回 (extract_fn, 是否可用)。extract_fn(raw) -> (text, pages)。"""
    if name == 'pypdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            return None

        def fn(raw):
            rd = PdfReader(io.BytesIO(raw))
            pages = len(rd.pages)
            parts = []
            for p in rd.pages[:40]:
                try:
                    parts.append(p.extract_text() or '')
                except Exception:
                    continue
            return '\n'.join(parts), pages
        return fn

    if name == 'pymupdf':
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
        except ImportError:
            return None

        def fn(raw):
            doc = fitz.open(stream=raw, filetype='pdf')
            pages = len(doc)
            return '\n'.join(p.get_text() for p in list(doc)[:40]), pages
        return fn

    if name == 'pdfplumber':
        try:
            import pdfplumber
        except ImportError:
            return None

        def fn(raw):
            with pdfplumber.open(io.BytesIO(raw)) as doc:
                pages = len(doc.pages)
                return '\n'.join((p.extract_text() or '') for p in doc.pages[:40]), pages
        return fn

    if name == 'pdfminer':
        try:
            from pdfminer.high_level import extract_text
        except ImportError:
            return None

        def fn(raw):
            return extract_text(io.BytesIO(raw), maxpages=40), 0
        return fn

    if name == 'PyPDF2':
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None

        def fn(raw):
            rd = PdfReader(io.BytesIO(raw))
            pages = len(rd.pages)
            parts = []
            for p in rd.pages[:40]:
                try:
                    parts.append(p.extract_text() or '')
                except Exception:
                    continue
            return '\n'.join(parts), pages
        return fn
    return None


BACKENDS = ('pypdf', 'pymupdf', 'pdfplumber', 'pdfminer', 'PyPDF2')


def extract_text_ex(raw: bytes, max_chars: int = 20000,
                    prefer: str = '', backends=None) -> dict:
    """按优先级自动挑选可用的 PDF 解析库，返回文本 + 诊断元数据。

    返回 {'text','backend','pages','reason','tried','available'}；
    任一后端抽出 >= MIN_TEXT_CHARS 字符即返回，不再继续尝试。
    """
    names = list(backends if backends is not None else BACKENDS)
    if prefer and prefer in names:
        names.remove(prefer)
        names.insert(0, prefer)
    elif prefer:
        names.insert(0, prefer)
    out = {'text': '', 'backend': '', 'pages': 0, 'reason': '',
           'tried': [], 'available': []}
    if not raw:
        out['reason'] = 'PDF 内容为空'
        return out
    best_text, best_backend, best_pages = '', '', 0
    for name in names:
        fn = _extractor(name)
        if fn is None:
            continue
        out['available'].append(name)
        try:
            text, pages = fn(raw)
        except Exception as e:
            out['tried'].append('%s: %s' % (name, str(e)[:100]))
            continue
        text = _clean_pdf_text(text, max_chars)
        out['tried'].append('%s: %d 字符' % (name, len(text)))
        if len(text) > len(best_text):
            best_text, best_backend, best_pages = text, name, pages
        if len(text) >= MIN_TEXT_CHARS:
            break
    out['text'], out['backend'], out['pages'] = best_text, best_backend, best_pages
    if not best_text:
        if not out['available']:
            out['reason'] = '未安装任何 PDF 解析库，请执行 pip install pypdf'
        elif out['tried']:
            out['reason'] = ('解析库均未能抽出文本，可能是扫描件/图片型 PDF（需 OCR）：'
                             + '；'.join(out['tried'][:3]))
        else:
            out['reason'] = 'PDF 无可抽取文本'
    elif len(best_text) < MIN_TEXT_CHARS:
        out['reason'] = 'PDF 文本仅 %d 字符，可能内容很短或部分页面为图片' % len(best_text)
    return out


def bytes_to_text(raw: bytes, max_chars: int = 20000) -> str:
    """兼容旧调用：只返回抽出的文本。"""
    return extract_text_ex(raw, max_chars)['text']


def download_and_extract(url: str, cfg: dict,
                         timeout: float = 0) -> tuple[str, dict]:
    """下载 + 抽文本，返回 (text, meta)。任何失败都不抛异常。"""
    raw, meta = _download_meta(url, cfg, float(timeout or cfg.get('timeout_s') or 30))
    meta['reason'] = meta.get('reason') or ''
    if not raw:
        return '', meta
    ex = extract_text_ex(raw, int(cfg.get('max_chars') or 20000),
                         prefer=str(cfg.get('backend') or ''))
    meta.update({'backend': ex['backend'], 'pages': ex['pages'],
                 'tried': ex['tried'], 'available': ex['available']})
    if ex['reason']:
        meta['reason'] = ex['reason']
    return ex['text'], meta


def text_from_result_ex(recipe, result, *, use_cache: bool = True) -> tuple[str, str, dict]:
    """返回 (PDF 纯文本, PDF 直链, 诊断信息)。任何失败都不抛异常。"""
    cfg = pdf_config(recipe)
    if not cfg.get('enabled'):
        return '', '', {'reason': '配方未启用 PDF 正文'}
    url = pdf_url_from_html(cfg, result.text or '', getattr(result, 'url', '') or '')
    if not url:
        return '', '', {'reason': '详情页未解析出 PDF 直链'}
    if use_cache:
        hit = _cache_get(url)
        if hit:
            return hit, url, {'backend': 'cache', 'reason': '', 'url': url}
    text, meta = download_and_extract(url, cfg)
    if text and use_cache:
        _cache_put(url, text)
    return text, url, meta


def text_from_result(recipe, result, *, use_cache: bool = True) -> tuple[str, str]:
    """兼容旧调用：只返回 (text, url)。"""
    text, url, _ = text_from_result_ex(recipe, result, use_cache=use_cache)
    return text, url


if __name__ == '__main__':
    import json
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else ''
    if not u:
        print('用法: python -m server.bidcrawl.pdf_text <详情页URL>')
        raise SystemExit(1)
    t0 = time.monotonic()
    cfg = {'enabled': True}
    r = httpx.get(u, headers={'User-Agent': UA}, timeout=25, trust_env=False)
    pdf_url = pdf_url_from_html(cfg, r.text, u)
    print('pdf_url =', pdf_url)
    if pdf_url:
        text, meta = download_and_extract(pdf_url, cfg)
        print('meta =', json.dumps(meta, ensure_ascii=False))
        print('chars =', len(text), 'ms =', int((time.monotonic() - t0) * 1000))
        print(text[:600])
