"""模式 D：下浮率 / 折扣率报价项目（PDF 开标一览表 + PDF 招标文件）。

与模式 C 的区别：
    模式 C —— 投标人报「每个产品的单价」，评标价 = Σ 单价 × 权重；
    模式 D —— 投标人只报「一个百分比」，评审价由该百分比换算，价格分按招标文件
              「价格评审 / 评标办法前附表」章节的偏差公式计算。

同一条模式 D 支持两种「报价口径 × 评分规则」组合，二者绝不混用：
    ┌────────────────┬──────────────────────┬────────────────────────────────┐
    │ 组合           │ 报价口径             │ 评分规则                       │
    ├────────────────┼──────────────────────┼────────────────────────────────┤
    │ 苏州烟草       │ rebate 下浮率        │ deduct 满分 ± 偏差扣分          │
    │                │ 折扣 = 1 − 下浮率   │ 满分 − |偏差| × 系数            │
    │ 周口烟草       │ discount_rate 折扣率 │ deviation 基本分 ± 偏差增减分   │
    │                │ 评审价 = 报价率/100 │ 基本分加至满分，低超阈值转扣分  │
    └────────────────┴──────────────────────┴────────────────────────────────┘
把折扣率当下浮率会算出 0.08 这种完全错误的评审价；把增减分法套成满分扣分法
同样会给出看似合理的错分。因此两者都靠「列标题 + 原文参数齐备」双重判定，
任何一项不齐就回退其它模式，绝不硬套默认值。

四条设计原则（与全项目一致）：
    1. 数字不过模型：开标一览表的报价率、项目需求清单的单价限价与预估量
       一律由 pdfplumber 直读 PDF 表格；千问只解析「规则」这类语义参数。
    2. 页锚点定位：95 页招标文件解析后约 6.6 万字，远超上下文窗口，
       直接喂全文会把规则章节截断掉。这里按 <!-- page:N --> 切片并按关键词
       给页打分，只把规则页 + 口径页送进模型，实际页码写进结果，来源可追溯。
    3. 正则兜底 + 原文核对：所有参与计算的参数都有从原文直接抠出的兜底值，
       模型给的 evidence 必须能在原文逐字命中，否则降级提示人工复核。
    4. 终态信息对照：若有「入围候选供应商公示」，逐家核对公示报价与开标表，
       但明确提示公示位次是综合评分排序，不等于价格分排名。
"""
from __future__ import annotations

import re
from pathlib import Path

from bidsummary import llm
from bidsummary.grids import to_number
from bidsummary.models import (DiscountBidder, DiscountProduct, DiscountResult,
                               FileItem)

RATE_KEYS = ("优惠率", "下浮率", "折扣率", "让利率")
NAME_KEYS = ("投标人名称", "投标单位名称", "投标单位", "投标人", "供应商名称")
COMPANY_RE = re.compile(r"公司|中心|集团|研究院|工厂|厂|合作社|工作室|事务所")

PAGE_RE = re.compile(r"<!--\s*page:(\d+)\s*-->")

# 给页打分的关键词：规则页权重最高，保证不被截断。
# 「评标办法前附表 / 基本分 / 偏差率」是增减分型项目（周口）的入口，
# 「价格评审 / 报价得分 / 下浮率」是满分扣分型项目（苏州）的入口，两边都要覆盖。
PAGE_KEYWORDS = (
    ("价格评审", 14), ("评标基准价", 12), ("报价得分", 10),
    ("评标办法前附表", 12), ("基本分", 11), ("偏差率", 9),
    ("分值构成", 9), ("评分标准", 6), ("得分公式", 7),
    ("报价计算方法", 9), ("直线插入法", 7), ("有效投标报价", 6),
    ("评审价", 8), ("折扣", 5), ("下浮率", 6), ("最高限价", 4),
    ("单价最高限价", 5), ("结算金额", 4), ("分项报价", 3),
)

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ------------------------------------------------------------------ PDF 表格直读
def _s(v) -> str:
    return "" if v is None else str(v).replace("\n", " ").replace("\r", " ").strip()


def _pdf_tables(path: Path) -> list[tuple[int, list[list]]]:
    """返回 [(页码, 表格)]，按页序。"""
    import pdfplumber

    out: list[tuple[int, list[list]]] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                try:
                    for t in (page.extract_tables() or []):
                        if t:
                            out.append((i, t))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _parse_opening(path: Path) -> list[tuple[str, float, int, str]]:
    """从 PDF 开标一览表读出 [(投标人, 报价数字%, 页码, 口径)]。

    两种极易混淆的报价口径必须分开：
      rebate        —— 「优惠率 / 下浮率(%)」：折扣 = 1 − 报价/100（苏州烟草）
      discount_rate —— 「投标报价（折扣率）市场价格的(%)」：评审价就是报价/100（周口烟草）
    把折扣率当下浮率会算出 0.08 这种完全错误的折扣，所以这里显式返回口径。
    """
    out: list[tuple[str, float, int, str]] = []
    seen: set[str] = set()
    for page, table in _pdf_tables(path):
        hr, c_name, c_rate = -1, -1, -1
        for r, row in enumerate(table[:5]):
            cells = [_s(c) for c in row]
            c_name = next((i for i, h in enumerate(cells)
                           if any(k in h for k in NAME_KEYS)), -1)
            c_rate = next((i for i, h in enumerate(cells)
                           if any(k in h for k in RATE_KEYS)), -1)
            if c_name >= 0 and c_rate >= 0 and c_name != c_rate:
                hr = r
                break
        if hr < 0:
            continue
        rate_header = _s(table[hr][c_rate]) if c_rate < len(table[hr]) else ""
        if any(k in rate_header for k in ("下浮率", "优惠率", "让利率")):
            kind = "rebate"
        elif "折扣" in rate_header:
            kind = "discount_rate"
        else:
            kind = "unknown"
        for row in table[hr + 1:]:
            cells = [_s(c) for c in row]
            if max(c_name, c_rate) >= len(cells):
                continue
            nm, raw = cells[c_name], cells[c_rate]
            rt = to_number(raw)
            if not nm or rt is None or len(nm) < 4 or not COMPANY_RE.search(nm):
                continue
            if any(k in nm for k in ("备注", "记录人", "唱标人", "监督人")):
                continue
            key = re.sub(r"\s+", "", nm)
            if key in seen:
                continue
            seen.add(key)
            out.append((key, float(rt), page, kind))
    return out


