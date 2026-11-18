# -*- coding: utf-8 -*-
"""列表 / 详情抽取：把不同形态的响应统一成扁平 dict。"""
from __future__ import annotations

from .json_api import extract_items as extract_json_items
from .html_list import extract_items as extract_html_items
from .detail import extract_detail

__all__ = ['extract_json_items', 'extract_html_items', 'extract_detail',
           'build_item', 'pick_items']


def build_item(fields: dict, row: dict, *, selector=None, text: str = '',
               base_url: str = '') -> dict:
    """按字段规则生成统一条目。

    先取非 template 字段，再取 template 字段：HTML 列表里 `{url}` 这类模板
    往往要引用同一行里刚抽出的链接/编号。
    """
    from ..recipe import resolve_field

    out = {}
    deferred = {}
    for key, spec in fields.items():
        # 纯 template 可能引用同一行里刚抽出的链接/编号，需延后；
        # {regex, template} 是「先抽分组再拼接」，不依赖同行的其他字段。
        if isinstance(spec, dict) and 'template' in spec and 'regex' not in spec:
            deferred[key] = spec
            continue
        val = resolve_field(spec, row, text=text, selector=selector, base_url=base_url)
        if val:
            out[key] = val
    ctx = dict(row)
    ctx.update(out)
    for key, spec in deferred.items():
        val = resolve_field(spec, ctx, text=text, selector=selector, base_url=base_url)
        if val:
            out[key] = val
    if row and isinstance(row, dict):
        out['_raw'] = row
    return out


def pick_items(recipe, result):
    """按 recipe.mode 分派，返回条目列表（已按配方字段摊平）。"""
    if recipe.mode == 'html_list':
        return extract_html_items(recipe, result)
    return extract_json_items(recipe, result)
