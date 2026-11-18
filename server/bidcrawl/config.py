# -*- coding: utf-8 -*-
"""采集配置：关键词 / 采购人白名单 / 剔除规则的快照读取与缓存。

配置与配方分离：改词、改规则不触发重新解析，下一轮跑批直接生效。
缓存 TTL 30s；页面保存后调 invalidate() 可立即生效。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

from . import db

TTL = 30.0
_cache: dict[str, tuple[float, 'Snapshot']] = {}
_lock = threading.Lock()


@dataclass
class Snapshot:
    platform_id: str
    keywords: list = field(default_factory=list)
    buyers: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    version: int = 0
    loaded_at: float = 0.0

    def keyword_words(self) -> list[str]:
        return [k['word'] for k in self.keywords]

    def sample_word(self) -> str:
        for k in self.keywords:
            if k.get('is_sample'):
                return k['word']
        return self.keywords[0]['word'] if self.keywords else ''


def _version(rows: list) -> int:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return int(hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12], 16)


def _scope_priority(row, platform_id: str) -> int:
    return 0 if row.get('scope') == platform_id else 1


def _dedupe(rows: list, key: str, platform_id: str) -> list:
    """平台级覆盖全局：同名（不区分大小写）保留平台级，其余按 sort_no。"""
    rows = sorted(rows, key=lambda r: (_scope_priority(r, platform_id),
                                       int(r.get('sort_no') or 0), int(r.get('id') or 0)))
    seen = set()
    out = []
    for r in rows:
        k = str(r.get(key) or '').strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def load_snapshot(platform_id: str, *, force: bool = False) -> Snapshot:
    now = time.time()
    with _lock:
        hit = _cache.get(platform_id)
        if hit and not force and now - hit[0] < TTL:
            return hit[1]

    scope = ['global', platform_id]
    keywords = db.q(
        'SELECT id, word, scope, is_sample, sort_no FROM bid_keyword '
        'WHERE deleted_at IS NULL AND enabled AND scope = ANY(%s)', (scope,))
    buyers = db.q(
        'SELECT id, pattern, match_type, scope, sort_no FROM bid_buyer_pattern '
        'WHERE deleted_at IS NULL AND enabled AND scope = ANY(%s)', (scope,))
    rules = db.q(
        'SELECT id, name, rule_type, value, scope, sort_no FROM bid_exclude_rule '
        'WHERE deleted_at IS NULL AND enabled AND scope = ANY(%s)', (scope,))
    for r in rules:
        if isinstance(r.get('value'), str):
            try:
                r['value'] = json.loads(r['value'])
            except Exception:
                r['value'] = {}

    keywords = _dedupe(keywords, 'word', platform_id)
    buyers = _dedupe(buyers, 'pattern', platform_id)
    # 剔除规则按 name 覆盖：平台级同名的规则替换全局同名规则
    rules = _dedupe(rules, 'name', platform_id)

    snap = Snapshot(platform_id=platform_id, keywords=keywords, buyers=buyers,
                    rules=rules, version=_version(keywords + buyers + rules),
                    loaded_at=now)
    with _lock:
        _cache[platform_id] = (now, snap)
    return snap


def invalidate(platform_id: str | None = None):
    with _lock:
        if platform_id:
            _cache.pop(platform_id, None)
        else:
            _cache.clear()


# ------------------------------------------------------------------ 匹配与校验
def buyer_matches(pattern: str, name: str, match_type: str = 'contains') -> bool:
    if not pattern or name is None:
        return False
    name = str(name)
    if match_type == 'regex':
        try:
            return re.search(pattern, name) is not None
        except re.error:
            return False
    return str(pattern).lower() in name.lower()


def validate_entry(entity: str, data: dict) -> list[str]:
    """配置保存前校验；返回错误列表（空 = 通过）。"""
    errs = []
    if entity == 'keyword':
        word = str(data.get('word') or '').strip()
        if not word:
            errs.append('关键词不能为空')
        elif len(word) > 32:
            errs.append('关键词长度不能超过 32')
        if not str(data.get('scope') or 'global').strip():
            errs.append('作用域不能为空')
    elif entity == 'buyer':
        pattern = str(data.get('pattern') or '').strip()
        if not pattern:
            errs.append('匹配内容不能为空')
        if str(data.get('match_type') or 'contains') == 'regex':
            try:
                re.compile(pattern)
            except re.error as e:
                errs.append('正则非法：%s' % e)
    elif entity == 'rule':
        if not str(data.get('name') or '').strip():
            errs.append('规则名称不能为空')
        rtype = str(data.get('rule_type') or '')
        if rtype not in ('title_regex', 'notice_type', 'buyer_regex', 'age_days', 'url_regex'):
            errs.append('规则类型非法：%s' % rtype)
        value = data.get('value')
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                errs.append('规则值必须是合法 JSON')
                value = {}
        if rtype.endswith('_regex') or rtype == 'notice_type':
            regex = value.get('regex') if isinstance(value, dict) else ''
            types = value.get('types') if isinstance(value, dict) else None
            if rtype == 'notice_type' and not types:
                errs.append('公告类型规则必须提供 types 数组')
            elif rtype != 'notice_type':
                if not regex:
                    errs.append('正则规则必须提供 regex')
                else:
                    try:
                        re.compile(str(regex))
                    except re.error as e:
                        errs.append('正则非法：%s' % e)
        elif rtype == 'age_days':
            try:
                days = int((value or {}).get('days'))
                if days <= 0:
                    raise ValueError
            except Exception:
                errs.append('时间窗规则必须提供正整数 days')
    return errs


def audit(entity: str, entity_id, action: str, before, after, operator: str):
    db.x('INSERT INTO bid_config_audit(entity, entity_id, action, before, after, operator) '
         'VALUES (%s,%s,%s,%s,%s,%s)',
         (entity, entity_id, action,
          json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
          json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
          operator or ''))
    db.commit()