def _parse_products(path: Path) -> list[DiscountProduct]:
    """从招标文件「项目需求」表读出产品清单（单价最高限价 + 预估采购量）。

    这些表常跨十几页、每页 3~4 行且只有第一页有表头；空白的投标报价模板表
    没有「预估采购量」列、单价列为空，会被自然跳过。
    """
    out: list[DiscountProduct] = []
    seen: set[str] = set()
    cmap: tuple[int, int, int, int, int, int] | None = None
    for page, table in _pdf_tables(path):
        # 表头：同时出现「单价/限价」与「预估/采购量」
        for row in table[:3]:
            cells = [_s(c) for c in row]
            joined = "".join(cells)
            if "预估" not in joined and "采购量" not in joined and "数量" not in joined:
                continue
            if "单价" not in joined and "限价" not in joined:
                continue
            c_seq = next((i for i, h in enumerate(cells) if "序号" in h), 0)
            c_name = next((i for i, h in enumerate(cells)
                           if "项目" in h and "参数" not in h), 1)
            c_spec = next((i for i, h in enumerate(cells) if "参数" in h), 2)
            c_unit = next((i for i, h in enumerate(cells) if "单位" in h), 3)
            c_cap = next((i for i, h in enumerate(cells)
                          if "单价" in h and ("限价" in h or "最高" in h)), -1)
            if c_cap < 0:
                c_cap = next((i for i, h in enumerate(cells) if "单价" in h), -1)
            c_qty = next((i for i, h in enumerate(cells)
                          if "预估" in h or "采购量" in h or "数量" in h), -1)
            if c_cap >= 0 and c_qty >= 0 and c_cap != c_qty:
                cmap = (c_seq, c_name, c_spec, c_unit, c_cap, c_qty)
                break
        if not cmap:
            continue
        c_seq, c_name, c_spec, c_unit, c_cap, c_qty = cmap
        for row in table:
            cells = [_s(c) for c in row]
            if c_seq >= len(cells):
                continue
            seq = cells[c_seq]
            if not re.fullmatch(r"\d{1,3}", seq or ""):
                continue
            cap = to_number(cells[c_cap]) if c_cap < len(cells) else None
            qty = to_number(cells[c_qty]) if c_qty < len(cells) else None
            nm = cells[c_name] if c_name < len(cells) else ""
            if cap is None or qty is None or not nm or seq in seen:
                continue
            seen.add(seq)
            out.append(DiscountProduct(
                seq=seq, name=re.sub(r"\s+", " ", nm),
                spec=cells[c_spec] if c_spec < len(cells) else "",
                unit=cells[c_unit] if c_unit < len(cells) else "",
                cap=float(cap), qty=float(qty), source_page=str(page)))
    return out


# ------------------------------------------------------------------ 页锚点定位
def _split_pages(md: str) -> list[tuple[int, str]]:
    parts = PAGE_RE.split(md or "")
    out: list[tuple[int, str]] = []
    for i in range(1, len(parts) - 1, 2):
        try:
            out.append((int(parts[i]), parts[i + 1]))
        except ValueError:
            continue
    return out


def _locate_context(md: str, max_chars: int = 17000) -> tuple[str, list[str]]:
    """按关键词给页打分，只把规则页 + 口径页送进模型（保留页锚点）。

    这是本次提示词优化的关键：全文 6.6 万字会被截断，而价格评审章节在第 72 页。
    """
    scored = []
    for no, txt in _split_pages(md):
        s = sum(w for k, w in PAGE_KEYWORDS if k in txt)
        if s:
            scored.append((s, no, txt))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = scored[:10]
    # 规则页必须入选；增减分型规则常跨两页（如周口 P27 起头、P28 续完），
    # 因此把规则页的下一页一并强制带入，避免只看到「得基本分 35 分」半句话。
    rule_pages = {no for _s, no, txt in scored
                  if ("评标基准价" in txt or "基本分" in txt or "价格评审" in txt)
                  and ("评分标准" in txt or "得分" in txt or "1%" in txt)}
    must = set(rule_pages)
    for no in rule_pages:
        must.add(no + 1)
    chosen = {no for _s, no, _t in picked}
    for s, no, txt in scored:
        if no in must and no not in chosen:
            picked.append((s, no, txt))
            chosen.add(no)
    picked.sort(key=lambda x: x[1])

    chunks: list[str] = []
    pages: list[str] = []
    used = 0
    for s, no, txt in picked:
        t = txt.strip()
        is_rule = "价格评审" in t and "评标基准价" in t
        if len(t) > 4200 and not is_rule:
            t = t[:4200]
        left = max_chars - used
        if left < 800:
            break
        if len(t) > left:
            t = t[:left]
        chunks.append(f"<!-- page:{no} -->\n{t}")
        pages.append(str(no))
        used += len(t) + 30
    return "\n\n".join(chunks), pages


