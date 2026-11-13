"""模式 E：投标人写在 Excel 工作表名称里（「一 sheet 一家」）。

真实项目里投标人并非都写在表格的某一列。本项目 `报价.xlsx` 有 31 个 sheet，
每个 sheet 名就是一家投标人的公司简称（「北京金车光电」「杭州柏年智能光电子」…），
sheet 内部是该家 149 个产品的单价明细表：

    序号 | 大类 | 产品名称 | 规格 | 单位 | 单价(含税) | 单价(不含税) | 最高限价（含税） | 权重

早期链路只找「投标单位 × 投标产品 × 单价」大表（模式 C）或「投标人名称 + 报价列」
（模式 B），两种都不命中，最终退化成模式 A 的一行「（未识别投标人）」空表。

本模块做三件事，坚持与全局一致的哲学：
  * **结构判断**：sheet 名是否投标人，由「千问判断提示词」+ 表头/行数结构校验共同决定，
    模型意见只作弱先验，结构校验才是准绳；
  * **数字直读**：单价、限价、权重全部读单元格，不经模型转写；
  * **规则来自原文、分数由代码算**：价格评分标准用 `pricerule.locate_in_files()`
    宽口径定位（支持写在评分表单元格里的「价格部分（50分）最低价法…」），
    参数由正则确定性抠取并由千问核对，得分纯代码计算。

评分口径（招标文件原文）：
    1. 价格总分 50 分，分项价格分 = 50 × 分项权重；
    2. 每项标的得分 =（所有投标人中最低价格 / 投标人投标价格）× 分项价格分；
    3. 报价偏离惩罚：X/Y < 0.5 或 X/Y ≥ 1.5（Y=该项有效投标人平均价）的产品
       该项不得分；剔除异常报价后重新计算 Y 与最低价，直到不再出现新的惩罚项；
    4. 投标人报价税率相同时按含税价计算，否则按不含税价。
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from bidsummary import llm, pricerule
from bidsummary.grids import read_grids, to_number
from bidsummary.models import (FileItem, SLBidder, SLCategory, SLProduct,
                               ShortlistResult)

__all__ = ["extract_sheet_quote"]

# 非投标人 sheet 名：汇总/说明/模板之类
NON_BIDDER = re.compile(
    r"^(?:sheet\s*\d*|工作表\s*\d*|汇总|合计|总计|说明|封面|目录|模板|备注|"
    r"价格|报价|清单|表\s*\d*|总表|附表|附件|附录|统计|数据|结果|评分|参数|"
    r"配置|首页|index|cover|summary|total|报价表|开标|一览)",
    re.I,
)
# 公司/字号常见行业词（仅作候选打分，不作为硬条件）
INDUSTRY = ("公司", "集团", "厂", "中心", "研究院", "事务所", "标识", "广告",
            "光电", "光电子", "文化", "科技", "实业", "传媒", "展览", "印刷",
            "智能", "控股", "装饰", "工贸", "商贸", "电子", "设计", "制作",
            "工程", "供应链", "图文", "数码", "股份", "有限", "灯箱", "标牌")

MIN_SHEETS = 5          # 至少 5 个 sheet 才认为可能是「一 sheet 一家」
MIN_PRODUCTS = 10       # 每个 sheet 至少 10 行产品明细


# ------------------------------------------------------------------ 判断提示词
SHEET_JUDGE_TMPL = """某招标项目的报价工作簿《{filename}》共有 {n} 个工作表，名称如下：
{sheet_list}

请判断哪些工作表代表「投标人」——真实项目常把每家投标人的公司全称或简称直接作为 sheet 名，
sheet 内是该家的产品单价明细；也可能混有汇总、说明、封面、目录、Sheet1 等非投标人页。

