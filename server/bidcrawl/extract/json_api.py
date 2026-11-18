# -*- coding: utf-8 -*-
"""JSON 接口列表抽取（jsonpath-ng）。"""
from __future__ import annotations

import json

from ..transport import FetchError


def _rows(recipe, result):
    data = result.data
    if data is None:
        raise FetchError('响应不是合法 JSON：%s' % result.url[:160], result.status, result.url)
    if recipe.items_path is None:
        return data if isinstance(data, list) else [data]
    rows = []
    for m in recipe.items_path.find(data):
        val = m.value
        if isinstance(val, list):
            rows.extend(val)
        elif val is not None:
            rows.append(val)
    return rows


def extract_items(recipe, result):
    from . import build_item

    items = []
    for raw in _rows(recipe, result):
        if isinstance(raw, dict):
            row = dict(raw)
            text = json.dumps(raw, ensure_ascii=False)
        else:
            row = {'_value': raw}
            text = str(raw or '')
        item = build_item(recipe.fields, row, text=text)
        if item.get('title') or item.get('url'):
            items.append(item)
    return items


def extract_total(recipe, result):
    """按 pagination.total_path 取总数；取不到返回 None。"""
    path = (recipe.pagination.get('total_path') or '').strip()
    if not path or result.data is None:
        return None
    try:
        import jsonpath_ng
        hit = jsonpath_ng.parse(path).find(result.data)
        if hit:
            return int(hit[0].value)
    except Exception:
        return None
    return None
