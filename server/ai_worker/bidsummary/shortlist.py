"""模式 C：入围单价矩阵（无开标记录表、按产品单价×权重评分的项目）。

适用形态（如「××分行门楣、标识及营销设施选型入围采购」）：
  * 招标文件附表给出 产品名称 / 单位 / 尺寸工艺 / 最高投标限价 / 权重（%），
    常按报价类别分多张附表（标识标牌和营销设施、门楣灯箱……），每张权重合计 100%；
  * 投标单位报价不在开标记录表里，而是一张「投标单位 × 投标产品 × 单价」大表；
  * 评标价 = ∑各产品投标单价（不含税）×权重，基准价 = 各有效投标人所报评标价的最低值，
    得分 = 基准价／投标评标价 × 分值。

分工（与模式 B 同一套哲学）：
  * 表格结构、单价、权重、限价 —— 代码直读单元格，不经模型转写；
  * 「投标报价评分标准」的分值/公式/基准价取法 —— 千问从招标文件原文抽取，
    再用正则兜底 + 原文核对，模型静默编造会被护栏拦下；
  * 全部计算（评标价、基准价、得分、百分比）—— 纯代码。
"""
from __future__ import annotations

import re
from pathlib import Path

from bidsummary import llm, pricerule
from bidsummary.grids import to_number
from bidsummary.models import (SLBidder, SLCategory, SLProduct, ShortlistResult,
                               FileItem)

__all__ = ["extract_shortlist"]


# ------------------------------------------------------------------ 文本工具
_STRIP = r"[\s　（）()【】\[\]、，,。．.\-—_/：:；;*×xX·＋+％%]"
_PUNCT = str.maketrans({c: "" for c in r"（）()【】[]、，,。．.-—_/：:；;·＋+"})


def _norm(s: str) -> str:
    """产品名/规格的宽松规范化：去空白、全半角标点，统一大小写。"""
    return re.sub(_STRIP, "", (s or "").translate(_PUNCT)).upper()


def _lcs_len(a: str, b: str, cap: int = 60) -> int:
    """最长公共子串长度（用于「尺寸与工艺」消歧），加 cap 防长串拖慢。"""
    a, b = a[:400], b[:400]
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
                    if best >= cap:
                        return best
        prev = cur
    return best


