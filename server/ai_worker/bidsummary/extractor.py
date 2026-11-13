"""阶段 4+5+6：章节定位 -> 千问结构化抽取 -> 规则校验。

支持四种项目形态（按优先级自动判定）：
  模式 B（opening_table）：所有投标人写在一张开标记录表里 —— 逐行直读 + 计算价格分
  模式 C（shortlist）：入围单价矩阵 —— 产品权重附表 + 投标单位×产品单价大表，∑单价×权重评分
  模式 D（discount）：下浮率 / 折扣率报价 —— 只报一个百分比；两种组合自动判定：
                      下浮率 + 满分偏差扣分（苏州）/ 折扣率 + 基本分增减分（周口）
  模式 A（bidder_folder）：一家投标人一个文件夹 —— 逐个投标人抽取
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from bidsummary import (discount, llm, opening, publicity, rules, scoring,
                        sheetquote, shortlist)
from bidsummary.config import CONFIG, Config
from bidsummary.fields import BIDDER_FIELDS, PROJECT_FIELDS, FieldDef
from bidsummary.models import BidderRow, FieldValue, FileItem, SummaryResult
from bidsummary.textutil import locate, locate_balanced, project_similarity

__all__ = ["run_extraction", "locate", "validate_row"]


# ------------------------------------------------------------------ 上下文拼接
def build_context(files: list[FileItem], defs: list[FieldDef], cfg: Config,
                  max_chars: int) -> str:
    """把多个文件的相关片段拼成一段上下文，带文件名锚点。

    两个必须同时满足的约束：
      · 每个文件都有配额——否则先出现的文件会把预算吃光，后面的招标文件完全进不来；
      · 超长文件按字段均衡采样——否则 100+ 页招标文件的「评标办法」章节会被
        从头截断丢掉（本项目实测：单份文件拼出 81590 字，预算只有 24000）。
    """
    n = max(1, len(files))
    per = max(5000, (max_chars - 300 * n) // n)
    groups = [list(d.aliases) for d in defs]
    aliases = [a for d in defs for a in d.aliases]
    blocks: list[str] = []
    for f in files:
        if not f.markdown:
            continue
        snippet = locate(f.markdown, aliases, cfg.snippet_window)
        if not snippet:
            snippet = f.markdown[:4000]
        if len(snippet) > per:
            sampled = locate_balanced(f.markdown, groups, cfg.snippet_window, per)
            if sampled:
                snippet = sampled
        if len(snippet) > per:
            snippet = snippet[:per]
        blocks.append(f"===== 文件：{f.rel} =====\n{snippet}")
    return "\n\n".join(blocks)


# ------------------------------------------------------------------ 提示词
RULES = """【抽取总则】
1. 只依据【资料】里出现的原文作答。资料没写的：value 填 null、confidence 填 0、note 填「未找到」。
   不得用行业常识补齐，不得从文件名或标题推断正文内容。
2. 取值必须是原文的字面值，不得改写格式：
   · 项目编号、日期、单位名称 —— 逐字照录，保留原文的括号、空格、连字符、全半角
   · 金额 —— 先按原文记录，再换算成以「元」为单位的纯数字放进 value，原文写法放进 display
3. 溯源：source_file 填所属文件名（即「===== 文件：xxx =====」中的 xxx）；
   source_page 填该内容往前最近的 <!-- page:N --> 里的 N，没有页码锚点填 null；
   quote 填支撑该取值的原文片段（不超过 60 字，必须逐字摘录，不得改写或概括）。
4. 冲突处理：同一字段在资料中出现多个不同取值时，value 取出现次数最多的那个，
   并把**全部**取值放进 candidates（各带 source_file 与 quote），note 注明「存在多个取值」。
   严禁自行判断哪个正确，选择权必须留给人。
5. 置信度按标尺如实填写，不要一律填 0.9：
   0.95~1.00　原文逐字照录，字段名与取值紧邻，无歧义
   0.80~0.94　需简单处理：大写金额换算、跨行拼接、表头与数值分列
   0.60~0.79　多处表述不一致但可判断，或需结合上下文理解
   0.00~0.59　依赖推断、字迹模糊、扫描件可能识别有误
