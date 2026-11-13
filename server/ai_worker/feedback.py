# -*- coding: utf-8 -*-
"""复核回流：从人工修正过的证据里挖掘「字段新叫法」，沉淀为待审别名（§14.4）。

后端 PATCH /cells 已负责把修正沉淀为 few-shot + 回归用例；
这里补最后一件宝 —— 别名字典，让「同族新表述」下次零 LLM 重跑即可命中。
挖出的别名默认 enabled=0（待管理员确认），避免误收污染提示词。
"""
from __future__ import annotations

import logging
import re
import time

from . import db

log = logging.getLogger('taizhang.ai.worker.feedback')

_ALIAS_RE = re.compile(r'[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9（）()／/·、]{1,18}')
_STOP = {'投标人', '招标人', '项目名称', '金额', '报价', '原文', '备注', '合计', '日期',
         '单位', '元', '如下', '说明', '未找到', '抽取失败', '扫描页', '本页', '无'}


def mine_aliases(summary_id):
    """扫描本汇总中 manual=1 的证据，从原文片段里挖候选别名。返回新增条数。"""
    rows = db.q("SELECT field_key, source_quote, value_text FROM ai_field_evidence "
                "WHERE summary_id=%s AND manual=1 AND source_quote IS NOT NULL "
                "AND source_quote<>'' LIMIT 200", (summary_id,))
    if not rows:
        return 0
    # 已有别名 + 字段字典里的标准叫法
    known = set()
    for a in db.q('SELECT alias FROM ai_field_alias'):
        known.add(str(a['alias']))
    for t in db.q('SELECT fields FROM ai_field_template'):
        f = t['fields']
        if isinstance(f, str):
            try:
                import json
                f = json.loads(f)
            except Exception:
                f = []
        for x in (f or []):
            known.add(str(x.get('label') or ''))
            known.update(str(v) for v in (x.get('aliases') or []))

    added = 0
    now = time.time()
    for r in rows:
        quote = str(r['source_quote'] or '')
        # 只取冒号/等号前的短语，或引号内短语 —— 那才是字段叫法，不是值
        segs = re.split(r'[:：=]', quote)[:1]
        segs += re.findall(r'[《「【]([^》」】]{2,20})[》」】]', quote)
        for seg in segs:
            for cand in _ALIAS_RE.findall(seg):
                cand = cand.strip()
                if (len(cand) < 2 or len(cand) > 18 or cand in known or cand in _STOP
                        or cand.isdigit() or cand == str(r['value_text'] or '').strip()):
                    continue
                known.add(cand)
                db.x('INSERT INTO ai_field_alias(scope, field_key, alias, source, '
                     'hit_count, enabled, created_at) '
                     "VALUES('BIDDER',%s,%s,'mined',0,0,%s) "
                     'ON CONFLICT(scope, field_key, alias) DO NOTHING',
                     (r['field_key'], cand, now))
                added += 1
                if added >= 40:
                    break
            if added >= 40:
                break
        if added >= 40:
            break
    db.commit()
    if added:
        log.info('[ai] 汇总 %s 挖掘出 %d 条待审别名', summary_id, added)
    return added
