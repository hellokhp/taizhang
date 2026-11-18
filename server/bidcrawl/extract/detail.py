# -*- coding: utf-8 -*-
"""详情页字段抽取（HTML / JSON / AI 正文解析三种来源）。

- scope=html：从详情页 HTML 用 css/xpath/regex 抽（本地夹具与老配方兼容）
- scope=json：从详情接口 JSON 抽；可用 detail.row_path 先定位到公告对象
- detail.ai.enabled：再把正文（noticeContent 之类）清洗成纯文本交给 LLM，
  结果按正文 hash 缓存；模型不可用时自动降级为正则，不阻断采集

三种来源的结果按「确定性字段 → AI 字段」顺序合并，AI 只在必要字段上覆盖。
- detail.pdf.enabled：正文在 PDF 文件里时，由 pdf_text.py 下载并转成文本，
  再走同一套 AI / 正则抽取；PDF 直链会写进 detail jsonb 便于追溯。

附件内嵌图片 / 扫描件的 OCR 仍留待后续，纯文本 PDF 已支持。
"""
from __future__ import annotations

import json

from parsel import Selector


def _detail_row(recipe, result) -> tuple[dict, str]:
    """按 detail.row_path 定位公告对象，返回 (row, 用于正则的 text)。"""
    detail = recipe.detail if hasattr(recipe, 'detail') else (recipe.get('detail') or {})
    data = result.data
    if not isinstance(data, dict):
        try:
            data = json.loads(result.text or '{}')
        except Exception:
            return {}, result.text or ''
    row_path = detail.get('row_path')
    if row_path:
        from ..recipe import dotted_get, jsonpath_get
        got = (jsonpath_get(data, str(row_path)) if str(row_path).startswith('$')
               else dotted_get(data, str(row_path)))
        if isinstance(got, dict):
            return got, json.dumps(got, ensure_ascii=False)
    return data, json.dumps(data, ensure_ascii=False)


def extract_detail(recipe, result, base_item: dict, *, ai: bool = True) -> dict:
    detail = recipe.detail if hasattr(recipe, 'detail') else (recipe.get('detail') or {})
    cfg = detail.get('extract') or {}
    scope = str(cfg.get('scope') or 'html')
    fields = cfg.get('fields') or {}
    ai_cfg = detail.get('ai') or {}
    if not fields and not ai_cfg.get('enabled'):
        return {}

    row = dict(base_item or {})
    selector = None
    if scope in ('json', 'json_api'):
        got, text = _detail_row(recipe, result)
        row.update(got)
    elif scope in ('html', 'html_page'):
        text = result.text or ''
        selector = Selector(text=text)
    else:
        text = result.text or ''

    from ..recipe import resolve_field

    out = {}
    # PDF 型详情页：把解析出的直链放进 row，配方可用 {"path":"pdf_url"} 引用，
    # 同时无条件写进 detail jsonb，便于追溯正文来源。
    pdf_cfg = detail.get('pdf') or {}
    if pdf_cfg.get('enabled'):
        from ..pdf_text import pdf_url_from_html
        purl = pdf_url_from_html(pdf_cfg, text, getattr(result, 'url', '') or '')
        if purl:
            row['pdf_url'] = purl
            out['pdf_url'] = purl

    for key, spec in fields.items():
        if not isinstance(spec, dict):
            continue
        val = resolve_field(spec, row, text=text, selector=selector)
        if val:
            out[key] = val

    if ai and ai_cfg.get('enabled'):
        from ..detail_ai import extract as ai_extract
        ctx = {'title': out.get('title') or (base_item or {}).get('title'),
               'buyer': out.get('buyer') or (base_item or {}).get('buyer'),
               'url': (base_item or {}).get('url')}
        try:
            extra = ai_extract(recipe, result, ctx)
        except Exception:
            extra = {}
        for key, val in (extra or {}).items():
            if not val:
                continue
            if key == '_ai_meta':
                out[key] = val          # 透传给 runner，用于统计与 detail.ai 审计
                continue
            if key.startswith('_'):
                continue
            out[key] = val
    return out
