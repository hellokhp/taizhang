"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- 文件项
@dataclass
class FileItem:
    path: Path
    rel: str                      # 相对开标文件夹的路径
    name: str
    ext: str
    size: int
    md5: str = ""
    kind: str = "OTHER"           # TENDER_DOC / OPENING_RECORD / PRICE_SHEET / QUALIFICATION / PUBLICITY / BID_FILE / OTHER
    bidder: str = ""              # 归属的投标人（阶段3结果）
    page_count: int = 0
    is_scanned: bool = False
    markdown: str = ""
    status: str = "NEW"           # NEW / PARSED / FAILED / SKIPPED
    error: str = ""

    @property
    def priority(self) -> int:
        """越小越优先喂给模型。公示属于终态信息，优先级最低。"""
        order = {
            "OPENING_RECORD": 0,
            "TENDER_DOC": 1,
            "PRICE_SHEET": 2,
            "BID_FILE": 3,
            "QUALIFICATION": 4,
            "OTHER": 5,
            "PUBLICITY": 7,
        }
        return order.get(self.kind, 9)


# ---------------------------------------------------------------- 字段值
@dataclass
class FieldValue:
    key: str
    label: str
    value: Any = None
    display: str = ""
    confidence: float = 0.0
    source_file: str = ""
    source_page: str = ""
    quote: str = ""
    candidates: list[dict] = field(default_factory=list)   # 同一字段的多来源候选
    conflict: bool = False
    note: str = ""
    manual: bool = False

    @property
    def level(self) -> str:
        from bidsummary.config import CONFIG

        if self.conflict or self.value in (None, ""):
            return "red"
        if self.confidence >= CONFIG.green:
            return "green"
        if self.confidence >= CONFIG.yellow:
            return "yellow"
        return "red"

    def to_display(self) -> str:
        if self.display:
            return self.display
        if self.value is None or self.value == "":
            return "未找到"
        return str(self.value)


@dataclass
class BidderRow:
    bidder: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 开标记录表（多行）
@dataclass
class PricePart:
    """一个报价区间（如「<10（含）元的含税单价合计」）。"""
    label: str
    col: int
    score: float = 0.0          # 该区间权重分，如 17
    min_price: float | None = None
    avg_price: float | None = None
    benchmark: float | None = None


@dataclass
class BidRow:
    """开标记录表里的一行 = 一家投标人。"""
    name: str
    seq: int
    src_row: int = 0                     # 在原表里的行号（0 基），用于追溯
    parts: list[float | None] = field(default_factory=list)   # 各区间的报价
    tax_rate: float | None = None
    duration: str = ""
    net_parts: list[float | None] = field(default_factory=list)  # 不含税
    incl_parts: list[float | None] = field(default_factory=list)  # 各分项含税对照价（多品类时）
    total: float | None = None          # 评标价（各区间合计 / 总报价）
    total_net: float | None = None
    incl_total: float | None = None     # 含税总价（单价比值法时用于展示对照）
    organizer_score: float | None = None   # 招标人自查得分（表内已有排名块时用于对照）
    part_scores: list[float] = field(default_factory=list)
    price_score: float | None = None
    rank: int = 0
    deviation: float | None = None      # 相对评标基准价的偏差率
    status: str = "正常"
    flags: list[str] = field(default_factory=list)


@dataclass
class LotResult:
    """一个标段（一个 sheet）。"""
    lot_name: str
    lot_no: str = ""
    open_time: str = ""
    source_file: str = ""
    sheet: str = ""
    header_row: int = 0
    parts: list[PricePart] = field(default_factory=list)
    incl_cols: list[int] = field(default_factory=list)   # 多品类时各分项的含税对照列
    part_labels: list[str] = field(default_factory=list)  # 各分项品类核心名（如「门楣发光字类」）
    tax_col: int = -1
    duration_col: int = -1
    name_col: int = 0
    rows: list[BidRow] = field(default_factory=list)
    use_net: bool = False               # 是否按不含税价评标
    priced_by: str = "含税单价合计"      # 表头提示
    pricing: str = "multi"              # multi=多区间单价合计 / single=单一总报价
    price_col: int = -1                 # 单一总报价时的评标价列
    incl_col: int = -1                  # 单一总报价时的含税价列
    signature: str = ""                 # 用于跨表去重
    itemized: bool = False              # 分项各自计分（各分项独立基准价 + Fn±偏差率×E1/E2）
    trimmed: list[str] = field(default_factory=list)   # 未参与基准价计算的投标人
    bench_n: int = 0                    # 参与基准价计算的家数
    bench_factor_used: float = 0.0      # 实际使用的平均值系数（未剔除时可能为 1）