def _page_text(md: str, needle: str) -> tuple[str, str]:
    """取包含某关键词的页（原文 + 页码），用于「评分依据」摘录。"""
    for no, txt in _split_pages(md):
        if needle in txt:
            return str(no), re.sub(r"\n{3,}", "\n\n", txt).strip()
    return "", ""


def _md_table_rows(page_text: str) -> list[list[str]]:
    """把页内 Markdown 表格还原成行，用作「评分详情」。"""
    rows: list[list[str]] = []
    for line in page_text.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


# ------------------------------------------------------------------ 千问提示词（模式D专用）
PROMPT_TMPL = """【任务】
下面的【招标文件片段】按 <!-- page:N --> 分页。请从中抽取「价格评审」的可执行参数。
本项目属于下浮率 / 折扣报价：投标人只报一个综合下浮率，折扣 = 1 − 下浮率，评审价即折扣。
注意：片段里可能同时出现「开标一览表」「项目需求清单」等无关内容，只解析评分规则。

【输出 JSON 结构】（缺失或原文未写明的字段返回 null / 空字符串）
{{
  "full_score": <报价部分满分数字>,
  "review_price_def": "<评审价口径的原文短句>",
  "discount_def": "<折扣与下浮率换算关系的原文短句>",
  "unit_price_def": "<中标单价如何由折扣折算的原文短句>",
  "benchmark_kind": "average" | "lowest" | "other",
  "benchmark_rule": "<评标基准价取法的原文短句>",
  "trim_guard": <有效报价人数超过几家才去高去低；不剔除填 0>,
  "trim_high": <去掉几个最高价>,
  "trim_low": <去掉几个最低价>,
  "above_deduct": <报价每高于基准价 1% 扣几分>,
  "below_deduct": <报价每低于基准价 1% 扣几分>,
  "formula": "<得分公式原文>",
  "cap_limit_pct": <最高投标折扣限价百分数>,
  "evidence_score": "<支撑满分的逐字原句>",
  "evidence_benchmark": "<支撑基准价取法的逐字原句>",
  "evidence_deduct": "<支撑扣分系数的逐字原句>",
  "page_hint": "<价格评审所在页码>"
}}

【判定要点】
1. full_score 取「一、报价部分（N 分）」或得分公式里的起始分值，不要取综合总分。
2. benchmark_kind：出现「平均值作为评标基准价」「以…平均价为评标基准价」→ average；
   出现「最低值」→ lowest。去高去低属于基准价的计算步骤，不改变 kind。
3. trim_guard / trim_high / trim_low：如「有效投标报价人数超过7家的，去掉一个最高价和一个最低价」
   → guard=7, high=1, low=1；原文未写剔除则三者填 0。
4. above_deduct / below_deduct 分别取「每高于…1% 扣 X 分」「每低于…1% 扣 Y 分」的 X、Y；
   这里的 1% 是 (报价−基准价)/基准价×100 的相对偏差百分点，不是下浮率本身。
5. evidence_* 必须逐字照抄原文（含标点），禁止改写、禁止省略号拼接、禁止自己组织语言。
6. 严禁把开标一览表里的具体下浮率数值写进任何规则参数。

【格式示例（另一个项目的片段，仅供理解字段，不要照抄数值）】
片段：「一、报价部分（30分）…以有效投标报价的最低值为评标基准价…每高于评标基准价1%的，扣0.8分；每低于评标基准价1%的，扣0.4分」
输出：{{"full_score":30,"benchmark_kind":"lowest","trim_guard":0,"trim_high":0,
       "trim_low":0,"above_deduct":0.8,"below_deduct":0.4,
       "evidence_score":"一、报价部分（30分）",
       "evidence_benchmark":"以有效投标报价的最低值为评标基准价"}}

【招标文件片段】
{context}
"""


