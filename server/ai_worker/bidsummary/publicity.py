"""公示信息自动识别与交叉核对。

设计原则：**千问只负责「看懂截图」，代码负责「比对结论」**。

  千问（视觉/文本） → 公示里写了谁中标、什么名次、哪个包件
  代码             → 把公示名单和本地算出的价格分排名逐家比对，
                     判断「价格分能不能解释这个结果」，生成核对结论

这样整条链路不依赖任何人工录入：文件夹里有什么就算什么。
公示信息属于外部已定结论，不参与任何分数计算，只用于事后核对。
"""
from __future__ import annotations

import re

from bidsummary import llm, scoring
from bidsummary.config import CONFIG
from bidsummary.models import FileItem, LotResult, PublicityCandidate, PublicityInfo
from bidsummary.textutil import project_similarity

__all__ = ["extract_publicity", "cross_check"]

# ------------------------------------------------------------------ 提示词
PUBLICITY_SCHEMA = """{
  "doc_type": "公示类型，取值为：中标候选人公示 / 中标结果公示 / 成交公告 / 入围结果公示 / 其他",
  "project_name": "公示中出现的项目全称；图中没有则 null",
  "project_code": "项目编号或招标编号；图中没有则 null",
  "publish_date": "发布日期，格式 YYYY-MM-DD；图中没有则 null",
  "notice_period": "公示期原文，如「2026-09-07 至 2026-09-10」；图中没有则 null",
  "lot_name": "本公示对应的包件或标段名称；未分标段则 null",
  "candidates": [
    {
      "rank_label": "原文的名次表述，逐字照抄，如「第一中标候选人」「中标人」「入围供应商」",
      "company": "中标单位全称，逐字照抄",
      "lot_name": "该候选人所属包件；未分标段则 null",
      "price": "图中明确列出的报价数字（不带单位和逗号）；没有则 null"
    }
  ],
  "note": "其他影响解读的信息，如「按综合得分排序」「公示期已结束」；没有则 null",
  "confidence": 0.0 到 1.0 之间的数字，表示你对本次识别的把握程度
}"""

PUBLICITY_RULES = """【抽取要求】
1. candidates 必须按图中出现顺序**完整列出**，不要只取前三名，不要合并同名条目，不要按报价重排。
2. rank_label 必须是原文写法（第一/第二/第三中标候选人、中标人、成交供应商、入围供应商…），
   不要统一改写成「第1名」。
3. company 必须逐字照抄：不得简写、不得省略「有限公司」「股份有限公司」等字样、
   不得修正图中的错别字。名称中间的换行请拼接后再输出。
4. price 只在图中明确列出报价时才填；金额只填数字，去掉「元」「￥」和千分位逗号。
5. 图中若同时公示多个包件，必须用 lot_name 区分，不得漏掉任何包件。
6. 图中看不到的信息一律填 null，严禁按常见公示格式推测。
7. 只输出 JSON 对象，不要解释文字，不要代码块标记。"""


def _vision_prompt(filename: str) -> str:
    return (
        f"下面这张图片是开标文件夹中的「{filename}」，可能是中标候选人公示、中标结果公示、"
        "成交公告或入围结果公示的截图。请抽取公示信息，按下列 JSON 结构输出。\n\n"
        "【输出结构】\n" + PUBLICITY_SCHEMA + "\n\n" + PUBLICITY_RULES
    )


def _text_prompt(filename: str, md: str) -> str:
    return (
        f"下面是开标文件夹中「{filename}」的文本内容，可能是中标候选人公示、中标结果公示、"
        "成交公告或入围结果公示。请抽取公示信息，按下列 JSON 结构输出。\n\n"
        "【输出结构】\n" + PUBLICITY_SCHEMA + "\n\n" + PUBLICITY_RULES
        + "\n\n【文件内容】\n" + md[:12000]
    )


# ------------------------------------------------------------------ 解析
def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.sub(r"[,\s，]", "", str(v))
    m = re.sub(r"[^\d.\-]", "", m)
    try:
        return float(m)
    except ValueError:
        return None


def _s(v) -> str:
    return "" if v in (None, "null", "None") else str(v).strip()


def _clean_lot(name: str) -> str:
    """规范化公示里的包件名。

    「包件一:营业网点形象标识用品入围供应商包件」→「包件一 · 营业网点形象标识用品入围供应商」
    「包件一」→「包件一」
    """
    s = _s(name)
    if not s:
        return ""
    m = re.match(r"^\s*(?:包件|标包|标段)\s*([一二三四五六七八\d])\s*[:：·・]?\s*(.*)$", s)
    if not m:
        return re.sub(r"(包件|标包|标段)$", "", s).strip()
    num, rest = m.group(1), m.group(2).strip()
    rest = re.sub(r"(包件|标包|标段)$", "", rest).strip(" 　·・:：")
    return f"包件{num} · {rest}" if rest else f"包件{num}"