只输出 JSON：
{{
  "bidders": ["<与上面输入逐字一致的 sheet 名>"],
  "non_bidders": ["<与上面输入逐字一致的 sheet 名>"],
  "reason": "<一句话判断依据>"
}}
"""


def _sheet_candidates(names: list[str]) -> list[str]:
    """确定性候选：排除纯序号/Sheet/汇总/说明页，保留像公司名的中文名。"""
    out = []
    for n in names:
        s = (n or "").strip()
        if not (2 <= len(s) <= 30):
            continue
        if NON_BIDDER.match(s):
            continue
        if re.fullmatch(r"[\d\s._\-]+", s):
            continue
        # 至少含 2 个汉字，避免 "A1"、"包1" 之类
        if len(re.findall(r"[\u4e00-\u9fff]", s)) < 2:
            continue
        out.append(s)
    return out


# ------------------------------------------------------------------ 表头/列定位
def _norm_seq(s: str) -> str:
    """序号归一：'1.\xa0' / '１' / '01' / '第1项' 统一成 '1'。

    不同投标人填表习惯不同，序号里常带句点、全角数字、不间断空格，
    若直接当 key 会让整家 149 项报价全部对不上主表（表现为「缺报 149 项」）。
    """
    s = (s or "").replace("\xa0", " ").strip()
    m = re.match(r"^(?:第)?\s*([0-9０-９]+)\s*(?:项|号|[.、．])?\s*$", s)
    if m:
        digits = m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        return str(int(digits))
    return re.sub(r"[\s.、．]", "", s)


def _find_cols(grid: list[list]) -> tuple[int, dict[str, int]] | None:
    """在表格前 8 行里找「产品名称 + 单价」表头，返回 (数据起始行, 列号字典)。

    有些投标人把表头拆成两行（第一行「单价」，第二行「(含税)：元」），
    这里把两行拼起来再判列，否则含税/不含税列会认不出来。
    """
    for hr, row in enumerate(grid[:8]):
        head = [str(c or "").replace("\n", "").strip() for c in row]
        text = "".join(head)
        if "产品名称" not in text and "产品" not in text and "名称" not in text:
            continue
        if "单价" not in text and "报价" not in text and "价格" not in text:
            continue
        data_start = hr + 1
        if hr + 1 < len(grid):
            nxt = [str(c or "").replace("\n", "").strip() for c in grid[hr + 1]]
            nt = "".join(nxt)
            if ("序号" in nt and "产品名称" in nt) or re.search(r"[（(][不]?含税", nt):
                head = [(h + (nxt[i] if i < len(nxt) else "")) for i, h in enumerate(head)]
                data_start = hr + 2

        def find(*keys: str) -> int:
            for i, h in enumerate(head):
                if all(k in h for k in keys):
                    return i
            return -1

        c_inc = find("单价", "含税")
        # 「单价(含税)：元」也含「不含税」？不含；但顺序上先判不含税更稳妥
        c_exc = -1
        for i, h in enumerate(head):
            if "单价" in h and "不含税" in h:
                c_exc = i
                break
        for i, h in enumerate(head):
            if "单价" in h and "含税" in h and "不含税" not in h:
                c_inc = i
                break
        c_any = find("单价")
        if c_any < 0:
            c_any = find("报价")
        c_price = c_inc if c_inc >= 0 else (c_exc if c_exc >= 0 else c_any)
        c_name = find("产品名称")
        if c_name < 0:
            c_name = find("产品")
        if c_name < 0:
            c_name = find("名称")
        cols = {
            "seq": find("序号"), "cat": find("大类") if find("大类") >= 0 else find("类别"),
            "name": c_name, "spec": find("规格"), "unit": find("单位"),
            "inc": c_inc, "exc": c_exc, "price": c_price,
            "cap": find("限价"), "weight": find("权重"),
        }
        if cols["name"] >= 0 and cols["price"] >= 0:
            return data_start, cols
    return None


def _sv(grid: list[list], r: int, c: int) -> str:
    if c < 0 or r >= len(grid) or c >= len(grid[r]):
        return ""
    v = grid[r][c]
    return "" if v is None else str(v).strip()


def _parse_sheet(grid: list[list]) -> dict | None:
    """解析一个 sheet：产品清单 + 含税/不含税单价 + 限价 + 权重。"""
    found = _find_cols(grid)
    if not found:
        return None
    start, c = found
    products: list[dict] = []
    prices_inc: dict[str, float] = {}
    prices_exc: dict[str, float] = {}
    seen: set[str] = set()
    for r in range(start, len(grid)):
        raw_seq = _sv(grid, r, c["seq"]) or str(len(products) + 1)
        seq = _norm_seq(raw_seq) or str(len(products) + 1)
        name = _sv(grid, r, c["name"])
        if not name or name in ("0", "/", "—"):
            continue
        if re.fullmatch(r"[\d.]+", name):      # 纯数字行不是产品
            continue
        # 有些 sheet 把长产品名拆成两行（如「公众教育区(立地」/「式 )」），
        # 序号会重复；按序号去重，首行（含完整名称与价格）为准。
        if seq in seen:
            continue
        seen.add(seq)
        inc = to_number(_sv(grid, r, c["inc"])) if c["inc"] >= 0 else None
        exc = to_number(_sv(grid, r, c["exc"])) if c["exc"] >= 0 else None
        base = to_number(_sv(grid, r, c["price"]))
        if inc is None and exc is None:
            inc = base
        cap = to_number(_sv(grid, r, c["cap"])) if c["cap"] >= 0 else None
        w = to_number(_sv(grid, r, c["weight"])) if c["weight"] >= 0 else 0.0
        products.append({
            "seq": seq, "name": name.replace("\n", " ").strip(),
            "cat": _sv(grid, r, c["cat"]),
            "spec": _sv(grid, r, c["spec"]).replace("\n", " "),
            "unit": _sv(grid, r, c["unit"]),
            "cap": cap, "weight": w or 0.0,
        })
        if inc is not None:
            prices_inc[seq] = inc
        if exc is not None:
            prices_exc[seq] = exc
    if len(products) < MIN_PRODUCTS:
        return None
    return {"products": products, "inc": prices_inc, "exc": prices_exc,
            "header": "|".join(str(x or "") for x in grid[max(0, start - 1)])}


# ------------------------------------------------------------------ 工作簿识别
def _detect_workbook(parsed: list[FileItem], log) -> tuple[FileItem, list[str], dict[str, dict], list[str]] | None:
    """找出「一 sheet 一家投标人」的工作簿。"""
    best = None
    for it in parsed:
        if it.ext.lower() not in (".xlsx", ".xlsm", ".xls"):
            continue
        try:
            grids = read_grids(Path(it.path))
        except Exception as e:
            log(f"  [sheet判定] 读取 {it.name} 失败：{e}")
            continue
        names = [n for n, _ in grids]
        if len(names) < MIN_SHEETS:
            continue
        cand = _sheet_candidates(names)
        if len(cand) < MIN_SHEETS:
            continue
        parsed_sheets: dict[str, dict] = {}
        for name, grid in grids:
            if name not in cand:
                continue
            d = _parse_sheet(grid)
            if d:
                parsed_sheets[name] = d
        if len(parsed_sheets) < MIN_SHEETS:
            continue
        # 结构一致性：以「有报价的序号数」为准（长产品名拆行不影响），
        # 表头只做去标点后的宽松比较（全/半角冒号、简写列名都算同类）。
        headers = {re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", d["header"])
                   for d in parsed_sheets.values()}
        counts = [len(d["inc"]) for d in parsed_sheets.values()]
        if not counts or min(counts) < MIN_PRODUCTS:
            continue
        if len(headers) > max(3, len(parsed_sheets) // 3) \
                or max(counts) - min(counts) > max(3, max(counts) // 10):
            continue
        if best is None or len(parsed_sheets) > len(best[2]):
            best = (it, names, parsed_sheets, cand)
    return best


def _judge_sheets(filename: str, names: list[str], structural: list[str], log) -> tuple[list[str], str]:
    """千问判断提示词：哪些 sheet 名是投标人。结构校验结果为准，模型意见作弱先验。"""
    prompt = SHEET_JUDGE_TMPL.format(
        filename=filename, n=len(names),
        sheet_list="\n".join(f"  {i + 1}. {n}" for i, n in enumerate(names)))
    try:
        data = llm.chat_json(prompt, system=llm.SYS_SHEET_BIDDER_JUDGE) or {}
    except Exception as e:
        log(f"  [sheet判定] 千问判断失败（{e}），改用结构校验结果")
        return structural, "千问判断不可用，按表头与行数结构判定"
    b = {str(x).strip() for x in (data.get("bidders") or []) if str(x).strip()}
    nb = {str(x).strip() for x in (data.get("non_bidders") or []) if str(x).strip()}
    reason = str(data.get("reason") or "").strip()
    if not b:
        return structural, "千问未给出有效判断，按结构校验结果"
    # 模型若把大部分结构有效页判成非投标人，说明它没理解版式，忽略它的否定意见
    kept = [n for n in structural if n not in nb]
    if len(b & set(structural)) >= len(structural) * 0.5 and kept:
        log(f"  [sheet判定] 千问确认 {len(b & set(structural))} 家投标人；"
            f"排除 {len(nb & set(structural))} 个非投标人 sheet　（{reason[:60]}）")
        return kept, reason
    log(f"  [sheet判定] 千问结果与结构校验分歧较大，以结构校验为准（{reason[:60]}）")
    return structural, "千问判断与结构校验分歧，已按结构校验结果保留"


# ------------------------------------------------------------------ 规则抽取
RULE_TMPL = """下面是招标文件里「评标办法 / 价格评分标准」附近的原文片段。
价格条款可能写在评分表的某一行单元格里，标题未必叫「投标报价评分标准」。
请解析价格评分规则，只输出 JSON（键名照抄）：
{{
  "full_score": <价格部分满分，纯数字，如 50>,
  "weight_rule": "<分项价格分如何由权重得出，原文表述>",
  "item_formula": "<每项标的得分的原文公式>",
  "deviation_low": <X/Y 小于多少时该项不得分，纯数字>,
  "deviation_high": <X/Y 大于等于多少时该项不得分，纯数字>,
  "tax_rule": "<含税价 / 不含税价的适用条件，原文表述>",
  "formula": "<整体得分公式的原文表述>",
  "evidence": "<逐字照抄的原文原句，含标点>"
}}

