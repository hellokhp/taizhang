# -*- coding: utf-8 -*-
"""HTML 列表页抽取（parsel / lxml）。"""
from __future__ import annotations

from parsel import Selector

from ..transport import FetchError


def _rows(recipe, text):
    sel = Selector(text=text)
    expr = str(recipe.discovery.get('row_selector') or '')
    if not expr:
        raise FetchError('html_list 配方缺少 row_selector')
    if expr.startswith('/') or expr.startswith('./'):
        return sel, sel.xpath(expr)
    return sel, sel.css(expr)


def row_context(row) -> dict:
    """行级模板上下文：整行文本 `_text` + 行内第一个可用链接 `_href`。

    配方里的 template 字段（如 `https://host{_href}`）靠它渲染；
    抽取、样本回测、试跑校验必须用同一份上下文，否则「校验不过但实际能采」。
    """
    text = row.get() or ''
    pre = {'_text': text.strip()}
    first_a = row.css('a::attr(href)').get() or row.xpath('.//a/@href').get() or ''
    if first_a:
        pre['_href'] = first_a.strip()
    return pre


def extract_items(recipe, result):
    from . import build_item

    if not result.text:
        raise FetchError('响应为空：%s' % result.url[:160], result.status, result.url)
    _sel, rows = _rows(recipe, result.text)
    items = []
    for row in rows:
        text = row.get() or ''
        # 给模板提供两个通用占位：整行文本与行内第一个链接
        pre = row_context(row)
        item = build_item(recipe.fields, pre, selector=row, text=text)
        url = str(item.get('url') or '').strip()
        # 空链接/锚点链接（隐藏行、广告位、占位符）不是公告，别入库
        if url.startswith(('#', 'javascript:', 'mailto:')):
            continue
        if item.get('title') or url:
            items.append(item)
    return items