@dataclass
class PriceRule:
    """从招标文件抽取的价格评分规则。"""
    total_score: float = 0.0
    method: str = ""
    formula: str = ""
    benchmark: str = ""
    benchmark_factor: float = 0.9
    parts: list[dict] = field(default_factory=list)   # [{"label":..., "score": 17}]
    note: str = ""
    source_file: str = ""
    source_page: str = ""
    confidence: float = 0.0
    behavior: str = ""          # ratio / deviation / proportional / full_deviation
    base_score: float = 0.0     # deviation 法的基本分（报价等于基准价时的得分）
    step_percent: float = 1.0   # deviation 法：偏差率每 1% 对应的增减分值
    low_limit: float = 0.0      # deviation 法：低于基准价超过该百分比后改为在满分基础上扣分
    step_high: float = 0.0      # full_deviation：报价高于参考值每 1% 的扣分
    step_low: float = 0.0       # full_deviation：报价低于参考值每 1% 的扣分
    itemized_e1: float = 0.0    # itemized_deviation：报价高于分项基准价每 1% 的扣分系数 E1
    itemized_e2: float = 0.0    # itemized_deviation：报价低于/等于分项基准价每 1% 的系数 E2
    benchmark_scope: str = ""   # 入围评标基准价计算范围（如「商务技术前60%入围」）
    trim_tiers: list[dict] = field(default_factory=list)
                                # full_deviation：按有效投标人家数分档去极值
                                # [{"max_n":5,"high":0,"low":0}, {"max_n":9,"high":1,"low":1}, ...]
    trim_low: int = 0           # 基准价计算时剔除最低报价家数
    trim_high: int = 0          # 基准价计算时剔除最高报价家数
    trim_guard: int = 0         # 有效投标人 >= 该家数时才执行剔除（0=不限制）
    confirmed: bool = False     # 招标文件是否明确写出了可计算的分值/公式
    basis: str = ""             # 判定说明
    evidence: str = ""          # 核对用的原文原句


@dataclass
class PublicityCandidate:
    """公示名单中的一条（一个中标候选人）。"""
    rank_label: str                  # 原文的名次表述，如「第一中标候选人」
    company: str
    lot_name: str = ""               # 所属包件/标段（原文有则填）
    price: float | None = None       # 公示中列出的报价（原文有则填）


@dataclass
class PublicityInfo:
    """中标候选人公示 —— 由千问视觉识别、代码核对，不参与任何计算。"""
    source_file: str = ""
    publish_date: str = ""
    notice_period: str = ""
    project_name: str = ""
    lot_name: str = ""
    candidates: list[PublicityCandidate] = field(default_factory=list)
    note: str = ""                   # 模型对公示内容的补充说明
    matches_project: bool = True     # 是否属于本文件夹的项目
    confidence: float = 0.0
    checks: list[str] = field(default_factory=list)   # 代码生成的核对结论


# ---------------------------------------------------------------- 入围单价矩阵（模式C）
@dataclass
class SLProduct:
    """权重附表里的一个产品（一行）。"""
    seq: str                        # 附表序号，如 "1"、"107"
    name: str                       # 产品名称
    unit: str = ""                  # 单位
    spec: str = ""                  # 尺寸与工艺
    cap: float | None = None        # 最高投标限价（不含税）
    weight: float = 0.0             # 权重（0~100）


@dataclass
class SLCategory:
    """一个报价类别（一张权重附表），如「标识标牌和营销设施」「门楣灯箱」。"""
    name: str
    source_file: str = ""
    score: float = 0.0              # 该类别价格分分值，如 14、21
    products: list[SLProduct] = field(default_factory=list)
    cap_weighted: float | None = None   # 限价加权合计 Σ 限价×权重（评标价口径的满额参照）
    benchmark: float | None = None      # 评标基准价（各有效投标人所报评标价的最低值）
    benchmark_holder: str = ""          # 基准价提供者（报价最低的投标人）
    benchmark_n: int = 0                # 参与基准价比较的家数