# ------------------------------------------------------------------ 表格读取
def _docx_tables(path: Path) -> list[tuple[str, list[list[str]]]]:
    """docx：按文档顺序返回 (前文摘要, 网格)。前文用于找「附表N ××」类标题。"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(str(path))
    out: list[tuple[str, list[list[str]]]] = []
    recent: list[str] = []
    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            t = Paragraph(child, d).text.strip()
            if t:
                recent.append(t)
                recent = recent[-3:]
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, d)
            grid = [[c.text.strip() for c in row.cells] for row in tbl.rows]
            if grid and any(any(c for c in row) for row in grid):
                out.append(("\n".join(recent), grid))
    return out


def _tables_of(it: FileItem) -> list[tuple[str, list[list[str]]]]:
    ext = it.ext.lower()
    try:
        if ext == ".docx":
            return _docx_tables(Path(it.path))
        if ext in (".xlsx", ".xlsm", ".xls"):
            from bidsummary.grids import read_grids
            return read_grids(Path(it.path))
    except Exception:
        return []
    return []


def _cell(grid: list[list], r: int, c: int) -> str:
    if r < len(grid) and c < len(grid[r]):
        return str(grid[r][c] or "").strip()
    return ""


def _header_row(grid: list[list], keys: list[str], within: int = 6) -> int:
    """返回第一行「同时含 keys 里全部关键词」的行号（0 基），找不到返回 -1。"""
    for r, row in enumerate(grid[:within]):
        text = "".join(str(c or "") for c in row)
        if all(k in text for k in keys):
            return r
    return -1


# ------------------------------------------------------------------ 权重附表
WEIGHT_KEYS = ["产品名称", "权重"]
CAP_KEYS = ["限价"]


def _parse_weight_table(grid: list[list]) -> list[SLProduct] | None:
    """识别「序号|产品名称|单位|尺寸与工艺|最高投标限价|权重」类附表。"""
    hr = _header_row(grid, WEIGHT_KEYS)
    if hr < 0:
        return None
    head = [(str(c or "")).replace("\n", "") for c in grid[hr]]

    def col(*keys: str) -> int:
        for i, h in enumerate(head):
            if all(k in h for k in keys):
                return i
        return -1

    c_seq, c_name = col("序号"), col("产品名称")
    c_cap, c_w = col("限价"), col("权重")
    c_unit, c_spec = col("单位"), -1
    if col("尺寸") >= 0:
        c_spec = col("尺寸")
    elif col("工艺") >= 0:
        c_spec = col("工艺")
    if c_name < 0 or c_w < 0:
        return None
    prods: list[SLProduct] = []
    for r in range(hr + 1, len(grid)):
        name = _cell(grid, r, c_name)
        seq = _cell(grid, r, c_seq) if c_seq >= 0 else str(len(prods) + 1)
        w = to_number(_cell(grid, r, c_w))
        if not name or w is None:
            continue
        cap = to_number(_cell(grid, r, c_cap)) if c_cap >= 0 else None
        prods.append(SLProduct(
            seq=seq, name=name.replace("\n", " ").strip(),
            unit=_cell(grid, r, c_unit) if c_unit >= 0 else "",
            spec=(_cell(grid, r, c_spec) if c_spec >= 0 else "").replace("\n", " "),
            cap=cap, weight=w))
    # 权重合计 ≈ 1（而家数 × 100）说明写成了小数，统一 ×100
    if prods and abs(sum(p.weight for p in prods) - 1.0) < 0.05:
        for p in prods:
            p.weight *= 100
    return prods if len(prods) >= 3 else None


def _category_name(context: str, idx: int) -> str:
    """从附表前文取类别名。上下文里可能残留上一张附表的标题，取**最后**一个。"""
    hits = re.findall(r"附表\s*\d+\s*[、.．：:]?\s*([^\n，。；]{2,30}?)(?:价格)?权重", context)
    if hits:
        return hits[-1].strip()
    lines = [x for x in (context or "").splitlines() if x.strip()]
    if lines:
        return lines[-1].strip()[:24]
    return f"报价类别{idx}"


# ------------------------------------------------------------------ 投标单位单价表
BIDDER_KEYS = [["投标单位"], ["投标人"], ["供应商名称"], ["单位名称"]]
PRODUCT_KEYS = [["投标产品"], ["产品名称"], ["货物名称"], ["服务/工程名称"], ["产品"], ["项目名称"]]
PRICE_KEYS = [["单价"], ["价格"], ["报价"]]


def _parse_bidder_table(grid: list[list]) -> dict[str, list[tuple[str, float | None]]] | None:
    """识别「投标单位|投标产品|单价」大表，返回 {投标单位: [(产品文本, 单价), ...]}。"""
    hr = -1
    cols = None
    for r, row in enumerate(grid[:6]):
        text = "".join(str(c or "") for c in row)
        if not any(k[0] in text for k in BIDDER_KEYS):
            continue
        if not any(k[0] in text for k in PRODUCT_KEYS):
            continue
        if not any(k[0] in text for k in PRICE_KEYS):
            continue
        head = [(str(c or "")).replace("\n", "") for c in row]

        def col(*keys: str) -> int:
            for i, h in enumerate(head):
                if all(k in h for k in keys):
                    return i
            return -1

        c_b = max((col(k[0]) for k in BIDDER_KEYS if col(k[0]) >= 0), default=-1)
        c_p = max((col(k[0]) for k in PRODUCT_KEYS if col(k[0]) >= 0), default=-1)
        c_v = max((col(k[0]) for k in PRICE_KEYS if col(k[0]) >= 0), default=-1)
        if min(c_b, c_p, c_v) >= 0:
            hr, cols = r, (c_b, c_p, c_v)
            break
    if hr < 0:
        return None
    c_b, c_p, c_v = cols
    groups: dict[str, list[tuple[str, float | None]]] = {}
    cur = ""
    for r in range(hr + 1, len(grid)):
        bidder = _cell(grid, r, c_b)
        if bidder and bidder not in ("0", "/", "—", "-"):
            cur = bidder.replace("\n", "")
            groups.setdefault(cur, [])
        pname = _cell(grid, r, c_p)
        price = to_number(_cell(grid, r, c_v))
        if not cur or not pname or pname in ("0", "/"):
            continue
        groups[cur].append((pname, price))
    groups = {k: v for k, v in groups.items() if v}
    return groups if len(groups) >= 2 else None


# ------------------------------------------------------------------ 产品匹配
def _match(pname: str, prods: list[SLProduct], cache: dict) -> SLProduct | None:
    """把投标单位表里的产品文本对回附表产品。

    附表「产品名称+单位+尺寸与工艺」常被拼成一句，且单位（个/平方/米…）
    可能插在产品名中间（如「悬挂式招牌（个）（24小时自助银行服务）」），
    所以：先按 规范化名称 ⊆ 行文本 找候选；不行再把该产品单位串从行文本里
    删掉重试；多候选时用「尺寸与工艺」最长公共子串消歧，平局取更长的
    产品名（「布灯箱门楣灯箱底板」优先于「布灯箱门楣灯箱」）。
    """
    np = _norm(pname)
    if not np:
        return None
    if np in cache:
        return cache[np]
    cands: list[SLProduct] = []
    for p in prods:
        nk = _norm(p.name)
        if nk and (nk in np or nk in np.replace(_norm(p.unit), "")):
            cands.append(p)
    if not cands:
        hit = None
    elif len(cands) == 1:
        hit = cands[0]
    else:
        hit = max(cands, key=lambda p: (_lcs_len(np, _norm(p.spec)),
                                        len(_norm(p.name))))
    cache[np] = hit
    return hit


# ------------------------------------------------------------------ 评分标准抽取
RULE_SYS = llm.SYS_RULE_PARSER

RULE_TMPL = """下面是招标文件中「投标报价评分标准」附近的原文片段（可能含评分表与备注）。
请解析价格评分规则，输出 JSON（只输出 JSON，键名照抄）：
{{
  "score_items": [
    {{"name": "<报价类别名称，照抄原文，如「标识报价+营销设施报价」>",
      "score": <该类别价格分分值，纯数字，如 14>,
      "evidence": "<支撑该分值的原文逐字原句>"}}
  ],
  "eval_price_rule": "<评标价如何构成的原文表述，如：∑各产品投标单价（不含税金额）×权重>",
  "benchmark_rule": "<评标基准价取法的原文表述，如：各有效投标人所报××评标价之和的最低值>",
  "formula": "<得分公式的原文表述，如：（评标基准价）／（投标报价）×14>",
  "evidence": "<支撑整体判定的原文原句，可拼接，逐字照抄>"
}}

