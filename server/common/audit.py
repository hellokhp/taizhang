# -*- coding: utf-8 -*-
"""字段级审计（从 main.py 原样抽出，函数体逐字不变）。"""
import time

from .catalog import FIELD_CATALOG, FIELD_KEYS
from .database import db

FIELD_LABELS = {k: l for k, l, g in FIELD_CATALOG}


def audit_write(entries):
    """写入审计日志（调用方须持有 lock）。
    entries = [(username, rid, action, field, old, new, allowed)]"""
    if not entries:
        return
    db.executemany(
        'INSERT INTO audit_log(ts, username, rid, action, field, old_value, new_value, allowed) '
        'VALUES(?,?,?,?,?,?,?,?)',
        [(time.time(), u, rid, act, f, '' if ov is None else str(ov),
          '' if nv is None else str(nv), 1 if ok else 0)
         for u, rid, act, f, ov, nv, ok in entries])
    # 只保留最近 5000 条，避免库无限增长
    db.execute('DELETE FROM audit_log WHERE id NOT IN '
               '(SELECT id FROM audit_log ORDER BY id DESC LIMIT 5000)')


def audit_diff(username, rid, old, new, action='update'):
    """对比新旧值，仅记录实际变更的字段。"""
    entries = []
    for k in FIELD_KEYS:
        ov = (old or {}).get(k, '')
        nv = (new or {}).get(k, '')
        if str(ov or '') == str(nv or ''):
            continue
        entries.append((username, rid, action, k, ov, nv, True))
    return entries