# ------------------------------------------------------------------ 千问提示词（增减分型专用）
PROMPT_DEVIATION_TMPL = """【任务】
下面的【招标文件片段】按 <!-- page:N --> 分页。请从中抽取「投标报价评分标准」的可执行参数。
本项目的价格分是【基本分 + 偏差率增减分】型，不是「满分减去偏差扣分」型；
规则常分散在「评标办法前附表」「分值构成」「评标基准价计算方法」「投标报价的偏差率」
等多个页和表格里，跨页的半句话要与下一页接起来读。

【输出 JSON 结构】（缺失或原文未写明的字段返回 null / 空字符串）
{{
  "rate_kind": "rebate" | "discount_rate",
  "full_score": <投标报价部分满分数字>,
  "base_score": <等于评标基准价时得的基本分数字>,
  "low_limit": <低于基准价超过百分之几后，由加分改为在满分基础上扣分>,
  "step": <每偏差 1% 加或扣的分值>,
  "benchmark_kind": "average" | "lowest" | "other",
  "benchmark_rule": "<评标基准价取法的原文短句>",
  "trim_guard": <有效报价超过几家才去高去低；不剔除填 0>,
  "trim_high": <去掉几个最高价>,
  "trim_low": <去掉几个最低价>,
  "deviation_formula": "<偏差率计算公式的原文短句>",
  "settlement_def": "<结算价格计算公式的原文短句>",
  "cap_limit_pct": <最高限价（折扣率报价）百分数，如 98>,
  "evidence_score": "<支撑「分值构成 / 基本分 / 满分」的逐字原句>",
  "evidence_benchmark": "<支撑基准价取法与去高去低门槛的逐字原句>",
  "evidence_deduct": "<支撑「每低 1% 加分 / 每高 1% 扣分 / 低超阈值转扣分」的逐字原句>",
  "page_hint": "<投标报价评分标准所在页码>"
}}

【判定要点】
1. full_score 取「投标报价：N 分」或「加至满分 N 分为止」里的 N，
   不要取综合总分 100，也不要取里面的商务分、技术分。
   base_score 取「得基本分 N 分」，通常比满分小 5 分左右，两者绝不可混。
2. low_limit 取「低于评标基准价 X% 以上（不含）时」的 X；
   step 取「每低 1% … 加 S 分」「每高 1% … 扣 S 分」「每再低 1% … 扣 S 分」里的 S。
   若三处 S 不一致，取出现次数最多的那个并在 evidence_deduct 里照抄对应原句。
3. rate_kind：开标一览表报「优惠率 / 下浮率」→ rebate（折扣 = 1 − 下浮率/100）；
   报「投标报价（含税，折扣率）市场价格的 %」→ discount_rate（评审价就是报价率本身，
   严禁再拿 1 去减）。本项目招标文件「最高限价（折扣率，含税）：市场价格的 98%」
   是报价上限，不是得分参数，也不是下浮率。
4. benchmark_kind：出现「算术平均值作为评标基准价」→ average；出现「最低值」→ lowest。
   trim_guard 取「超过 N 家（不含）时，去掉一个最高价和一个最低价」的 N，
   并令 trim_high=1、trim_low=1；原文未写剔除则三者填 0。
5. deviation_formula 与 settlement_def 分别照抄「偏差率=…」「结算金额=…」原句。
6. evidence_* 必须逐字照抄原文（含标点），禁止改写、禁止省略号拼接、禁止自己组织语言。
   **每条证据只摘同一页内的完整一句短原句**，不要把跨页的半句话拼起来
   （PDF 分页常把表格里的一句话切成两半，拼接后无法在原文逐字命中）。
   例如 evidence_deduct 取「投标人有效投标报价高于评标基准价的按每高1%在基本分35 分的基础上扣1 分的比例扣分，扣完为止。」即可。
7. 严禁把开标一览表里的具体报价数字写进任何规则参数。

【格式示例（另一个项目的片段，仅供理解字段，不要照抄数值）】
片段：「分值构成（总分100分）投标报价：50 分…以所有有效投标人投标报价的算术平均值作为
评标基准价…投标人有效投标报价与评标基准价相等的，得基本分 45 分；低于基准价的按每低 1%
在基本分 45 分的基础上加 1 分，加至满分 50 分为止；若低于基准价 5% 以上（不含）时，
按每再低 1% 在满分 50 分的基础上扣 1 分；高于基准价的按每高 1% 扣 1 分」
输出：{{"full_score":50,"base_score":45,"low_limit":5,"step":1,
       "benchmark_kind":"average","trim_guard":0,"trim_high":0,"trim_low":0,
       "evidence_score":"投标人有效投标报价与评标基准价相等的，得基本分 45 分",
       "evidence_benchmark":"以所有有效投标人投标报价的算术平均值作为评标基准价"}}

【招标文件片段】
{context}
"""


# ------------------------------------------------------------------ 正则兜底 / 原文核对
def _flat(text: str) -> str:
    return re.sub(r"[\s　]+", "", text or "")


def _regex_hints(md: str) -> dict:
    """从招标文件全文里直接抠出可计算参数，作为模型结果的兜底/纠偏。"""
    t = _flat(md)
    h: dict = {}
    m = re.search(r"折扣\s*[=＝]\s*1\s*[-－]\s*(?:综合)?下浮率", t)
    if m:
        h["discount_def"] = m.group(0)
    m = re.search(r"本项目评审价\s*[=＝]\s*折扣", t)
    if m:
        h["review_price_def"] = m.group(0)
    m = re.search(r"各项产品的中标单价\s*[=＝]\s*[^；。]{4,80}", t)
    if m:
        h["unit_price_def"] = m.group(0)

    m = re.search(r"报价部分[（(](\d+(?:\.\d+)?)分[)）]", t)
    if not m:
        m = re.search(r"报价得分\s*[=＝]\s*(\d+(?:\.\d+)?)\s*[-－]", t)
    if m:
        h["full_score"] = float(m.group(1))

    m = re.search(r"超过(\d+)家的?[，,]?去掉一个最高价和一个最低价", t)
    if m:
        h["trim_guard"], h["trim_high"], h["trim_low"] = int(m.group(1)), 1, 1

    m = re.search(r"每高于评标基准价1[％%]的?[，,]?扣(\d+(?:\.\d+)?)分", t)
    if m:
        h["above_deduct"] = float(m.group(1))
    m = re.search(r"每低于评标基准价1[％%]的?[，,]?扣(\d+(?:\.\d+)?)分", t)
    if m:
        h["below_deduct"] = float(m.group(1))
    # 公式里的乘数也可作为扣分系数旁证
    m = re.search(r"报价得分[=＝]\d+[-－]\(投标报价[-－]评标基准价\)/评标基准价\*100\*(\d+(?:\.\d+)?)", t)
    if m and "above_deduct" not in h:
        h["above_deduct"] = float(m.group(1))
    m = re.search(r"报价得分[=＝]\d+[-－]\(评标基准价[-－]投标报价\)/评标基准价\*100\*(\d+(?:\.\d+)?)", t)
    if m and "below_deduct" not in h:
        h["below_deduct"] = float(m.group(1))

    if "平均值作为评标基准价" in t or "平均价为评标基准价" in t or "报价中的平均值" in t:
        h["benchmark_kind"] = "average"
    elif "最低值" in t and "基准价" in t:
        h["benchmark_kind"] = "lowest"

    m = re.search(r"最高(?:投标)?限价[^0-9￥¥]{0,34}[￥¥]?\s*([\d,]+(?:\.\d+)?)\s*元", t)
    if m:
        h["cap"] = float(m.group(1).replace(",", ""))
    m = re.search(r"最高投标折扣限价为(\d+(?:\.\d+)?)[％%]", t)
    if m:
        h["cap_limit_pct"] = float(m.group(1))
    return h


