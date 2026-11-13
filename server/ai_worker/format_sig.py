# -*- coding: utf-8 -*-
"""版式族指纹 format_sig（§14.3）。

注意区分：
- 内容指纹 `ai_summary_task.fingerprint` = md5(sorted sha|name)，用于「同批文件不重算」；
- 版式族指纹 `format_sig` = 结构特征短哈希，用于「按版式检索 few-shot 样例」。

两者用途完全不同，不能混。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter


def compute(items, mode='', prepared=None):
    """由解析后的 FileItem 列表 + 物化清单算 16 位版式族指纹。

    只取结构特征：文件类型组合、是否扫描、表头关键词、模式、列数量级。
    不含公司名、金额、项目编号 —— 避免版式族被内容打散，也避免原文特征外泄。
    """
    exts = Counter()
    kinds = Counter()
    scanned = 0
    header_hints = []
    for it in (items or []):
        ext = str(getattr(it, 'ext', '') or '').lower()
        exts[ext] += 1
        kinds[str(getattr(it, 'kind', '') or '')] += 1
        if getattr(it, 'is_scanned', False):
            scanned += 1
        md = str(getattr(it, 'markdown', '') or '')[:3000]
        for kw in ('投标报价', '投标总价', '开标一览表', '报价合计', '含税', '不含税',
                   '下浮率', '折扣率', '评标价', '基准价', '工期', '服务期',
                   '单价', '权重', '分项', '包件', '标段'):
            if kw in md and kw not in header_hints:
                header_hints.append(kw)
    shape = ''.join('%s:%d;' % (k, v) for k, v in sorted(exts.items()))
    kinds_s = ''.join('%s:%d;' % (k, v) for k, v in sorted(kinds.items()) if k)
    hints = ','.join(sorted(header_hints))
    n_files = len(prepared or items or [])
    bucket = 'S' if scanned else 'T'
    raw = '|'.join([mode or '', shape, kinds_s, hints, bucket,
                    'n%d' % (n_files // 5)])       # 文件数按 5 分桶，微小增减不改族
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]


def as_json(items, prepared=None):
    """供 ai_prompt_fewshot.file_kinds 存的结构特征（相似度兜底检索用）。"""
    return json.dumps({
        'exts': sorted(set(str(getattr(i, 'ext', '') or '').lower() for i in (items or []))),
        'kinds': sorted(set(str(getattr(i, 'kind', '') or '') for i in (items or []))),
        'scanned': sum(1 for i in (items or []) if getattr(i, 'is_scanned', False)),
        'n': len(prepared or items or []),
    }, ensure_ascii=False)