def _parse(filename: str, d: dict) -> PublicityInfo:
    cands: list[PublicityCandidate] = []
    for c in d.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        company = _s(c.get("company"))
        if not company:
            continue
        cands.append(PublicityCandidate(
            rank_label=_s(c.get("rank_label")) or "候选人",
            company=company,
            lot_name=_clean_lot(c.get("lot_name")),
            price=_num(c.get("price")),
        ))
    try:
        conf = float(d.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return PublicityInfo(
        source_file=filename,
        publish_date=_s(d.get("publish_date")),
        notice_period=_s(d.get("notice_period")),
        project_name=_s(d.get("project_name")),
        lot_name=_clean_lot(d.get("lot_name")),
        candidates=cands,
        note=_s(d.get("note")),
        confidence=max(0.0, min(1.0, conf)),
    )


# ------------------------------------------------------------------ 主流程
def extract_publicity(items: list[FileItem], project_name: str, log=print) -> list[PublicityInfo]:
    """识别文件夹内所有公示类文件；判定是否属于本项目。"""
    out: list[PublicityInfo] = []
    pubs = [i for i in items if i.kind == "PUBLICITY" and i.status == "PARSED"]
    if not pubs:
        return out

    for it in pubs:
        log(f"[公示] 识别 {it.rel}…")
        try:
            if it.ext.lower() in (".jpg", ".jpeg", ".png"):
                d = llm.vision_json(it.path.read_bytes(), _vision_prompt(it.name))
            else:
                d = llm.chat_json(_text_prompt(it.name, it.markdown),
                                  system=llm.SYS_VISION_EXTRACTOR)
        except Exception as e:
            log(f"       识别失败：{e}")
            continue

        info = _parse(it.rel, d)
        if not info.candidates:
            log("       未识别到候选人名单，跳过")
            continue

        # ---- 归属判定：公示里的项目名与本次项目名差太多 → 属于其他项目 ----
        if project_name and info.project_name:
            sim = project_similarity(project_name, info.project_name)
            info.matches_project = sim >= 0.45
            if not info.matches_project:
                it.status = "SKIPPED"
                it.error = f"公示属于其他项目：《{info.project_name}》，相似度 {sim:.2f}"
                log(f"       判定为其他项目的公示（相似度 {sim:.2f}），已排除")
                out.append(info)
                continue

        log(f"       公示 {len(info.candidates)} 条候选人"
            + (f"，发布日期 {info.publish_date}" if info.publish_date else ""))
        out.append(info)
    return out


# ------------------------------------------------------------------ 交叉核对（纯代码）
def _norm_company(s: str) -> str:
    """公司名归一化，用于与开标记录表比对（容忍括号、空格、公司后缀差异）。"""
    s = re.sub(r"[（(].*?[)）]", "", s or "")
    s = re.sub(r"[\s　·．.]", "", s)
    s = s.replace("股份有限公司", "有限公司")
    return s


CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}


def _lot_hint(text: str) -> int:
    """从「包件一」「包2」「标段三」「…/02」里取出包件序号，取不到返回 0。"""
    m = re.search(r"(?:包件|标包|标段|包)\s*([0-9一二三四五六七八])", text or "")
    if m:
        v = m.group(1)
        return int(v) if v.isdigit() else CN_NUM.get(v, 0)
    m = re.search(r"/(\d{1,3})\s*$", (text or "").strip())
    if m:
        return int(m.group(1))
    return 0


lot_hint = _lot_hint          # 公开别名：其他模块判断标段序号用


def _lot_order(lots: list[LotResult], hint: str) -> list[LotResult]:
    """按公示所属包件把标段排序：命中的排前面。

    同一家公司在多个包件都投标是常态（本案 57 家里大量重名），
    若不按包件限定，就会拿到另一个包件的同名行，位次全部算错。
    """
    idx = _lot_hint(hint)
    if not idx:
        return lots
    hit = [l for l in lots if idx in (_lot_hint(l.lot_name), _lot_hint(l.lot_no))]
    rest = [l for l in lots if l not in hit]
    return hit + rest


def _find_row(lots: list[LotResult], company: str):
    """在开标记录表里找这家公司，返回 (lot, row)。"""
    target = _norm_company(company)
    for lot in lots:
        for row in lot.rows:
            cand = _norm_company(row.name)
            if not cand or not target:
                continue
            if cand == target or cand in target or target in cand:
                return lot, row
    return None, None


