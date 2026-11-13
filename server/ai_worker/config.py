# -*- coding: utf-8 -*-
"""Worker 运行配置：只从环境变量读，绝不 import 后端 main（避免 3.8/3.10 依赖冲突）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent            # 项目根（taizhang/）

PG_CONF = {
    'host': os.environ.get('PG_HOST', '127.0.0.1'),
    'port': int(os.environ.get('PG_PORT', '5432')),
    'user': os.environ.get('PG_USER', 'postgres'),
    'password': os.environ.get('PG_PASSWORD', 'yinhe123.'),
    'dbname': os.environ.get('PG_DB', 'yadecai'),
}


def _storage_root() -> Path:
    env = os.environ.get('STORAGE_ROOT', '').strip()
    if env:
        return Path(env)
    if os.name == 'nt':
        return ROOT / 'storage'
    return Path('/var/lib/taizhang/storage')


STORAGE_ROOT = _storage_root()
BLOBS = STORAGE_ROOT / 'blobs'
AI_TMP = STORAGE_ROOT / 'ai_tmp'

REDIS_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
WAKE_KEY = 'taizhang:ai:wakeup'
TASK_CHANNEL = 'taizhang:ai:task:'

WORKER_ID = os.environ.get('AI_WORKER_ID', 'w1')
HEARTBEAT_STALE = int(os.environ.get('AI_HEARTBEAT_STALE', '300'))
MAX_ATTEMPTS = int(os.environ.get('AI_MAX_ATTEMPTS', '3'))
MAX_FILES = 500
MAX_FILE_MB = 200
TASK_TIMEOUT = 7200              # 单任务硬超时 2 小时
TMP_TTL = 86400                  # 物化临时目录保留 24 小时
# 单文件解析超时（秒）。扫描件逐页调视觉模型，一个坏文件就能把整个任务拖住，
# 超时后只把这个文件标 SKIPPED，其余文件继续，任务不至于永远停在 PARSING。
PARSE_FILE_TIMEOUT = int(os.environ.get('AI_PARSE_FILE_TIMEOUT', '300'))

# 进度阶段 -> 展示文案 / 百分比
STAGES = {
    'SCANNING': ('扫描并物化招标文件', 10),
    'PARSING': ('解析文件内容', 35),
    'GROUPING': ('归集投标人与标段', 55),
    'EXTRACTING': ('模型理解并抽取字段', 70),
    'VALIDATING': ('规则校验与价格分', 88),
    'SAVING': ('写入标准化数据库', 96),
}