6. 只输出一个 JSON 对象，键名严格使用下方给定的字段，不得增删、不得改名、不得嵌套。"""


def _schema_block(keys: list[str]) -> str:
    tmpl = {
        "value": "<机器可用值；找不到填 null>",
        "display": "<用于表格展示的原文字符串>",
        "confidence": "<0~1 的数字，按总则第 5 条标尺评估>",
        "source_file": "<来源文件名>",
        "source_page": "<页码数字，没有则 null>",
        "quote": "<支撑该值的原文逐字片段，≤60 字>",
        "candidates": "[{'value':..,'source_file':..,'quote':..}]，仅多处取值冲突时填写，否则为 []",
        "note": "<差异说明；没有则 null>",
    }
    return json.dumps({k: tmpl for k in keys}, ensure_ascii=False, indent=2)


def build_bidder_prompt(bidder: str, context: str) -> str:
    keys = [d.key for d in BIDDER_FIELDS]
    desc = "\n".join(
        f"- {d.key}（{d.label}）类型={d.type}；原文常见写法={'/'.join(d.aliases)}；口径：{d.hint}"
        for d in BIDDER_FIELDS
    )
    return (
        f"下面是投标人「{bidder}」的投标文件片段。请按下方字段表抽取开标信息。\n\n"
        + RULES
        + "\n\n【本项目特有口径】\n"
        f"- bidder_name：以投标函落款或封面盖章单位为准。若与文件夹名「{bidder}」不一致，"
        "value 取文件内名称，note 写明「目录名：xxx」，便于核对。\n"
        "- bid_price：按优先级取 ①开标一览表/开标记录中的投标总价 → ②投标函报价 → ③分项报价合计。"
        "同一数字有多处出现时，把每一处都放进 candidates，不得自行取舍。\n"
        "- 若报价为费率、下浮率、折扣率形式（如「下浮 5%」「费率 92%」）：value 填 null，"
        "display 保留原文，note 写「费率报价，需人工折算」——**严禁自行折算成金额**。\n"
        "- price_in_words：原文照录，不要改写为阿拉伯数字。\n"
        "- deposit：原文只写「已递交/已缴纳」而无金额时，value 填 null，note 说明原文写法。\n"
        "- qualification：只能填「满足」「不满足」「存疑」三者之一；资料未提供资格证明文件时填「存疑」。\n"
        "- reject_reason：仅当资料中明确记载废标/否决/无效投标情形时才填，否则填 null。\n\n"
        "【待抽取字段说明】\n" + desc
        + "\n\n【输出格式】（每个字段一个对象，键名照抄）\n" + _schema_block(keys)
        + "\n\n【资料】\n" + context
    )


def build_project_prompt(context: str) -> str:
    keys = [d.key for d in PROJECT_FIELDS]
    desc = "\n".join(
        f"- {d.key}（{d.label}）原文常见写法={'/'.join(d.aliases)}；口径：{d.hint}"
        for d in PROJECT_FIELDS
    )
    return (
        "下面是本项目的招标文件与开标记录片段。请按下方字段表抽取项目基本信息。\n\n"
        + RULES
        + "\n\n【本项目特有口径】\n"
        "- project_name：取招标文件封面或招标公告标题上的项目全称，含括号内的补充说明（如服务期限、"
        "包件划分），不要截断、不要省略括号内容。\n"
        "- project_code：若同时存在「招标编号」「项目编号」「标段编号」，优先取覆盖整个项目的那个；"
        "其他编号写进 note，不要拼成一个字符串。\n"
        "- tenderer：取招标人/采购人全称（合同签约主体），不要填代理机构。\n"
        "- open_time / open_place：逐字照录，不要补「北京时间」等原文没有的字样。\n"
        "- budget：取最高投标限价或预算金额；若分标段/分年度列示，value 填总额，"
        "分项金额写进 note；原文只列单价限价时，value 填 null，note 说明「仅设单价限价」。\n"
        "- 若资料涉及多个标段/包件，抽取项目整体信息，各标段差异与限价差异写进 note。\n\n"
        "【待抽取字段说明】\n" + desc
        + "\n\n【输出格式】（每个字段一个对象，键名照抄）\n" + _schema_block(keys)
        + "\n\n【资料】\n" + context
    )


# ------------------------------------------------------------------ 解析响应
def _to_field(defs: dict[str, FieldDef], key: str, raw: dict) -> FieldValue:
    d = defs.get(key)
    raw = raw if isinstance(raw, dict) else {}
    val = raw.get("value")
    try:
        conf = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return FieldValue(
        key=key, label=d.label if d else key, value=val,
        display=str(raw.get("display") or ""), confidence=conf,
        source_file=str(raw.get("source_file") or ""),
        source_page=("" if raw.get("source_page") in (None, "null") else str(raw.get("source_page"))),
        quote=str(raw.get("quote") or ""),
        candidates=[c for c in (raw.get("candidates") or []) if isinstance(c, dict)],
        note="" if raw.get("note") in (None, "null") else str(raw.get("note")),
    )


def parse_response(defs: dict[str, FieldDef], data: dict) -> dict[str, FieldValue]:
    return {k: _to_field(defs, k, data.get(k)) for k in defs}


# ------------------------------------------------------------------ 校验
def _to_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", "").replace("，", ""))
    try:
        return float(s)
    except ValueError:
        return None


def _cn2num(text: str) -> float | None:
    try:
        import cn2an
        return float(cn2an.cn2an(text, "smart"))
    except Exception:
        return None


def validate_row(row: BidderRow, budget: float | None) -> None:
    price = row.fields.get("bid_price")
    if price:
        p = _to_float(price.value)
        if p is not None:
            price.value = p
            words = row.fields.get("price_in_words")
            if words and words.value:
                n = _cn2num(str(words.value))
                if n is not None and abs(n - p) > max(1.0, p * 0.01):
                    price.confidence = min(price.confidence, 0.55)
                    row.flags.append("报价小写与大写不一致，请人工核对")
            if budget and (p <= 0 or p > budget * 1.5):
                price.confidence = min(price.confidence, 0.6)
                row.flags.append(f"报价 {p:,.2f} 元超出预算 {budget:,.2f} 元的合理区间")

        vals = {_to_float(c.get("value")) for c in price.candidates}
        vals.discard(None)
        if p is not None:
            vals.add(p)
        if len(vals) > 1:
            price.conflict = True
            row.flags.append(f"报价存在 {len(vals)} 种取值（" +
                             "、".join(f"{v:,.2f}" for v in sorted(vals)) + "），需人工裁决")

    dep = row.fields.get("deposit")
    if dep and dep.value is not None:
        dep.value = _to_float(dep.value)

    for f in row.fields.values():
        if f.value in (None, "") and not f.note:
            f.note = "未找到"


# ------------------------------------------------------------------ 干扰文件识别
def _compact(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


TENDER_MARKS = ("投标人须知", "评标办法", "招标公告", "投标文件格式", "合同条款",
                "投标邀请", "采购需求")


def refine_kinds(parsed: list[FileItem], log=print) -> None:
    """文件名看不出类型时按正文判定。

    真实项目里招标文件常被命名成「××采购××采购.pdf」，既没有「招标文件」也没有「开标」，
    若只按文件名分类，整条链路会退化成「一家投标人」的空表。
    """
    for f in parsed:
        if f.kind != "OTHER" or not f.markdown:
            continue
        if f.ext not in (".pdf", ".doc", ".docx", ".txt"):
            continue
        hits = [k for k in TENDER_MARKS if k in f.markdown]
        if len(hits) >= 2:
            f.kind = "TENDER_DOC"
            log(f"[识别] {f.name} 按正文判定为招标文件（含「{hits[0]}」等 {len(hits)} 个章节标题）")


def _verify_code(fv: FieldValue, corpus: str) -> None:
    """项目编号必须能在原文里找到；模型把标段后缀拼进来时按原文回退。"""
    v = str(fv.value or "").strip()
    if not v or not corpus:
        return
    cc = _compact(corpus)
    if v in corpus or _compact(v) in cc:
        return
    parts = v.split("/")
    for i in range(len(parts) - 1, 0, -1):
        cand = "/".join(parts[:i]).strip()
        if cand and _compact(cand) in cc:
            fv.note = (fv.note + "；" if fv.note else "") + (
                f"原文未见「{'/'.join(parts[i:])}」，已按原文回退为 {cand}")
            fv.value = cand
            fv.display = cand
            return


OPEN_TIME_RE = re.compile(r"开标时间\s*[：:]\s*([^\n|]{6,40})")


def _fill_eval_method(project: dict, corpus: str) -> None:
    """评标办法的确定性兜底：模型漏抽时，从招标文件原文直接抠「综合评估法」等。

    评标办法是固定术语（综合评估法/综合评分法/最低评标价法/经评审的最低投标价法），
    原文一定能定位到，不该因千问一次漏抽就显示「未找到」。
    """
    fv = project.get("eval_method")
    if fv and fv.value not in (None, "", "未找到"):
        return
    flat = re.sub(r"[\s　]+", "", corpus or "")
    patterns = [
        r"评标办法[（(]?(综合评估法|综合评分法|最低评标价法|经评审的最低投标价法)",
        r"采用(综合评估法|综合评分法|最低评标价法|经评审的最低投标价法)",
        r"本?项目评标(?:采用|办法为)[：:]?(综合评估法|综合评分法|最低评标价法|经评审的最低投标价法)",
    ]
    hit = ""
    for pat in patterns:
        m = re.search(pat, flat)
        if m:
            hit = m.group(1)
            break
    if not hit:
        for kw in ("综合评估法", "综合评分法", "最低评标价法", "经评审的最低投标价法"):
            if kw in flat:
                hit = kw
                break
    if hit:
        if fv is None:
            fv = FieldValue(key="eval_method", label="评标办法", value=hit,
                            confidence=0.9, note="原文术语直接定位")
            project["eval_method"] = fv
        else:
            fv.value = hit
            fv.display = hit
            fv.confidence = max(fv.confidence or 0, 0.9)
            fv.note = (fv.note + "；" if fv.note else "") + "原文术语直接定位"


def _crosscheck_open_time(project: dict, parsed: list[FileItem], log=print) -> list[str]:
    """用代码核对开标时间的多来源冲突（模型可能只看到其中一处）。

    招标文件常写「计划开标时间」，开标记录写「实际开标时间」，二者可能相差数日
    （本项目：招标文件 10 月 15 日，两份开标记录均为 10 月 23 日）。按抽取总则
    「同一字段多个取值时取出现次数最多的那个，其余全部列入候选」，这里逐文件投票：
    开标记录两票、招标文件一票，多数取值应为开标记录上的实际时间。
    """
    fv = project.get("open_time")
    if not fv or fv.value in (None, "") or not isinstance(fv, FieldValue):
        return []

    def norm(s):
        """把「2026年01月21日09:30整」与「2026年01月21日09时30分」视作同一时刻。

        同一时间的不同写法（时/分、全半角、带"整"字）不该被判成冲突；
        仍保留日期差异（如 10月15日 vs 10月23日 的延期开标）。
        """
        t = re.sub(r"[\s　]", "", str(s or ""))
        nums = re.findall(r"\d{1,4}", t)
        if len(nums) >= 5:
            return ("dt",) + tuple(int(x) for x in nums[:5])
        return ("txt", t)

    # 模型有时把后一句「开标地点：…」一并粘进开标时间，先裁到时间表达式本身
    m = re.match(r"\s*(\d{4}年\d{1,2}月\d{1,2}日[^\s，,；;]{0,14})", str(fv.value))
    if m and len(m.group(1)) < len(str(fv.value)):
        fv.value = m.group(1).strip()
        fv.display = fv.value

    votes: dict[str, dict] = {}
    for f in parsed:
        if f.kind not in ("OPENING_RECORD", "TENDER_DOC") or not f.markdown:
            continue
        for m in OPEN_TIME_RE.finditer(f.markdown[:20000]):
            v = re.split(r"[，,；;]", m.group(1).strip())[0].strip()
            # 占位符（—/-/未找到/同…截止时间）不是可作为「冲突」的具体时间，忽略，
            # 避免把它们与真实开标日期并列造成误报。
            if not v or v in ("—", "-", "－", "/", "未找到", "无") or v.startswith("同"):
                continue
            k = norm(v)
            if not k:
                continue
            item = votes.setdefault(k, {"display": v, "files": set()})
            item["files"].add(f.rel)
    if not votes:
        return []

    ranked = sorted(votes.items(), key=lambda kv: (-len(kv[1]["files"]), kv[0]))
    top_k, top = ranked[0]
    cur = str(fv.value or "").strip()
    if top_k == norm(fv.value):
        return []
    # 模型给出的是占位/叙述性表述（如「同投标文件递交截止时间。」）→ 用原文的具体值
    # 静默补全，不当作「冲突」，避免污染待复核事项。
    if (not cur or cur in ("—", "-", "－", "/", "未找到", "无") or cur.startswith("同")):
        fv.value = top["display"]
        fv.display = top["display"]
        fv.source_file = "、".join(sorted(top["files"]))
        return []

    others = [v["display"] for k, v in ranked if k != top_k]
    old = str(fv.value)
    fv.candidates.append({"value": old, "source_file": fv.source_file,
                          "quote": fv.quote or f"开标时间：{old}"})
    for k, v in ranked:
        if k == top_k:
            continue
        fv.candidates.append({"value": v["display"],
                              "source_file": "、".join(sorted(v["files"])),
                              "quote": f"开标时间：{v['display']}"})
    fv.value = top["display"]
    fv.display = top["display"]
    fv.source_file = "、".join(sorted(top["files"]))
    fv.conflict = True
    fv.confidence = min(fv.confidence, 0.75)
    fv.note = (fv.note + "；" if fv.note else "") + (
        f"多来源不一致，按出现次数最多取「{top['display']}」，其余取值已列入候选")
    log(f"[核对] 开标时间冲突：多数取值「{top['display']}」，另有「{'、'.join(others)}」")
    return [f"开标时间存在多个取值：已按多数取「{top['display']}」，"
            f"招标文件另记「{'、'.join(others) or '—'}」，候选值已全部保留，"
            f"请人工核对是否为延期开标。"]


def _detect_unrelated(parsed: list[FileItem], project_name: str) -> list[dict]:
    """识别混进本项目的其他项目文件（如别家的入围公示截图）。"""
    if not project_name or len(project_name) < 6:
        return []
    out = []
    for f in parsed:
        if f.kind != "OTHER":
            continue
        m = re.search(r"项目名称\s*[：:]\s*([^\n|]{6,60})", f.markdown or "")
        if not m:
            continue
        other = m.group(1).strip()
        sim = project_similarity(project_name, other)
        if sim < 0.45:
            out.append({"file": f.rel, "reason": f"内容属于其他项目：《{other}》，相似度 {sim:.2f}"})
            f.status = "SKIPPED"
            f.error = out[-1]["reason"]
    return out


# ------------------------------------------------------------------ 主流程
def run_extraction(items: list[FileItem], cfg: Config | None = None, log=print) -> SummaryResult:
    cfg = cfg or CONFIG
    from bidsummary.scanner import group_by_bidder

    result = SummaryResult(files=items)
    parsed = [i for i in items if i.status == "PARSED"]
    result.failed_files = [{"file": i.rel, "reason": i.error} for i in items if i.status == "FAILED"]
    if not parsed:
        log("[警告] 没有任何文件解析成功")
        return result

    # ---------- 项目级信息 ----------
    refine_kinds(parsed, log)
    proj_files = [f for f in parsed if f.kind in ("TENDER_DOC", "OPENING_RECORD")]
    proj_files = proj_files or sorted(parsed, key=lambda f: f.priority)[:2]
    ctx = build_context(proj_files, PROJECT_FIELDS, cfg, cfg.max_chars_project)
    try:
        data = llm.chat_json(build_project_prompt(ctx))
        result.project = parse_response({d.key: d for d in PROJECT_FIELDS}, data)
        corpus = "\n\n".join(f.markdown for f in proj_files)
        _fill_eval_method(result.project, corpus)
        if result.project.get("project_code"):
            _verify_code(result.project["project_code"], corpus)
        result.warnings.extend(_crosscheck_open_time(result.project, parsed, log))
        log(f"[抽取] 项目信息完成（{len(proj_files)} 份文件）")
    except Exception as e:
        log(f"[警告] 项目信息抽取失败：{e}")
        result.project = {d.key: FieldValue(key=d.key, label=d.label, note="抽取失败")
                          for d in PROJECT_FIELDS}

    project_name = str(result.project.get("project_name").value or "")

    # ---------- 干扰文件 ----------
    result.excluded_files = _detect_unrelated(parsed, project_name)
    for e in result.excluded_files:
        log(f"[排除] {e['file']} —— {e['reason']}")
    parsed = [i for i in parsed if i.status == "PARSED"]

    # ---------- 公示信息（千问识别 + 代码核对，纯自动） ----------
    pubs = publicity.extract_publicity(parsed, project_name, log)
    for p in pubs:
        if p.matches_project:
            result.publicity.append(p)
        else:
            result.excluded_files.append({
                "file": p.source_file,
                "reason": f"公示内容属于其他项目：《{p.project_name}》"})
    # 公示属终态信息，不再参与后续字段抽取，避免干扰报价与规则判断
    parsed = [i for i in parsed if i.kind != "PUBLICITY" and i.status == "PARSED"]

    # ---------- 模式判定：开标记录表 >> 投标人文件夹 ----------
    lots = opening.extract_lots(parsed, log)
    if lots:
        # 按包件号排序，避免 sheet 顺序导致「包件二」排在「包件一」前面
        def _pkg_key(l):
            n = publicity.lot_hint(l.lot_no) or publicity.lot_hint(l.lot_name)
            return (n == 0, n, l.lot_name)
        lots.sort(key=_pkg_key)
        result.lots = lots
        tender = [f for f in parsed if f.kind == "TENDER_DOC"]
        tender_md = "\n\n".join(f.markdown for f in tender)
        labels = [p.label for p in lots[0].parts]
        log(f"[抽取] 解析价格评分规则（{len(labels)} 个报价区间）…")
        result.rule = rules.extract_rule(
            tender_md, labels, tender[0].rel if tender else "", log)
        log(f"       规则：{result.rule.method or '未识别'} 总分 {result.rule.total_score:g}"
            f"  基准价系数 {result.rule.benchmark_factor}")
        if not result.rule.confirmed:
            log(f"       ⚠ {result.rule.basis}")
            result.warnings.append(f"价格评分规则：{result.rule.basis}。"
                                   "表中价格分仅供参考，请以评标委员会口径为准。")
        budget = None
        bv = result.project.get("budget")
        if bv:
            try:
                budget = float(bv.value)
            except (TypeError, ValueError):
                budget = None
        pno = str(result.project.get("project_code").value or "") if result.project.get("project_code") else ""
        suffix = pno.split("/")[-1] if "/" in pno else ""
        for lot in lots:
            # 标段名只有「包件一」时，用招标文件里的包件名称补全
            if re.fullmatch(r"包件[一二三四五六]", lot.lot_name or ""):
                m = re.search(re.escape(lot.lot_name) + r"\s*[：:]\s*([^，。；\n]{4,30})", tender_md)
                if m:
                    lot.lot_name = f"{lot.lot_name} · {m.group(1).strip()}"
            if not lot.lot_no and suffix:
                m = re.search(r"包件\s*([一二三四五六])", lot.lot_name or "")
                idx = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}.get(
                    m.group(1), 0) if m else 0
                if idx and suffix.lstrip("0") == str(idx):
                    lot.lot_no = pno
            scoring.score_lot(lot, result.rule)
            lot_suffix = lot.lot_no.split("/")[-1] if "/" in (lot.lot_no or "") else ""
            same_package = (not lot_suffix) or (not suffix) or lot_suffix == suffix
            result.warnings.extend(scoring.analyze(
                lot, result.rule, budget if same_package else None))
            if budget and not same_package:
                result.warnings.append(
                    f"{lot.lot_name}：本文件夹招标文件对应包件编号 {pno or '—'}，"
                    f"与 {lot.lot_no} 不同，未用该预算做超限价判断，请补对应包件的限价文件。")

        # ---------- 公示名单 × 本表价格分：交叉核对（纯代码，无人工） ----------
        for p in result.publicity:
            p.checks = publicity.cross_check(p, lots, result.rule.total_score)
            if p.checks:
                log(f"[公示] {p.source_file} 完成核对（{len(p.checks)} 条结论）")
                for c in p.checks:
                    log(f"       · {c}")

        result.tokens = llm.usage()
        return result

    # ---------- 模式 E：投标人写在 Excel sheet 名里（一 sheet 一家，分项加权最低价法） ----------
    # 必须排在模式 C 之前：这类工作簿每个 sheet 都带「产品名称+权重」列，
    # 若交给模式 C 会被当成 31 张权重附表，且找不到「投标单位」列而退化成空表。
    sq = sheetquote.extract_sheet_quote(parsed, log)
    if sq:
        result.shortlist = sq
        result.warnings.extend(sq.warnings)
        if not sq.confirmed:
            result.warnings.append(sq.basis)
        result.tokens = llm.usage()
        return result

    # ---------- 模式 C：入围单价矩阵（无开标记录表，按 产品单价×权重 评分） ----------
    sl = shortlist.extract_shortlist(parsed, log)
    if sl:
        result.shortlist = sl
        result.warnings.extend(sl.warnings)
        if not sl.confirmed:
            result.warnings.append(sl.basis)
        result.tokens = llm.usage()
        return result

    # ---------- 模式 D：下浮率 / 折扣率报价（PDF 开标表只报一个百分比） ----------
    dis = discount.extract_discount(parsed, log)
    if dis:
        result.discount = dis
        result.warnings.extend(dis.warnings)
        if not dis.confirmed:
            result.warnings.append(dis.basis)
        result.tokens = llm.usage()
        return result

    # ---------- 模式 A：按投标人文件夹 ----------
    budget = _to_float(result.project.get("budget").value) if result.project.get("budget") else None
    groups = group_by_bidder(parsed)
    unknown = groups.pop("（未识别投标人）", None)
    if unknown and not groups:
        groups["（未识别投标人）"] = unknown
        log("[提示] 未能按目录识别出投标人，将全部文件按一家处理")
        msg = ("未在文件夹内找到「开标记录表 / 开标一览表」（需含投标人名称与报价列），"
               "也没有识别出按投标人分的子目录，已把全部文件当作一家处理。"
               "请确认开标一览表是否放在本文件夹内，或检查该表表头是否含「投标人名称/投标单位」与报价列。")
        log(f"[警告] {msg}")
        result.warnings.append(msg)

    bidders = list(groups.items())
    log(f"[抽取] 开始抽取 {len(bidders)} 家投标人信息（并发 {cfg.concurrency}）")

    def work(bidder: str, files: list[FileItem]) -> BidderRow:
        ctx = build_context(files, BIDDER_FIELDS, cfg, cfg.max_chars_per_bidder)
        row = BidderRow(bidder=bidder, files=[f.rel for f in files])
        try:
            data = llm.chat_json(build_bidder_prompt(bidder, ctx))
            row.fields = parse_response({d.key: d for d in BIDDER_FIELDS}, data)
            name_f = row.fields.get("bidder_name")
            if name_f and name_f.value and str(name_f.value).strip() != bidder:
                name_f.note = (name_f.note + "；" if name_f.note else "") + f"目录名：{bidder}"
            if not name_f or not name_f.value:
                row.fields["bidder_name"] = FieldValue(
                    key="bidder_name", label="投标人名称", value=bidder,
                    confidence=0.8, source_file=files[0].rel, note="取自文件夹名称")
        except Exception as e:
            row.fields = {d.key: FieldValue(key=d.key, label=d.label, note="抽取失败")
                          for d in BIDDER_FIELDS}
            row.fields["bidder_name"] = FieldValue(
                key="bidder_name", label="投标人名称", value=bidder, confidence=0.8,
                source_file=files[0].rel)
            row.flags.append(f"抽取失败：{e}")
        validate_row(row, budget)
        return row

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        futs = {pool.submit(work, b, fs): b for b, fs in bidders}
        for fut in as_completed(futs):
            b = futs[fut]
            done += 1
            try:
                result.rows.append(fut.result())
                log(f"  [{done}/{len(bidders)}] {b} 完成")
            except Exception as e:
                log(f"  [{done}/{len(bidders)}] {b} 异常：{e}")

    def sort_key(r: BidderRow):
        v = _to_float(r.fields.get("bid_price").value) if r.fields.get("bid_price") else None
        return (v is None, v if v is not None else 0)

    result.rows.sort(key=sort_key)

    for p in result.publicity:
        p.checks = publicity.cross_check_rows(p, result.rows)
        for c in p.checks:
            log(f"[公示] · {c}")

    result.tokens = llm.usage()
    return result