要求：
1. 只摘原文明确写出的内容；片段里没有的键返回 null，严禁按惯例脑补。
2. score 必须是原文括号里写明的分值数字（如「（14分）」→ 14）。
3. evidence 必须逐字照抄原句（含标点），供程序回原文核对。

【招标文件片段】
{snippet}"""


def _rule_slice(md: str) -> str:
    """截取「投标报价评分标准」附近 ±2500 字。

    固定关键词只覆盖一部分写法；招标文件把标题写成「价格部分（50分）」
    「最低价法」「价格总分…」时，改用 pricerule 的宽口径密度定位，
    避免明明有价格评分条款却返回空串（模型看不到原文）。
    """
    if not md:
        return ""
    m = re.search(r"投标报价评分标准|价格分[（(（]?\s*\d|报价得分", md)
    if not m:
        m = re.search(r"评标基准价", md)
    if m:
        i = m.start()
        return md[max(0, i - 400): i + 2800]
    from bidsummary import pricerule
    seg, _ = pricerule.locate_price_rule(md, window=2800, before=400)
    return seg


def _regex_hints(text: str) -> dict:
    """确定性兜底：直接从原文抠 分值/基准价取法/评标价公式。"""
    t = re.sub(r"[\s　]+", "", text or "")
    h: dict = {"score_items": []}

    def _clean_name(s: str) -> str:
        # 去掉表格竖线等噪声；含「标准/办法/细则」的是标题行，不是报价类别
        s = s.strip(" |｜\t")
        return "" if re.search(r"标准|办法|细则|评分项|因素", s) else s

    # 「××报价得分（N分）」
    for m in re.finditer(r"([^，。；、\n|｜]{2,26}?)报价得分[（(](\d+(?:\.\d+)?)[)）]分?", t):
        nm = _clean_name(m.group(1))
        if nm:
            h["score_items"].append({"name": nm, "score": float(m.group(2))})
    # 「……价格分（N分）」
    for m in re.finditer(r"([^，。；、\n|｜]{2,26}?)价格分[（(](\d+(?:\.\d+)?)[)）]", t):
        nm = _clean_name(m.group(1))
        if not nm:
            continue
        if not any(i["name"] in nm or nm in i["name"] for i in h["score_items"]):
            h["score_items"].append({"name": nm, "score": float(m.group(2))})
    # 基准价取法
    m = re.search(r"评标基准价为([^。；]{4,60})", t)
    if m:
        h["benchmark_rule"] = m.group(1)
    # 评标价构成
    m = re.search(r"评标价[=＝](∑[^。；]{2,80})", t)
    if m:
        h["eval_price_rule"] = m.group(1)
    # 得分公式
    m = re.search(r"得分[=＝]（[^（）]{2,30}基准价[^（）]{0,30}）／（[^（）]{2,30}报价[^（）]{0,30}）[×x](\d+(?:\.\d+)?)", t)
    if m:
        h["formula"] = m.group(0)
    return h


def _grounded(quote: str, corpus: str) -> bool:
    """模型给的 evidence 必须能在原文里找到（去空白后子串匹配，≥10 字才核对）。"""
    q = re.sub(r"[\s　]+", "", quote or "")
    if len(q) < 10:
        return True
    return q in re.sub(r"[\s　]+", "", corpus or "")


def _extract_rule(parsed: list[FileItem], log) -> tuple[ShortlistResult, list[dict]] | None:
    """从招标文件里抽「投标报价评分标准」：千问抽取 + 正则兜底 + 原文核对。"""
    res = ShortlistResult()
    tender_md = ""
    for it in parsed:
        if it.kind in ("TENDER_DOC", "OPENING_RECORD") and it.markdown:
            tender_md = it.markdown
            res.rule_source_file = it.rel
            break
    if not tender_md:
        for it in sorted(parsed, key=lambda x: x.priority):
            if it.markdown:
                tender_md, res.rule_source_file = it.markdown, it.rel
                break
    if not tender_md:
        return None

    res.rule_text = _rule_slice(tender_md)
    if not res.rule_text:
        seg, src, page = pricerule.locate_in_files(parsed)
        if seg:
            res.rule_text = seg
            res.rule_source_file = src
            res.rule_source_page = page
    corpus = "\n".join(f.markdown for f in parsed)
    hints = _regex_hints(res.rule_text)

    data: dict = {}
    if res.rule_text:
        try:
            log("  [规则] 千问解析「投标报价评分标准」…")
            data = llm.chat_json(RULE_TMPL.format(snippet=res.rule_text),
                                  system=RULE_SYS) or {}
        except Exception as e:
            log(f"  [规则] 千问解析失败（{e}），使用正则兜底")
            data = {}

    def _items(src: dict) -> list[dict]:
        out = []
        for it_ in (src.get("score_items") or []):
            if isinstance(it_, dict) and it_.get("score") is not None:
                try:
                    out.append({"name": str(it_.get("name") or "").strip(),
                                "score": float(it_.get("score"))})
                except (TypeError, ValueError):
                    continue
        return out

    model_items = _items(data)
    regex_items = hints.get("score_items") or []
    # 正则结果可信度更高（直接来自原文），覆盖模型
    items_out = regex_items or model_items
    res.eval_price_rule = hints.get("eval_price_rule") or str(
        data.get("eval_price_rule") or "∑各产品投标单价×权重")
    res.benchmark_rule = hints.get("benchmark_rule") or str(
        data.get("benchmark_rule") or "")
    # 正则命中的公式自带「得分=」，模型也可能照抄；统一去掉前缀，
    # 避免展示层再拼一次变成「得分=得分=…×14」
    res.formula = re.sub(r"^得分\s*[=＝]\s*", "",
                         str(hints.get("formula") or data.get("formula") or ""))

    evid = str(data.get("evidence") or "")
    if evid and not _grounded(evid, corpus):
        # 分值/基准价等关键参数已由正则直接取自原文（items_out 优先用 regex_items），
        # 这里只是模型的「整体证据句」未能逐字核对——提示人工以「评分依据」表原文为准
        res.warnings.append("千问返回的规则证据未能在原文逐字核对到（可能改写了表格内容）；"
                            "分值与基准价取法已由正则直接取自招标文件原文，"
                            "请以汇总表「评分依据」页的原文摘录为准")
        evid = ""
    res.confirmed = bool(items_out) and ("最低" in (res.benchmark_rule or "")
                                         or "基准价" in (res.benchmark_rule or ""))
    if not res.confirmed:
        res.basis = ("未能在招标文件中确认「分值 + 评标基准价取法」，"
                     "表中价格分仅供参考，请以评标委员会口径为准")
    else:
        res.basis = "已按招标文件「投标报价评分标准」确认分值与基准价取法"
    return res, items_out


# ------------------------------------------------------------------ 主入口
def extract_shortlist(parsed: list[FileItem], log=print):
    """识别并计算入围单价矩阵项目。不匹配返回 None（继续走模式 A）。"""
    cats: list[SLCategory] = []
    bidder_groups: dict[str, list[tuple[str, float | None]]] = {}
    price_sheet = ""
    rule_detail: list[list[str]] = []

    for it in parsed:
        tables = _tables_of(it)
        if not tables:
            continue
        got_bid = False
        for ctx, grid in tables:
            prods = _parse_weight_table(grid)
            if prods is not None:
                cat = SLCategory(name=_category_name(ctx, len(cats) + 1),
                                 source_file=it.rel, products=prods)
                cat.cap_weighted = (sum((p.cap or 0) * p.weight for p in prods) / 100
                                    if all(p.cap is not None for p in prods) else None)
                cats.append(cat)
                log(f"  [权重表] {it.name}「{cat.name}」{len(prods)} 个产品")
                continue
            groups = _parse_bidder_table(grid)
            if groups is not None and not got_bid and len(groups) >= 2:
                bidder_groups = groups
                price_sheet = it.rel
                got_bid = True
                continue
            # 评分标准明细表（评分项|评分因素|评分标准）
            if not rule_detail:
                hr = _header_row(grid, ["评分标准"])
                if hr >= 0 and ("评分因素" in "".join(str(c or "") for c in grid[hr])
                                or "评分项" in "".join(str(c or "") for c in grid[hr])):
                    rule_detail = [[str(c or "").strip() for c in row]
                                   for row in grid[hr:] if any(str(c or "").strip() for c in row)]

    if not cats or not bidder_groups:
        return None

    # ---------- 评分规则（千问 + 正则兜底） ----------
    rule_and_items = _extract_rule(parsed, log)
    if rule_and_items is None:
        res, score_items = ShortlistResult(), []
    else:
        res, score_items = rule_and_items
    res.categories = cats
    res.price_sheet_file = price_sheet
    res.rule_detail = rule_detail

    # 分值映射：按名称关键词对回类别，对不上按顺序
    for i, cat in enumerate(cats):
        cat.score = 0.0
    for item in score_items:
        target = None
        for cat in cats:
            key = cat.name[:4]
            if key and key[:2] in item["name"]:
                target = cat
                break
        if target is None and score_items and len(score_items) == len(cats):
            target = cats[min(len(cats) - 1, score_items.index(item))]
        if target is not None:
            target.score = item["score"]
        else:
            cats[0].score = cats[0].score or item["score"]
    total = sum(c.score for c in cats)
    if total and total < 5:  # 写成了 0.14/0.21 之类的小数
        for c in cats:
            c.score *= 100

    # ---------- 产品匹配 + 单价归位（纯代码） ----------
    all_prods = [p for c in cats for p in c.products]
    prod_by_seq = {p.seq: (p, ci) for ci, c in enumerate(cats) for p in c.products}
    cache: dict = {}
    bidders: list[SLBidder] = []
    unmatched_total = 0
    for name, rows in bidder_groups.items():
        b = SLBidder(name=name, source_file=price_sheet)
        seen: set[str] = set()
        for pname, price in rows:
            p = _match(pname, all_prods, cache)
            if p is None:
                unmatched_total += 1
                continue
            if p.seq in seen:
                continue
            seen.add(p.seq)
            if price is not None:
                b.prices[p.seq] = price
        bidders.append(b)
    if unmatched_total:
        res.warnings.append(f"单价表中有 {unmatched_total} 行产品未能对上权重附表，已忽略（明细见运行日志）")

    # ---------- 计算（纯代码） ----------
    # 口径说明：评标价 = ∑**各产品**单价×权重，权重合计 100%。
    # 缺报任何一项都会让评标价虚假偏低（缺项按 0 计），出现「报得越少分越高」的
    # 荒谬结果；超过最高投标限价按惯例应否决。因此：
    #   * 只有「该类别全部产品均已报价且未超限价」的投标人才计分并参与基准价；
    #   * 缺报/超限价的照常列出其（不完整的）评标价，但得分标空并写明原因。
    for b in bidders:
        b.quoted = len(b.prices)
        for ci, cat in enumerate(cats):
            seqs = [p.seq for p in cat.products]
            missing = [s for s in seqs if s not in b.prices]
            over = [p.seq for p in cat.products
                    if p.seq in b.prices and p.cap is not None
                    and b.prices[p.seq] > p.cap + 1e-9]
            b.missing.extend(f"{cat.name}#{s}" for s in missing)
            b.over_cap.extend(f"{cat.name}#{s}" for s in over)
            ev = sum(b.prices[s] * prod_by_seq[s][0].weight for s in seqs if s in b.prices) / 100
            raw = sum(b.prices[s] for s in seqs if s in b.prices)
            b.eval_prices.append(ev if b.prices else None)
            b.raw_sums.append(raw if b.prices else None)
            if missing:
                b.flags.append(f"{cat.name}：缺报 {len(missing)} 项，评标价不完整，未计分")
            if over:
                b.flags.append(f"{cat.name}：{len(over)} 项超最高投标限价，未计分")

    # 基准价：报价齐全且未超限价的投标人才参与（「有效投标人」的可计算近似）
    for ci, cat in enumerate(cats):
        valid = [b for b in bidders
                 if not [s for p in cat.products for s in [p.seq] if s not in b.prices]
                 and not [p for p in cat.products
                          if p.seq in b.prices and p.cap is not None
                          and b.prices[p.seq] > p.cap + 1e-9]]
        cat.benchmark_n = len(valid)
        if valid:
            low = min(valid, key=lambda b: b.eval_prices[ci])
            cat.benchmark = low.eval_prices[ci]
            cat.benchmark_holder = low.name
        else:
            res.warnings.append(f"「{cat.name}」没有报价齐全且未超限价的投标人，该类别基准价缺失")

    for b in bidders:
        total_score = 0.0
        for ci, cat in enumerate(cats):
            ev = b.eval_prices[ci]
            complete = not any(f.startswith(f"{cat.name}：") for f in b.flags)
            if ev and cat.benchmark and cat.score and complete:
                sc = min(cat.score, round(cat.benchmark / ev * cat.score, 2))
                b.scores.append(sc)
                total_score += sc
            else:
                b.scores.append(None)
        # 任一类别未计分，合计不给出（避免「部分报价」与「完整报价」混排误导）
        b.total_score = round(total_score, 2) if all(
            s is not None for s in b.scores) and b.scores else None

    ranked = sorted(
        bidders,
        key=lambda b: (b.total_score is None, -(b.total_score or 0),
                       sum(e for e in b.eval_prices if e) or 0))
    for i, b in enumerate(ranked, 1):
        b.rank = i
    res.bidders = ranked

    log(f"  [模式C] {len(cats)} 个报价类别 × {len(all_prods)} 个产品 × {len(bidders)} 家投标单位")
    for cat in cats:
        if cat.benchmark:
            log(f"  [基准价] {cat.name}：{cat.benchmark:,.2f}"
                f"（{cat.benchmark_holder}，{cat.benchmark_n} 家参与比较）")
        else:
            log(f"  [基准价] {cat.name}：缺失")
    return res
