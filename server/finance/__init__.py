# -*- coding: utf-8 -*-
"""财务部业务域（方案 B 框架 · 新模块参照）。

依赖方向：finance → common（只进不出）。
基建（db / lock / 鉴权 / 审计）全部复用 server/common/，本包不自建。
路由通过 finance/api.py 的 finance_router 接入 main.py，双前缀挂载。
"""
