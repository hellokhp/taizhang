# -*- coding: utf-8 -*-
"""全局配置入口：PostgreSQL 连接的唯一事实源。

后端主进程 / AI Worker / 标讯采集 CLI & Worker 都从这里读取，避免同一份
PG 参数散落在多个 .py、.env、supervisor environment 里。

读取优先级（后者覆盖前者）：
  1. 内置兜底默认值（仅在 config.json 不存在时起作用）
  2. server/config.json 的 postgres 段   ← 日常改配置只改这一个文件
  3. 进程环境中显式设置的 PG_*（向后兼容 / 临时覆盖，项目自带配置已不再设置）

可用 TAIZHANG_CONFIG 指定配置文件的绝对路径（多实例部署）。
纯标准库实现，Python 3.8（后端）与 3.10+（AI Worker）都能加载。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SERVER_DIR / 'config.json'

_DEFAULTS = {'host': '127.0.0.1', 'port': 5432, 'user': 'postgres',
             'password': 'admin123.', 'dbname': 'yadecai'}
_KEYS = ('host', 'port', 'user', 'password', 'dbname')
_ENV = {'host': 'PG_HOST', 'port': 'PG_PORT', 'user': 'PG_USER',
        'password': 'PG_PASSWORD', 'dbname': 'PG_DB'}

_lock = threading.Lock()
_cache = None


def config_path():
    p = os.environ.get('TAIZHANG_CONFIG', '').strip()
    return Path(p).expanduser() if p else DEFAULT_CONFIG_PATH


def _from_file():
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        raise RuntimeError('统一配置文件解析失败：%s（%s）' % (path, e))
    if not isinstance(data, dict):
        raise RuntimeError('统一配置文件必须是 JSON 对象：%s' % path)
    sec = data.get('postgres') or data.get('pg') or {}
    if not isinstance(sec, dict):
        raise RuntimeError('postgres 段必须是对象：%s' % path)
    return {k: sec[k] for k in _KEYS if k in sec}


def _stamp():
    p = config_path()
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), 0, 0)


def pg_conf(force=False):
    """返回 psycopg2.connect(**...) 可直接使用的连接参数副本。

    按文件 mtime + size 缓存：改完 config.json，新连接立即读到新值
    （已建立的连接需重启进程才会重建）。
    """
    global _cache
    stamp = _stamp()
    with _lock:
        if not force and _cache and _cache[0] == stamp:
            return dict(_cache[1])
        conf = dict(_DEFAULTS)
        conf.update(_from_file())
        for key, env_name in _ENV.items():
            val = os.environ.get(env_name)
            if val is not None and str(val).strip():
                conf[key] = val
        try:
            conf['port'] = int(conf['port'])
        except (TypeError, ValueError):
            raise RuntimeError('PG 端口必须是整数，当前值：%r' % (conf['port'],))
        conf['password'] = str(conf['password'] or '')
        _cache = (stamp, dict(conf))
        return dict(conf)


_REDIS_DEFAULTS = {'host': '127.0.0.1', 'port': 6379, 'password': '', 'db': 0}
_REDIS_KEYS = ('host', 'port', 'password', 'db')
_REDIS_ENV = {'host': 'REDIS_HOST', 'port': 'REDIS_PORT',
              'password': 'REDIS_PASSWORD', 'db': 'REDIS_DB'}


def redis_conf():
    """返回 redis.Redis(**...) 可直接使用的连接参数。

    与 pg_conf 同源（同一份 config.json），但独立缓存戳，避免互相干扰。
    config.json 无 redis 段时用内置默认值；REDIS_* 环境变量可临时覆盖。
    """
    path = config_path()
    sec = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('redis'), dict):
                sec = {k: data['redis'][k] for k in _REDIS_KEYS if k in data['redis']}
    except Exception as e:
        raise RuntimeError('统一配置文件解析失败（redis 段）：%s（%s）' % (path, e))
    conf = dict(_REDIS_DEFAULTS)
    conf.update(sec)
    for key, env_name in _REDIS_ENV.items():
        val = os.environ.get(env_name)
        if val is not None and str(val).strip():
            conf[key] = val
    try:
        conf['port'] = int(conf['port'])
        conf['db'] = int(conf['db'])
    except (TypeError, ValueError):
        raise RuntimeError('REDIS 端口/db 必须是整数')
    conf['password'] = str(conf['password'] or '')
    return conf


def lock_backend():
    """返回锁后端：'local'（进程内，默认）或 'redis'（分布式）。

    优先级：LOCK_BACKEND 环境变量 > config.json 的 lock_backend 段 > 'local'。
    默认 local 保证阶段 2 改动对现有部署零行为变化；生产多进程时改配置即可。
    """
    env = os.environ.get('LOCK_BACKEND', '').strip().lower()
    if env in ('local', 'redis'):
        return env
    try:
        if config_path().is_file():
            data = json.loads(config_path().read_text(encoding='utf-8'))
            v = str((data or {}).get('lock_backend', '')).strip().lower()
            if v in ('local', 'redis'):
                return v
    except Exception:
        pass
    return 'local'


def pg_password():
    """供日志脱敏使用：从统一配置取密码，而不是从环境变量。"""
    return pg_conf()['password']


def pg_summary():
    """脱敏摘要，可安全写日志。"""
    c = pg_conf()
    return '%s:%s/%s（user=%s，密码%s）' % (
        c['host'], c['port'], c['dbname'], c['user'],
        '已配置' if c['password'] else '未配置')


if __name__ == '__main__':
    import sys

    print('配置文件：%s' % config_path())
    print('生效连接：%s' % pg_summary())
    try:
        import psycopg2
        conn = psycopg2.connect(connect_timeout=3, **pg_conf())
        cur = conn.cursor()
        cur.execute('SELECT version()')
        print('连通性：OK（%s）' % str(cur.fetchone()[0]).split(',')[0])
        cur.close()
        conn.close()
    except Exception as e:
        print('连通性：失败（%s：%s）' % (type(e).__name__, str(e)[:120]))
        sys.exit(1)
