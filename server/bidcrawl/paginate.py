# -*- coding: utf-8 -*-
"""翻页与页宽探测。

运行时原则：优先用 size_max 一次取更多；服务端忽略页宽就按固定条数翻页；
超时/5xx 自动降档并记住本轮结果，不反复试探。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .extract import pick_items
from .extract.json_api import extract_total
from .transport import FetchError, FetchResult


class CancelledError(RuntimeError):
    """任务被用户取消，在安全点抛出。"""


@dataclass
class Page:
    page: int
    size: int | None
    items: list
    total: int | None
    raw: FetchResult


def _set_path(obj: dict, path: str, value):
    """按点号路径写值：页码常嵌在 pageInfo.current / dto.pageNo 里。"""
    parts = str(path).split('.')
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            return obj
        cur = nxt
    cur[parts[-1]] = value
    return obj


def _get_path(obj: dict, path: str):
    cur = obj
    for p in str(path).split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _inject(spec: dict, page: int, size: int | None, recipe) -> dict:
    """把 page/size 写进 params 或 body（模板里已有占位符时由格式化替换）。"""
    p = recipe.pagination
    size_param = p.get('size_param')
    page_param = p.get('page_param')
    spec = dict(spec)
    # 页码在 URL 路径里的站点（search_2.jspx）：第 1 页必须是「不带页码」的地址，
    # 其余页由 request.url 模板里的 {page} 渲染，因此这里只覆盖第 1 页。
    if p.get('page_in_url'):
        start = int(p.get('start') or 1)
        if page == start and p.get('first_url'):
            spec['url'] = p['first_url']
    for target in ('params', 'body'):
        obj = spec.get(target)
        if not isinstance(obj, dict):
            continue
        obj = dict(obj)
        if page_param and _get_path(obj, page_param) is not None:
            _set_path(obj, page_param, page)
        if size_param and size and _get_path(obj, size_param) is not None:
            _set_path(obj, size_param, size)
        spec[target] = obj
    return spec


def _key_fp(items: list) -> str:
    slim = [{k: v for k, v in it.items() if k != '_raw'} for it in items]
    return hashlib.sha1(json.dumps(slim, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def iter_pages(recipe, fetcher, keyword: str, *, size: int | None = None,
               max_pages: int | None = None, cancel=None, key_of=None):
    """逐页产出 Page；命中停止条件后自然结束。"""
    p = recipe.pagination
    ptype = str(p.get('type') or 'none')
    limit = 1 if ptype == 'none' else int(max_pages or p.get('max_pages') or 10)
    start = int(p.get('start') or 1)
    page = start
    use_size = recipe.effective_size() if size is None else size
    if not p.get('size_param'):
        use_size = None
    stop_when = set(p.get('stop_when') or ['empty_items', 'page_unchanged', 'reached_total'])

    prev_fp = ''
    seen_keys = set()
    downgrades = 0
    attempts = 0
    max_attempts = limit * 3 + 3

    while attempts < max_attempts:
        attempts += 1
        if cancel and cancel():
            raise CancelledError('用户取消')
        spec = _inject(recipe.request_spec(), page, use_size, recipe)
        ctx = {'keyword': keyword, 'page': page, 'size': use_size or 0}
        res = fetcher.request(spec, ctx)
        if not res.ok:
            # 超时 / 429 / 5xx：优先降档重试同一页，最多两档
            if res.status in (0, 429, 500, 502, 503, 504) and use_size and \
                    use_size > 20 and downgrades < 2:
                use_size = max(20, use_size // 2)
                downgrades += 1
                continue
            raise FetchError('第 %d 页请求失败（HTTP %s）：%s'
                             % (page, res.status, (res.error or '')[:200]),
                             res.status, res.url)

        items = pick_items(recipe, res)
        total = extract_total(recipe, res)
        yield Page(page=page, size=use_size, items=items, total=total, raw=res)

        if 'empty_items' in stop_when and not items:
            break
        fp = _key_fp(items)
        if 'page_unchanged' in stop_when and page > start and fp == prev_fp:
            break
        prev_fp = fp

        if key_of:
            keys = [key_of(it) for it in items]
            if 'all_duplicate' in stop_when and keys and \
                    all(k and k in seen_keys for k in keys):
                break
            seen_keys.update(k for k in keys if k)

        if ptype == 'none':
            break
        if 'reached_total' in stop_when and total is not None:
            width = use_size or len(items)
            if width and (page - start + 1) * width >= total:
                break
        if use_size and len(items) < use_size:
            break
        page += 1
        # max_pages 是硬上限：部分站点对超范围页码会回退到首页/末页，
        # 只靠 page_unchanged 判定会多翻页甚至绕过翻页预算。
        if ptype != 'none' and (page - start + 1) > limit:
            break

    if attempts >= max_attempts:
        raise FetchError('翻页超过安全次数上限（%d），已停止' % max_attempts)


def fetch_first(recipe, fetcher, keyword: str, size: int | None = None) -> Page:
    for pg in iter_pages(recipe, fetcher, keyword, size=size, max_pages=1):
        return pg
    raise FetchError('未取到任何页面')


def probe_size(recipe, fetcher, keyword: str) -> dict:
    """实测服务端页宽上限。

    返回 {'size_max': n, 'behavior': ok|truncate|ignore|error, 'probe': [...]}。
    判定口径：
      - 返回 4xx/5xx      → 超上限，取上一档
      - 返回数 < 请求数 且 total 明确大于返回数 → truncate，上限 = 返回数
      - 返回数 < 请求数 且无 total      → 视为数据不足，保持请求页宽（不误降档）
      - 返回数 == 请求数 → 该档有效，继续探下一档
      - 返回数 != 请求数且 total 不大于 → 服务端忽略页宽（ignore）
    """
    p = recipe.pagination
    if not p.get('size_param'):
        return {'size_max': None, 'behavior': 'ignore', 'probe': [],
                'note': '配方未声明 size_param，服务端固定条数'}
    sizes = [int(x) for x in (p.get('size_probe') or [20, 50, 100, 200, 500])]
    last_ok = None
    probe = []
    behavior = 'ok'
    for size in sizes:
        spec = _inject(recipe.request_spec(), int(p.get('start') or 1), size, recipe)
        res = fetcher.request(spec, {'keyword': keyword,
                                     'page': int(p.get('start') or 1), 'size': size})
        if not res.ok:
            probe.append({'size': size, 'status': res.status, 'items': 0})
            behavior = 'error' if size == sizes[0] else 'ok'
            break
        items = pick_items(recipe, res)
        n = len(items)
        total = extract_total(recipe, res)
        probe.append({'size': size, 'status': res.status, 'items': n, 'total': total})
        if n == size:
            last_ok = size
            continue
        if n < size:
            if total is not None and n < total:
                return {'size_max': n, 'behavior': 'truncate', 'probe': probe}
            if total is None and n > 0:
                # 没有总数时无法区分「截断」与「数据本来就少」：不擅自降档
                last_ok = size
                continue
            return {'size_max': n or (last_ok or sizes[0]), 'behavior': 'ignore',
                    'probe': probe}
        # n > size：服务端返回比请求更多，按实际值继续
        last_ok = n
    if behavior == 'error':
        return {'size_max': last_ok or sizes[0], 'behavior': 'error', 'probe': probe}
    cap = last_ok or sizes[0]
    return {'size_max': cap, 'behavior': behavior, 'probe': probe}


def apply_probe(recipe, result: dict):
    """把探测结果写回配方（内存对象），供保存新版本时落库。"""
    p = recipe.pagination
    if result.get('size_max'):
        p['size_max'] = int(result['size_max'])
        p['size'] = int(result['size_max'])
    p['size_cap_behavior'] = result.get('behavior') or p.get('size_cap_behavior') or 'ok'