def _grounded(quote: str, md: str) -> bool:
    """证据逐字核对：按句拆分后逐句比对。

    表格里的评分规则常被 PDF 分页截成两半，模型若把跨页半句拼成一句，
    整串必然无法在原文命中。这里允许「多句证据中多数逐字命中」，
    既保持反幻觉约束，也不会因为分页这类排版问题误报。
    """
    flat_md = _flat(md)
    parts = [_flat(p) for p in re.split(r"[\n；;]", quote or "")]
    parts = [q for q in parts if len(q) >= 10 and "…" not in q and "..." not in q]
    if not parts:
        return False
    hit = sum(1 for q in parts if q in flat_md)
    return hit > 0 and hit >= len(parts) * 0.5


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 增减分型：正则兜底
def _regex_hints_deviation(md: str) -> dict:
    """从「评标办法前附表」原文直接抠出基本分增减分法的全部参数。

    周口烟草的规则跨 P27/P28 两页、夹在表格里，模型容易把「基本分 35」
    和「满分 40」混成一个数；这里逐条正则取值，作为计算的最终依据。
    """
    t = _flat(md)
    h: dict = {}

    m = re.search(r"投标报价[:：](\d+(?:\.\d+)?)分", t)
    if not m:
        m = re.search(r"加至满分(\d+(?:\.\d+)?)分", t)
    if m:
        h["full_score"] = float(m.group(1))

    m = re.search(r"得基本分(\d+(?:\.\d+)?)分", t)
    if not m:
        m = re.search(r"在基本分(\d+(?:\.\d+)?)分的基础上", t)
    if m:
        h["base_score"] = float(m.group(1))

    m = re.search(r"低于(?:评标)?基准价(\d+(?:\.\d+)?)%以上", t)
    if m:
        h["low_limit"] = float(m.group(1))

    steps = [float(x) for x in re.findall(
        r"每(?:低|高|再低|再高)1%[^。；]{0,44}?(?:加|扣)(\d+(?:\.\d+)?)分", t)]
    if steps:
        # 取出现次数最多的步长（三处表述应一致），平票取第一个
        h["step"] = max(set(steps), key=lambda v: (steps.count(v), -steps.index(v)))

    # 「超过5家（不含）时，去掉一个最高价和一个最低价」中间有逗号，不能排除逗号字符
    m = re.search(r"超过(\d+)家[^。]{0,20}?去掉一个最高价和一个最低价", t)
    if m:
        h["trim_guard"], h["trim_high"], h["trim_low"] = int(m.group(1)), 1, 1

    if "算术平均值作为评标基准价" in t or "平均值为评标基准价" in t:
        h["benchmark_kind"] = "average"
        m = re.search(r"(以[^。；]{0,28}算术平均值作为评标基准价)", t)
        if m:
            h["benchmark_rule"] = m.group(1)
    elif "最低值" in t and "基准价" in t:
        h["benchmark_kind"] = "lowest"

    # 表格文字在 PDF 文本层里没有句号分隔，必须按公式自身的结束标志截断，
    # 否则会把后面「条款号/评分因素/投标有效期」等无关表格内容一起吞进来。
    m = re.search(r"(偏差率[=＝][^。；]{0,60}?/评标基准价)", t)
    if m:
        h["deviation_formula"] = m.group(1)
    m = re.search(r"(结算金额[=＝][^。；]{0,40}?×数量)", t)
    if m:
        h["settlement_def"] = m.group(1)

    m = re.search(r"最高限价[^。；]{0,30}?市场价格的(\d+(?:\.\d+)?)%", t)
    if m:
        h["cap_limit_pct"] = float(m.group(1))
    m = re.search(r"最高(?:投标)?折扣限价为(\d+(?:\.\d+)?)%", t)
    if m and "cap_limit_pct" not in h:
        h["cap_limit_pct"] = float(m.group(1))
    return h


def _rule_pages_text(md: str, limit: int = 5) -> tuple[str, str, list[list[str]]]:
    """拼接规则相关页的原文，供「评分依据」页逐字展示（跨页规则不会只剩半句）。"""
    pages = _split_pages(md)
    primary = ("价格评审", "基本分", "评标基准价计算方法", "投标报价的偏差率")
    secondary = ("最高限价（折扣率", "结算金额=")
    picked: list[tuple[int, str]] = []
    for no, txt in pages:
        if any(k in txt for k in primary):
            picked.append((no, txt))
    if len(picked) < limit:
        have = {n for n, _ in picked}
        for no, txt in pages:
            if no in have:
                continue
            if any(k in txt for k in secondary):
                picked.append((no, txt))
    picked.sort(key=lambda x: x[0])
    picked = picked[:limit]
    if not picked:
        pg, tx = _page_text(md, "价格评审")
        return (f"【招标文件 P{pg}】\n{tx}" if tx else ""), pg, (_md_table_rows(tx) if tx else [])
    texts: list[str] = []
    for no, txt in picked:
        clean = re.sub(r"\n{3,}", "\n\n", txt).strip()[:2600]
        texts.append(f"【招标文件 P{no}】\n{clean}")
    detail: list[list[str]] = []
    for _, txt in picked:
        detail.extend(_md_table_rows(txt))
    return "\n\n".join(texts), ",".join(str(no) for no, _ in picked), detail[:30]