@dataclass
class SLBidder:
    """一个投标单位在单价矩阵里的报价与得分。"""
    name: str
    source_file: str = ""
    prices: dict[str, float] = field(default_factory=dict)    # 附表序号 -> 投标单价
    quoted: int = 0                       # 报价产品数
    missing: list[str] = field(default_factory=list)          # 未报价产品序号
    over_cap: list[str] = field(default_factory=list)         # 超最高限价产品序号
    # 各类别（按 SLCategory 顺序）：评标价 / 未加权单价合计 / 得分
    eval_prices: list[float | None] = field(default_factory=list)
    raw_sums: list[float | None] = field(default_factory=list)
    scores: list[float | None] = field(default_factory=list)
    total_score: float | None = None       # 价格分合计
    rank: int = 0
    flags: list[str] = field(default_factory=list)
    # 模式 E（分项加权最低价法）：偏离惩罚统计
    penalty_low: int = 0                  # X/Y < 下限，被判该项不得分的产品数
    penalty_high: int = 0                 # X/Y ≥ 上限，被判该项不得分的产品数
    per_item_scores: dict[str, float] = field(default_factory=dict)  # 产品序号 -> 该项得分


@dataclass
class ShortlistResult:
    """模式C整体结果：产品矩阵 + 评分依据 + 各投标单位汇总。

    variant:
      aggregate —— 原有模式 C：∑单价×权重得评标价，基准价/评标价×分值；
      per_item  —— 模式 E：投标人写在 Excel sheet 名里，按
                  「每项得分 =（该产品最低价/本家价）×满分×分项权重」逐项计分并求和。
    """
    categories: list[SLCategory] = field(default_factory=list)
    bidders: list[SLBidder] = field(default_factory=list)
    price_sheet_file: str = ""             # 投标单位单价表来源文件
    rule_text: str = ""                    # 投标报价评分标准原文（逐字）
    rule_detail: list[list[str]] = field(default_factory=list)   # 评分标准表（评分项/因素/标准）
    formula: str = ""                      # 得分公式（如 基准价/投标报价×分值）
    eval_price_rule: str = ""              # 评标价构成（如 Σ单价×权重）
    benchmark_rule: str = ""               # 基准价取法（如 最低值）
    rule_source_file: str = ""
    rule_source_page: str = ""
    confirmed: bool = False
    basis: str = ""                        # 判定说明
    warnings: list[str] = field(default_factory=list)
    variant: str = "aggregate"             # aggregate | per_item
    full_score: float = 0.0                # 价格部分满分（per_item）
    price_basis: str = ""                  # 计价口径：含税价 / 不含税价
    deviation_low: float = 0.0             # X/Y 下限（低于判该项不得分）
    deviation_high: float = 0.0            # X/Y 上限（不低于判该项不得分）
    tax_rule: str = ""                     # 税率口径原文
    penalty_items: list[dict] = field(default_factory=list)  # 偏离惩罚明细
    sheet_source_file: str = ""            # sheet 名投标人来源工作簿
    sheet_names: list[str] = field(default_factory=list)     # 被采纳的 sheet 名


# ---------------------------------------------------------------- 下浮率/折扣项目（模式D）
@dataclass
class DiscountProduct:
    """招标文件「项目需求」清单里的一行：单价最高限价 + 预估采购量。"""
    seq: str
    name: str
    spec: str = ""
    unit: str = ""
    cap: float = 0.0                # 单价最高限价（元，含税）
    qty: float = 0.0                # 预估采购量
    source_page: str = ""


@dataclass
class DiscountBidder:
    """开标一览表里的一家投标人：只报一个综合下浮率 / 折扣率。"""
    name: str
    rate: float = 0.0               # 原文数字（%）：下浮率 7.86 或 折扣率 67.00
    discount: float = 0.0           # 评审价：下浮率 → 1−rate/100；折扣率 → rate/100
    review_price: float = 0.0       # 评审价 = 折扣
    deviation: float | None = None  # 相对基准价的偏差（百分点，带符号）
    price_score: float | None = None
    unit_sum: float = 0.0           # ∑ 各项折算单价
    est_total: float = 0.0          # ∑ 限价×预估量×折扣（按预估量的结算价）
    per_100: float = 0.0            # 每 100 元市场价的实际结算额（元）
    rank: int = 0
    in_benchmark: bool = False      # 是否参与基准价计算（被去高/去低剔除的为 False）
    valid: bool = True              # 报价是否在有效区间（0~100% / 不超过最高限价）
    over_cap: bool = False          # 报价折扣率超过招标文件最高限价
    publicity_rank: int = 0         # 公示里的入围候选位次（0=未列入）
    publicity_rate: float | None = None   # 公示里该家的报价折扣率（用于与开标表核对）
    flags: list[str] = field(default_factory=list)


