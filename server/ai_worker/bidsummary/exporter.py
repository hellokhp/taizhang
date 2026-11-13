"""阶段 7：输出 Excel。

四种形态：
  模式 B（开标记录表）→ 项目概况 + 每个标段一张「开标报价分析表」+ 来源追溯 + 文件清单
  模式 C（入围单价矩阵）→ 项目概况 + 价格评分汇总 + 单价明细 + 评分依据 + 文件清单
  模式 D（下浮率/折扣）→ 项目概况 + 价格评分汇总 + 产品单价明细 + 评分依据 + 文件清单
  模式 A（投标人文件夹）→ 开标汇总表 + 来源追溯 + 文件清单
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from bidsummary.config import CONFIG
from bidsummary.fields import BIDDER_FIELDS, PROJECT_FIELDS
from bidsummary.models import FieldValue, LotResult, SummaryResult
from bidsummary.scoring import summarize_lot

TITLE_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
LABEL_FILL = PatternFill("solid", fgColor="F2F2F2")
NOTE_FILL = PatternFill("solid", fgColor="FFF2CC")
TOP3_FILL = PatternFill("solid", fgColor="E2EFDA")
BAD_FILL = PatternFill("solid", fgColor="FFC7CE")
LEVEL_FILL = {
    "green": PatternFill("solid", fgColor="C6EFCE"),
    "yellow": PatternFill("solid", fgColor="FFEB9C"),
    "red": PatternFill("solid", fgColor="FFC7CE"),
}
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

KIND_LABEL = {
    "TENDER_DOC": "招标文件", "OPENING_RECORD": "开标记录", "PRICE_SHEET": "报价文件",
    "QUALIFICATION": "资格文件", "BID_FILE": "投标文件", "PUBLICITY": "公示公告",
    "OTHER": "其他",
}

FMT_MONEY = "#,##0.00"
FMT_SCORE = "0.00"
FMT_PCT = "0.00%"


def _cell(ws, r, c, value, *, font=None, fill=None, align="left", wrap=False,
          border=True, fmt=None):
    cell = ws.cell(row=r, column=c, value=value)
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    if border:
        cell.border = BORDER
    if fmt:
        cell.number_format = fmt
    return cell


def _fmt_money(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _short_label(label: str, i: int) -> str:
    s = (label or "").replace("最高投标单价限价", "")
    for t in ("元的，含税单价合计", "的，含税单价合计", "的含税单价合计", "元的含税单价合计",
              "含税单价合计", "不含税单价合计", "，"):
        s = s.replace(t, "")
    s = s.strip(" ：:") or f"区间{i + 1}"
    return f"区间{i + 1}\n{s}"


def _safe_sheet_name(name: str, used: set[str]) -> str:
    s = re.sub(r"[\\/*?:\[\]]", "", name or "标段")[:28] or "标段"
    base, n = s, 2
    while s in used:
        s = f"{base[:26]}_{n}"
        n += 1
    used.add(s)
    return s


# ================================================================== 公示核对块
def _block_publicity(ws, r: int, ncol: int, result: SummaryResult) -> int:
    """把公示信息写入项目概况。

    数据全部来自流水线本身：千问识别截图 → 结构化字段；核对结论由 scoring/publicity
    的纯代码逻辑生成。全程不需要人工录入，重新跑一遍就是最新结果。
    """
    for idx, p in enumerate(result.publicity):
        title = "中标候选人公示（千问识别，代码核对；外部既定结论，不参与评分计算）"
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
        _cell(ws, r, 1, title, font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
        ws.row_dimensions[r].height = 22
        r += 1

        meta = [f"来源文件：{p.source_file}"]
        if p.publish_date:
            meta.append(f"发布日期：{p.publish_date}")
        if p.notice_period:
            meta.append(f"公示期：{p.notice_period}")
        if p.lot_name:
            meta.append(f"公示标段：{p.lot_name}")
        meta.append(f"识别置信度：{p.confidence:.0%}" if p.confidence else "识别置信度：未给出")
        _cell(ws, r, 1, "公示信息", font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, "　｜　".join(meta), wrap=True)
        ws.row_dimensions[r].height = 20
        r += 1

        lines = []
        for c in p.candidates:
            prefix = f"{c.lot_name}　" if c.lot_name else ""
            price = f"　报价 {c.price:,.2f} 元" if c.price is not None else ""
            lines.append(f"{prefix}{c.rank_label}：{c.company}{price}")
        _cell(ws, r, 1, "公示名单", font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, "\n".join(lines) if lines else "未识别到候选人名单", wrap=True)
        ws.row_dimensions[r].height = max(20, min(120, 16 * max(1, len(lines))))
        r += 1

        if p.checks or p.note:
            body = "\n".join([f"· {c}" for c in p.checks] + ([f"· 备注：{p.note}"] if p.note else []))
            _cell(ws, r, 1, "核对结论", font=Font(bold=True), fill=LABEL_FILL)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
            _cell(ws, r, 2, body, wrap=True, fill=NOTE_FILL)
            ws.row_dimensions[r].height = max(
                24, min(180, 16 * (len(p.checks) + (1 if p.note else 0))))
            r += 1

        if idx < len(result.publicity) - 1:
            r += 1
    return r


# ================================================================== 模式 B
def _sheet_overview(wb: Workbook, result: SummaryResult, folder: Path) -> None:
    ws = wb.active
    ws.title = "项目概况"
    ncol = 6

    for c in range(1, ncol + 1):
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    pname = (result.project.get("project_name").value if result.project.get("project_name")
             else None) or folder.name
    _cell(ws, 1, 1, f"{pname}　开标汇总", font=Font(size=16, bold=True, color="FFFFFF"),
          fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 34

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"数据来源：{folder}　｜　生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}"
          f"　｜　模型：{CONFIG.text_model}　｜　本表由 AI 辅助生成，须经人工复核确认",
          font=Font(size=9, color="808080"), align="center")

    r = 3
    for d in PROJECT_FIELDS:
        fv = result.project.get(d.key, FieldValue(key=d.key, label=d.label))
        _cell(ws, r, 1, d.label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, _fmt_money(fv.value) if d.type == "money" else fv.to_display(), wrap=True)
        r += 1

    # ---- 标段概览 ----
    r += 1
    _cell(ws, r, 1, "标段概览", font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    heads = ["标段", "标段编号", "投标人数量", "最低评标价", "最高评标价", "平均评标价"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, r, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r += 1
    for lot in result.lots:
        s = summarize_lot(lot)
        for i, v in enumerate([lot.lot_name, lot.lot_no, s["count"],
                               _fmt_money(s["min"]), _fmt_money(s["max"]), _fmt_money(s["avg"])],
                              start=1):
            _cell(ws, r, i, v, align="center" if i >= 3 else "left", wrap=True)
        r += 1

    # ---- 评分规则 ----
    r += 1
    _cell(ws, r, 1, "价格评分规则（由招标文件识别，分数由系统按公式计算）",
          font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    rule = result.rule
    parts_txt = "、".join(
        f"{p['label']}={p['score']:g}分" if p.get("score") is not None else f"{p['label']}=未识别"
        for p in (rule.parts or []))
    is_itemized = rule.behavior == "itemized_deviation"
    bench_txt = (rule.benchmark or "未识别")
    if is_itemized:
        bench_txt += "（各分项独立计算，无系数、无去极值）"
        if rule.benchmark_scope:
            bench_txt += f"；入围范围：{rule.benchmark_scope}"
    else:
        bench_txt += f"（平均值系数 {rule.benchmark_factor:g}）"
    lines = [
        ("评分方法", rule.method or "未识别"),
        ("价格总分", f"{rule.total_score:g} 分" if rule.total_score else "未识别"),
        ("计算公式", rule.formula or "未识别"),
        ("基准价", bench_txt),
        ("分项分值 Fn" if is_itemized else "区间分值", parts_txt or "未识别"),
        ("补充说明", rule.note or "—"),
        ("规则出处", f"{rule.source_file}"
                     + (f" 第 {rule.source_page} 页" if rule.source_page else "")),
        ("规则状态", rule.basis or ("招标文件明确" if rule.confirmed else "未确认")),
    ]
    for label, value in lines:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, value, wrap=True)
        ws.row_dimensions[r].height = 30
        r += 1

    # ---- 公示核对（千问识别截图 + 代码比对，无人工录入） ----
    if result.publicity:
        r += 1
        r = _block_publicity(ws, r, ncol, result)

    # ---- 待复核 ----
    r += 1
    _cell(ws, r, 1, "待复核事项", font=Font(bold=True, color="9C0006"), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    flags = [f"· {w}" for w in result.warnings]
    for lot in result.lots:
        for row in lot.rows:
            for f in row.flags:
                flags.append(f"· {lot.lot_name}／{row.name}：{f}")
    for f in result.excluded_files:
        flags.append(f"· 已排除干扰文件：{f['file']} —— {f['reason']}")
    for f in result.failed_files:
        flags.append(f"· 文件读取失败：{f['file']} —— {f['reason']}")
    if not flags:
        flags = ["· 无"]
    _cell(ws, r, 2, "\n".join(flags), wrap=True)
    ws.row_dimensions[r].height = max(30, min(220, 16 * len(flags)))

    r += 2
    _cell(ws, r, 1, "说明", font=Font(bold=True))
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    single_mode = any(l.pricing == "single" for l in result.lots)
    itemized_mode = result.rule.behavior == "itemized_deviation" and any(
        l.itemized for l in result.lots)
    if itemized_mode:
        rr = result.rule
        score_note = (
            f"2. 价格分按分项独立计算：每个报价分项各自取该分项各投标人报价的算术平均值作为"
            f"评标基准价；偏差率=(该项报价−该项基准价)÷该项基准价×100；"
            f"报价高于基准价 Dn=Fn−偏差率×100×E1({rr.itemized_e1:g})，"
            f"报价≤基准价 Dn=Fn+偏差率×100×E2({rr.itemized_e2:g})；"
            f"每个分项 Dn 扣完为止、最低 0 分（某项为负不冲抵其它分项）；\n"
            f"投标报价得分=D1+D2+D3+D4；\n"
            + (f"3. 评标基准价入围范围：{rr.benchmark_scope}；本表按全部有效投标人该分项报价"
               f"算术平均计算（未做商务技术入围筛选，须评标委员会核对入围名单）；\n"
               if rr.benchmark_scope else "")
            + "4. 价格分以投标报价（不含税）计算，含税报价仅作对照；\n")
    elif single_mode and result.rule.behavior == "full_deviation":
        rr = result.rule
        score_note = (
            f"2. 价格分按「P=N 得满分 {rr.total_score:g} 分；每高于 N 1% 扣 {rr.step_high:g} 分、"
            f"每低于 N 1% 扣 {rr.step_low:g} 分」计算，不足 1% 按插入法连续折算，扣完为止；"
            f"N 按有效投标人家数分档去极值后取算术平均（被去极值者仍参与评分）；\n")
    elif single_mode and result.rule.behavior == "deviation" and result.rule.base_score:
        score_note = (
            f"2. 价格分按「基本分 {result.rule.base_score:g} 分 + 偏差率 × "
            f"{result.rule.step_percent:g} 分/1%」计算：报价等于基准价得基本分，"
            f"低于基准价每低 1% 加分（最高 {result.rule.total_score:g} 分"
            + (f"，低 {result.rule.low_limit:g}% 以上改为每再低 1% 扣分" if result.rule.low_limit else "")
            + "），高于基准价每高 1% 扣分；\n")
    elif single_mode:
        score_note = (
            "2. 价格分按「A≤B 得满分，A>B 时 (B/A)×满分」计算，B 为去高去低后均价×系数的基准价；\n")
    else:
        score_note = "2. 价格分按「价格分=(P基准/P)×A」逐区间计算后相加，单区间最高得满分；\n"
    tail_note = ("5. " if itemized_mode else "3. ") + (
        "各投标人税率不一致时，按招标文件规定改用不含税单价合计参与评标；\n")
    tail_note += ("6. " if itemized_mode else "4. ") + \
        "本表仅含价格分，商务与服务分需人工评审后填入。"
    _cell(ws, r, 2,
          "1. 报价与税率均为开标记录表原始数值，直接读取单元格，未经模型转写；\n"
          + score_note + tail_note, wrap=True)
    ws.row_dimensions[r].height = 92 if itemized_mode else 76

    for i, w in enumerate((18, 30, 14, 16, 16, 16), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False


def _sheet_lot(wb: Workbook, lot: LotResult, result: SummaryResult, title: str) -> None:
    ws = wb.create_sheet(title)
    n = len(lot.parts)
    itemized = bool(lot.itemized and result.rule.behavior == "itemized_deviation")

    cols: list[tuple[str, str, int, str]] = [
        ("排名", "rank", 6, "int"),
        ("投标人名称", "name", 30, "text"),
    ]
    if itemized:
        # 每个分项：不含税评标价 + 含税对照价；末尾汇总（仅对照，不用于评分）
        for i in range(n):
            label = lot.part_labels[i] if i < len(lot.part_labels) else lot.parts[i].label
            cols.append((f"{label}\n不含税报价", f"p{i}", 14, "money"))
            cols.append((f"{label}\n含税报价", f"inc{i}", 14, "money"))
        cols.append(("各分项合计\n(不含税·对照)", "total", 15, "money"))
        for i in range(n):
            sc = lot.parts[i].score
            label = lot.part_labels[i] if i < len(lot.part_labels) else f"分项{i + 1}"
            cols.append((f"{label}\n得分\n(满分{sc:g})", f"s{i}", 10, "score"))
        cols.append(("投标报价得分\n(D1+D2+…)", "score", 13, "score"))
        cols.append(("综合偏差率\n(对照)", "dev", 11, "pct"))
        cols.append(("状态", "status", 16, "text"))
    else:
        for i in range(n):
            cols.append((_short_label(lot.parts[i].label, i), f"p{i}", 16, "money"))
        cols.append(("评标价合计", "total", 16, "money"))
        cols.append(("税率%", "tax", 8, "num"))
        cols.append(("不含税评标价", "total_net", 16, "money"))
        for i in range(n):
            sc = lot.parts[i].score
            cols.append((f"分数{i + 1}\n({sc:g})", f"s{i}", 10, "score"))
        cols.append(("价格分", "score", 10, "score"))
        cols.append(("偏差率", "dev", 10, "pct"))
        cols.append(("服务期限", "dur", 22, "text"))
        cols.append(("状态", "status", 16, "text"))

    ncol = len(cols)
    last = get_column_letter(ncol)

    # ---- 标题 ----
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1, f"{lot.lot_name}　开标报价分析表",
          font=Font(size=15, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 32

    # ---- 副信息 ----
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"标段编号：{lot.lot_no or '—'}　｜　开标时间：{lot.open_time or '—'}"
          f"　｜　投标人 {len(lot.rows)} 家　｜　计价口径：{lot.priced_by}"
          + ("（参与方税率不一致，按招标文件改用不含税价评标）" if lot.use_net else ""),
          font=Font(size=10), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 22

    rule = result.rule
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)
    if itemized:
        fn = "；".join(f"{lot.part_labels[i] if i < len(lot.part_labels) else p.label} Fn={p.score:g}"
                      for i, p in enumerate(lot.parts))
        rule_txt = (f"各分项独立算基准价（该分项各投标人报价的算术平均）"
                    f"｜Dn = Fn ± 偏差率×100×E（高于用 E1={rule.itemized_e1:g}，"
                    f"低于用 E2={rule.itemized_e2:g}）；每项扣完为止（0≤Dn≤Fn）；"
                    f"投标报价得分=ΣDn"
                    f"｜{fn}　｜　满分 {rule.total_score:g} 分"
                    f"　｜　规则来源：{rule.source_file or '—'}"
                    + (f" 第 {rule.source_page} 页" if rule.source_page else "")
                    + ("" if rule.confirmed else f"　｜　⚠ {rule.basis}"))
    else:
        rule_txt = (f"价格分 = （P基准 ÷ P）× A　｜　P基准 = max(最低有效报价, 平均有效报价×{rule.benchmark_factor:g})"
                    f"　｜　价格总分 {rule.total_score:g} 分　｜　规则来源：{rule.source_file or '—'}"
                    + (f" 第 {rule.source_page} 页" if rule.source_page else "")
                    + ("" if rule.confirmed else f"　｜　⚠ {rule.basis}"))
    _cell(ws, 3, 1, rule_txt,
          font=Font(size=9, color="C00000" if not rule.confirmed else "404040"),
          wrap=True, fill=None if rule.confirmed else NOTE_FILL)
    ws.row_dimensions[3].height = (30 if rule.confirmed else 42) if itemized else (18 if rule.confirmed else 30)

    # ---- 表头 ----
    hr = 5
    for i, (head, _, width, _f) in enumerate(cols, start=1):
        _cell(ws, hr, i, head, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[hr].height = 46

    # ---- 数据 ----
    r = hr + 1
    for row in lot.rows:
        bad = bool([f for f in row.flags if "缺" in f])
        _cell(ws, r, 1, row.rank, align="center",
              font=Font(bold=True, color="C00000" if row.rank <= 3 else "000000"),
              fill=TOP3_FILL if row.rank <= 3 else None)
        _cell(ws, r, 2, row.name, wrap=True)
        c = 3
        if itemized:
            for i in range(n):
                v = row.parts[i] if i < len(row.parts) else None
                _cell(ws, r, c, v, align="right", fmt=FMT_MONEY)
                c += 1
                iv = row.incl_parts[i] if i < len(row.incl_parts) else None
                _cell(ws, r, c, iv, align="right", fmt=FMT_MONEY, font=Font(color="808080"))
                c += 1
            _cell(ws, r, c, row.total, align="right", fmt=FMT_MONEY, font=Font(bold=True))
            c += 1
            for i in range(n):
                sv = row.part_scores[i] if i < len(row.part_scores) else None
                _cell(ws, r, c, sv, align="right", fmt=FMT_SCORE)
                c += 1
            _cell(ws, r, c, row.price_score, align="right", fmt=FMT_SCORE,
                  font=Font(bold=True, color="C00000"))
            c += 1
            _cell(ws, r, c, row.deviation, align="right", fmt=FMT_PCT)
            c += 1
            _cell(ws, r, c, "；".join(row.flags) if bad else (row.status or "正常"),
                  wrap=True, fill=BAD_FILL if bad else None)
        else:
            for i in range(n):
                v = row.parts[i] if i < len(row.parts) else None
                _cell(ws, r, c, v, align="right", fmt=FMT_MONEY)
                c += 1
            _cell(ws, r, c, row.total, align="right", fmt=FMT_MONEY, font=Font(bold=True))
            c += 1
            _cell(ws, r, c, row.tax_rate, align="center", fmt="0.##")
            c += 1
            net = row.total / (1 + (row.tax_rate or 0) / 100.0) if row.total else None
            _cell(ws, r, c, net, align="right", fmt=FMT_MONEY)
            c += 1
            for i in range(n):
                sv = row.part_scores[i] if i < len(row.part_scores) else None
                _cell(ws, r, c, sv, align="right", fmt=FMT_SCORE)
                c += 1
            _cell(ws, r, c, row.price_score, align="right", fmt=FMT_SCORE,
                  font=Font(bold=True, color="C00000"))
            c += 1
            _cell(ws, r, c, row.deviation, align="right", fmt=FMT_PCT)
            c += 1
            _cell(ws, r, c, row.duration, wrap=True)
            c += 1
            _cell(ws, r, c, "；".join(row.flags) if bad else (row.status or "正常"),
                  wrap=True, fill=BAD_FILL if bad else None)
        ws.row_dimensions[r].height = 24
        r += 1

    # ---- 基准价统计 ----
    r += 1
    _cell(ws, r, 1, "分项基准价" if itemized else "区间基准价",
          font=Font(bold=True), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    parts = []
    for i, p in enumerate(lot.parts):
        label = (lot.part_labels[i] if i < len(lot.part_labels)
                 else _short_label(p.label, i).replace(chr(10), " "))
        parts.append(
            f"{label}：最低 {_fmt_money(p.min_price)} ／ 平均 {_fmt_money(p.avg_price)} ／ "
            f"基准价 {_fmt_money(p.benchmark)}（满分 Fn={p.score:g} 分）"
            + ("，" + rule.benchmark_scope if itemized and rule.benchmark_scope else ""))
    _cell(ws, r, 2, "\n".join(parts), wrap=True)
    ws.row_dimensions[r].height = max(30, 18 * len(parts))

    ws.freeze_panes = ws.cell(row=hr + 1, column=3)
    ws.sheet_view.zoomScale = 90
    ws.auto_filter.ref = f"A{hr}:{last}{hr + len(lot.rows)}"



def _sheet_lot_single(wb: Workbook, lot: LotResult, result: SummaryResult, title: str) -> None:
    """单一总报价标段（如「A≤基准价得满分，(B/A)×满分」这类规则）。"""
    ws = wb.create_sheet(title)
    rule = result.rule
    full = rule.total_score or 50.0
    if rule.behavior == "full_deviation" and rule.trim_tiers:
        segs: list[str] = []
        prev = 0
        for t in sorted(rule.trim_tiers, key=lambda x: x.get("max_n", 0)):
            mx = t.get("max_n", 0)
            label = f"≤{mx} 家" if mx < 100000 else f">{prev} 家"
            segs.append(f"{label} 去 {t.get('high', 0)} 高 {t.get('low', 0)} 低")
            prev = mx
        trim_txt = "按家数分档（" + "；".join(segs) + "）"
    else:
        trim_txt = (f"去 {rule.trim_high} 高 {rule.trim_low} 低"
                    + (f"（有效投标人 ≥{rule.trim_guard} 家时）" if rule.trim_guard else "")) \
            if (rule.trim_high or rule.trim_low) else "取全部报价平均"

    cols: list[tuple[str, str, int, str]] = [
        ("排名", "rank", 6, "int"),
        ("投标人名称", "name", 36, "text"),
        ("投标报价\n(不含税·评标价)" if lot.use_net else "投标报价\n(含税·评标价)",
         "p", 18, "money"),
    ]
    if lot.use_net:
        cols.append(("投标报价\n(含税)", "incl", 18, "money"))
        cols.append(("税率%", "tax", 8, "num"))
    elif lot.incl_col >= 0 and lot.incl_col != lot.price_col:
        cols.append(("投标报价\n(不含税)", "incl", 18, "money"))
    if lot.tax_col >= 0 and not lot.use_net:
        cols.append(("税率%", "tax", 8, "num"))
    cols += [
        (f"价格分\n(满分 {full:g})", "score", 12, "score"),
        ("偏差率\n(A−B)/B", "dev", 12, "pct"),
        ("招标人\n自算得分", "org", 12, "score"),
        ("差异", "diff", 9, "num"),
        ("服务期限", "dur", 20, "text"),
        ("状态", "status", 24, "text"),
    ]
    ncol = len(cols)
    last = get_column_letter(ncol)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1, f"{lot.lot_name}　开标报价分析表",
          font=Font(size=15, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 32

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"标段编号：{lot.lot_no or '—'}　｜　开标时间：{lot.open_time or '—'}"
          f"　｜　投标人 {len(lot.rows)} 家　｜　计价口径：{lot.priced_by}"
          f"　｜　基准价 B = {_fmt_money(lot.parts[0].benchmark if lot.parts else None)} 元",
          font=Font(size=10), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 22

    if rule.behavior == "full_deviation":
        formula = (f"价格分：P=N 得满分 {full:g} 分；每高于 N 1% 减 {rule.step_high:g} 分、"
                   f"每低于 N 1% 减 {rule.step_low:g} 分（不足 1% 按插入法，扣完为止）"
                   f"　｜　N = {trim_txt}，取剩余评标价算术平均值"
                   f"　｜　P = 投标人评标价")
    elif rule.behavior == "deviation" and rule.base_score:
        formula = (
            f"价格分：A=B 得基本分 {rule.base_score:g} 分；低于 B 每低 1% 加 {rule.step_percent:g} 分"
            + (f"（低 {rule.low_limit:g}% 以内，最高 {full:g} 分；再低每 1% 扣 {rule.step_percent:g} 分）"
               if rule.low_limit else f"（最高 {full:g} 分）")
            + f"；高于 B 每高 1% 扣 {rule.step_percent:g} 分，扣完为止"
            f"　｜　B = {trim_txt}的算术平均值 × {rule.benchmark_factor:g}"
            f"　｜　A = 投标人评标价"
        )
    else:
        formula = (f"价格分 = A≤B 时取满分 {full:g}；A>B 时 =（B ÷ A）× {full:g}"
                   f"　｜　B = {trim_txt}的算术平均值 × {rule.benchmark_factor:g}"
                   f"　｜　A = 投标人评标价（不含税）")
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)
    _cell(ws, 3, 1,
          formula + f"　｜　规则来源：{rule.source_file or '—'}"
          + (f" 第 {rule.source_page} 页" if rule.source_page else "")
          + ("" if rule.confirmed else f"　｜　⚠ {rule.basis}"),
          font=Font(size=9, color="C00000" if not rule.confirmed else "404040"),
          wrap=True, fill=None if rule.confirmed else NOTE_FILL)
    ws.row_dimensions[3].height = 30

    hr = 5
    for i, (head, _, width, _f) in enumerate(cols, start=1):
        _cell(ws, hr, i, head, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[hr].height = 42

    r = hr + 1
    for row in lot.rows:
        bad = bool(row.flags)
        diff = (round(row.price_score - row.organizer_score, 2)
                if (row.price_score is not None and row.organizer_score is not None) else None)
        values = {
            "rank": row.rank, "name": row.name, "p": row.total,
            "incl": row.incl_total, "tax": row.tax_rate,
            "score": row.price_score, "dev": row.deviation,
            "org": row.organizer_score, "diff": diff, "dur": row.duration,
            "status": "；".join(row.flags) if bad else (row.status or "正常"),
        }
        for i, (_head, key, _w, fmt) in enumerate(cols, start=1):
            v = values.get(key)
            if key == "rank":
                _cell(ws, r, i, v, align="center",
                      font=Font(bold=True, color="C00000" if row.rank <= 3 else "000000"),
                      fill=TOP3_FILL if row.rank <= 3 else None)
            elif key == "name":
                _cell(ws, r, i, v, wrap=True)
            elif key == "status":
                _cell(ws, r, i, v, wrap=True, fill=NOTE_FILL if bad else None)
            elif key == "dur":
                _cell(ws, r, i, v, wrap=True)
            elif key == "diff":
                _cell(ws, r, i, v, align="right", fmt=FMT_SCORE,
                      fill=BAD_FILL if (v not in (None, 0) and abs(v) > 0.011) else None)
            elif key == "score":
                _cell(ws, r, i, v, align="right", fmt=FMT_SCORE,
                      font=Font(bold=True, color="C00000"))
            elif key == "tax":
                _cell(ws, r, i, v, align="center", fmt="0.##")
            else:
                _cell(ws, r, i, v, align="right", fmt=FMT_MONEY if fmt == "money" else FMT_SCORE)
        ws.row_dimensions[r].height = 22
        r += 1

    # ---- 基准价形成过程 ----
    r += 1
    part = lot.parts[0] if lot.parts else None
    valid = [x for x in lot.rows if x.total]
    trimmed = lot.trimmed or []
    detail = (
        f"有效投标人 {len(valid)} 家　｜　最低 {_fmt_money(part.min_price if part else None)}"
        f"　｜　最高 {_fmt_money(max((x.total for x in valid), default=None))}"
        + (f"\n剔除最高/最低：{'、'.join(trimmed)}" if trimmed else "\n（不满足剔除条件，取全部报价）")
        + f"\n参与基准价计算 {lot.bench_n or len(valid)} 家，算术平均值 {_fmt_money(part.avg_price if part else None)}"
        + (f" × {lot.bench_factor_used:g}" if (lot.bench_factor_used or 0) not in (0, 1.0) else "")
        + f" = 基准价 B {_fmt_money(part.benchmark if part else None)} 元"
    )
    _cell(ws, r, 1, "基准价 B", font=Font(bold=True), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    _cell(ws, r, 2, detail, wrap=True)
    ws.row_dimensions[r].height = 44

    r += 2
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    _cell(ws, r, 1,
          "排序说明：同分时按评标价由低到高排列，仅为表格展示顺序；招标文件规定"
          "「综合得分相等则评审总价低者排名高」，实际名次须待技术、商务与服务分汇总后确定。",
          font=Font(size=9, color="C00000"), wrap=True)
    ws.row_dimensions[r].height = 28

    ws.freeze_panes = ws.cell(row=hr + 1, column=3)
    ws.sheet_view.zoomScale = 90
    ws.auto_filter.ref = f"A{hr}:{last}{hr + len(lot.rows)}"


def _sheet_trace_b(wb: Workbook, result: SummaryResult) -> None:
    ws = wb.create_sheet("来源追溯")
    heads = ["标段", "投标人", "字段", "取值", "来源文件", "工作表", "单元格"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, 1, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r = 2
    for lot in result.lots:
        for row in lot.rows:
            f = lot.source_file
            base = [lot.lot_name, row.name]
            for i, p in enumerate(lot.parts):
                label = (lot.part_labels[i] if i < len(lot.part_labels)
                         else _short_label(p.label, i).replace("\n", " "))
                _cell(ws, r, 1, base[0]); _cell(ws, r, 2, base[1])
                _cell(ws, r, 3, label + ("（不含税·评标）" if lot.itemized else ""))
                _cell(ws, r, 4, row.parts[i], align="right", fmt=FMT_MONEY)
                _cell(ws, r, 5, f); _cell(ws, r, 6, lot.sheet)
                _cell(ws, r, 7, f"{get_column_letter(p.col + 1)}{row.src_row + 1}", align="center")
                r += 1
                # 分项含税对照价（同一品类的含税列）
                if lot.itemized and i < len(lot.incl_cols) and lot.incl_cols[i] >= 0:
                    _cell(ws, r, 1, base[0]); _cell(ws, r, 2, base[1])
                    _cell(ws, r, 3, label + "（含税·对照）")
                    iv = row.incl_parts[i] if i < len(row.incl_parts) else None
                    _cell(ws, r, 4, iv, align="right", fmt=FMT_MONEY)
                    _cell(ws, r, 5, f); _cell(ws, r, 6, lot.sheet)
                    _cell(ws, r, 7, f"{get_column_letter(lot.incl_cols[i] + 1)}{row.src_row + 1}",
                          align="center")
                    r += 1
            if lot.tax_col >= 0:
                _cell(ws, r, 1, base[0]); _cell(ws, r, 2, base[1])
                _cell(ws, r, 3, "增值税税率（%）")
                _cell(ws, r, 4, row.tax_rate, align="right", fmt="0.##")
                _cell(ws, r, 5, f); _cell(ws, r, 6, lot.sheet)
                _cell(ws, r, 7, f"{get_column_letter(lot.tax_col + 1)}{row.src_row + 1}",
                      align="center")
                r += 1
            if lot.duration_col >= 0:
                _cell(ws, r, 1, base[0]); _cell(ws, r, 2, base[1])
                _cell(ws, r, 3, "服务期限"); _cell(ws, r, 4, row.duration, wrap=True)
                _cell(ws, r, 5, f); _cell(ws, r, 6, lot.sheet)
                _cell(ws, r, 7, f"{get_column_letter(lot.duration_col + 1)}{row.src_row + 1}",
                      align="center")
                r += 1
    for col, w in zip("ABCDEFG", (28, 32, 22, 16, 40, 30, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"


def _sheet_files(wb: Workbook, result: SummaryResult) -> None:
    ws = wb.create_sheet("文件清单")
    heads = ["序号", "文件名", "相对路径", "文件类型", "页数", "是否扫描件",
             "解析状态", "大小(KB)", "备注"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, 1, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    for i, f in enumerate(result.files, start=2):
        status = {"PARSED": "成功", "FAILED": "失败", "NEW": "未处理",
                  "SKIPPED": "已排除"}.get(f.status, f.status)
        vals = [i - 1, f.name, f.rel, KIND_LABEL.get(f.kind, f.kind), f.page_count or "",
                "是" if f.is_scanned else "否", status, round(f.size / 1024, 1), f.error]
        for j, v in enumerate(vals, start=1):
            _cell(ws, i, j, v, wrap=(j == 9),
                  fill=BAD_FILL if f.status in ("FAILED", "SKIPPED") else None)
    for col, w in zip("ABCDEFGHI", (6, 32, 46, 12, 6, 10, 10, 10, 46)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"


def _build_mode_b(result: SummaryResult, folder: Path) -> Workbook:
    wb = Workbook()
    _sheet_overview(wb, result, folder)
    used: set[str] = {"项目概况"}
    for lot in result.lots:
        name = _safe_sheet_name(lot.lot_name, used)
        if lot.pricing == "single":
            _sheet_lot_single(wb, lot, result, name)
        else:
            _sheet_lot(wb, lot, result, name)
    _sheet_trace_b(wb, result)
    _sheet_files(wb, result)
    return wb


# ================================================================== 模式 A
def _build_mode_a(result: SummaryResult, folder: Path) -> Workbook:
    wb = Workbook()
    ncol = 1 + len(BIDDER_FIELDS)
    ws = wb.active
    ws.title = "开标汇总表"

    pname = (result.project.get("project_name").value if result.project.get("project_name")
             else None) or folder.name
    for c in range(1, ncol + 1):
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1, f"{pname}  开标汇总表",
          font=Font(size=16, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 34

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"数据来源：{folder}  ｜  生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}  ｜  "
          f"模型：{CONFIG.text_model}  ｜  本表由 AI 辅助生成，须经人工复核确认后生效",
          font=Font(size=9, color="808080"), align="center")

    r = 3
    for d in PROJECT_FIELDS:
        fv = result.project.get(d.key, FieldValue(key=d.key, label=d.label))
        _cell(ws, r, 1, d.label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, _fmt_money(fv.value) if d.type == "money" else fv.to_display(), wrap=True)
        r += 1

    r += 1
    header_row = r
    _cell(ws, r, 1, "序号", font=Font(bold=True), fill=HEAD_FILL, align="center")
    for i, d in enumerate(BIDDER_FIELDS, start=2):
        _cell(ws, r, i, d.label, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
    ws.row_dimensions[r].height = 30
    r += 1

    data_start = r
    for idx, row in enumerate(result.rows, start=1):
        _cell(ws, r, 1, idx, align="center")
        for i, d in enumerate(BIDDER_FIELDS, start=2):
            fv = row.fields.get(d.key, FieldValue(key=d.key, label=d.label))
            text = _fmt_money(fv.value) if d.type == "money" else fv.to_display()
            if fv.manual:
                text += "（人工）"
            _cell(ws, r, i, text, fill=LEVEL_FILL.get(fv.level), wrap=True,
                  align="right" if d.type == "money" else "left")
        ws.row_dimensions[r].height = 26
        r += 1

    r += 1
    prices = []
    for row in result.rows:
        fv = row.fields.get("bid_price")
        try:
            if fv and fv.value is not None:
                prices.append(float(fv.value))
        except (TypeError, ValueError):
            pass
    stats = [("投标人数量", f"{len(result.rows)} 家"),
             ("最低报价", (_fmt_money(min(prices)) + " 元") if prices else "—"),
             ("最高报价", (_fmt_money(max(prices)) + " 元") if prices else "—"),
             ("平均报价", (_fmt_money(sum(prices) / len(prices)) + " 元") if prices else "—")]
    for label, value in stats:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, value)
        r += 1

    if result.publicity:
        r += 1
        r = _block_publicity(ws, r, min(ncol, 6), result)
        r += 1

    r += 1
    _cell(ws, r, 1, "待复核事项", font=Font(bold=True, color="9C0006"), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    flags = [f"· {row.bidder}：{fl}" for row in result.rows for fl in row.flags]
    flags += [f"· 文件读取失败：{f['file']} —— {f['reason']}" for f in result.failed_files]
    flags += [f"· 已排除干扰文件：{f['file']} —— {f['reason']}" for f in result.excluded_files]
    _cell(ws, r, 2, "\n".join(flags or ["· 无（所有关键字段均已通过校验）"]), wrap=True)
    ws.row_dimensions[r].height = max(30, min(200, 15 * max(1, len(flags))))

    r += 2
    _cell(ws, r, 1, "图例", font=Font(bold=True))
    for i, (text, lvl) in enumerate([("高置信（可直接采用）", "green"),
                                     ("建议复核", "yellow"),
                                     ("缺失 / 冲突（必须复核）", "red")]):
        _cell(ws, r, 2 + i * 3, text, fill=LEVEL_FILL[lvl])

    ws.column_dimensions["A"].width = 6
    for i, d in enumerate(BIDDER_FIELDS, start=2):
        ws.column_dimensions[get_column_letter(i)].width = d.width
    ws.freeze_panes = ws.cell(row=data_start, column=2)
    ws.sheet_view.zoomScale = 90

    # 来源追溯
    ws2 = wb.create_sheet("来源追溯")
    heads = ["投标人/项目", "字段", "抽取结果", "置信度", "来源文件", "页码", "原文片段", "标记"]
    for i, h in enumerate(heads, start=1):
        _cell(ws2, 1, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r2 = 2
    for d in PROJECT_FIELDS:
        fv = result.project.get(d.key)
        if not fv:
            continue
        vals = ["（项目信息）", d.label, fv.to_display(), f"{fv.confidence:.2f}",
                fv.source_file, fv.source_page, fv.quote, ""]
        for i, v in enumerate(vals, start=1):
            _cell(ws2, r2, i, v, wrap=(i == 7))
        r2 += 1
    for row in result.rows:
        for d in BIDDER_FIELDS:
            fv = row.fields.get(d.key)
            if not fv:
                continue
            show = _fmt_money(fv.value) if d.type == "money" else fv.to_display()
            mark = "冲突" if fv.conflict else ("缺失" if fv.value in (None, "") else
                                               ("复核" if fv.level == "yellow" else ""))
            vals = [row.bidder, d.label, show, f"{fv.confidence:.2f}",
                    fv.source_file, fv.source_page, fv.quote or fv.note, mark]
            for i, v in enumerate(vals, start=1):
                _cell(ws2, r2, i, v, wrap=(i == 7),
                      fill=(LEVEL_FILL["red"] if mark in ("冲突", "缺失")
                            else (LEVEL_FILL["yellow"] if mark == "复核" else None)))
            r2 += 1
            for c in fv.candidates:
                vals = [row.bidder, f"{d.label}（候选值）", _fmt_money(c.get("value")), "",
                        c.get("source_file", ""), "", c.get("quote", ""), "候选"]
                for i, v in enumerate(vals, start=1):
                    _cell(ws2, r2, i, v, wrap=(i == 7))
                r2 += 1
    for col, w in zip("ABCDEFGH", (22, 20, 22, 9, 34, 7, 46, 8)):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A2"

    _sheet_files(wb, result)
    return wb


# ================================================================== 模式 C（入围单价矩阵）
def _sheet_sl_overview(wb: Workbook, result: SummaryResult, folder: Path) -> None:
    sl = result.shortlist
    is_item = sl.variant == "per_item"
    ws = wb.active
    ws.title = "项目概况"
    ncol = 6

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    pname = (result.project.get("project_name").value if result.project.get("project_name")
             else None) or folder.name
    _cell(ws, 1, 1,
          f"{pname}　开标汇总（分项加权最低价法）" if is_item
          else f"{pname}　开标汇总（入围单价矩阵）",
          font=Font(size=16, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"数据来源：{folder}　｜　生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}"
          f"　｜　模型：{CONFIG.text_model}　｜　本表由 AI 辅助生成，须经人工复核确认",
          font=Font(size=9, color="808080"), align="center")

    r = 3
    for d in PROJECT_FIELDS:
        fv = result.project.get(d.key, FieldValue(key=d.key, label=d.label))
        _cell(ws, r, 1, d.label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, _fmt_money(fv.value) if d.type == "money" else fv.to_display(), wrap=True)
        r += 1

    # ---- 报价类别概览 ----
    r += 1
    _cell(ws, r, 1, "报价类别概览" if not is_item else "价格评分口径概览",
          font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    if is_item:
        heads = ["报价类别", "产品数", "价格满分", "计价口径", "投标人来源", "参与比较家数"]
    else:
        heads = ["报价类别", "产品数", "价格分值", "评标基准价", "基准价来源（最低报价）", "参与比较家数"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, r, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r += 1
    for cat in sl.categories:
        if is_item:
            vals = [cat.name, len(cat.products),
                    f"{sl.full_score:g} 分" if sl.full_score else "未识别",
                    sl.price_basis or "未识别",
                    "Excel 工作表名称（一 sheet 一家）",
                    cat.benchmark_n]
        else:
            vals = [cat.name, len(cat.products),
                    f"{cat.score:g} 分" if cat.score else "未识别",
                    _fmt_money(cat.benchmark),
                    cat.benchmark_holder or "—",
                    cat.benchmark_n]
        for i, v in enumerate(vals, start=1):
            _cell(ws, r, i, v, align="center" if i >= 2 else "left", wrap=True)
        r += 1

    # ---- 价格评分规则 ----
    r += 1
    _cell(ws, r, 1, "投标报价评分标准（由招标文件识别，分数由系统按公式计算）",
          font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    lines = [
        ("评标价构成" if not is_item else "分项价格分",
         sl.eval_price_rule or ("∑各产品投标单价（不含税）×权重" if not is_item
                                else "满分 × 分项权重")),
        ("基准价取法" if not is_item else "偏离惩罚",
         sl.benchmark_rule if not is_item else
         (f"X/Y < {sl.deviation_low:g} 或 X/Y ≥ {sl.deviation_high:g} 的产品该项不得分；"
          "剔除异常报价后迭代重算平均价 Y 与最低价（原文：其他投标人重新计算偏离值进行评分，"
          "直到无偏离扣分情况）")),
        ("得分公式", sl.formula or ("（评标基准价）／（投标报价）×分值" if not is_item
                                   else "（该项最低价 / 本家单价）× 满分 × 分项权重")),
        ("规则出处", sl.rule_source_file + (f" 第 {sl.rule_source_page} 页"
                                             if sl.rule_source_page else "")),
        ("规则状态", ("✓ " if sl.confirmed else "⚠ ") + sl.basis),
        ("单价表来源", sl.price_sheet_file or "—"),
    ]
    if is_item:
        lines.append(("税率口径", sl.tax_rule or "投标人报价税率相同时按含税价，否则按不含税价"))
        lines.append(("投标人识别", f"在工作簿《{Path(sl.sheet_source_file).name}》中"
                                    f"以 {len(sl.sheet_names)} 个工作表名称识别投标人"
                                    "（千问判断 + 表头/行数结构校验）"))
    for label, value in lines:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, value, wrap=True)
        ws.row_dimensions[r].height = 30
        r += 1

    # ---- 待复核 ----
    r += 1
    _cell(ws, r, 1, "待复核事项", font=Font(bold=True, color="9C0006"), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    flags = [f"· {w}" for w in result.warnings]
    for b in sl.bidders:
        for f in b.flags:
            flags.append(f"· {b.name}：{f}")
    for f in result.excluded_files:
        flags.append(f"· 已排除干扰文件：{f['file']} —— {f['reason']}")
    for f in result.failed_files:
        flags.append(f"· 文件读取失败：{f['file']} —— {f['reason']}")
    if not flags:
        flags = ["· 无"]
    _cell(ws, r, 2, "\n".join(flags), wrap=True)
    ws.row_dimensions[r].height = max(30, min(260, 16 * len(flags)))

    r += 2
    _cell(ws, r, 1, "说明", font=Font(bold=True))
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    if is_item:
        note = (
            f"1. 各产品投标单价直接读取单元格原文，未经模型转写；计价口径为{sl.price_basis or '见规则'}；"
            "权重与最高限价取自报价表/招标文件附表；\n"
            "2. 投标人由 Excel 工作表名称识别（一 sheet 一家），并由千问判断提示词 + 表头/行数结构校验确认；\n"
            f"3. 价格总分 {sl.full_score:g} 分，分项价格分 = {sl.full_score:g} × 分项权重；"
            "每项得分 =（该项所有有效投标人最低价 / 本家单价）× 分项价格分；\n"
            f"4. 报价偏离惩罚：X/Y < {sl.deviation_low:g} 或 X/Y ≥ {sl.deviation_high:g} 的产品该项不得分，"
            "剔除异常报价后迭代重算平均价 Y 与最低价，直到无新的惩罚项；\n"
            "5. 「加权报价水平」= ∑单价×权重，仅用于横向对照报价高低，不是本口径的评标价；"
            "价格分由逐项比值求和得到；\n"
            "6. 本表仅含价格分，商务、技术、服务部分需评标委员会评审。")
    else:
        note = (
            "1. 各产品投标单价直接读取单元格原文，未经模型转写；权重与最高限价取自招标文件附表；\n"
            "2. 评标价 = ∑各产品投标单价（不含税）×权重（每类别权重合计 100%）；"
            "评标基准价为各有效投标人所报评标价的最低值；\n"
            "3. 缺报任一产品或超最高投标限价的投标人，评标价不完整、不具备可比性，"
            "该类别不计分（得分列留空并注明原因），亦不参与基准价比较；\n"
            "4. 「评标价占单价合计%」= 加权评标价 ÷ 所报产品未加权单价合计；"
            "「占限价%」= 加权评标价 ÷ 限价加权合计；\n"
            "5. 本表仅含价格分，商务、技术、服务部分需评标委员会评审。")
    _cell(ws, r, 2, note, wrap=True)
    ws.row_dimensions[r].height = 120 if is_item else 96

    for i, w in enumerate((18, 30, 12, 16, 30, 14), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False


def _sheet_sl_summary(wb: Workbook, result: SummaryResult) -> None:
    """价格评分汇总：各投标单位的价格（评标价）汇总、价格分汇总、占单价合计百分比。"""
    sl = result.shortlist
    is_item = sl.variant == "per_item"
    ws = wb.create_sheet("价格评分汇总")
    cats = sl.categories
    nprod = sum(len(c.products) for c in cats)
    total_score = sl.full_score if is_item else sum(c.score for c in cats)

    cols: list[tuple[str, int, str]] = [
        ("排名", 6, "int"), ("投标单位", 30, "text"),
        (f"报价产品数\n(共{nprod})", 10, "text"), ("缺报\n产品数", 8, "int"),
    ]
    for cat in cats:
        short = cat.name[:8]
        cols.append(("加权报价水平\n(∑单价×权重)" if is_item
                     else f"{short}\n评标价(∑单价×权重)", 15, "money"))
        cols.append((("未加权单价合计" if is_item else f"{short}\n未加权单价合计"), 14, "money"))
        cols.append((("加权价占\n单价合计%" if is_item else f"{short}\n加权价占\n单价合计%"), 11, "pct"))
        cols.append((("占限价%" if is_item else f"{short}\n占限价%"), 10, "pct"))
        cols.append((f"价格得分\n(满分{sl.full_score:g})" if is_item
                     else f"{short}得分\n(满分{cat.score:g})", 11, "score"))
    cols.append((f"价格分合计\n(满分{total_score:g})", 12, "score"))
    cols.append(("超限价\n产品数", 8, "int"))
    if is_item:
        cols.append(("偏离惩罚\n偏低项", 9, "int"))
        cols.append(("偏离惩罚\n偏高项", 9, "int"))
    cols.append(("报价状态 / 备注", 42, "text"))

    ncol = len(cols)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    if is_item:
        title_txt = (f"投标价格汇总与价格分一览（{sl.price_basis}；分项价格分 = "
                     f"{sl.full_score:g}×分项权重；每项得分 = 该项最低价÷本家单价×分项价格分）")
        bench_txt = (f"评分口径：逐项取最低价，共 {nprod} 个分项；"
                     f"偏离惩罚阈值 X/Y < {sl.deviation_low:g} 或 ≥ {sl.deviation_high:g}；"
                     f"投标人 {len(sl.bidders)} 家（由工作表名称识别）")
    else:
        title_txt = ("投标价格汇总与价格分一览（评标价 = ∑各产品投标单价×权重；"
                     "基准价 = 各有效投标人所报评标价最低值；得分 = 基准价÷评标价×分值）")
        bench_txt = "　｜　".join(
            f"{c.name}：基准价 {_fmt_money(c.benchmark)}（{c.benchmark_holder or '—'}，{c.benchmark_n} 家参与）"
            for c in cats)
    _cell(ws, 1, 1, title_txt,
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1, bench_txt, font=Font(size=10), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 22
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)
    _cell(ws, 3, 1,
          f"评分依据：{sl.rule_source_file or '—'}　｜　{sl.eval_price_rule or '∑单价×权重'}"
          f"　｜　{sl.benchmark_rule or ('逐项最低价' if is_item else '基准价=最低值')}"
          + ("" if sl.confirmed else f"　｜　⚠ {sl.basis}"),
          font=Font(size=9, color="C00000" if not sl.confirmed else "404040"),
          wrap=True, fill=None if sl.confirmed else NOTE_FILL)

    hr = 5
    for i, (head, width, _f) in enumerate(cols, start=1):
        _cell(ws, hr, i, head, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[hr].height = 52

    r = hr + 1
    for b in sl.bidders:
        scored = b.total_score is not None
        _cell(ws, r, 1, b.rank, align="center",
              font=Font(bold=True, color="C00000" if scored and b.rank <= 6 else "000000"),
              fill=TOP3_FILL if scored and b.rank <= 6 else None)
        _cell(ws, r, 2, b.name, wrap=True)
        _cell(ws, r, 3, f"{b.quoted}/{nprod}", align="center")
        _cell(ws, r, 4, len(b.missing), align="center",
              fill=BAD_FILL if b.missing else None)
        c = 5
        for ci, cat in enumerate(cats):
            ev = b.eval_prices[ci] if ci < len(b.eval_prices) else None
            raw = b.raw_sums[ci] if ci < len(b.raw_sums) else None
            sc = b.scores[ci] if ci < len(b.scores) else None
            _cell(ws, r, c, ev, align="right", fmt=FMT_MONEY, font=Font(bold=True))
            _cell(ws, r, c + 1, raw, align="right", fmt=FMT_MONEY)
            _cell(ws, r, c + 2, (ev / raw) if (ev and raw) else None,
                  align="right", fmt=FMT_PCT)
            _cell(ws, r, c + 3,
                  (ev / cat.cap_weighted) if (ev and cat.cap_weighted) else None,
                  align="right", fmt=FMT_PCT)
            _cell(ws, r, c + 4, sc, align="right", fmt=FMT_SCORE,
                  font=Font(bold=True, color="C00000" if sc else "808080"))
            c += 5
        _cell(ws, r, c, b.total_score, align="right", fmt=FMT_SCORE,
              font=Font(bold=True, color="C00000" if scored else "808080"))
        _cell(ws, r, c + 1, len(b.over_cap), align="center",
              fill=BAD_FILL if b.over_cap else None)
        nxt = c + 2
        if is_item:
            _cell(ws, r, nxt, b.penalty_low, align="center",
                  fill=BAD_FILL if b.penalty_low else None)
            _cell(ws, r, nxt + 1, b.penalty_high, align="center",
                  fill=BAD_FILL if b.penalty_high else None)
            nxt += 2
        note = "；".join(b.flags) if b.flags else ("报价完整" if scored else "—")
        _cell(ws, r, nxt, note, wrap=True,
              fill=NOTE_FILL if b.flags else None)
        ws.row_dimensions[r].height = 24
        r += 1

    ws.freeze_panes = ws.cell(row=hr + 1, column=3)
    ws.sheet_view.zoomScale = 85
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(ncol)}{hr + len(sl.bidders)}"


def _sheet_sl_matrix(wb: Workbook, result: SummaryResult) -> None:
    """单价明细：产品 × 投标单位 的单价矩阵 + 每个产品的最低/平均/最高。"""
    sl = result.shortlist
    ws = wb.create_sheet("单价明细")
    bidders = sl.bidders
    fixed = 6   # 序号 类别 产品名称 单位 权重 限价
    ncol = fixed + len(bidders) + 3

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    basis = sl.price_basis if sl.variant == "per_item" else "不含税"
    _cell(ws, 1, 1, f"各产品投标单价明细（元，{basis}；红色 = 超最高投标限价；空白 = 未报价）",
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30

    hr = 3
    heads = ["序号", "类别", "产品名称", "单位", "权重%", "最高限价"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, hr, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
    for j, b in enumerate(bidders):
        _cell(ws, hr, fixed + 1 + j, b.name, font=Font(bold=True, size=8),
              fill=HEAD_FILL, align="center", wrap=True)
    for k, h in enumerate(("最低单价", "平均单价", "最高单价")):
        _cell(ws, hr, fixed + len(bidders) + 1 + k, h, font=Font(bold=True),
              fill=LABEL_FILL, align="center", wrap=True)

    r = hr + 1
    for cat in sl.categories:
        for p in cat.products:
            _cell(ws, r, 1, p.seq, align="center")
            _cell(ws, r, 2, cat.name[:10], align="center")
            _cell(ws, r, 3, p.name, wrap=True)
            _cell(ws, r, 4, p.unit, align="center")
            _cell(ws, r, 5, p.weight / 100, align="right", fmt="0.00%")
            _cell(ws, r, 6, p.cap, align="right", fmt=FMT_MONEY)
            vals = [b.prices.get(p.seq) for b in bidders]
            for j, v in enumerate(vals):
                over = (v is not None and p.cap is not None and v > p.cap + 1e-9)
                _cell(ws, r, fixed + 1 + j, v, align="right", fmt=FMT_MONEY,
                      font=Font(size=9, bold=over, color="C00000" if over else "000000"),
                      fill=BAD_FILL if over else None)
            quoted = [v for v in vals if v is not None]
            if quoted:
                _cell(ws, r, fixed + len(bidders) + 1, min(quoted), align="right",
                      fmt=FMT_MONEY, font=Font(bold=True, color="C00000"))
                _cell(ws, r, fixed + len(bidders) + 2,
                      sum(quoted) / len(quoted), align="right", fmt=FMT_MONEY)
                _cell(ws, r, fixed + len(bidders) + 3, max(quoted), align="right", fmt=FMT_MONEY)
            r += 1

    for i, w in enumerate((6, 10, 44, 6, 8, 11), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for j in range(len(bidders)):
        ws.column_dimensions[get_column_letter(fixed + 1 + j)].width = 10
    for k in range(3):
        ws.column_dimensions[get_column_letter(fixed + len(bidders) + 1 + k)].width = 11
    ws.freeze_panes = ws.cell(row=hr + 1, column=fixed + 1)
    ws.sheet_view.zoomScale = 80


def _sheet_sl_rule(wb: Workbook, result: SummaryResult) -> None:
    """评分依据：招标文件「投标报价评分标准 / 评分详情」原文与类别权重。"""
    sl = result.shortlist
    ws = wb.create_sheet("评分依据")

    ws.merge_cells("A1:D1")
    _cell(ws, 1, 1, "投标报价评分标准（招标文件原文摘录）",
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 28

    r = 2
    _cell(ws, r, 1, "评分标准原文", font=Font(bold=True), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
    _cell(ws, r, 2, sl.rule_text or "未能在招标文件中定位到相关章节",
          wrap=True, font=Font(size=10))
    ws.row_dimensions[r].height = max(40, min(300, 14 * (len(sl.rule_text or "") // 60 + 1)))
    r += 2

    if sl.variant == "per_item":
        _cell(ws, r, 1, "价格评分规则关键参数（正则直取原文，分数由代码计算）",
              font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        params = [
            ("评标办法 / 价格部分", f"{result.project.get('eval_method').to_display() if result.project.get('eval_method') else '—'}"
                                    f"　｜　价格满分 {sl.full_score:g} 分"),
            ("计价口径", sl.price_basis or "—"),
            ("税率的适用条件", sl.tax_rule or "—"),
            ("分项价格分", sl.eval_price_rule or "—"),
            ("每项得分公式", sl.formula or "—"),
            ("偏离惩罚阈值", f"X/Y < {sl.deviation_low:g} 或 X/Y ≥ {sl.deviation_high:g}（该项不得分）"),
            ("基准价 / 迭代规则", sl.benchmark_rule or "—"),
            ("投标人识别", f"《{Path(sl.sheet_source_file).name}》的 {len(sl.sheet_names)} 个 sheet 名"),
            ("规则出处", f"{sl.rule_source_file} 第 {sl.rule_source_page} 页"
                          if sl.rule_source_page else sl.rule_source_file),
            ("规则状态", ("✓ " if sl.confirmed else "⚠ ") + sl.basis),
        ]
        for k, v in params:
            _cell(ws, r, 1, k, font=Font(bold=True), fill=LABEL_FILL)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
            _cell(ws, r, 2, v, wrap=True)
            ws.row_dimensions[r].height = 28
            r += 1
        r += 1

    if sl.rule_detail:
        _cell(ws, r, 1, "评分详情（评分项 / 评分因素 / 评分标准，摘自招标文件）",
              font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        for i, h in enumerate(("评分项", "评分因素", "评分标准"), start=1):
            _cell(ws, r, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
        r += 1
        detail = list(sl.rule_detail)
        if detail and detail[0] and "评分项" in (detail[0][0] or ""):
            detail = detail[1:]          # 原表自带表头，别重复写一行
        for row in detail:
            cells = [row[0] if len(row) > 0 else "", row[1] if len(row) > 1 else "",
                     row[2] if len(row) > 2 else ""]
            _cell(ws, r, 1, cells[0], wrap=True, align="center")
            _cell(ws, r, 2, cells[1], wrap=True, align="center")
            ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
            _cell(ws, r, 3, cells[2], wrap=True, font=Font(size=10))
            ws.row_dimensions[r].height = max(22, min(160, 13 * (len(cells[2]) // 55 + 1)))
            r += 1
        r += 1

    _cell(ws, r, 1, "各报价类别的价格分值（识别结果）", font=Font(bold=True, color="FFFFFF"),
          fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    r += 1
    for i, h in enumerate(("报价类别", "产品数", "价格分值", "权重合计"), start=1):
        _cell(ws, r, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r += 1
    for cat in sl.categories:
        wsum = sum(p.weight for p in cat.products)
        for i, v in enumerate((cat.name, len(cat.products),
                               f"{cat.score:g}" if cat.score else "未识别",
                               f"{wsum:g}%"), start=1):
            _cell(ws, r, i, v, align="center" if i > 1 else "left")
        r += 1

    for col, w in zip("ABCD", (22, 22, 60, 40)):
        ws.column_dimensions[col].width = w
    ws.sheet_view.showGridLines = False


def _sheet_sq_penalty(wb: Workbook, result: SummaryResult) -> None:
    """模式 E：报价偏离惩罚明细（哪些产品、哪一家、X/Y 多少、判了哪一项）。"""
    sl = result.shortlist
    ws = wb.create_sheet("偏离惩罚明细")
    ncol = 7
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1,
          f"报价偏离惩罚明细（X=本家单价，Y=该分项有效投标人平均价；"
          f"X/Y < {sl.deviation_low:g} 或 ≥ {sl.deviation_high:g} 时该项不得分）",
          font=Font(size=13, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"共 {len(sl.penalty_items)} 项；剔除异常报价后迭代重算 Y 与最低价，"
          "直到无新的偏离扣分情况。被惩罚的投标人该项得 0 分，其余分项正常计分。",
          font=Font(size=9, color="808080"), align="center", wrap=True)

    hr = 4
    heads = ["序号", "产品名称", "投标人", "本家单价 X", "有效平均价 Y", "偏离值 X/Y", "判定"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, hr, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center")
    r = hr + 1
    for it in sl.penalty_items:
        vals = [it.get("seq", ""), it.get("name", ""), it.get("bidder", ""),
                it.get("price"), it.get("avg"), it.get("ratio"), it.get("kind", "")]
        for i, v in enumerate(vals, start=1):
            fmt = FMT_MONEY if i in (4, 5) else ("0.000" if i == 6 else None)
            _cell(ws, r, i, v, align="right" if i in (4, 5, 6) else "left",
                  fmt=fmt, fill=BAD_FILL if i == 7 else None, wrap=(i == 2))
        r += 1
    if not sl.penalty_items:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
        _cell(ws, r, 1, "无偏离惩罚项：全部投标人的各项报价均在阈值区间内。", align="center")
    for i, w in enumerate((8, 44, 28, 14, 14, 14, 18), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=hr + 1, column=1)
    ws.auto_filter.ref = f"A{hr}:G{max(hr, r - 1)}"


def _build_mode_c(result: SummaryResult, folder: Path) -> Workbook:
    wb = Workbook()
    _sheet_sl_overview(wb, result, folder)
    _sheet_sl_summary(wb, result)
    _sheet_sl_matrix(wb, result)
    if result.shortlist.variant == "per_item":
        _sheet_sq_penalty(wb, result)
    _sheet_sl_rule(wb, result)
    _sheet_files(wb, result)
    return wb


# ================================================================== 模式 D：下浮率/折扣报价
def _sheet_d_overview(wb: Workbook, result: SummaryResult, folder: Path) -> None:
    # 模式D 项目概况：按报价口径（下浮率 / 折扣率）分别展示规则与换算口径
    d = result.discount
    is_rate = d.rate_kind == "discount_rate"
    ws = wb.active
    ws.title = "项目概况"
    ncol = 6

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    pname = (result.project.get("project_name").value if result.project.get("project_name")
             else None) or folder.name
    _cell(ws, 1, 1, f"{pname}　开标汇总（{'折扣率报价' if is_rate else '下浮率 / 折扣报价'}）",
          font=Font(size=16, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"数据来源：{folder}　｜　生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}"
          f"　｜　模型：{CONFIG.text_model}　｜　本表由 AI 辅助生成，须经人工复核确认",
          font=Font(size=9, color="808080"), align="center")

    r = 3
    for fd in PROJECT_FIELDS:
        fv = result.project.get(fd.key, FieldValue(key=fd.key, label=fd.label))
        _cell(ws, r, 1, fd.label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, _fmt_money(fv.value) if fd.type == "money" else fv.to_display(), wrap=True)
        r += 1

    r += 1
    _cell(ws, r, 1, "折扣报价与价格评审概览", font=Font(bold=True, color="FFFFFF"),
          fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    trim_txt = (f"有效报价 > {d.trim_guard} 家时，去掉 {d.trim_high} 个最高、"
                f"{d.trim_low} 个最低" if (d.trim_high or d.trim_low) else "不剔除")
    if is_rate:
        overview = [
            ("投标人家数", f"{len(d.bidders)} 家（有效 {sum(1 for b in d.bidders if b.valid)} 家）"),
            ("报价口径", "投标报价（含税，折扣率）= 市场价格的百分比；评审价 = 折扣率/100"),
            ("最高限价（折扣率）",
             f"市场价格的 {d.cap_limit_pct:g}%（超过即否决投标）" if d.cap_limit_pct else "未识别"),
            ("结算公式", d.settlement_def or "结算金额 = 市场价格 × 入围折扣率 × 数量"),
            ("产品单价清单", "招标文件未给出产品单价限价清单，另见「单价结算换算」页按市场价基数折算"),
            ("评标基准价", f"{d.benchmark:.6f}（{d.benchmark_n} 家参与）" if d.benchmark else "未识别"),
            ("去高去低", trim_txt),
            ("投标报价分值", f"满分 {d.full_score:g} 分｜基本分 {d.base_score:g} 分"),
            ("偏差增减分", f"每 1% 加减 {d.step:g} 分；低于基准价超 {d.low_limit:g}% 后转扣分"),
            ("公示核对", (f"{d.publicity_file} 列出 {d.publicity_n} 家入围候选供应商，"
                       f"报价与开标表一致（公示位次为综合评分排序，非价格分排名）")
             if d.publicity_n else "未找到入围候选供应商公示"),
        ]
    else:
        overview = [
            ("投标人家数", f"{len(d.bidders)} 家（有效 {sum(1 for b in d.bidders if b.valid)} 家）"),
            ("项目需求产品数", f"{len(d.products)} 项"),
            ("最高限价（Σ限价×预估量）", _fmt_money(d.cap_total)),
            ("招标文件写明的最高限价", _fmt_money(d.stated_cap) if d.stated_cap else "未识别"),
            ("评标基准价", f"{d.benchmark:.6f}（{d.benchmark_n} 家参与）" if d.benchmark else "未识别"),
            ("去高去低", trim_txt),
            ("报价部分满分", f"{d.full_score:g} 分"),
            ("扣分系数", f"每高于基准价 1% 扣 {d.above_deduct:g} 分；"
                     f"每低于基准价 1% 扣 {d.below_deduct:g} 分"),
        ]
    for label, value in overview:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, value, wrap=True)
        r += 1

    r += 1
    _cell(ws, r, 1, "价格评审规则（由招标文件识别，分数由系统按公式计算）",
          font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    lines = [
        ("投标报价口径", d.discount_def),
        ("评审价口径", d.review_price_def),
        ("单价 / 结算口径", d.unit_price_def),
        ("基准价取法", d.benchmark_rule or "有效投标报价的平均值"),
        ("得分公式", d.formula),
        ("规则出处", f"{d.rule_source_file}"
                    + (f"　P{d.rule_source_page}" if d.rule_source_page else "")),
        ("千问页定位", f"P{', P'.join(d.llm_pages)}" if d.llm_pages else "—"),
        ("规则状态", ("✓ " if d.confirmed else "⚠ ") + d.basis),
        ("开标表来源", d.opening_file),
        ("产品清单来源", d.product_file if d.products else "本项目无产品单价清单"),
    ]
    if d.publicity_n:
        lines.append(("公示来源", d.publicity_file))
    for label, value in lines:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
        _cell(ws, r, 2, value, wrap=True)
        ws.row_dimensions[r].height = 28
        r += 1

    r += 1
    _cell(ws, r, 1, "待复核事项", font=Font(bold=True, color="9C0006"), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    flags = [f"· {w}" for w in result.warnings]
    for b in d.bidders:
        for f in b.flags:
            flags.append(f"· {b.name}：{f}")
    for f in result.excluded_files:
        flags.append(f"· 已排除干扰文件：{f['file']} —— {f['reason']}")
    for f in result.failed_files:
        flags.append(f"· 文件读取失败：{f['file']} —— {f['reason']}")
    if not flags:
        flags = ["· 无"]
    _cell(ws, r, 2, "\n".join(flags), wrap=True)
    ws.row_dimensions[r].height = max(30, min(260, 16 * len(flags)))

    r += 2
    _cell(ws, r, 1, "说明", font=Font(bold=True))
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=ncol)
    if is_rate:
        note = (
            "1. 开标一览表的报价折扣率由程序直接读取 PDF 表格，未经模型转写；"
            "千问只负责解析「投标报价评分标准」规则。\n"
            "2. 本项目报价即折扣率（如 67% = 评审价 0.67），严禁按「1 − 下浮率」再换算一次；"
            "结算金额 = 市场价格 × 入围折扣率 × 数量。\n"
            "3. 「单价结算换算」页以假设的市场价基数演示各投标人的结算单价；"
            "招标文件未给出产品单价清单，实际结算以当期市场调查价与实际数量为准。\n"
            f"4. 评标基准价 = 有效报价算术平均值（超过 {d.trim_guard} 家时去掉 1 个最高、1 个最低）；"
            "被剔除的投标人仍参与价格评分。\n"
            f"5. 价格分：等于基准价得基本分 {d.base_score:g}；每低 1% 加 {d.step:g} 分至满分 "
            f"{d.full_score:g}；低超 {d.low_limit:g}% 后在满分基础上每再低 1% 扣 {d.step:g} 分；"
            f"每高 1% 扣 {d.step:g} 分。偏差率 = (报价−基准价)/基准价×100，保留 2 位。\n"
            "6. 公示位次按综合评分（报价 40 + 商务 25 + 技术 35）排序，与价格分排名不是一回事；"
            "本表仅含价格分，商务与技术分需评标委员会评审。")
    else:
        note = (
            "1. 开标一览表的优惠率、招标文件项目需求清单的单价最高限价与预估量，均由程序直接读取 PDF 表格，"
            "未经模型转写；千问只负责解析「价格评审」规则。\n"
            "2. 折扣 = 1 − 下浮率/100；招标文件明确「本项目评审价 = 折扣」，价格分按折扣（评审价）计算。\n"
            "3. 各产品折算单价 = 单价最高限价 × 折扣；预估结算价 = Σ(限价 × 预估量 × 折扣)，"
            "按预估采购量估算，实际结算以实际交付数量为准。\n"
            "4. 评标基准价 = 有效投标报价的平均值（人数超过门槛时去掉一个最高和一个最低）；"
            "被剔除的投标人仍参与价格评分。\n"
            "5. 价格分：报价等于基准价得满分；高于基准价每 1% 扣 X 分、低于每 1% 扣 Y 分，"
            "按 (报价−基准价)/基准价×100 的相对偏差百分点直线插入，四舍五入保留 2 位，下限 0 分。\n"
            "6. 本表仅含价格分；商务、技术、服务部分需评标委员会评审。")
    _cell(ws, r, 2, note, wrap=True)
    ws.row_dimensions[r].height = 150

    for i, w in enumerate((22, 34, 14, 16, 22, 14), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False



def _sheet_d_summary(wb: Workbook, result: SummaryResult) -> None:
    # 价格评分汇总：两种报价口径列不同，计算值都来自 discount.py 的纯代码结果
    d = result.discount
    is_rate = d.rate_kind == "discount_rate"
    ws = wb.create_sheet("价格评分汇总")
    full = d.full_score
    if is_rate:
        cols = [
            ("排名", 6), ("投标单位", 32), ("投标报价\n折扣率(%)", 13),
            ("评审价\n(折扣率/100)", 14), ("相对基准价\n偏差(%)", 13),
            (f"价格分\n(满分 {full:g})", 12), ("每100元市场价\n结算(元)", 15),
            ("每100元市场价\n优惠(元)", 15), ("占最高限价\n(%)", 12),
            ("参与\n基准价", 9), ("公示\n位次", 8), ("报价状态 / 备注", 36),
        ]
    else:
        cols = [
            ("排名", 6), ("投标单位", 32), ("优惠率/下浮率\n(%)", 12),
            ("投标折扣\n(评审价)", 13), ("相对基准价\n偏差(%)", 13),
            (f"价格分\n(满分 {full:g})", 12), ("折算单价\n合计(元)", 15),
            ("预估结算价\n(元)", 16), ("结算价占\n最高限价%", 12),
            ("参与\n基准价", 9), ("报价状态 / 备注", 40),
        ]
    ncol = len(cols)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    title = ("投标价格汇总与价格分一览（报价折扣率即评审价；"
             "结算金额 = 市场价格 × 入围折扣率 × 数量）" if is_rate else
             "投标价格汇总与价格分一览（折扣 = 1 − 下浮率；评审价 = 折扣；"
             "各产品单价 = 单价最高限价 × 折扣）")
    _cell(ws, 1, 1, title, font=Font(size=14, bold=True, color="FFFFFF"),
          fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    rule_txt = (f"基本分 {d.base_score:g} / 满分 {full:g}，每 1% 加减 {d.step:g} 分，"
                f"低超 {d.low_limit:g}% 转扣" if is_rate else
                f"满分 {full:g} 分　｜　高于扣 {d.above_deduct:g}/1%，低于扣 {d.below_deduct:g}/1%")
    _cell(ws, 2, 1,
          f"评标基准价 {d.benchmark:.6f}（{d.benchmark_n} 家参与；"
          + (f"有效报价 > {d.trim_guard} 家，已去掉 {d.trim_high} 个最高、{d.trim_low} 个最低）"
             if (d.trim_high or d.trim_low) else "不去高去低")
          + f"　｜　{rule_txt}"
          + (f"　｜　最高限价 {d.cap_limit_pct:g}%" if d.cap_limit_pct else ""),
          font=Font(size=10), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 24

    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)
    _cell(ws, 3, 1,
          f"评分依据：{d.rule_source_file} P{d.rule_source_page}「投标报价评分标准」"
          f"　｜　{d.review_price_def or ('评审价=报价折扣率' if is_rate else '评审价=折扣')}"
          f"　｜　{d.discount_def}"
          + ("" if d.confirmed else f"　｜　⚠ {d.basis}"),
          font=Font(size=9, color="C00000" if not d.confirmed else "404040"),
          wrap=True, fill=None if d.confirmed else NOTE_FILL)

    hr = 5
    for i, (head, width) in enumerate(cols, start=1):
        _cell(ws, hr, i, head, font=Font(bold=True), fill=HEAD_FILL,
              align="center", wrap=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[hr].height = 44

    r = hr + 1
    for b in d.bidders:
        scored = b.price_score is not None
        _cell(ws, r, 1, b.rank, align="center",
              font=Font(bold=True, color="C00000" if scored and b.rank <= 3 else "000000"),
              fill=TOP3_FILL if scored and b.rank <= 3 else None)
        _cell(ws, r, 2, b.name, wrap=True)
        _cell(ws, r, 3, b.rate, align="right", fmt="0.00")
        _cell(ws, r, 4, b.discount, align="right", fmt="0.0000", font=Font(bold=True))
        _cell(ws, r, 5, b.deviation, align="right", fmt="0.00")
        _cell(ws, r, 6, b.price_score, align="right", fmt=FMT_SCORE,
              font=Font(bold=True, color="C00000" if scored else "808080"))
        if is_rate:
            _cell(ws, r, 7, b.per_100, align="right", fmt=FMT_MONEY, font=Font(bold=True))
            _cell(ws, r, 8, (100 - b.per_100) if b.valid else None,
                  align="right", fmt=FMT_MONEY)
            _cell(ws, r, 9, (b.rate / d.cap_limit_pct * 100) if d.cap_limit_pct else None,
                  align="right", fmt="0.00")
            _cell(ws, r, 10, "是" if b.in_benchmark else "否", align="center",
                  fill=None if b.in_benchmark else NOTE_FILL)
            _cell(ws, r, 11, b.publicity_rank or "—", align="center",
                  font=Font(bold=True, color="C00000") if b.publicity_rank else None,
                  fill=TOP3_FILL if b.publicity_rank <= 3 and b.publicity_rank else None)
            note_c = 12
        else:
            _cell(ws, r, 7, b.unit_sum, align="right", fmt=FMT_MONEY)
            _cell(ws, r, 8, b.est_total, align="right", fmt=FMT_MONEY, font=Font(bold=True))
            _cell(ws, r, 9, b.discount, align="right", fmt=FMT_PCT)
            _cell(ws, r, 10, "是" if b.in_benchmark else "否", align="center",
                  fill=None if b.in_benchmark else NOTE_FILL)
            note_c = 11
        note = "；".join(b.flags) if b.flags else ("有效报价" if b.valid else "—")
        _cell(ws, r, note_c, note, wrap=True, fill=NOTE_FILL if b.flags else None)
        ws.row_dimensions[r].height = 24
        r += 1

    ws.freeze_panes = ws.cell(row=hr + 1, column=3)
    ws.sheet_view.zoomScale = 85
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(ncol)}{hr + len(d.bidders)}"


def _sheet_d_settlement(wb: Workbook, result: SummaryResult) -> None:
    # 无产品单价清单的折扣率项目：按假设市场价基数演示「单价 × 折扣率」结算额
    d = result.discount
    ws = wb.create_sheet("单价结算换算")
    bidders = d.bidders
    ncol = 1 + len(bidders) + 3

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1, "各投标单位单价结算换算（结算金额 = 市场价格 × 入围折扣率 × 数量）",
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          "招标文件未给出产品单价最高限价清单；本表以假设的「市场价基数」演示折算结算单价，"
          "不是招标文件数据，实际结算以当期市场调查价与实际交付数量为准。"
          + (f"　｜　最高限价：市场价格的 {d.cap_limit_pct:g}%" if d.cap_limit_pct else ""),
          font=Font(size=9), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 30

    hr = 4
    _cell(ws, hr, 1, "市场价基数\n(元,含税)", font=Font(bold=True),
          fill=HEAD_FILL, align="center", wrap=True)
    for j, b in enumerate(bidders):
        _cell(ws, hr, 2 + j, f"{b.name}\n报价 {b.rate:g}%",
              font=Font(bold=True, size=8), fill=HEAD_FILL, align="center", wrap=True)
    for k, h in enumerate(("最低结算价", "平均结算价", "最高结算价")):
        _cell(ws, hr, 2 + len(bidders) + k, h, font=Font(bold=True),
              fill=LABEL_FILL, align="center", wrap=True)
    ws.row_dimensions[hr].height = 52

    r = hr + 1
    for base in (100, 500, 1000, 5000, 10000, 50000):
        _cell(ws, r, 1, base, align="right", fmt=FMT_MONEY, font=Font(bold=True))
        vals = []
        for j, b in enumerate(bidders):
            v = round(base * b.discount, 2) if b.valid else None
            vals.append(v)
            _cell(ws, r, 2 + j, v, align="right", fmt=FMT_MONEY, font=Font(size=9),
                  fill=BAD_FILL if b.over_cap else None)
        quoted = [v for v in vals if v is not None]
        if quoted:
            _cell(ws, r, 2 + len(bidders), min(quoted), align="right",
                  fmt=FMT_MONEY, font=Font(bold=True, color="C00000"))
            _cell(ws, r, 3 + len(bidders), sum(quoted) / len(quoted),
                  align="right", fmt=FMT_MONEY)
            _cell(ws, r, 4 + len(bidders), max(quoted), align="right", fmt=FMT_MONEY)
        ws.row_dimensions[r].height = 20
        r += 1

    _cell(ws, r, 1, "每 100 元\n优惠额", font=Font(bold=True), fill=LABEL_FILL,
          align="center", wrap=True)
    for j, b in enumerate(bidders):
        _cell(ws, r, 2 + j, round(100 - b.per_100, 2) if b.valid else None,
              align="right", fmt=FMT_MONEY, fill=LABEL_FILL)
    ws.row_dimensions[r].height = 30

    ws.column_dimensions["A"].width = 15
    for j in range(len(bidders)):
        ws.column_dimensions[get_column_letter(2 + j)].width = 15
    for k in range(3):
        ws.column_dimensions[get_column_letter(2 + len(bidders) + k)].width = 14
    ws.freeze_panes = ws.cell(row=hr + 1, column=2)
    ws.sheet_view.zoomScale = 80



def _sheet_d_products(wb: Workbook, result: SummaryResult) -> None:
    """产品单价明细：60 个产品 × 各投标人的折算单价（限价 × 折扣）。"""
    d = result.discount
    ws = wb.create_sheet("产品单价明细")
    bidders = d.bidders
    fixed = 7   # 序号 产品名称 单位 限价 预估量 限价金额 页码
    ncol = fixed + len(bidders) + 3

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    _cell(ws, 1, 1, "各产品折算单价明细（元，含税；单价 = 单价最高限价 × 投标折扣）",
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    _cell(ws, 2, 1,
          f"共 {len(d.products)} 项　｜　Σ(单价最高限价 × 预估采购量) = {_fmt_money(d.cap_total)} 元"
          f"　｜　招标文件写明最高限价 {_fmt_money(d.stated_cap) if d.stated_cap else '未识别'} 元"
          "　｜　各家列显示按该家折扣折算后的中标单价（保留 2 位）",
          font=Font(size=9), align="center", fill=NOTE_FILL, wrap=True)
    ws.row_dimensions[2].height = 22

    hr = 4
    heads = ["序号", "产品名称", "单位", "单价最高限价\n(元,含税)", "预估\n采购量",
             "限价金额\n(限价×量)", "PDF\n页码"]
    for i, h in enumerate(heads, start=1):
        _cell(ws, hr, i, h, font=Font(bold=True), fill=HEAD_FILL, align="center", wrap=True)
    for j, b in enumerate(bidders):
        _cell(ws, hr, fixed + 1 + j, f"{b.name}\n折扣 {b.discount:.4f}",
              font=Font(bold=True, size=8), fill=HEAD_FILL, align="center", wrap=True)
    for k, h in enumerate(("最低折算单价", "平均折算单价", "最高折算单价")):
        _cell(ws, hr, fixed + len(bidders) + 1 + k, h, font=Font(bold=True),
              fill=LABEL_FILL, align="center", wrap=True)
    ws.row_dimensions[hr].height = 52

    r = hr + 1
    for p in d.products:
        _cell(ws, r, 1, p.seq, align="center")
        _cell(ws, r, 2, p.name, wrap=True)
        _cell(ws, r, 3, p.unit, align="center")
        _cell(ws, r, 4, p.cap, align="right", fmt=FMT_MONEY)
        _cell(ws, r, 5, p.qty, align="right", fmt="#,##0.##")
        _cell(ws, r, 6, p.cap * p.qty, align="right", fmt=FMT_MONEY)
        _cell(ws, r, 7, p.source_page, align="center")
        vals = []
        for j, b in enumerate(bidders):
            v = round(p.cap * b.discount, 2) if b.valid else None
            vals.append(v)
            _cell(ws, r, fixed + 1 + j, v, align="right", fmt=FMT_MONEY,
                  font=Font(size=9))
        quoted = [v for v in vals if v is not None]
        if quoted:
            _cell(ws, r, fixed + len(bidders) + 1, min(quoted), align="right",
                  fmt=FMT_MONEY, font=Font(bold=True, color="C00000"))
            _cell(ws, r, fixed + len(bidders) + 2, sum(quoted) / len(quoted),
                  align="right", fmt=FMT_MONEY)
            _cell(ws, r, fixed + len(bidders) + 3, max(quoted), align="right", fmt=FMT_MONEY)
        ws.row_dimensions[r].height = 20
        r += 1

    # 合计行
    _cell(ws, r, 1, "合计", font=Font(bold=True), fill=LABEL_FILL, align="center")
    _cell(ws, r, 2, "Σ单价 / Σ限价金额 / 各家预估结算价", font=Font(bold=True), fill=LABEL_FILL)
    for c in (3, 5, 7):
        _cell(ws, r, c, "", fill=LABEL_FILL)
    _cell(ws, r, 4, sum(p.cap for p in d.products), align="right", fmt=FMT_MONEY,
          font=Font(bold=True), fill=LABEL_FILL)
    _cell(ws, r, 6, d.cap_total, align="right", fmt=FMT_MONEY,
          font=Font(bold=True), fill=LABEL_FILL)
    for j, b in enumerate(bidders):
        _cell(ws, r, fixed + 1 + j, b.est_total, align="right", fmt=FMT_MONEY,
              font=Font(bold=True, size=9), fill=LABEL_FILL)

    for i, w in enumerate((6, 40, 7, 14, 10, 14, 10), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for j in range(len(bidders)):
        ws.column_dimensions[get_column_letter(fixed + 1 + j)].width = 13
    for k in range(3):
        ws.column_dimensions[get_column_letter(fixed + len(bidders) + 1 + k)].width = 13
    ws.freeze_panes = ws.cell(row=hr + 1, column=fixed + 1)
    ws.sheet_view.zoomScale = 80


def _sheet_d_rule(wb: Workbook, result: SummaryResult) -> None:
    # 评分依据：招标文件规则原文 + 识别参数 + 千问页定位记录
    d = result.discount
    is_rate = d.rate_kind == "discount_rate"
    ws = wb.create_sheet("评分依据")
    ws.merge_cells("A1:D1")
    _cell(ws, 1, 1, "投标报价评分标准（招标文件原文摘录）",
          font=Font(size=14, bold=True, color="FFFFFF"), fill=TITLE_FILL, align="center")
    ws.row_dimensions[1].height = 28

    r = 2
    _cell(ws, r, 1, "规则原文", font=Font(bold=True), fill=LABEL_FILL)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
    _cell(ws, r, 2, d.rule_text or "未能在招标文件中定位到相关章节",
          wrap=True, font=Font(size=10))
    ws.row_dimensions[r].height = max(60, min(400, 14 * (len(d.rule_text or "") // 55 + 1)))
    r += 2

    if d.rule_detail:
        _cell(ws, r, 1, "评分详情（评分项 / 评审项目 / 评分标准，摘自招标文件）",
              font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        for row in d.rule_detail[:20]:
            cells = [row[0] if len(row) > 0 else "", row[1] if len(row) > 1 else "",
                     (row[2] if len(row) > 2 else "") + ("｜" + row[3] if len(row) > 3 else "")]
            _cell(ws, r, 1, cells[0], wrap=True, align="center")
            _cell(ws, r, 2, cells[1], wrap=True, align="center")
            ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
            _cell(ws, r, 3, cells[2], wrap=True, font=Font(size=10))
            ws.row_dimensions[r].height = max(22, min(180, 13 * (len(cells[2]) // 55 + 1)))
            r += 1
        r += 1

    _cell(ws, r, 1, "识别出的可执行参数（原文正则直取，千问结构化抽取交叉校验）",
          font=Font(bold=True, color="FFFFFF"), fill=TITLE_FILL)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    r += 1
    if is_rate:
        params = [
            ("报价口径", d.discount_def),
            ("评审价 / 偏差率", d.review_price_def),
            ("结算公式", d.settlement_def or d.unit_price_def),
            ("最高限价（折扣率）", f"市场价格的 {d.cap_limit_pct:g}%（超过否决投标）"
             if d.cap_limit_pct else "未识别"),
            ("报价满分 / 基本分", f"{d.full_score:g} 分 / {d.base_score:g} 分"),
            ("偏差增减分步长", f"每 1% 加减 {d.step:g} 分"),
            ("低幅上限", f"低于基准价超过 {d.low_limit:g}% 后由加分转为在满分基础上扣分"),
            ("基准价取法", d.benchmark_rule or "有效报价算术平均值"),
            ("评标基准价", f"{d.benchmark:.6f}（{d.benchmark_n} 家参与）" if d.benchmark else "未识别"),
            ("去高去低", (f"有效报价 > {d.trim_guard} 家时去掉 {d.trim_high} 高 / {d.trim_low} 低"
                      if (d.trim_high or d.trim_low) else "不剔除")),
            ("得分公式", d.formula),
            ("公示核对", (f"{d.publicity_file} 列出 {d.publicity_n} 家入围候选供应商；"
                      f"公示位次为综合评分排序，非价格分排名") if d.publicity_n else "未找到公示"),
        ]
    else:
        params = [
            ("报价部分满分", f"{d.full_score:g} 分"),
            ("评审价口径", d.review_price_def or "评审价 = 折扣"),
            ("折扣定义", d.discount_def or "折扣 = 1 − 下浮率/100"),
            ("中标单价口径", d.unit_price_def or "单价 = 限价 × 折扣"),
            ("基准价取法", d.benchmark_rule or "有效报价平均值"),
            ("评标基准价", f"{d.benchmark:.6f}（{d.benchmark_n} 家参与）" if d.benchmark else "未识别"),
            ("去高去低", (f"有效报价 > {d.trim_guard} 家时去掉 {d.trim_high} 高 / {d.trim_low} 低"
                      if (d.trim_high or d.trim_low) else "不剔除")),
            ("高于基准价扣分", f"每 1% 扣 {d.above_deduct:g} 分"),
            ("低于基准价扣分", f"每 1% 扣 {d.below_deduct:g} 分"),
            ("得分公式", d.formula),
        ]
    params += [
        ("规则出处", f"{d.rule_source_file}　P{d.rule_source_page}"),
        ("千问页定位", f"按关键词命中并送模型的页：P{', P'.join(d.llm_pages)}"
                    if d.llm_pages else "—"),
        ("规则状态", ("✓ " if d.confirmed else "⚠ ") + d.basis),
    ]
    for label, value in params:
        _cell(ws, r, 1, label, font=Font(bold=True), fill=LABEL_FILL)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        _cell(ws, r, 2, value, wrap=True)
        ws.row_dimensions[r].height = 28
        r += 1

    for col, w in zip("ABCD", (22, 26, 62, 40)):
        ws.column_dimensions[col].width = w
    ws.sheet_view.showGridLines = False



def _build_mode_d(result: SummaryResult, folder: Path) -> Workbook:
    wb = Workbook()
    _sheet_d_overview(wb, result, folder)
    _sheet_d_summary(wb, result)
    if result.discount.products:
        _sheet_d_products(wb, result)
    else:
        _sheet_d_settlement(wb, result)
    _sheet_d_rule(wb, result)
    _sheet_files(wb, result)
    return wb



# ================================================================== 对外
def save(result: SummaryResult, source_folder: Path, out_path: Path | None = None) -> Path:
    CONFIG.ensure_dirs()
    if out_path is None:
        stem = (result.project.get("project_name").value or source_folder.name)
        stem = "".join(ch for ch in str(stem) if ch not in r'\/:*?"<>|')[:40]
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
        out_path = CONFIG.output_dir / f"开标汇总表_{stem}_{stamp}.xlsx"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = (_build_mode_d(result, source_folder) if result.mode == "discount"
          else _build_mode_c(result, source_folder) if result.mode == "shortlist"
          else _build_mode_b(result, source_folder) if result.mode == "opening_table"
          else _build_mode_a(result, source_folder))
    wb.save(out_path)
    return out_path


def print_preview(result: SummaryResult) -> None:
    def bar(n=88):
        print("─" * n)

    pname = (result.project.get("project_name").to_display()
             if result.project.get("project_name") else "—")
    print()
    bar()
    print(f"  开标汇总 ｜ {pname}")
    bar()
    if result.mode == "discount":
        d = result.discount
        is_rate = d.rate_kind == "discount_rate"
        print(f"  报价口径：{d.discount_def}"
              + (f"　｜　最高限价 {d.cap_limit_pct:g}%" if d.cap_limit_pct else ""))
        print(f"  评分规则：{d.formula}")
        print(f"  基准价：{d.benchmark:.6f}（{d.benchmark_n} 家参与）"
              + (f"　｜　有效报价 > {d.trim_guard} 家，去 1 高 1 低"
                 if (d.trim_high or d.trim_low) else ""))
        print(f"  规则状态：{'✓ ' if d.confirmed else '⚠ '}{d.basis}")
        print(f"  {'产品清单' if not is_rate else '结算公式'}："
              + (f"{d.settlement_def}" if is_rate else
                 f"{len(d.products)} 项　Σ(限价×预估量)={_fmt_money(d.cap_total)} 元")
              + f"　｜　页定位 P{', P'.join(d.llm_pages)}")
        if d.publicity_n:
            print(f"  公示核对：{d.publicity_file} 列出 {d.publicity_n} 家入围候选供应商，"
                  f"报价与开标表一致（公示位次按综合评分，非价格分排名）")
        for w in result.warnings:
            print(f"    ⚠ {w}")
        print()
        if is_rate:
            heads = ["排名", "投标单位", "折扣率%", "评审价", "偏差%", "价格分",
                     "每百元结算", "公示位次"]
            widths = [5, 34, 10, 10, 10, 9, 14, 10]
        else:
            heads = ["排名", "投标单位", "下浮率%", "折扣", "偏差%", "价格分", "预估结算价"]
            widths = [5, 34, 10, 10, 10, 9, 18]
        print("   " + "".join(h.ljust(w) for h, w in zip(heads, widths)))
        for b in d.bidders:
            dev = f"{b.deviation:+.2f}" if b.deviation is not None else "—"
            sc = f"{b.price_score:.2f}" if b.price_score is not None else "—"
            vals = ([b.rank, b.name[:32], f"{b.rate:g}", f"{b.discount:.4f}", dev, sc,
                     f"{b.per_100:.2f}", b.publicity_rank or "—"] if is_rate else
                    [b.rank, b.name[:32], f"{b.rate:g}", f"{b.discount:.4f}", dev, sc,
                     _fmt_money(b.est_total)])
            print("   " + "".join(str(v).ljust(w) for v, w in zip(vals, widths)))
        bar()
        flags = [f"· {w}" for w in result.warnings]
        flags += [f"· {b.name}：{f}" for b in d.bidders for f in b.flags]
        print("  待复核：" + ("无" if not flags else ""))
        for f in flags[:10]:
            print("   " + f)
    elif result.mode == "shortlist":
        sl = result.shortlist
        is_item = sl.variant == "per_item"
        f_txt = sl.formula or ("（该项最低价 / 本家单价）× 分项价格分" if is_item
                               else "（评标基准价）／（投标报价）×分值")
        if not f_txt.startswith("得分"):
            f_txt = "得分=" + f_txt
        if is_item:
            print(f"  评分规则：{sl.price_basis}　｜　{sl.eval_price_rule or '满分×分项权重'}"
                  f"　｜　偏离惩罚 X/Y < {sl.deviation_low:g} 或 ≥ {sl.deviation_high:g}"
                  f"　｜　{f_txt}")
        else:
            print(f"  评分规则：评标价={sl.eval_price_rule or '∑单价×权重'}"
                  f"　｜　基准价={sl.benchmark_rule or '最低值'}"
                  f"　｜　{f_txt}")
        print(f"  规则状态：{'✓ ' if sl.confirmed else '⚠ '}{sl.basis}")
        for w in result.warnings:
            print(f"    ⚠ {w}")
        for cat in sl.categories:
            if is_item:
                print(f"  ▌{cat.name}　{len(cat.products)} 个产品　满分 {sl.full_score:g}"
                      f"　计价口径 {sl.price_basis}　投标人 {len(sl.bidders)} 家（sheet 名识别）")
            else:
                print(f"  ▌{cat.name}　{len(cat.products)} 个产品　满分 {cat.score:g}"
                      f"　基准价 {_fmt_money(cat.benchmark)}"
                      f"（{cat.benchmark_holder}，{cat.benchmark_n} 家参与）")
        print()
        heads = ["排名", "投标单位", "加权报价水平" if is_item else "评标价", "价格分", "备注"]
        widths = [5, 34, 26, 9, 30]
        print("   " + "".join(h.ljust(w) for h, w in zip(heads, widths)))
        for b in sl.bidders:
            evs = " / ".join(_fmt_money(e) for e in b.eval_prices)
            sc = f"{b.total_score:.2f}" if b.total_score is not None else "—"
            print("   " + "".join(str(v).ljust(w) for v, w in zip(
                [b.rank, b.name[:32], evs, sc, "；".join(b.flags)[:28]], widths)))
        bar()
        flags = [f"· {w}" for w in result.warnings]
        flags += [f"· {b.name}：{f}" for b in sl.bidders for f in b.flags]
        print("  待复核：" + ("无" if not flags else ""))
        for f in flags[:10]:
            print("   " + f)
    elif result.mode == "opening_table":
        r = result.rule
        if r.behavior == "itemized_deviation":
            fn = "、".join(f"{p['label']} {p['score']:g}" for p in r.parts)
            b_txt = (f"{len(r.parts)} 分项各自算基准价；Dn=Fn±偏差率×100×E"
                     f"（高 E1={r.itemized_e1:g}/低 E2={r.itemized_e2:g}）"
                     f"；每项最低0分；得分=ΣDn；Fn[{fn}]")
        elif r.behavior == "full_deviation":
            b_txt = (f"P=N 得满分 {r.total_score:g}；每高 1% 扣 {r.step_high:g} 分、"
                     f"每低 1% 扣 {r.step_low:g} 分；N 按家数分 {len(r.trim_tiers)} 档去极值")
        elif r.behavior == "deviation" and r.base_score:
            b_txt = (f"基本分 {r.base_score:g} + 偏差率×{r.step_percent:g} 分/1%"
                     "（低者加分、高者扣分）")
        elif any(l.pricing == "single" for l in result.lots):
            b_txt = "A≤B 得满分，A>B 得 (B/A)×满分；B=去最高/最低后均价×系数"
        else:
            b_txt = f"P基准=max(最低价, 均价×{r.benchmark_factor:g})"
        print(f"  评分规则：{r.method or '未识别'}　总分 {r.total_score:g}　{b_txt}"
              + (f"　出处 P{r.source_page}" if r.source_page else ""))
        print(f"  规则状态：{'✓ ' if r.confirmed else '⚠ '}{r.basis or '未确认'}")
        for w in result.warnings:
            print(f"    ⚠ {w}")
        for lot in result.lots:
            print()
            print(f"  ▌{lot.lot_name}　（{len(lot.rows)} 家｜{lot.priced_by}）")
            if lot.itemized and r.behavior == "itemized_deviation":
                heads = ["排名", "投标人"] + [f"{p.label[:6]}({p.score:g})" for p in lot.parts] \
                        + ["价格分"]
                widths = [5, 30] + [11] * len(lot.parts) + [9]
                print("   " + "".join(h.ljust(w) for h, w in zip(heads, widths)))
                print("   分项基准价：" + "、".join(
                    f"{p.label} {_fmt_money(p.benchmark)}" for p in lot.parts))
                for row in lot.rows:
                    sc = f"{row.price_score:.2f}" if row.price_score is not None else "—"
                    partsc = [f"{v:.2f}" if v is not None else "—" for v in row.part_scores]
                    print("   " + "".join(str(v).ljust(w) for v, w in zip(
                        [row.rank, row.name[:28]] + partsc + [sc], widths)))
            else:
                heads = ["排名", "投标人", "评标价合计", "价格分", "偏差率"]
                widths = [5, 32, 15, 9, 10]
                print("   " + "".join(h.ljust(w) for h, w in zip(heads, widths)))
                for row in lot.rows:
                    dev = f"{row.deviation * 100:+.2f}%" if row.deviation is not None else "—"
                    sc = f"{row.price_score:.2f}" if row.price_score is not None else "—"
                    print("   " + "".join(str(v).ljust(w) for v, w in zip(
                        [row.rank, row.name[:30], _fmt_money(row.total), sc, dev], widths)))
        bar()
        flags = [f"· {w}" for w in result.warnings]
        flags += [f"· {lot.lot_name}／{r_.name}：{f}" for lot in result.lots
                  for r_ in lot.rows for f in r_.flags]
        flags += [f"· 已排除：{e['file']}（{e['reason']}）" for e in result.excluded_files]
        print("  待复核：" + ("无" if not flags else ""))
        for f in flags[:10]:
            print("   " + f)
    else:
        info = [f"{d.label}：{_fmt_money(result.project[d.key].value) if d.type == 'money' else result.project[d.key].to_display()}"
                for d in PROJECT_FIELDS if result.project.get(d.key)]
        for i in range(0, len(info), 2):
            print("  " + "    ".join(info[i:i + 2]))
        bar()
        widths = [4, 30, 16, 22, 14]
        heads = ["#", "投标人", "报价(元)", "工期/服务期", "保证金"]
        print("  " + "".join(h.ljust(w) for h, w in zip(heads, widths)))
        for i, row in enumerate(result.rows, 1):
            def g(k):
                fv = row.fields.get(k)
                if not fv:
                    return "—"
                return _fmt_money(fv.value) if fv.key in ("bid_price", "deposit") else fv.to_display()
            vals = [i, row.bidder[:28], g("bid_price"), g("duration")[:20], g("deposit")]
            print("  " + "".join(str(v).ljust(w) for v, w in zip(vals, widths)))
        bar()
        flags = [f"· {r_.bidder}：{f}" for r_ in result.rows for f in r_.flags]
        print("  待复核：" + ("无" if not flags else ""))
        for f in flags[:10]:
            print("   " + f)
    if result.publicity:
        bar()
        for p in result.publicity:
            print(f"  公示核对（{p.source_file}）：")
            for c in p.checks:
                print(f"    · {c}")
    u = result.tokens
    print(f"  Token：输入 {u.get('in', 0):,} / 输出 {u.get('out', 0):,}")
    bar()