# ------------------------------------------------------------------ 公示对照
def _parse_publicity(items: list[FileItem]) -> tuple[dict[str, tuple[int, float | None]], str]:
    """从「入围候选供应商公示」读出 {公司名: (位次, 报价折扣率%)}。

    只用于交叉核对，不参与任何计算；公示位次是综合评分排序，不等于价格分排名。
    """
    for it in items:
        md = it.markdown or ""
        if "入围候选供应商" not in md and "中标候选人" not in md:
            continue
        pat = re.compile(r"第([一二三四五六七八九十]+)(?:入围候选供应商|中标候选人)[:：]\s*([^\n]+)")
        hits: dict[str, tuple[int, float | None]] = {}
        for m in pat.finditer(md):
            rank = CN_NUM.get(m.group(1), 0)
            name = re.sub(r"\s+", "", m.group(2))
            # 网页公示有时会在末尾再渲染一遍整段名单，形成「公司名+报价+第二…」的长串。
            # 按内容分隔符截断（不能用「集团」这类后缀截断，「纵达文化集团有限公司」
            # 会被截成「纵达文化集团」而与开标表对不上）。
            name = re.split(r"投标报价|市场价格的|第[一二三四五六七八九十]+"
                            r"(?:入围|中标)|[：:]", name)[0]
            if not rank or len(name) < 4 or not COMPANY_RE.search(name):
                continue
            if name in hits:
                continue
            tail = md[m.end():m.end() + 150]
            pm = re.search(r"市场价格的(\d+(?:\.\d+)?)%", tail)
            hits[name] = (rank, float(pm.group(1)) if pm else None)
        if hits:
            return hits, it.rel
    return {}, ""


# ------------------------------------------------------------------ 主入口
def _score_deviation_price(a: float, bench: float, res: DiscountResult) -> tuple[float, float]:
    """复用 scoring.py 里已验证的「基本分 + 偏差率增减分」实现，避免两套公式漂移。"""
    from bidsummary.models import PriceRule
    from bidsummary.scoring import _score_deviation

    pr = PriceRule(total_score=res.full_score, base_score=res.base_score,
                   step_percent=res.step, low_limit=res.low_limit,
                   behavior="deviation")
    sc, dev = _score_deviation(a, bench, pr)
    return round(max(0.0, min(res.full_score, sc)), 2), dev


def _attach_publicity(parsed: list[FileItem], res: DiscountResult, log) -> None:
    """公示只用于交叉核对，不参与任何计算；公示位次是综合评分排序。"""
    pub, pub_file = _parse_publicity(parsed)
    if not pub:
        return
    res.publicity_file, res.publicity_n = pub_file, len(pub)
    mismatch: list[str] = []
    for b in res.bidders:
        key = re.sub(r"\s+", "", b.name)
        hit = pub.get(key)
        if hit is None:
            # 公示与开标表的公司名偶尔差一两个尾字（如「集团有限公司」/「有限公司」）
            for pk, pv in pub.items():
                if min(len(pk), len(key)) >= 6 and (key.startswith(pk) or pk.startswith(key)):
                    hit = pv
                    break
        if not hit:
            continue
        b.publicity_rank, b.publicity_rate = hit
        if b.publicity_rate is not None and abs(b.publicity_rate - b.rate) > 0.011:
            mismatch.append(f"{b.name}（开标表 {b.rate:g}%，公示 {b.publicity_rate:g}%）")
    if mismatch:
        res.warnings.append("公示报价与开标表不一致：" + "；".join(mismatch))
    else:
        log(f"  [公示核对] {pub_file} 列出 {len(pub)} 家入围候选供应商，"
            f"报价与开标表逐家一致（公示位次按综合评分，不等于价格分排名）")