@dataclass
class DiscountResult:
    """模式D整体结果：折扣报价 + 单价折算/结算换算 + 价格评审得分。

    一条模式 D 支持两种「报价口径 × 评分规则」组合，绝不混用：
      rebate        + deduct    —— 苏州烟草：报下浮率，折扣=1−下浮率，满分±偏差扣分
      discount_rate + deviation —— 周口烟草：直接报折扣率，基本分±偏差增减分
    """
    rate_kind: str = "rebate"           # rebate | discount_rate
    score_kind: str = "deduct"          # deduct | deviation
    products: list[DiscountProduct] = field(default_factory=list)
    bidders: list[DiscountBidder] = field(default_factory=list)
    full_score: float = 50.0            # 报价部分满分
    base_score: float = 0.0             # 基本分（deviation 专用，如 35）
    low_limit: float = 0.0              # 低于基准价超过该幅度后由加分转扣分（deviation）
    step: float = 1.0                   # 每 1% 偏差加减分值（deviation）
    benchmark: float | None = None      # 评标基准价（有效报价平均，>guard 家去高去低）
    benchmark_n: int = 0
    trim_high: int = 0
    trim_low: int = 0
    trim_guard: int = 7                 # 有效报价人数超过该家数才剔除
    above_deduct: float = 1.0           # 每高于基准价 1% 扣的分（deduct）
    below_deduct: float = 0.5           # 每低于基准价 1% 扣的分（deduct）
    cap_total: float = 0.0              # ∑ 限价×预估量
    stated_cap: float | None = None     # 招标文件写明的项目最高限价（用于交叉核对）
    cap_limit_pct: float | None = None  # 最高限价（折扣率报价%，如 98）
    discount_def: str = ""              # 折扣定义原文，如「折扣=1-下浮率」
    review_price_def: str = ""          # 评审价口径原文，如「本项目评审价=折扣」
    unit_price_def: str = ""            # 中标单价口径原文
    settlement_def: str = ""            # 结算金额公式原文，如「结算金额=市场价格×入围折扣率×数量」
    benchmark_rule: str = ""
    formula: str = ""
    rule_text: str = ""                 # 「价格评审」章节原文（逐字）
    rule_detail: list[list[str]] = field(default_factory=list)
    rule_source_file: str = ""
    rule_source_page: str = ""
    opening_file: str = ""
    product_file: str = ""
    publicity_file: str = ""            # 入围候选供应商公示来源文件
    publicity_n: int = 0                # 公示列出的入围候选供应商家数
    llm_pages: list[str] = field(default_factory=list)   # 本次实际喂给千问的页码
    confirmed: bool = False
    basis: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class SummaryResult:
    project: dict[str, FieldValue] = field(default_factory=dict)
    rows: list[BidderRow] = field(default_factory=list)          # 模式A：按投标人文件夹
    lots: list[LotResult] = field(default_factory=list)          # 模式B：开标记录表多行
    shortlist: ShortlistResult | None = None                     # 模式C：入围单价矩阵
    discount: DiscountResult | None = None                       # 模式D：下浮率/折扣报价
    rule: PriceRule = field(default_factory=PriceRule)
    files: list[FileItem] = field(default_factory=list)
    failed_files: list[dict] = field(default_factory=list)
    excluded_files: list[dict] = field(default_factory=list)     # 疑似其他项目的干扰文件
    warnings: list[str] = field(default_factory=list)            # 需要人工注意的全局提示
    publicity: list[PublicityInfo] = field(default_factory=list)  # 公示核对信息（可多份）
    tokens: dict[str, int] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        if self.discount:
            return "discount"
        if self.shortlist:
            return "shortlist"
        return "opening_table" if self.lots else "bidder_folder"
