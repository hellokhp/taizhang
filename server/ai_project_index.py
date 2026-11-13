# -*- coding: utf-8 -*-
"""开标汇总 AI · 项目选择器的轻量索引 + 服务端模糊搜索（Python 3.8 兼容）。

为什么需要它
------------
老实现 `SELECT rid, payload FROM rows ORDER BY updated_at DESC LIMIT 500`
把每一行的完整台账 JSON（48 字段）都读进内存并 json.loads，再整体交给前端本地过滤。
项目数上万后会同时踩三个坑：
  1. LIMIT 500 硬截断 —— 500 条之外的老项目前端根本拿不到，搜不到；
  2. 每次请求读全部 payload —— 网络 + 反序列化随项目数线性增长；
  3. 前端持有全量列表 —— 首屏体积与内存都线性增长。

这里改成服务端搜索 + 分页：
  · 进程内只缓存搜索必需的轻量字段（rid / 项目名 / 折叠名 / SELF 范围人员 / 是否有招标文件目录），
    不缓存 payload，重建时按 payload 提取后即丢弃；
  · 用 (rows 行数, MAX(updated_at), 目录表行数, 目录表 MAX(created_at)) 做指纹，
    台账或目录没变化就零 DB 读；变化才全量重建一次（copy-on-write，读路径无锁）；
  · 模糊匹配口径与 frontend/src/lib/fuzzy.js 完全一致（子串优先 + 子序列兜底 +
    多关键词 AND + 相关度排序），返回 nameRanges / ridRanges 给前端做高亮，
    前端不再需要下载全量项目，也不用重复实现匹配算法。

部署前提：supervisor 用 --workers 1（单进程），缓存是进程级的，与 main.py 的
进程级 lock / 单连接模型一致。若将来改多 worker，每个 worker 各持一份索引、
靠指纹各自失效，结果仍然正确，只是内存 ×N。

线程模型：FastAPI 同步接口在线程池执行，多线程可能并发搜/重建。
  - 读：先在无锁下取 (版本, 列表) 快照，用自己的快照过滤，期间即使被替换也不受影响；
  - 写：拿 lock 单飞重建，构造好新列表后整体替换引用（原子赋值）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---- 与 frontend/src/lib/fuzzy.js 严格对齐的匹配口径 ----
_RE_SPACE = re.compile(r'\s+')
_RE_BOUNDARY = re.compile(r'[\s（(\/、，,·_\-—:：]')


def _lower(s: Any) -> str:
    """只做大小写折叠，不改变字符数（中文不受影响，便于高亮区间对齐）。"""
    return str(s if s is not None else '').lower()


def tokenize(query: Any) -> List[str]:
    return [t for t in _RE_SPACE.split(_lower(query).strip()) if t]


def _substr_hit(hay: str, needle: str) -> Optional[Dict[str, Any]]:
    i = hay.find(needle)
    if i < 0:
        return None
    if i == 0 and len(hay) == len(needle):
        score = 1000
    elif i == 0:
        score = 640
    else:
        score = 420 - min(i, 240)
        if i > 0 and _RE_BOUNDARY.match(hay[i - 1]):
            score += 60
    return {'score': score, 'ranges': [[i, i + len(needle)]]}


def _subseq_hit(hay: str, needle: str) -> Optional[Dict[str, Any]]:
    if not hay or not needle:
        return None
    ti = 0
    start = -1
    prev = -1
    gaps = 0
    ranges: List[List[int]] = []
    for i in range(len(hay)):
        if ti >= len(needle):
            break
        if hay[i] == needle[ti]:
            if start < 0:
                start = i
            if prev >= 0 and i - prev > 1:
                gaps += i - prev - 1
            if ranges and ranges[-1][1] == i:
                ranges[-1][1] = i + 1
            else:
                ranges.append([i, i + 1])
            prev = i
            ti += 1
    if ti < len(needle):
        return None
    span = prev - start + 1
    return {'score': 120 - gaps * 2 - span * 0.3, 'ranges': ranges}


def match_one(text: Any, token: str) -> Optional[Dict[str, Any]]:
    hay = _lower(text)
    needle = _lower(token)
    if not needle:
        return None
    return _substr_hit(hay, needle) or _subseq_hit(hay, needle)


def merge_ranges(ranges: Sequence[Sequence[int]]) -> List[List[int]]:
    if not ranges:
        return []
    sorted_r = sorted((list(r) for r in ranges), key=lambda x: x[0])
    out: List[List[int]] = [sorted_r[0]]
    for r in sorted_r[1:]:
        last = out[-1]
        if r[0] <= last[1]:
            last[1] = max(last[1], r[1])
        else:
            out.append(r)
    return out


def split_by_ranges(text: Any, ranges: Sequence[Sequence[int]]) -> List[Dict[str, Any]]:
    """把字符串按区间切成 [{text, hit}]，前端直接渲染，避免它再做 Unicode 索引切片。"""
    s = str(text if text is not None else '')
    if not ranges:
        return [{'text': s, 'hit': False}]
    parts: List[Dict[str, Any]] = []
    pos = 0
    for r in ranges:
        a, b = int(r[0]), int(r[1])
        if a > pos:
            parts.append({'text': s[pos:a], 'hit': False})
        parts.append({'text': s[a:b], 'hit': True})
        if b > pos:
            pos = b
    if pos < len(s):
        parts.append({'text': s[pos:], 'hit': False})
    return parts


def in_scope(item: Dict[str, Any], user_names: Sequence[str]) -> bool:
    """行级范围判定，与 main.row_in_scope 严格同口径（不折叠大小写）：
    项目的 lead/copilot/reviewer 任一命中「当前用户账号 + 显示名」即可见。"""
    return any(p and p in user_names for p in item['persons'])


def score_item(item: Dict[str, Any], tokens: Sequence[str]) \
        -> Optional[Tuple[float, List[List[int]], List[List[int]]]]:
    """对一条索引项打分。返回 (score, nameRanges, ridRanges)，未命中返回 None。"""
    name = item['nameFold']
    rid = item['ridFold']
    total = 0.0
    name_ranges: List[List[int]] = []
    rid_ranges: List[List[int]] = []
    for tok in tokens:
        a = match_one(name, tok)
        b = match_one(rid, tok)
        if not a and not b:
            return None
        total += max(a['score'] if a else float('-inf'), b['score'] if b else float('-inf'))
        if a:
            name_ranges.extend(a['ranges'])
        if b:
            rid_ranges.extend(b['ranges'])
    return (total + (5 if item.get('hasTenderDir') else 0),
            merge_ranges(name_ranges), merge_ranges(rid_ranges))


class ProjectIndex:
    """进程级项目索引。db 由调用方注入（ai_summary_api 的 PgDB / 测试用 FakeDB）。

    只依赖两条只读 SQL，均用 `?` 占位符（PgDB 会转 %s）：
      · 指纹：rows 与 bid_fs_dir 的 (COUNT, MAX(时间))；
      · 重建：rows 的 (rid, payload, updated_at) + bid_fs_dir 的 tender 标记。
    """

    def __init__(self, db: Any, lock: Any, person_fields: Sequence[str] = ('lead', 'copilot', 'reviewer')):
        self._db = db
        self._lock = lock
        self._person_fields = tuple(person_fields)
        self._items: List[Dict[str, Any]] = []
        self._sig: Optional[Tuple[Any, ...]] = None
        self._built_at = 0.0
        self._build_count = 0

    # ---------------- 指纹 / 重建 ----------------
    def _fingerprint(self) -> Tuple[Any, ...]:
        r = self._db.execute(
            'SELECT (SELECT COUNT(*) FROM rows) AS n, '
            '(SELECT COALESCE(MAX(updated_at),0) FROM rows) AS ts, '
            '(SELECT COUNT(*) FROM bid_fs_dir) AS dn, '
            '(SELECT COALESCE(MAX(created_at),0) FROM bid_fs_dir) AS dts'
        ).fetchone()
        return (int(r['n']), float(r['ts']), int(r['dn']), float(r['dts']))

    def _build(self) -> List[Dict[str, Any]]:
        # 目录标记：只取有 tender_node_id 的 rid，列表小， avoids 每行 payload 解析
        have: set = set()
        try:
            for d in self._db.execute(
                    "SELECT rid FROM bid_fs_dir WHERE COALESCE(tender_node_id,'')<>''").fetchall():
                have.add(str(d['rid']))
        except Exception:
            pass

        items: List[Dict[str, Any]] = []
        rows = self._db.execute('SELECT rid, payload, updated_at FROM rows').fetchall()
        for r in rows:
            rid = str(r['rid'])
            raw = r['payload']
            try:
                d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                d = {}
            name = str(d.get('projectName') or '') or rid
            # 人员字段只做去空格、不做大小写折叠，和 main.row_in_scope 完全一致
            persons = tuple(str(d.get(f) or '').strip() for f in self._person_fields)
            try:
                updated = float(r['updated_at'] or 0)
            except Exception:
                updated = 0.0
            items.append({
                'rid': rid,
                'name': name,
                'nameFold': _lower(name),
                'ridFold': _lower(rid),
                'persons': persons,
                'hasTenderDir': rid in have,
                'updatedAt': updated,
            })
        # 默认顺序与老接口一致：updated_at DESC
        items.sort(key=lambda x: (-x['updatedAt'], x['rid']))
        self._build_count += 1
        self._built_at = time.time()
        return items

    def _ensure(self, force: bool = False) -> List[Dict[str, Any]]:
        """拿锁单飞重建；copy-on-write，读路径不受影响。

        `force` 只表示调用方在锁外看到指纹变了；**真正是否重建以锁内指纹为准**。
        这样并发请求先后发现同一份数据变化时，后到的线程直接复用先到线程的构建结果，
        不会排队各做一次全量重建。
        """
        with self._lock:
            sig = self._fingerprint()
            if self._sig == sig and self._items:
                return self._items
            new_items = self._build()
            self._items = new_items
            self._sig = sig
            return new_items

    def snapshot(self) -> Tuple[Tuple[Any, ...], List[Dict[str, Any]]]:
        """无锁读快照。调用方用同一个列表处理完本次请求，期间替换不影响。"""
        return self._sig or (), self._items

    # ---------------- 搜索 ----------------
    def search(self, query: str, offset: int, limit: int,
               scope: str = 'ALL', names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """返回 {projects, total, limit, offset, hasMore, indexed, stale, builtAt}。

        scope='SELF' 时用 names（账号 + 显示名）做行级过滤，与 main.row_in_scope 同口径；
        前端只拿到当前页 + 命中高亮区间，不再持有全量项目。
        """
        tokens = tokenize(query)
        stale = False
        # 快速路径：先在无锁快照上试一次；若指纹变了再重建后重算。
        sig, items = self.snapshot()
        try:
            fresh_sig = self._fingerprint()
            if fresh_sig != sig:
                stale = True
        except Exception:
            fresh_sig = sig  # 指纹查询失败不阻断搜索，用当前快照兜底

        user_names: Tuple[str, ...] = ()
        if scope == 'SELF':
            user_names = tuple(str(n or '').strip() for n in (names or ()) if str(n or '').strip())

        def run(source: List[Dict[str, Any]]) -> Tuple[List[Tuple[float, int, Dict[str, Any], List, List]], int]:
            hits: List[Tuple[float, int, Dict[str, Any], List, List]] = []
            for idx, it in enumerate(source):
                if scope == 'SELF' and not in_scope(it, user_names):
                    continue
                if tokens:
                    m = score_item(it, tokens)
                    if m is None:
                        continue
                    score, nr, rr = m
                else:
                    score, nr, rr = 0.0, [], []
                hits.append((score, idx, it, nr, rr))
            if tokens:
                hits.sort(key=lambda h: (-h[0], len(h[2]['name']), h[1]))
            return hits, len(hits)

        if stale:
            items = self._ensure(force=True)
        hits, total = run(items)

        # 先按前端过滤口径重排（有 query 时 score 已带 hasTenderDir +5）
        page = hits[offset:offset + limit]
        projects: List[Dict[str, Any]] = []
        for score, idx, it, nr, rr in page:
            projects.append({
                'rid': it['rid'],
                'name': it['name'],
                'hasTenderDir': it['hasTenderDir'],
                'nameParts': split_by_ranges(it['name'], nr if tokens else []),
                'ridParts': split_by_ranges(it['rid'], rr if tokens else []),
            })
        return {
            'projects': projects,
            'total': total,
            'limit': limit,
            'offset': offset,
            'hasMore': (offset + len(projects)) < total,
            'indexed': len(items),          # 索引内项目总数（权限过滤前），供 UI 显示规模
            'stale': stale,                 # 本次是否因数据变化重建（诊断用）
            'builtAt': self._built_at,
        }

    # ---------------- 诊断 ----------------
    def stats(self) -> Dict[str, Any]:
        return {
            'indexed': len(self._items),
            'signature': list(self._sig) if self._sig else None,
            'builtAt': self._built_at,
            'buildCount': self._build_count,
            'bytesApprox': sum(len(i['name']) + len(i['rid']) + 64 for i in self._items),
        }
