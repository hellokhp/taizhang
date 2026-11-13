"""汇总表字段定义。

改动这里即可增删/调整汇总表的列 —— 不需要改数据库，也不需要改代码逻辑。
  key      : 内部标识
  label    : 表头
  type     : text / money / number / date / bool / enum
  aliases  : 用于在原文中定位该字段的关键词
  hint     : 写给模型的业务口径（非常重要，模型不知道你们行业的默认规则）
  width    : Excel 列宽
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FieldDef:
    key: str
    label: str
    type: str = "text"
    aliases: list[str] = field(default_factory=list)
    hint: str = ""
    required: bool = True
    width: int = 16


# ------------------------------------------------------------------ 项目级
PROJECT_FIELDS: list[FieldDef] = [
    FieldDef("project_name", "项目名称", aliases=["项目名称", "工程名称", "采购项目名称"],
             hint="招标文件封面或招标公告上的项目全称", width=34),
    FieldDef("project_code", "项目编号", aliases=["项目编号", "招标编号", "采购编号", "标段编号"],
             hint="原文中的编号，原样照录，不要改动格式", width=22),
    FieldDef("tenderer", "招标人（采购人）", aliases=["招标人", "采购人", "建设单位", "业主"],
             hint="招标/采购单位全称", width=26),
    FieldDef("agency", "招标代理机构", aliases=["代理机构", "招标代理"],
             hint="没有就返回 null", required=False, width=24),
    FieldDef("open_time", "开标时间", aliases=["开标时间", "投标截止时间", "开标日期"],
             hint="原文照录，不要格式化改写", width=20),
    FieldDef("open_place", "开标地点", aliases=["开标地点", "开标室"], required=False, width=24),
    FieldDef("budget", "预算/最高限价",
             aliases=["最高限价", "预算金额", "招标控制价", "采购预算", "项目规模", "项目预算"],
             type="money", hint="转为以元为单位的数字", required=False, width=16),
    FieldDef("eval_method", "评标办法", aliases=["评标办法", "评标方法", "评审方法", "评标方式",
                                                 "评分标准", "评标委员会"],
             required=False, hint="如综合评分法、最低评标价法", width=18),
]

# ------------------------------------------------------------------ 投标人级
BIDDER_FIELDS: list[FieldDef] = [
    FieldDef("bidder_name", "投标人名称", aliases=["投标人", "投标单位", "投标人全称", "供应商名称"],
             hint="必须与封面/投标函落款处完全一致的公司全称", width=30),
    FieldDef("bid_price", "投标报价（元）", aliases=["投标报价", "投标总价", "总报价", "报价合计",
                                                    "开标一览", "报价汇总", "投标函"],
             type="money",
             hint="优先取「开标一览表」中的投标总价；若多处出现且数值不同，全部列进 candidates，"
                  "不要自行判断哪个正确。单位统一换算为元。如为费率/下浮率报价，value 填折算后的金额，"
                  "并在 note 说明原文写法", width=18),
    FieldDef("price_in_words", "报价大写", aliases=["大写", "人民币大写", "金额大写"],
             required=False, hint="原文照录", width=22),
    FieldDef("duration", "工期/服务期", aliases=["工期", "交货期", "服务期", "履约期限", "完成期限"],
             hint="原文照录，如「合同签订后90日历天」", width=20),
    FieldDef("quality", "质量标准", aliases=["质量标准", "质量要求", "质量目标"],
             required=False, hint="如「合格」「符合国家验收标准」", width=18),
    FieldDef("deposit", "投标保证金（元）", aliases=["投标保证金", "保证金"],
             type="money", required=False, hint="金额，单位元；若原文只写「已递交」无金额，value 填 null 并在 note 说明", width=16),
    FieldDef("legal_person", "法定代表人", aliases=["法定代表人", "法人代表"],
             required=False, width=14),
    FieldDef("authorized_rep", "授权代表/联系人", aliases=["授权代表", "委托代理人", "联系人", "项目经理"],
             required=False, width=16),
    FieldDef("contact", "联系方式", aliases=["联系电话", "联系方式", "手机号码"],
             required=False, width=18),
    FieldDef("qualification", "资格响应", aliases=["资格", "资质", "营业执照", "响应情况"],
             required=False, hint="判断资格条件是否满足，取值为「满足」/「不满足」/「存疑」之一", width=12),
    FieldDef("reject_reason", "废标/否决项", aliases=["废标", "否决", "无效投标", "不予受理"],
             required=False, hint="仅在明确记载废标/否决情形时填写，否则填 null", width=22),
    FieldDef("remark", "备注", aliases=["备注", "说明", "其他承诺"],
             required=False, hint="其他需要评审委员会关注的事项，没有就填 null", width=26),
]

PROJECT_MAP = {f.key: f for f in PROJECT_FIELDS}
BIDDER_MAP = {f.key: f for f in BIDDER_FIELDS}
