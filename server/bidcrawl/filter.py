# -*- coding: utf-8 -*-
"""按配置做保留 / 剔除判定。

输入是 normalize 之后的条目；输出只含判定结果，不写库。
被剔除的条目不入库，只计入 bid_crawl_run.dropped。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import buyer_matches
from .normalize import LOCAL_TZ, parse_datetime


@dataclass
class Decision:
    keep: bool
    reason: str = ''
    rule: str = ''
    detail: str = ''


def recipe_filters(recipe) -> dict:
    """取配方的 normalize.filters（兼容 CompiledRecipe 与原始 dict）。

    配方即插件：平台特有口径只写在配方里，代码里不出现站点分支。
    """
    n = recipe.normalize if hasattr(recipe, 'normalize') else \
        (recipe.get('normalize') if isinstance(recipe, dict) else {})
    f = (n or {}).get('filters') or {}
    return f if isinstance(f, dict) else {}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ('false', '0', 'off', 'no', '')
    return bool(v)


class RuleEngine:
    def __init__(self, snapshot, platform_id: str, *, filters: dict | None = None):
        self.platform_id = platform_id
        self.buyers = [(b['pattern'], b.get('match_type') or 'contains')
                       for b in snapshot.buyers]
        # 金融机构自建采购门户（银行/保险/证券/信托/银联）的采购人天然合规，
        # 由配方声明跳过全局采购人白名单；未声明时保持开启，其他平台行为不变。
        cfg = filters or {}
        override = cfg.get('override') if isinstance(cfg.get('override'), dict) else {}
        flag = cfg.get('buyer_whitelist', override.get('buyer_whitelist', True))
        self.buyer_whitelist_enabled = _truthy(flag)
        self.rules = []
        for r in snapshot.rules:
            value = r.get('value') or {}
            if isinstance(value, str):
                value = {}
            item = {'name': r['name'], 'rule_type': r['rule_type'], 'value': value,
                    'scope': r.get('scope')}
            if r['rule_type'] in ('title_regex', 'url_regex', 'buyer_regex'):
                try:
                    item['_re'] = re.compile(str(value.get('regex') or ''))
                except re.error:
                    continue
            elif r['rule_type'] == 'notice_type':
                types = value.get('types') or []
                if isinstance(types, str):
                    types = [types]
                item['_types'] = [str(t) for t in types if str(t).strip()]
            elif r['rule_type'] == 'age_days':
                try:
                    item['_days'] = int(value.get('days'))
                except Exception:
                    continue
            self.rules.append(item)

    def evaluate(self, item: dict, now=None) -> Decision:
        title = str(item.get('title') or '')
        buyer = str(item.get('buyer') or '')
        url = str(item.get('url') or '')
        now = now or datetime.now(LOCAL_TZ)

        for rule in self.rules:
            rtype = rule['rule_type']
            hit = False
            detail = ''
            if rtype == 'title_regex':
                hit = rule['_re'].search(title)
                detail = rule['_re'].pattern
            elif rtype == 'url_regex':
                hit = rule['_re'].search(url)
                detail = rule['_re'].pattern
            elif rtype == 'buyer_regex':
                hit = bool(buyer) and rule['_re'].search(buyer)
                detail = rule['_re'].pattern
            elif rtype == 'notice_type':
                hit = any(t and t in title for t in rule['_types'])
                detail = ','.join(rule['_types'])
            elif rtype == 'age_days':
                publish = item.get('publish_at')
                publish = parse_datetime(publish) if publish and not isinstance(publish, datetime) else publish
                if publish:
                    if publish.tzinfo is None:
                        publish = publish.replace(tzinfo=LOCAL_TZ)
                    hit = (now - publish) > timedelta(days=rule['_days'])
                    detail = '%s 天' % rule['_days']
            if hit:
                return Decision(False, 'excluded', rule['name'], detail)

        if not self.buyer_whitelist_enabled:
            return Decision(True, 'buyer_whitelist_off')

        if self.buyers:
            if not buyer:
                # 采购人为空时白名单无法判定，宁可保留也不误杀（页面会显示覆盖率）
                return Decision(True, 'buyer_missing')
            for pattern, mtype in self.buyers:
                if buyer_matches(pattern, buyer, mtype):
                    return Decision(True, 'buyer_hit', '', pattern)
            return Decision(False, 'buyer_not_in_whitelist')

        return Decision(True)


def preview(items: list[dict], snapshot, platform_id: str,
            filters: dict | None = None) -> dict:
    """用给定条目离线重跑过滤，返回增减条数与样例（配置「影响预览」）。"""
    engine = RuleEngine(snapshot, platform_id, filters=filters)
    kept, dropped = [], []
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        d = engine.evaluate(item)
        row = {'title': item.get('title'), 'buyer': item.get('buyer'),
               'url': item.get('url'), 'publish_at': item.get('publish_at')}
        if d.keep:
            kept.append(row)
        else:
            row['reason'] = d.reason
            row['rule'] = d.rule
            dropped.append(row)
    return {'total': len(items), 'kept': len(kept), 'dropped': len(dropped),
            'samples': dropped[:10],
            'buyer_whitelist_enabled': engine.buyer_whitelist_enabled,
            'config_version': snapshot.version}
