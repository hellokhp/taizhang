# -*- coding: utf-8 -*-
"""把台账内容寻址的 blob 物化成「带原名 + 扩展名」的真实目录树。

台账磁盘上只有 `storage/blobs/xx/yy/<sha256>`，文件名与目录结构只存在于
fs_node；而 AI 的 scanner 靠目录名和扩展名做文件分类与投标人归属，
所以必须先还原。物化结果放 STORAGE_ROOT/ai_tmp/<task_id>/，与 fs 的
tmp/blobs 隔离，Worker 崩溃残留由 clean_old() 按 TTL 兜底。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import config

log = logging.getLogger('taizhang.ai.worker.materialize')

_BAD = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def _safe_rel(rel, name):
    """防御性复核路径穿越：上传时 fs.safe_name 已净化，这里再挡一层。"""
    rel = str(rel or '').replace('\\', '/').strip()
    parts = []
    for seg in rel.split('/'):
        seg = _BAD.sub('_', seg).strip('. ')
        if not seg or seg in ('.', '..'):
            continue
        parts.append(seg[:120])
    if not parts:
        parts = [_BAD.sub('_', str(name or 'file'))[:120] or 'file']
    # 目录层级也限深，避免恶意深目录
    return '/'.join(parts[:10])


def materialize(task_id, nodes):
    """返回 (work_dir, prepared, skipped)。

    prepared: [{node_id, rel_path, name, ext, size, sha256, disk_path}]（已落盘）
    skipped:  [{... , reason}]（超限/缺 blob，供 ai_file 记 SKIPPED）
    """
    work = config.AI_TMP / ('task_%s' % task_id)
    if work.exists():
        shutil.rmtree(str(work), ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    prepared, skipped = [], []
    total = 0
    for n in nodes[:config.MAX_FILES]:
        sha = str(n.get('sha256') or '')
        rel = _safe_rel(n.get('rel_path'), n.get('name'))
        size = int(n.get('size') or 0)
        if size > config.MAX_FILE_MB * 1024 * 1024:
            skipped.append({**n, 'reason': '超过 %dMB 上限' % config.MAX_FILE_MB})
            continue
        total += size
        if total > config.MAX_FILE_MB * 1024 * 1024 * 20:   # 单任务总量 4GB 硬上限
            skipped.append({**n, 'reason': '单任务文件总量超限'})
            continue
        if not sha or len(sha) < 4:
            skipped.append({**n, 'reason': '缺少 blob_sha'})
            continue
        src = config.BLOBS / sha[0:2] / sha[2:4] / sha
        if not src.is_file():
            skipped.append({**n, 'reason': 'blob 文件缺失'})
            continue
        dst = (work / rel).resolve()
        try:
            dst.relative_to(work.resolve())       # 最终防线：不许逃出 work
        except ValueError:
            skipped.append({**n, 'reason': '路径越界'})
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 硬链接优先（省磁盘、省时间）；Windows/跨卷失败则回退复制
            try:
                os.link(str(src), str(dst))
            except Exception:
                shutil.copyfile(str(src), str(dst))
        except Exception as e:
            skipped.append({**n, 'reason': '物化失败：%s' % str(e)[:80]})
            continue
        prepared.append({**n, 'rel_path': rel, 'disk_path': str(dst)})
    if len(nodes) > config.MAX_FILES:
        for n in nodes[config.MAX_FILES:]:
            skipped.append({**n, 'reason': '超过 %d 文件上限' % config.MAX_FILES})
    log.info('[ai] task %s 物化 %d 个文件，跳过 %d 个，目录 %s',
             task_id, len(prepared), len(skipped), work)
    return work, prepared, skipped


def clean(work):
    try:
        shutil.rmtree(str(work), ignore_errors=True)
    except Exception:
        pass


def clean_old(ttl=None):
    """清理超过 TTL 的物化残留（Worker 启动时调一次）。"""
    ttl = ttl or config.TMP_TTL
    root = config.AI_TMP
    if not root.exists():
        return 0
    n, cutoff = 0, time.time() - ttl
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith('task_'):
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(str(d), ignore_errors=True)
                n += 1
        except Exception:
            pass
    return n
