# -*- coding: utf-8 -*-
"""提示词版本化 + 四层拼装 + few-shot 按版式检索（§14.2 / §14.5）。

核心：把 vendor `bidsummary/llm.py` 里 7 个硬编码 `SYS_*` 外置成 DB 版本，
按「稳定 system + 数据驱动字段契约 + 按 format_sig 检索的 few-shot + 当次文档」拼装。
DB 表没有 active 版本时，自动用代码常量 seed 一条 v1（纯工程化、行为不变）。
"""
from __future__ import annotations

import json
import logging
import time

from . import db, format_sig

log = logging.getLogger('taizhang.ai.worker.prompt')

SCENES = ('extractor', 'price_rule', 'price_review', 'sheet_judge',
          'price_rule_general', 'vision_transcribe', 'vision')


def _vendor_systems():
    """从 vendor 包读 7 个 SYS_* 常量作为 seed（不在后端 3.8 进程里 import）。"""
    from bidsummary import llm
    return {
        'extractor': llm.SYS_EXTRACTOR,
        'price_rule': llm.SYS_RULE_PARSER,
        'price_review': llm.SYS_PRICE_REVIEW_PARSER,
        'sheet_judge': llm.SYS_SHEET_BIDDER_JUDGE,
        'price_rule_general': llm.SYS_PRICE_RULE_GENERAL,
        'vision_transcribe': llm.SYS_VISION_TRANSCRIBE,
        'vision': llm.SYS_VISION_EXTRACTOR,
    }


def ensure_seed():
    """首次启动把 7 个 SYS_* 灌进 ai_prompt_version（ON CONFLICT DO NOTHING）。"""
    systems = _vendor_systems()
    now = time.time()
    for scene, text in systems.items():
        db.x('INSERT INTO ai_prompt_version(scene, version, system_prompt, status, '
             "note, created_by, created_at, activated_at) "
             "VALUES(%s,'v1',%s,'active',%s,'seed',%s,%s) "
             'ON CONFLICT(scene, version) DO NOTHING',
             (scene, text, '从 vendor llm.py 的 SYS_* 常量迁入（行为不变）', now, now))
    db.commit()


def active_version(scene):
    row = db.one("SELECT version, system_prompt, user_tmpl FROM ai_prompt_version "
                 "WHERE scene=%s AND status='active' ORDER BY id DESC LIMIT 1", (scene,))
    if row:
        return row
    sysmap = _vendor_systems()
    return {'version': 'code-default', 'system_prompt': sysmap.get(scene, ''),
            'user_tmpl': None}


def _field_contract():
    """L2 数据驱动字段契约：字段字典 aliases/hint + 人工挖掘的别名。"""
    try:
        tpls = db.q('SELECT scope, fields FROM ai_field_template')
        aliases = db.q('SELECT scope, field_key, alias FROM ai_field_alias WHERE enabled=1')
    except Exception:
        return ''
    amap = {}
    for a in aliases:
        amap.setdefault(a['scope'], {}).setdefault(a['field_key'], []).append(a['alias'])
    lines = ['【字段契约】以下字段的别名与业务口径以本段为准（金额单位一律「元」）：']
    for t in tpls:
        fields = t['fields']
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = []
        for f in (fields or []):
            key = f.get('key')
            if not key:
                continue
            als = amap.get(t['scope'], {}).get(key, [])
            seg = '- %s：%s' % (key, f.get('label') or '')
            if f.get('hint'):
                seg += '；口径：%s' % f['hint']
            if als:
                seg += '；原文也可能写作：%s' % '、'.join(als[:12])
            lines.append(seg)
    return '\n'.join(lines) if len(lines) > 1 else ''


def fewshots(scene, sig, limit=3):
    """按版式族指纹检索最相似的 few-shot；命中计数 +1 供淘汰低效样例。"""
    try:
        exact = db.q('SELECT id, input_excerpt, expected_json FROM ai_prompt_fewshot '
                     'WHERE scene=%s AND enabled=1 AND format_sig=%s '
                     'ORDER BY hits DESC, id DESC LIMIT %s', (scene, sig, limit))
        if len(exact) < limit:
            other = db.q('SELECT id, input_excerpt, expected_json FROM ai_prompt_fewshot '
                         'WHERE scene=%s AND enabled=1 AND format_sig<>%s '
                         'ORDER BY hits DESC, id DESC LIMIT %s',
                         (scene, sig, limit - len(exact)))
            exact = exact + other
        for r in exact:
            db.x('UPDATE ai_prompt_fewshot SET hits=hits+1 WHERE id=%s', (r['id'],))
        db.commit()
        return exact
    except Exception:
        db.rollback()
        return []


def build(scene, base_system, user_prompt, sig='', with_contract=True):
    """四层拼装，返回 (system_prompt, user_prompt, version)。

    L1 稳定 system（DB active 版本，缺则回落代码常量）
    L2 字段契约（数据驱动，可热更新）
    L3 few-shot（按 format_sig 检索 2-3 条，非全量注入）
    L4 当次文档（原样 user_prompt）
    """
    ver = active_version(scene)
    system = ver.get('system_prompt') or base_system or ''
    version = '%s:%s' % (scene, ver.get('version') or 'default')
    parts = []
    if with_contract:
        contract = _field_contract()
        if contract:
            parts.append(contract)
    if sig:
        shots = fewshots(scene, sig)
        if shots:
            blocks = []
            for i, s in enumerate(shots, 1):
                blocks.append('【样例 %d】原文片段：\n%s\n正确抽取：\n%s' % (
                    i, str(s['input_excerpt'] or '')[:600],
                    json.dumps(s['expected_json'], ensure_ascii=False, default=str)[:1200]))
            parts.append('【同类版式参考样例】以下仅供理解版式，不要照抄其中的公司名与金额：\n'
                         + '\n\n'.join(blocks))
    parts.append('【本次资料】\n' + (user_prompt or ''))
    if ver.get('user_tmpl'):
        tmpl = ver['user_tmpl']
        prompt = tmpl.replace('{contract}', '\n'.join(parts[:-1])).replace('{document}', user_prompt or '')
    else:
        prompt = '\n\n'.join(p for p in parts if p)
    return system, prompt, version


def resolve_system(scene, base_system=''):
    """只取 L1（给不便改 prompt 的调用点用，例如 vision_markdown）。"""
    return active_version(scene).get('system_prompt') or base_system
