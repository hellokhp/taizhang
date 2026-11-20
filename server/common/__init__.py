# -*- coding: utf-8 -*-
"""平台公共内核 · 阶段 1（纯重构，行为零变化）。

本包只收纳被多个应用/模块共享的基础设施：
  config      —— PG 连接配置（薄封装，转发 shared_config，保持唯一事实源）
  database    —— 每线程连接 + PgDB 数据访问层 + 路径常量
  guard       —— 并发/超时/健康（薄转发，保证全进程单一实例）
  catalog     —— 字段/权限/角色目录（纯数据）
  auth_tables —— 用户/角色/会话建表与种子（纯函数）
  security    —— 鉴权与权限计算
  audit       —— 字段级审计

阶段 1 约束：被抽出的函数签名与函数体逐字保持不变；初始化副作用（init_db /
seed_roles / backfill_role_perms / sync_users / migrate_from_sqlite）仍由 server.main
在启动时调用，common 内不产生任何启动副作用。
"""