# ------------------------------------------------------------------ 主入口
def extract_discount(parsed: list[FileItem], log=print) -> DiscountResult | None:
    """识别并解析下浮率 / 折扣率项目；不符合两种已支持组合就返回 None 交给后续模式。"""
    pdfs = [f for f in parsed if f.ext.lower() == ".pdf" and f.status == "PARSED"]
    if len(pdfs) < 2:
        return None

    # ---- ① 开标一览表：必须含「投标人 + 报价率」且 >=2 家 ----
    opening, rows = None, []
    for it in pdfs:
        got = _parse_opening(Path(it.path))
        if len(got) >= 2:
            opening, rows = it, got
            break
    if opening is None:
        return None
    rate_kind = rows[0][3]
    if rate_kind not in ("rebate", "discount_rate"):
        return None

    # ---- ② 招标文件：按「规则章节特征」打分选取 ----
    # 不能只看某个关键词就取第一份：入围候选供应商公示里同样写了「投标报价（折扣率）」，
    # 且常排在招标文件前面，误选会导致规则参数全空（曾使周口项目静默回退到模式 A）。
    def _tender_score(it: FileItem) -> int:
        md = it.markdown or ""
        s = sum(w for k, w in (("评标基准价", 12), ("基本分", 10), ("价格评审", 12),
                               ("评分标准", 6), ("评标办法", 5), ("偏差率", 5),
                               ("投标人须知", 3), ("下浮率", 4)) if k in md)
        s += min(len(md) // 5000, 6)
        if ("入围候选供应商公示" in md or "中标候选人公示" in md) and "评标基准价" not in md:
            s -= 30
        return s

    others = [it for it in pdfs if it is not opening]
    if not others:
        return None
    tender = max(others, key=_tender_score)
    if _tender_score(tender) < 10:
        return None
    tender_md = tender.markdown or ""

    # ---- ③ 产品清单（数字直读，不经模型；折扣率报价项目可能没有清单） ----
    prods = _parse_products(Path(tender.path))
    log(f"  [模式D候选] 开标表 {opening.name}（{len(rows)} 家 / {rate_kind} 口径），"
        f"招标文件 {len(tender_md)} 字符，项目需求产品 {len(prods)} 项")

    # ---- ④ 规则形态判定：报价口径与评分规则必须成对匹配 ----
    hints = _regex_hints(tender_md)             # 满分 ± 偏差扣分（苏州）
    dh = _regex_hints_deviation(tender_md)      # 基本分 ± 偏差增减分（周口 / 安阳）
    deduct_ready = all(k in hints for k in ("full_score", "above_deduct",
                                            "below_deduct", "discount_def"))
    deviation_ready = all(k in dh for k in ("full_score", "base_score",
                                            "low_limit", "step"))
    if rate_kind == "rebate" and deduct_ready:
        score_kind = "deduct"
    elif rate_kind == "discount_rate" and deviation_ready:
        score_kind = "deviation"
    else:
        log(f"  [模式D跳过] 报价口径={rate_kind}，但未能从招标文件原文抽齐与之配套的"
            f"完整评分参数（满分±偏差扣分 / 基本分±偏差增减分），"
            f"回退其它模式以免套用错误公式")
        return None

    # ---- ⑤ 千问解析价格评审规则（页锚点定位后的上下文） ----
    context, pages = _locate_context(tender_md)
    tmpl = PROMPT_DEVIATION_TMPL if score_kind == "deviation" else PROMPT_TMPL
    log(f"  [提示词] 按页锚点定位到 {len(pages)} 页送千问：P{', P'.join(pages)}"
        f"　｜　规则形态 {score_kind}")
    data: dict = {}
    try:
        data = llm.chat_json(tmpl.format(context=context),
                             system=llm.SYS_PRICE_REVIEW_PARSER, max_tokens=2200)
        log(f"  [规则] 千问已完成「{'基本分增减分' if score_kind == 'deviation' else '价格评审'}」结构化抽取")
    except Exception as e:
        log(f"  [警告] 千问规则抽取失败，改用原文正则兜底：{e}")

    res = DiscountResult(rate_kind=rate_kind, score_kind=score_kind,
                         products=prods, opening_file=opening.rel,
                         product_file=tender.rel, rule_source_file=tender.rel,
                         llm_pages=pages)
    res.cap_total = round(sum(p.cap * p.qty for p in prods), 2)

    if score_kind == "deviation":
        res.full_score = float(dh["full_score"])
        res.base_score = float(dh["base_score"])
        res.low_limit = float(dh["low_limit"])
        res.step = float(dh["step"])
        res.above_deduct = res.step           # 展示列复用：每高 1% 扣 step 分
        res.below_deduct = res.step           # 低超阈值后每再低 1% 扣 step 分
        res.trim_guard = int(dh.get("trim_guard", 0))
        res.trim_high = int(dh.get("trim_high", 0))
        res.trim_low = int(dh.get("trim_low", 0))
        res.cap_limit_pct = _f(dh.get("cap_limit_pct"), None)
        res.benchmark_rule = str(dh.get("benchmark_rule")
                                 or data.get("benchmark_rule") or "")
        res.settlement_def = str(dh.get("settlement_def")
                                 or data.get("settlement_def") or "")
        res.formula = (f"报价等于基准价得基本分 {res.base_score:g}；"
                       f"每低 1% 加 {res.step:g} 分至满分 {res.full_score:g}；"
                       f"低超 {res.low_limit:g}% 后按每再低 1% 在满分基础上扣 {res.step:g} 分；"
                       f"每高 1% 扣 {res.step:g} 分（不足 1% 按插入法，保留 2 位）")
        flat = _flat(tender_md)
        m = re.search(r"(投标报价采用折扣率形式)", flat)
        res.discount_def = (m.group(1) + "；评审价 = 折扣率/100（不得按 1−下浮率 再换算）"
                            if m else
                            "投标报价（含税，折扣率）= 市场价格的百分比；评审价 = 折扣率/100")
        res.review_price_def = str(dh.get("deviation_formula")
                                   or data.get("deviation_formula")
                                   or "评审价 = 投标报价折扣率（含税价）")
        res.unit_price_def = res.settlement_def or "结算金额=市场价格×入围折扣率×数量"
        res.confirmed = True
        res.basis = ("报价满分、基本分、低幅上限、加减分步长与去高去低门槛"
                     "均由招标文件原文正则直取，千问结构化抽取作交叉校验")
    else:
        res.stated_cap = hints.get("cap")
        res.discount_def = hints.get("discount_def") or str(data.get("discount_def") or "")
        res.review_price_def = hints.get("review_price_def") or str(data.get("review_price_def") or "")
        res.unit_price_def = hints.get("unit_price_def") or str(data.get("unit_price_def") or "")
        res.benchmark_rule = str(data.get("benchmark_rule") or "") or (
            "以有效投标报价的平均值作为评标基准价"
            if hints.get("benchmark_kind") == "average" else "")
        res.formula = str(data.get("formula") or "") or (
            f"报价得分={hints.get('full_score', 50):g}-|报价−基准价|/基准价×100×扣分系数")
        res.full_score = _f(hints.get("full_score"), None) or _f(data.get("full_score"), 50) or 50
        res.trim_guard = int(_f(hints.get("trim_guard"), None)
                             or _f(data.get("trim_guard"), 0) or 0)
        res.trim_high = int(_f(hints.get("trim_high"), None)
                            or _f(data.get("trim_high"), 0) or 0)
        res.trim_low = int(_f(hints.get("trim_low"), None)
                           or _f(data.get("trim_low"), 0) or 0)
        res.above_deduct = _f(hints.get("above_deduct"), None) or _f(data.get("above_deduct"), 1) or 1
        res.below_deduct = _f(hints.get("below_deduct"), None) or _f(data.get("below_deduct"), 0.5) or 0.5
        res.confirmed = bool(hints.get("full_score") and hints.get("above_deduct")
                             and hints.get("below_deduct"))
        res.basis = ("满分、基准价取法、去高去低门槛与扣分系数均由招标文件原文正则直取，"
                     "千问结构化抽取作交叉校验" if res.confirmed else
                     "未能从原文确认全部价格评审参数，价格分仅供参考")

    # ---- 证据逐字核对：不通过只提示，不覆盖已由正则直取的参数 ----
    bad_ev = []
    for k in ("evidence_score", "evidence_benchmark", "evidence_deduct"):
        ev = str(data.get(k) or "")
        if ev and not _grounded(ev, tender_md):
            bad_ev.append(k)
    if bad_ev:
        res.warnings.append(
            f"千问返回的 {len(bad_ev)} 条规则证据未能在原文逐字核对到（表格文字可能被改写）；"
            "参与计算的参数已由正则直接取自招标文件原文，请以「评分依据」页原文摘录为准")

    kind = (dh.get("benchmark_kind") if score_kind == "deviation"
            else hints.get("benchmark_kind")) or data.get("benchmark_kind") or "average"
    if str(kind) not in ("average", "lowest"):
        res.warnings.append(f"评标基准价取法识别为「{kind}」，程序按有效报价平均值计算，请人工复核")
        kind = "average"

    # ---- 规则原文摘录（跨页合并，避免只剩半句话） ----
    rule_text, rule_pages, rule_detail = _rule_pages_text(tender_md)
    res.rule_text = rule_text
    res.rule_source_page = rule_pages or str(data.get("page_hint") or "")
    res.rule_detail = rule_detail
    if res.stated_cap and abs(res.cap_total - res.stated_cap) > 1:
        res.warnings.append(
            f"产品清单 Σ(单价最高限价×预估量)={res.cap_total:,.2f} 与招标文件写明的最高限价 "
            f"{res.stated_cap:,.2f} 不一致，请核对清单是否读取完整")

    # ---- ⑥ 报价 → 评审价（纯代码换算） ----
    cap_unit_sum = sum(p.cap for p in prods)
    for nm, rate, _page, _kind in rows:
        d = (1 - rate / 100) if rate_kind == "rebate" else (rate / 100)
        b = DiscountBidder(name=nm, rate=rate, discount=d, review_price=d,
                           per_100=100 * d, unit_sum=cap_unit_sum * d,
                           est_total=res.cap_total * d)
        if rate_kind == "discount_rate" and res.cap_limit_pct:
            lim = res.cap_limit_pct / 100
            b.over_cap = d > lim + 1e-9
            b.valid = 0 < d <= lim + 1e-9
            if b.over_cap:
                b.flags.append(f"报价折扣率 {rate:g}% 超过最高限价 "
                               f"{res.cap_limit_pct:g}%，按招标文件应否决投标")
        else:
            b.valid = 0 < d <= 1.0
            if not b.valid:
                b.flags.append("折扣超出 0~100% 有效区间，按招标文件应为无效投标")
        res.bidders.append(b)

    # ---- ⑦ 评标基准价：有效报价平均（超过门槛去 1 高 1 低） ----
    valid = [b for b in res.bidders if b.valid]
    pool = list(valid)
    if (res.trim_high or res.trim_low) and len(valid) > res.trim_guard > 0:
        ordered = sorted(valid, key=lambda b: b.discount)
        removed = ordered[:res.trim_low]
        if res.trim_high:
            removed += ordered[-res.trim_high:]
        rm_ids = {id(x) for x in removed}
        pool = [b for b in valid if id(b) not in rm_ids]
        for b in removed:
            b.flags.append("基准价计算时作为最高/最低报价剔除（仍参与价格评分）")
    for b in pool:
        b.in_benchmark = True
    if pool:
        res.benchmark = (min(b.discount for b in pool) if kind == "lowest"
                         else sum(b.discount for b in pool) / len(pool))
        res.benchmark_n = len(pool)
    else:
        res.warnings.append("没有有效投标报价，无法计算评标基准价与价格分")

    # ---- ⑧ 价格分（纯代码，两套公式严格分开） ----
    if res.benchmark:
        for b in valid:
            if score_kind == "deviation":
                sc, dev = _score_deviation_price(b.discount, res.benchmark, res)
                b.price_score, b.deviation = sc, dev
            else:
                dev = (b.discount - res.benchmark) / res.benchmark * 100
                b.deviation = dev
                deduct = res.above_deduct if dev > 0 else res.below_deduct
                b.price_score = max(0.0, min(res.full_score,
                                             round(res.full_score - abs(dev) * deduct, 2)))
    ranked = sorted(res.bidders,
                    key=lambda b: (b.price_score is None,
                                   -(b.price_score or 0), b.discount))
    for i, b in enumerate(ranked, 1):
        b.rank = i
    res.bidders = ranked          # 汇总表与明细矩阵都按价格分排名顺序展示

    # ---- ⑨ 公示核对（终态信息，不参与计算） ----
    _attach_publicity(parsed, res, log)

    if score_kind == "deviation":
        log(f"  [模式D] 直接报折扣率 × 基本分增减分；{len(res.bidders)} 家投标人；"
            f"基准价 {res.benchmark:.6f}（{res.benchmark_n} 家参与）；"
            f"基本分 {res.base_score:g} / 满分 {res.full_score:g}，每 1% 加减 {res.step:g} 分，"
            f"低超 {res.low_limit:g}% 转扣"
            + (f"；最高限价 {res.cap_limit_pct:g}%" if res.cap_limit_pct else ""))
    else:
        log(f"  [模式D] {len(prods)} 个产品 × {len(res.bidders)} 家投标人；"
            f"基准价 {res.benchmark:.6f}（{res.benchmark_n} 家参与）；"
            f"满分 {res.full_score:g}，高于扣 {res.above_deduct:g}/1%，低于扣 {res.below_deduct:g}/1%")
    return res