要求：
1. 片段里没写的键返回 null，严禁按行业惯例脑补分值、阈值或公式。
2. deviation_low / deviation_high 只填数字（如 0.5），不要带「X/Y」或单位。
3. evidence 必须是逐字照抄的原文，供程序回原文核对；不能改写字句或数字。

【招标文件片段】
{snippet}"""


def _tight_rule(text: str) -> str:
    """把宽窗口收敛成「价格条款」原文：优先取整行表格行，其次取连续段落。"""
    for kw in ("最低价法", "价格总分", "分项价格分", "每项标的得分", "评标基准价"):
        i = text.find(kw)
        if i < 0:
            continue
        # 表格行：从本行行首到行尾
        ls = text.rfind("\n", 0, i) + 1
        le = text.find("\n", i)
        line = text[ls: le if le >= 0 else len(text)].strip()
        if line.startswith("|"):
            return line.strip("| ").strip()
        # 正文：取关键词前后一段，尽量不跨空行
        seg = text[ls: min(len(text), ls + 1200)]
        cut = seg.find("\n\n")
        return (seg[:cut] if cut > 0 else seg).strip()
    return text[:1200].strip()


def _rule_detail(text: str, corpus: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in corpus.splitlines():
        if not line.startswith("|"):
            continue
        if "价格部分" in line and ("最低价法" in line or "价格总分" in line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3:
                rows.append([cells[0], cells[1], cells[2]])
        elif "评审说明" in line and "综合评分法" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3:
                rows.append([cells[0], "评标办法", cells[2]])
    return rows[:6]


def _extract_rule(parsed: list[FileItem], res: ShortlistResult, log) -> dict:
    """宽口径定位 + 千问解析 + 正则确定性覆盖 + 原文核对。"""
    snippet, src, page = pricerule.locate_in_files(parsed)
    res.rule_source_file, res.rule_source_page = src, page
    res.rule_text = _tight_rule(snippet) if snippet else ""
    res.rule_detail = _rule_detail(snippet, "\n".join(f.markdown for f in parsed))

    hints = pricerule.regex_hints(snippet or res.rule_text)
    data: dict = {}
    if snippet:
        try:
            log("  [规则] 千问解析「评标办法 / 价格评分标准」…")
            data = llm.chat_json(RULE_TMPL.format(snippet=snippet[:6000]),
                                  system=llm.SYS_PRICE_RULE_GENERAL) or {}
        except Exception as e:
            log(f"  [规则] 千问解析失败（{e}），使用正则兜底")
    ev = str(data.get("evidence") or "")
    if ev and not pricerule.grounded(ev, "\n".join(f.markdown for f in parsed)):
        res.warnings.append("千问给出的规则证据未能逐字核对到原文，"
                            "分值与阈值已改由正则直接取自招标文件原文")
        ev = ""

    def _num(src: dict, key: str, default=0.0):
        v = src.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    # 正则直取原文参数，优先级高于模型
    full = hints.get("full_score") or _num(data, "full_score")
    weight_base = hints.get("weight_base") or _num(data, "weight_base", full)
    dev_low = hints.get("deviation_low") or _num(data, "deviation_low", 0.5)
    dev_high = hints.get("deviation_high") or _num(data, "deviation_high", 1.5)
    out = {
        "full_score": full, "weight_base": weight_base,
        "dev_low": dev_low, "dev_high": dev_high,
        "item_formula": hints.get("item_formula") or str(data.get("item_formula") or ""),
        "tax_rule": hints.get("tax_rule") or str(data.get("tax_rule") or ""),
        "formula": str(data.get("formula") or ""),
        "evidence": ev,
    }
    res.full_score = full
    res.deviation_low, res.deviation_high = dev_low, dev_high
    res.tax_rule = out["tax_rule"]
    res.formula = out["item_formula"] or out["formula"]
    res.eval_price_rule = (f"分项价格分 = {weight_base:g} × 分项权重；"
                           f"每项得分 =（该项最低价 / 本家单价）× 分项价格分")
    res.benchmark_rule = "每项标的取所有有效投标人的最低价；偏离异常报价迭代剔除后重算平均价 Y"
    res.confirmed = bool(full and out["item_formula"] and
                         (hints.get("deviation_rule") or (dev_low and dev_high)))
    res.basis = ("已按招标文件价格评分标准确认满分、分项公式与偏离惩罚阈值"
                 if res.confirmed else
                 "未能完整确认价格评分标准（满分 / 分项公式 / 偏离阈值），价格分请复核")
    return out


# ------------------------------------------------------------------ 计价口径
def _choose_price_basis(sheets: list[str], data: dict[str, dict], log) -> tuple[str, str]:
    """税率相同 → 含税价；否则不含税价（招标文件第 4 条）。"""
    rates: list[float] = []
    for name in sheets:
        d = data[name]
        rs = []
        for seq, inc in d["inc"].items():
            exc = d["exc"].get(seq)
            if exc and inc:
                rs.append(inc / exc - 1)
        if rs:
            rates.append(statistics.median(rs))
    if rates and max(rates) - min(rates) <= 0.002:
        basis = "inc" if any(data[s]["inc"] for s in sheets) else "exc"
        label = "含税价" if basis == "inc" else "不含税价"
        log(f"  [计价口径] {len(rates)} 家税率一致（中位 {statistics.median(rates):.2%}），按{label}计算")
        return basis, label
    basis = "exc" if any(data[s]["exc"] for s in sheets) else "inc"
    label = "不含税价" if basis == "exc" else "含税价"
    log(f"  [计价口径] 各家税率不一致（极差 {(max(rates) - min(rates)) if rates else 0:.2%}），按{label}计算")
    return basis, label


# ------------------------------------------------------------------ 计分
def _score(products: list[SLProduct], bidders: list[SLBidder], res: ShortlistResult,
           log) -> None:
    """分项加权最低价法 + 偏离惩罚（纯代码，数值不经模型）。"""
    full = res.full_score or 50.0
    lo, hi = res.deviation_low or 0.5, res.deviation_high or 1.5
    for p in products:
        seq = p.seq
        # 有效报价：单价 > 0 且未超过该项最高限价
        valid: dict[str, float] = {}
        for b in bidders:
            v = b.prices.get(seq)
            if v is None or v <= 0:
                continue
            if p.cap is not None and v > p.cap + 1e-9:
                continue
            valid[b.name] = v
        if not valid:
            continue
        excluded: dict[str, str] = {}
        for _ in range(12):
            active = {n: v for n, v in valid.items() if n not in excluded}
            if len(active) < 2:
                break
            y = statistics.fmean(active.values())
            fresh = {n: ("low" if v / y < lo else "high" if v / y >= hi else "")
                     for n, v in active.items()}
            fresh = {n: t for n, t in fresh.items() if t}
            if not fresh:
                break
            for n, t in fresh.items():
                excluded.setdefault(n, t)
        active = {n: v for n, v in valid.items() if n not in excluded}
        if not active:
            continue
        low_price = min(active.values())
        y_final = statistics.fmean(active.values())
        item_full = full * p.weight / 100
        for b in bidders:
            v = b.prices.get(seq)
            if v is None or v <= 0:
                continue
            if b.name in excluded:
                b.per_item_scores[seq] = 0.0
                if excluded[b.name] == "low":
                    b.penalty_low += 1
                else:
                    b.penalty_high += 1
                res.penalty_items.append({
                    "seq": seq, "name": p.name, "bidder": b.name, "price": v,
                    "avg": y_final, "ratio": v / y_final if y_final else 0,
                    "kind": "偏低(X/Y<%g)" % lo if excluded[b.name] == "low"
                            else "偏高(X/Y≥%g)" % hi,
                })
            elif p.cap is not None and v > p.cap + 1e-9:
                b.per_item_scores[seq] = 0.0
            else:
                b.per_item_scores[seq] = min(item_full, low_price / v * item_full)

    for b in bidders:
        s = sum(b.per_item_scores.values())
        b.total_score = round(s, 2)
        b.scores = [b.total_score]
        if b.penalty_low or b.penalty_high:
            b.flags.append(f"报价偏离惩罚：偏低 {b.penalty_low} 项、偏高 {b.penalty_high} 项（该项不得分）")
        if b.over_cap:
            b.flags.append(f"超最高投标限价 {len(b.over_cap)} 项，未参与该项比较")
        if b.missing:
            b.flags.append(f"缺报 {len(b.missing)} 项产品")
    ranked = sorted(bidders, key=lambda b: (-b.total_score, sum(b.prices.values())))
    for i, b in enumerate(ranked, 1):
        b.rank = i
    res.bidders = ranked
    log(f"  [模式E] 价格分：最高 {ranked[0].total_score:.2f}（{ranked[0].name}），"
        f"最低 {ranked[-1].total_score:.2f}；偏离惩罚共 {len(res.penalty_items)} 项")


# ------------------------------------------------------------------ 主入口
def extract_sheet_quote(parsed: list[FileItem], log=print) -> ShortlistResult | None:
    """识别并计算「sheet 名即投标人」项目；不匹配返回 None。"""
    found = _detect_workbook(parsed, log)
    if not found:
        return None
    it, all_names, sheet_data, candidates = found
    structural = [n for n in candidates if n in sheet_data]
    if len(structural) < MIN_SHEETS:
        return None
    names, judge_reason = _judge_sheets(it.name, all_names, structural, log)
    names = [n for n in names if n in sheet_data]
    if len(names) < MIN_SHEETS:
        return None

    res = ShortlistResult(variant="per_item")
    res.price_sheet_file = it.rel
    res.sheet_source_file = it.rel
    res.sheet_names = names
    log(f"  [识别] {it.name}：{len(names)} 个 sheet 名即投标人（{judge_reason}）")

    # ---------- 规则（宽口径定位 + 千问 + 正则） ----------
    _extract_rule(parsed, res, log)
    if not res.full_score:
        return None                    # 连满分都没确认，不套用任何默认公式

    # ---------- 产品清单（取首个 sheet 为主表） ----------
    master = sheet_data[names[0]]["products"]
    wsum = sum(float(x["weight"] or 0) for x in master)
    scale = 100.0 if 0.8 <= wsum <= 1.2 else 1.0
    products: list[SLProduct] = []
    for x in master:
        products.append(SLProduct(
            seq=str(x["seq"]), name=x["name"], spec=x["spec"], unit=x["unit"],
            cap=x["cap"], weight=float(x["weight"] or 0) * scale))
    cat = SLCategory(name="价格部分（全部分项）", source_file=it.rel,
                     score=res.full_score, products=products)
    if all(p.cap is not None for p in products):
        cat.cap_weighted = sum((p.cap or 0) * p.weight for p in products) / 100
    cat.benchmark_n = len(names)
    cat.benchmark = None               # 逐项取最低价，无单一评标基准价
    cat.benchmark_holder = "逐项最低价（见单价明细）"
    res.categories = [cat]

    # ---------- 计价口径 ----------
    basis, label = _choose_price_basis(names, sheet_data, log)
    res.price_basis = label

    # ---------- 单价归位（纯代码直读） ----------
    bidders: list[SLBidder] = []
    for n in names:
        d = sheet_data[n]
        b = SLBidder(name=n, source_file=it.rel)
        src = d["inc"] if basis == "inc" else d["exc"]
        if not src:
            src = d["inc"] or d["exc"]
        for p in products:
            if p.seq in src and src[p.seq] is not None:
                b.prices[p.seq] = src[p.seq]
        b.quoted = len(b.prices)
        b.missing = [p.seq for p in products if p.seq not in b.prices]
        b.over_cap = [p.seq for p in products
                      if p.seq in b.prices and p.cap is not None
                      and b.prices[p.seq] > p.cap + 1e-9]
        # 加权报价水平（仅作汇总对照，本口径的得分由逐项比值求和得到）
        ev = sum(b.prices[p.seq] * p.weight for p in products if p.seq in b.prices) / 100
        b.eval_prices = [ev if b.prices else None]
        b.raw_sums = [sum(b.prices.values()) if b.prices else None]
        bidders.append(b)

    # ---------- 计分 ----------
    _score(products, bidders, res, log)
    log(f"  [模式E] {len(names)} 家投标人 × {len(products)} 个产品，"
        f"计价口径 {label}，满分 {res.full_score:g}")
    if not res.confirmed:
        res.warnings.append(res.basis)
    return res
