# -*- coding: utf-8 -*-
"""财务模块 Pydantic 模型（请求/响应结构）。

把校验逻辑放这里，api.py 只引用模型，不内联一堆字段判断。
"""
from pydantic import BaseModel


class ItemIn(BaseModel):
    title: str = ''
    amount: float = 0
    remark: str = ''


class ItemOut(BaseModel):
    id: int
    title: str
    amount: float