def cross_check(info: PublicityInfo, lots: list[LotResult], rule_full: float) -> list[str]:
    """把公示名单与本表算出的价格分排名比对，生成人可读的核对结论。

    这里刻意不下「中标结果对不对」的判断——价格分只是评分的一部分，
    真正要回答的是：**价格分能不能解释这个公示结果**。
    """
    checks: list[str] = []
    if not lots or not info.candidates:
        return checks

    full = rule_full or 0.0
    hits = []
    missing = []
    dup_lots: list[str] = []
    for c in info.candidates:
        # 优先在公示所指包件里找；候选人自带包件名时以候选人的为准
        ordered = _lot_order(lots, c.lot_name or info.lot_name)
        lot, row = _find_row(ordered, c.company)
        if row is None:
            missing.append(c.company)
            continue
        # 同名出现在多个包件时，提示需按包件核对
        same_name = [l for l in lots
                     if any(_norm_company(r.name) == _norm_company(row.name) for r in l.rows)]
        if len(same_name) > 1:
            dup_lots.append(f"{c.company}（出现在 {'、'.join(l.lot_name for l in same_name)}）")
        hits.append((c, lot, row))

    # 1) 名单能否对上开标记录表
    if missing:
        checks.append("公示名单中 "
                      + "、".join(missing)
                      + " 未在开标记录表中找到同名投标人，请核对是否名称写法不同或不属于本包件。")

    if not hits:
        return checks

    # 2) 逐家说明：价格分、并列情况、在并列组中的报价位次
    for c, lot, row in hits:
        if row.price_score is None:
            continue
        same_score = [r for r in lot.rows
                      if r.price_score is not None
                      and abs(r.price_score - row.price_score) < 0.005]
        is_full = full and abs(row.price_score - full) < 0.005

        if is_full and len(same_score) >= 3:
            # 并列满分时，按评标价升序算它在并列组里的位置
            group = sorted(same_score, key=lambda r: (r.total if r.total is not None else 0))
            pos = next((i for i, r in enumerate(group, 1) if r is row), 0)
            checks.append(
                f"{c.rank_label}「{row.name}」〔{lot.lot_name}〕价格分 {row.price_score:.2f}（满分），"
                f"属 {len(same_score)} 家并列满分企业之一，其评标价在并列组内按低价排第 {pos} 位"
                f"——价格分无法区分名次，最终名次由技术、商务与服务部分得分决定。"
            )
        elif is_full:
            checks.append(f"{c.rank_label}「{row.name}」〔{lot.lot_name}〕价格分 "
                          f"{row.price_score:.2f}（满分），价格维度无异议。")
        else:
            rank = row.rank
            checks.append(
                f"{c.rank_label}「{row.name}」〔{lot.lot_name}〕价格分 {row.price_score:.2f}，"
                f"在该标段 {len(lot.rows)} 家中价格分排第 {rank} 位"
                + ("；说明该企业依靠非价格部分得分胜出，价格分不构成其排名依据。"
                   if rank > 3 else "；价格排名与公示名次基本吻合。")
            )

    # 3) 公示名次与价格分名次是否冲突（只在非并列时给结论）
    if len(hits) >= 2 and full:
        pairs = [(c, lot, row) for c, lot, row in hits if row.price_score is not None]
        all_full = all(abs(r.price_score - full) < 0.005 for _, _, r in pairs)
        if all_full:
            checks.append("公示名单中的候选人价格分均为满分，仅凭价格分无法复现公示名次，"
                          "属正常情形（评分办法中技术/商务/服务分占比更高）。")

    # 4) 公示报价与本表读取的报价是否一致（防截图中另有数字）
    for c, lot, row in hits:
        if c.price is None or row.total is None:
            continue
        if row.total and abs(c.price - row.total) / max(row.total, 1) > 0.005:
            checks.append(f"{c.rank_label}「{row.name}」公示报价 {c.price:,.2f} 元与开标记录表"
                          f" {row.total:,.2f} 元不一致，请核对是否含税/不含税口径不同或公示另有依据。")

    # 5) 多家包件同时投标的提示（防止拿错标段）
    if dup_lots:
        checks.append("以下单位在多个包件同时投标，核对时已按公示所列包件取值，请确认包件一致："
                      + "；".join(dup_lots) + "。")

    return checks


def cross_check_rows(info: PublicityInfo, rows: list) -> list[str]:
    """模式 A（一家投标人一个文件夹）下的核对：公示名单能否对上已抽取的投标人及其报价位次。"""
    checks: list[str] = []
    if not rows or not info.candidates:
        return checks

    def price_of(r) -> float | None:
        f = r.fields.get("bid_price")
        if not f or f.value in (None, ""):
            return None
        try:
            return float(f.value)
        except (TypeError, ValueError):
            return None

    ranked = sorted([r for r in rows if price_of(r) is not None], key=price_of)
    pos = {id(r): i for i, r in enumerate(ranked, 1)}
    by_name = {_norm_company(r.bidder): r for r in rows}

    missing: list[str] = []
    for c in info.candidates:
        key = _norm_company(c.company)
        row = by_name.get(key) or next(
            (r for k, r in by_name.items() if k and (k in key or key in k)), None)
        if row is None:
            missing.append(c.company)
            continue
        p = price_of(row)
        if p is None:
            checks.append(f"{c.rank_label}「{row.bidder}」的投标报价未能从文件中抽取，无法核对。")
            continue
        checks.append(f"{c.rank_label}「{row.bidder}」报价 {p:,.2f} 元，"
                      f"在已抽取的 {len(ranked)} 家中按报价由低到高排第 {pos[id(row)]} 位。")

    if missing:
        checks.append("公示名单中 " + "、".join(missing)
                      + " 未在本次抽取的投标人中找到，请核对是否名称写法不同。")
    return checks
