# -*- coding: utf-8 -*-
"""字段正则配置：把「一个字段 + 一堆匹配词」拼成完整正则。

为什么要这一层：配方里的 detail.ai.fallback_regex 是整条正则，改一个词（比如
给「预算」补上「项目规模」）就得逐个平台改配方，还得懂正则。现在把匹配词提到
`bid_regex_alias`，模板（含 {alts} 占位）放在 `bid_regex_field`，运行时拼装：

    (?:{alts})[：:\\s]*([^\\n，。；]{1,40})
      ↓  alts = 预算|项目规模|合同估算价…
    (?:预算|项目规模|合同估算价|…)[：:\\s]*([^\\n，。；]{1,40})

优先级（高 → 低）：配置中心 > 配方手写 fallback_regex > 内置 DEFAULT_FALLBACK_REGEX。
所以页面上加一个词，所有平台下一次抽取立即生效，不用动配方。

多进程注意：worker 是独立进程，API 侧改了配置无法直接清它的缓存，
故这里用 60 秒 TTL，改完最多一分钟生效。
"""
from __future__ import annotations

import re
import time

from . import db

CACHE_TTL_S = 60
_cache: dict = {}          # platform_id -> (ts, {field_key: 正则})


def _alias_order(item) -> tuple:
    """平台级别名优先，其次长词优先（「投标文件递交截止时间」要排在「递交截止时间」前）。"""
    scope, alias = item[0], item[1]
    return (0 if scope != 'global' else 1, -len(alias))


def build_rules(fields: list[dict], aliases: list[dict], platform_id: str = '') -> tuple[dict, dict]:
    """返回 ({field_key: 正则}, {field_key: 错误})。

    任何一个字段拼装失败只影响它自己：跳过并记错误，其余字段照常工作。
    """
    by_field: dict = {}
    for a in aliases:
        scope = str(a.get('scope') or 'global')
        if scope not in ('global', platform_id or ''):
            continue
        by_field.setdefault(a['field_key'], []).append((scope, str(a['alias'] or '')))

    rules, errors = {}, {}
    for f in fields:
        key = str(f['field_key'])
        tpl = str(f.get('pattern_tpl') or '')
        if not tpl:
            continue
        if '{alts}' not in tpl:
            # 纯固定句式（没有可配置的匹配词），原样使用
            try:
                re.compile(tpl)
                rules[key] = tpl
            except re.error as e:
                errors[key] = '模板不是合法正则：%s' % e
            continue
        items = by_field.get(key) or []
        seen = []
        for _scope, alias in sorted(items, key=_alias_order):
            a = alias.strip()
            if a and a not in seen:
                seen.append(a)
        if not seen:
            continue
        alts = '|'.join(re.escape(a) for a in seen)
        pat = tpl.replace('{alts}', alts)
        try:
            re.compile(pat)
        except re.error as e:
            errors[key] = '拼装后的正则不合法：%s' % e
            continue
        rules[key] = pat
    return rules, errors


def load_rules(platform_id: str = '', force: bool = False) -> dict:
    """给抽取用的 {field_key: 正则}，带进程内 TTL 缓存（按平台分别计时）。"""
    now = time.time()
    hit = _cache.get(platform_id)
    if not force and hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        fields = db.q('SELECT field_key, pattern_tpl FROM bid_regex_field '
                      'WHERE enabled ORDER BY sort_no, field_key')
        aliases = db.q('SELECT field_key, alias, scope FROM bid_regex_alias '
                       'WHERE deleted_at IS NULL AND enabled '
                       'ORDER BY field_key, sort_no, id')
        rules, _errors = build_rules([dict(x) for x in fields],
                                     [dict(x) for x in aliases], platform_id)
    except Exception:
        # 配置表读不到（老库/迁移中）不能影响采集：当作没有配置，走配方与内置
        rules = {}
    _cache[platform_id] = (now, rules)
    return rules


def invalidate():
    """本进程内立即失效（worker 进程仍靠 TTL）。"""
    _cache.clear()


def describe(platform_id: str = '') -> tuple[list, dict]:
    """给页面用：字段 + 别名明细 + 拼好的正则 + 错误。"""
    fields = [dict(x) for x in db.q(
        'SELECT field_key, label, pattern_tpl, value_hint, enabled, sort_no '
        'FROM bid_regex_field ORDER BY sort_no, field_key')]
    aliases = [dict(x) for x in db.q(
        'SELECT id, field_key, alias, scope, enabled, sort_no FROM bid_regex_alias '
        'WHERE deleted_at IS NULL ORDER BY field_key, sort_no, id')]
    enabled_aliases = [a for a in aliases if a.get('enabled')]
    rules, errors = build_rules([f for f in fields if f.get('enabled')],
                                enabled_aliases, platform_id)
    grouped: dict = {}
    for a in aliases:
        grouped.setdefault(a['field_key'], []).append(a)
    out = []
    for f in fields:
        item = dict(f)
        item['aliases'] = grouped.get(f['field_key'], [])
        item['regex'] = rules.get(f['field_key'], '')
        item['error'] = errors.get(f['field_key'], '')
        out.append(item)
    return out, errors
