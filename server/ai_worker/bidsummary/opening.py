"""开标记录表解析（模式 B：一张表里有多家投标人）。

很多真实项目的开标文件夹里**没有按投标人分目录**，只有一张「开标记录表 / 开标一览表」，
所有投标人按行排列、每个 sheet 一个标段。这时按文件夹分组必然只抽出一行空壳。

本模块负责：
  1. 在网格里定位表头、投标人列、报价列、税率列、期限列（规则优先，AI 兜底）
  2. 逐行直读数值（不经过大模型，保证数字准确）
  3. 输出每个标段一个 LotResult

两个容易踩的坑，这里都按内容而不是文件名处理：
  · 文件名可能只叫「终端一览表」「报价汇总」，跟「开标」两个字毫无关系；
  · 同一张 sheet 里常排出两段表（序号 1-8 一段、9-10 续在下面），中间夹一行「备注/记录人」。
"""
from __future__ import annotations

import re
from pathlib import Path

from bidsummary import llm
from bidsummary.grids import read_grids, to_number
from bidsummary.models import BidRow, FileItem, LotResult, PricePart

NAME_KEYS = ("投标人名称", "投标人", "投标单位", "单位名称", "供应商名称", "供应商", "响应人")
TAX_KEYS = ("税率",)
DUR_KEYS = ("服务期限", "工期", "交货期", "服务期", "履约期限", "完成期限")
PRICE_KEYS = ("单价合计", "报价", "总价", "金额", "合计", "单价")
PRICE_EXCLUDE = ("税率", "期限", "工期", "内容", "备注", "序号", "排名", "编号",
                 "投标人", "投标单位", "供应商", "说明", "得分", "分值")
STOP = ("开标人", "签名人", "监督人", "记录人", "唱标人", "评委", "招标人代表",
        "合计", "总计", "平均", "说明", "备注", "以上", "注：")

# 招标人自算的「价格分」表：只作对照，不参与计算
PRICE_SCORE_KEYS = ("价格分", "价格得分", "报价得分", "投标报价得分")
SCORE_KEYS = PRICE_SCORE_KEYS + ("综合得分", "得分")
COMPANY_HINT = ("公司", "集团", "中心", "研究院", "设计院", "医院", "大学", "学院",
                "厂", "合作社", "事务所", "联合体", "商行", "经营部")


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _find_header(grid: list[list]) -> tuple[int, int] | None:
    """返回 (表头行号, 投标人列号)。"""
    for r, row in enumerate(grid[:20]):
        cells = [_s(c) for c in row]
        non_empty = sum(1 for c in cells if c)
        if non_empty < 3:
            continue
        for c, cell in enumerate(cells):
            if any(k in cell for k in NAME_KEYS) and len(cell) <= 20:
                return r, c
    return None


def _second_table_start(header: list[str], name_col: int) -> int:
    """右侧常另有一块「排名 / 得分」小表（招标人自算结果），读数据时必须截断。"""
    for c in range(name_col + 1, len(header)):
        h = header[c]
        if not h:
            continue
        if any(k in h for k in ("排名", "得分", "价格得分")):
            return c
        if c > name_col + 1 and any(k in h for k in NAME_KEYS) and len(h) <= 20:
            return c
    return len(header)


def _classify_cols(header: list[str], limit: int | None = None) -> tuple[list[int], int, int]:
    price_cols, tax_col, dur_col = [], -1, -1
    end = len(header) if limit is None else min(limit, len(header))
    for c in range(end):
        h = header[c]
        if not h:
            continue
        if any(k in h for k in TAX_KEYS) and len(h) <= 20:
            tax_col = c
            continue
        if any(k in h for k in DUR_KEYS) and len(h) <= 25:
            dur_col = c
            continue
        if any(k in h for k in PRICE_KEYS) and not any(k in h for k in PRICE_EXCLUDE):
            price_cols.append(c)
    return price_cols, tax_col, dur_col


def _is_net_col(h: str) -> bool:
    return any(k in h for k in ("不含税", "税前", "除税"))


def _is_inc_col(h: str) -> bool:
    return "含税" in h and not _is_net_col(h)


def _category_key(h: str) -> str:
    """价格列的「品类核心名」：去掉含税/不含税、单位、报价字样后的部分。

    用于把「门楣发光字类投标报价（含税）（元）」和
    「门楣发光字类投标报价（不含税）（元）」归为同一品类的两列。
    """
    s = _s(h)
    s = re.sub(r"[（(]\s*(含税|不含税|税前|除税|元|%|％)\s*[）)]", "", s)
    s = re.sub(r"含税|不含税|税前|除税", "", s)
    s = re.sub(r"[（(]\s*元\s*[）)]|[（(]\s*%\s*[）)]", "", s)
    s = re.sub(r"投标报价|报价|单价合计|总价|金额|合计|单价", "", s)
    s = re.sub(r"[\s　（）()·:：/、\-—]", "", s)
    return s.strip()


def _group_price_cols(header: list[str], price_cols: list[int]) -> dict[str, dict]:
    """按品类核心名把价格列分组，返回 {品类: {"net": 列, "inc": 列, "cols": [列]}}。

    去掉含税/不含税等修饰后为空的列名（如「投标报价(不含税)(元)」）统一归入
    「__plain__」一组，避免同一笔总报价的含税/不含税两列被拆成两个不同品类。
    """
    groups: dict[str, dict] = {}
    for c in price_cols:
        h = header[c] if c < len(header) else ""
        key = _category_key(h) or "__plain__"
        g = groups.setdefault(key, {"net": -1, "inc": -1, "cols": []})
        g["cols"].append(c)
        if _is_net_col(h):
            if g["net"] < 0:
                g["net"] = c
        elif _is_inc_col(h):
            if g["inc"] < 0:
                g["inc"] = c
    return groups


def _resolve_pricing(header: list[str], price_cols: list[int], tax_col: int):
    """判断报价形态并选择评标列。

    返回 (pricing, 评标列/列表, 含税对照列/列表, 标签)，pricing 取值：
      · single   — 单一总报价（只有 1 个品类，含税/不含税成对或单列）。取不含税列评标。
      · itemized — 多品类**且至少 2 个品类各自同时有含税+不含税两列**（如安徽工行
                   门楣/灯箱/标识/配件各有两列）。每个品类取不含税列评标、含税列对照，
                   后续按分项独立基准价计分。
      · multi    — 其他多列（如焦作的多个「含税单价合计」价格区间，彼此无净价/含税配对）。
                   保持原有区间合计口径（比值法/逐区间），不拆分品类。

    判定 itemized 必须「成对品类≥2」：单一品类成对仍是 single；
    若干区间列各自独立（无任何成对品类）则是 multi，绝不能当分项独立基准价处理。
    """
    if len(price_cols) == 1:
        return "single", price_cols[0], -1, header[price_cols[0]]
    groups = _group_price_cols(header, price_cols)
    paired = [g for g in groups.values() if g["net"] >= 0 and g["inc"] >= 0]
    # 单一品类（含税/不含税成对，或全是同一口径的多列）→ 单一总报价
    if len(groups) == 1:
        g = next(iter(groups.values()))
        net = g["net"] if g["net"] >= 0 else (g["inc"] if g["inc"] >= 0 else g["cols"][0])
        inc = g["inc"] if g["inc"] >= 0 and g["inc"] != net else -1
        return "single", net, inc, header[net]
    # 多品类 + 至少 2 个品类各自成对 → 分项独立基准价
    if len(paired) >= 2:
        eval_cols: list[int] = []
        incl_cols: list[int] = []
        for key, g in groups.items():
            eval_cols.append(g["net"] if g["net"] >= 0 else g["cols"][0])
            incl_cols.append(g["inc"] if g["inc"] >= 0 else -1)
        return "itemized", eval_cols, incl_cols, ""
    # 其他多列 → 普通多区间（保持原 price_cols，不拆品类）
    return "multi", -1, -1, ""


def _is_stop(cell: str) -> bool:
    return bool(cell) and any(cell.startswith(s) for s in STOP)


def _normalize_lot_name(name: str) -> str:
    """「包1」→「包件一」；已带描述的名称原样返回。"""
    m = re.fullmatch(r"\s*包\s*([0-9一二三四五六]+)\s*", name or "")
    if not m:
        return name
    cn = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}
    return "包件" + cn.get(m.group(1), m.group(1))


def _generic(name: str) -> bool:
    return bool(re.fullmatch(r"(Sheet|sheet)\d*|\d+", (name or "").strip()))


def _parse_meta(grid: list[list], header_row: int) -> dict:
    text = "\n".join(_s(c) for row in grid[:header_row] for c in row if _s(c))
    meta = {"lot_name": "", "lot_no": "", "open_time": "", "title": ""}
    for pat, key in (
        (r"标段[（(]包[)）]?名称\s*[：:]\s*(.+)", "lot_name"),
        (r"标段名称\s*[：:]\s*(.+)", "lot_name"),
        (r"标段编号\s*[：:]\s*([^\s]+)", "lot_no"),
        (r"开标时间\s*[：:]\s*([0-9]{4}[-年][^\n]*)", "open_time"),
    ):
        m = re.search(pat, text)
        if m:
            meta[key] = m.group(1).strip()
    if not meta["lot_name"]:
        m = re.search(r"【\s*(包件[一二三四五六])\s*[：:]\s*([^】]+)】", text)
        if m:
            meta["lot_name"] = f"{m.group(1)} · {m.group(2).strip()}"
        else:
            m = re.search(r"包件名称\s*[：:]\s*([^\n]+)", text)
            if m:
                meta["lot_name"] = m.group(1).strip()
    if not meta["lot_no"]:
        m = re.search(r"([A-Z][A-Z0-9]{5,}\s*/\s*\d{1,3})", text)
        if m:
            meta["lot_no"] = m.group(1).replace(" ", "")
    # 一张表只有一个标段、sheet 名又是 Sheet1 时，用表头标语当标段名
    for kw in ("开标一览表", "开标记录表", "开标记录", "唱标记录"):
        if kw in text:
            meta["title"] = kw
            break
    return meta


# ---------------------------------------------------------------- 结构识别
def _is_score_sheet(header: list[str]) -> bool:
    """只有「投标人 + 得分」没有报价列的表 = 招标人自己的排名/评分表。"""
    has_name = any(any(k in h for k in NAME_KEYS) and len(h) <= 20 for h in header if h)
    has_score = any(any(k in h for k in SCORE_KEYS) and len(h) <= 14 for h in header if h)
    has_price = any(any(k in h for k in PRICE_KEYS) and not any(k in h for k in PRICE_EXCLUDE)
                    for h in header if h)
    return has_name and has_score and not has_price


def _has_score_table(grid: list[list]) -> bool:
    return any(_is_score_sheet([_s(c) for c in row]) for row in grid[:10])


def _header_sig(header: list[str]) -> str:
    return re.sub(r"[\s　]+", "", "|".join(header))


def _first_data_row(grid: list[list], header_row: int, name_col: int) -> int:
    for r in range(header_row + 1, min(len(grid), header_row + 4)):
        row = grid[r]
        if name_col < len(row) and _s(row[name_col]):
            return r
    return -1


def _locate_blocks(grid: list[list]) -> list[tuple[int, int]]:
    """找出所有「投标人表头」块。一张 sheet 里可能排了两段同结构的表。"""
    out: list[tuple[int, int]] = []
    pos = 0
    while pos < len(grid):
        found = _find_header(grid[pos:])
        if not found:
            break
        hr, nc = pos + found[0], found[1]
        header = [_s(c) for c in grid[hr]]
        if _is_score_sheet(header):
            pos = hr + 1
            continue
        out.append((hr, nc))
        nxt = hr + 1
        while nxt < len(grid):
            row = grid[nxt]
            nm = _s(row[nc]) if nc < len(row) else ""
            if _is_stop(nm) or (not nm and any(_is_stop(_s(c)) for c in row if _s(c))):
                break
            nxt += 1
        pos = nxt + 1
    return out


def _read_block(grid: list[list], header_row: int, name_col: int, price_cols: list[int],
                tax_col: int, dur_col: int, incl_col: int, seq: int,
                rows: list[BidRow], incl_cols: list[int] | None = None) -> int:
    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        name = _s(row[name_col]) if name_col < len(row) else ""
        if _is_stop(name):
            break
        if not name:
            # 名称列空但其他列有内容：可能是合并行/说明行，跳过
            others = [_s(c) for c in row if _s(c)]
            if others and any(_is_stop(o) for o in others):
                break
            continue
        seq += 1
        parts = [to_number(row[c]) if c < len(row) else None for c in price_cols]
        tax = to_number(row[tax_col]) if 0 <= tax_col < len(row) else None
        dur = _s(row[dur_col]) if 0 <= dur_col < len(row) else ""
        incl = to_number(row[incl_col]) if 0 <= incl_col < len(row) else None
        incl_parts = ([to_number(row[c]) if 0 <= c < len(row) else None for c in incl_cols]
                      if incl_cols else [])
        rows.append(BidRow(name=name, seq=seq, src_row=r, parts=parts,
                           tax_rate=tax, duration=dur, incl_total=incl,
                           incl_parts=incl_parts))
    return seq


def _parse_score_table(grid: list[list]) -> dict[str, float]:
    """读出招标人自算的价格分：{投标人原名: 价格分}。只做对照，不参与计算。

    注意：这块表常与开标记录左右并排，左边也有一个「投标单位名称」列。
    名称列必须取**得分列左边最近的那一个**，否则会把左表的人名与右表按名次排好的分数错位配对。
    """
    for r, row in enumerate(grid[:10]):
        cells = [_s(c) for c in row]
        score_col = next((c for c, h in enumerate(cells)
                          if any(k in h for k in PRICE_SCORE_KEYS) and len(h) <= 14), -1)
        if score_col < 0:
            continue
        name_col = next((c for c in range(score_col - 1, -1, -1)
                         if any(k in cells[c] for k in NAME_KEYS) and len(cells[c]) <= 20), -1)
        if name_col < 0:
            continue
        out: dict[str, float] = {}
        for rr in range(r + 1, len(grid)):
            vals = grid[rr]
            if name_col >= len(vals) or score_col >= len(vals):
                continue
            nm = _s(vals[name_col])
            sc = to_number(vals[score_col])
            if nm and sc is not None and not _is_stop(nm):
                out[nm] = sc
        if out:
            return out
    return {}


def ai_locate(grid: list[list]) -> dict | None:
    """规则识别失败时，让千问看一眼表格结构，返回列映射。"""
    sample = "\n".join(
        f"r{i}: " + " | ".join(f"[{c}]{_s(v)}" for c, v in enumerate(row) if _s(v))
        for i, row in enumerate(grid[:12])
    )
    prompt = (
        "下面是一张「开标记录表」的前若干行，每格的格式是 [列号]内容。\n"
        "请判断表结构，只输出 JSON：\n"
        '{"header_row": <表头行号>, "name_col": <投标人名称所在列号>, '
        '"price_cols": [<各报价区间所在列号，按表头顺序>], '
        '"tax_col": <税率列号或-1>, "duration_col": <服务期限列号或-1>, '
        '"stop_keywords": [<数据行结束后出现的行首关键词>]}\n'
        "要求：price_cols 只包含真正的报价/金额列，不要包含税率、期限、招标内容、序号。\n\n"
        "表格内容：\n" + sample
    )
    try:
        d = llm.chat_json(prompt, max_tokens=800)
        if d.get("header_row") is not None and d.get("price_cols"):
            return d
    except Exception:
        pass
    return None


def parse_sheet(grid: list[list], sheet: str, source: str,
                allow_ai: bool = True, min_rows: int = 1) -> LotResult | None:
    """把一个 sheet 解析成一个标段；不像开标记录表就返回 None。

    allow_ai=False 时只走规则，用于「文件名看不出是开标表」的兜底嗅探，避免无谓的模型调用；
    min_rows 用于同样的目的：兜底嗅探时至少要有 2 家投标人才认。
    """
    if not grid or _has_score_table(grid):
        return None                     # 纯排名/得分表，只提供对照分

    blocks = _locate_blocks(grid)
    header_row = name_col = -1
    price_cols: list[int] = []
    tax_col = dur_col = -1

    if blocks:
        header_row, name_col = blocks[0]
        head_cells = [_s(c) for c in grid[header_row]]
        price_cols, tax_col, dur_col = _classify_cols(
            head_cells, _second_table_start(head_cells, name_col))

    if header_row < 0 or not price_cols:
        if not allow_ai:
            return None
        m = ai_locate(grid)
        if not m:
            return None
        header_row = int(m["header_row"])
        name_col = int(m["name_col"])
        price_cols = [int(c) for c in m["price_cols"]]
        tax_col = int(m.get("tax_col", -1))
        dur_col = int(m.get("duration_col", -1))
        blocks = [(header_row, name_col)]

    header = [_s(c) for c in grid[header_row]]
    meta = _parse_meta(grid, header_row)
    pricing, price_sel, incl_sel, plabel = _resolve_pricing(header, price_cols, tax_col)

    incl_cols: list[int] = []
    part_labels: list[str] = []
    itemized = False
    if pricing == "single":
        price_col, incl_col = price_sel, incl_sel
        if price_col in price_cols:
            price_cols = [price_col]
    elif pricing == "itemized":
        # price_sel / incl_sel 是与品类一一对应的列列表
        eval_cols = list(price_sel)
        incl_cols = list(incl_sel)
        part_labels = [_category_key(header[c]) or f"品类{i + 1}"
                       for i, c in enumerate(eval_cols)]
        itemized = True                     # 多品类各自成对 → 分项独立基准价、独立计分
        price_col = incl_col = -1
        price_cols = eval_cols
    else:
        # 普通多区间（如多个「含税单价合计」区间）：保持原 price_cols 与比值法口径
        price_col = incl_col = -1
        itemized = False

    # ---- 逐行读取（同 sheet 的续段一并读入） ----
    rows: list[BidRow] = []
    seq = 0
    sig = _header_sig(header)
    seq_col = next((i for i, h in enumerate(header) if "序号" in h), -1)
    for bi, (b_hr, b_nc) in enumerate(blocks):
        b_header = [_s(c) for c in grid[b_hr]] if b_hr < len(grid) else []
        if bi > 0:
            if b_nc != name_col or _header_sig(b_header) != sig:
                break
            first = _first_data_row(grid, b_hr, name_col)
            if seq_col >= 0 and first >= 0:
                got = to_number(grid[first][seq_col]) if seq_col < len(grid[first]) else None
                if got is None or abs(got - (seq + 1)) > 0.01:
                    break               # 序号重排 → 是另一张表（另一包件），不并入
        seq = _read_block(grid, b_hr, name_col, price_cols, tax_col, dur_col, incl_col,
                          seq, rows, incl_cols=incl_cols or None)

    if not rows or len(rows) < min_rows:
        return None

    lot_name = _normalize_lot_name(meta["lot_name"] or sheet)
    if _generic(lot_name):
        lot_name = meta["title"] or "开标一览表"

    lot = LotResult(
        lot_name=lot_name,
        lot_no=meta["lot_no"],
        open_time=meta["open_time"],
        source_file=source,
        sheet=sheet,
        header_row=header_row,
        name_col=name_col,
        tax_col=tax_col,
        duration_col=dur_col,
        parts=[PricePart(label=(plabel if pricing == "single" else
                                (part_labels[i] if i < len(part_labels)
                                 else (header[c] if c < len(header) else f"列{c}"))), col=c)
               for i, c in enumerate(price_cols)],
        rows=rows,
        pricing=pricing,
        price_col=price_col,
        incl_col=incl_col,
        incl_cols=incl_cols,
        part_labels=part_labels,
        itemized=itemized,
    )
    _attach_totals(lot)
    _attach_organizer_scores(grid, header_row, rows)
    return lot


def _attach_organizer_scores(grid: list[list], header_row: int, rows: list[BidRow]) -> int:
    """若表里右侧另有「排名/投标单位/得分」块，把招标人自算得分读出来做对照。"""
    if header_row >= len(grid):
        return 0
    header = [_s(c) for c in grid[header_row]]
    idx_score = next((c for c, h in enumerate(header)
                      if h and any(k in h for k in SCORE_KEYS) and len(h) <= 12), -1)
    if idx_score < 0:
        return 0
    idx_name = -1
    for c in range(idx_score - 1, -1, -1):
        h = header[c]
        if h and any(k in h for k in ("投标单位", "投标人", "供应商")) and len(h) <= 20:
            idx_name = c
            break
    if idx_name < 0:
        return 0
    by_name = {_name_key(r.name): r for r in rows}
    hit = 0
    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        if idx_name >= len(row) or idx_score >= len(row):
            continue
        nm = _s(row[idx_name])
        sc = to_number(row[idx_score])
        target = by_name.get(_name_key(nm))
        if target is not None and sc is not None:
            target.organizer_score = sc
            hit += 1
    return hit


def _attach_totals(lot: LotResult) -> None:
    """算出每家各区间合计（评标价），判断是否需要按不含税价评标。"""
    if lot.pricing == "single":
        label = lot.parts[0].label if lot.parts else ""
        net_basis = any(k in label for k in ("不含税", "税前", "除税"))
        lot.use_net = net_basis
        lot.priced_by = "不含税总报价" if net_basis else "含税总报价"
        for r in lot.rows:
            r.total = r.parts[0] if r.parts else None
            r.total_net = r.total if net_basis else None
            if r.incl_total is None and r.total is not None and not net_basis:
                r.incl_total = r.total
            elif r.incl_total is None and r.total is not None and net_basis:
                r.incl_total = r.total * (1 + (r.tax_rate or 0) / 100.0)
        return

    # 多品类分项：评标列已直接选中不含税列，无需再按税率折算；use_net 保持 False
    # 让 scoring._value 直接读 r.parts。含税对照价单独放在 r.incl_parts。
    if lot.itemized:
        lot.use_net = False
        lot.priced_by = "分项不含税报价（逐项计分）"
        for r in lot.rows:
            vals = [v for v in r.parts if v is not None]
            r.total = sum(vals) if vals else None      # 仅用于展示/对照，不参与分项评分
            ivals = [v for v in r.incl_parts if v is not None]
            r.incl_total = sum(ivals) if ivals else None
        return

    taxes = {r.tax_rate for r in lot.rows if r.tax_rate is not None}
    lot.use_net = len(taxes) > 1          # 同时出现小规模/一般纳税人 → 按不含税价评标
    lot.priced_by = "不含税单价合计" if lot.use_net else "含税单价合计"

    for r in lot.rows:
        if lot.use_net:
            div = (1 + (r.tax_rate or 0) / 100.0)
            r.net_parts = [None if p is None else p / div for p in r.parts]
            base = r.net_parts
        else:
            base = r.parts
        vals = [v for v in base if v is not None]
        r.total = sum(vals) if vals else None
        r.total_net = r.total if lot.use_net else None


def _relation(a: LotResult, b: LotResult) -> str | None:
    """比较两个标段是否同一份数据（含截断副本）。返回 equal / subset / superset / None。"""
    ma = {r.name: (r.total or 0) for r in a.rows}
    mb = {r.name: (r.total or 0) for r in b.rows}
    if len(ma) != len(a.rows) or len(mb) != len(b.rows):
        return None                      # 有重名投标人，不做判断
    common = set(ma) & set(mb)
    if len(common) < 2:
        return None
    if any(abs(ma[n] - mb[n]) > 0.01 for n in common):
        return None                      # 同名但报价不同 → 是两个不同标段
    if set(ma) == set(mb):
        return "equal"
    if set(ma) < set(mb):
        return "subset"
    if set(mb) < set(ma):
        return "superset"
    return None


def _name_key(name: str) -> str:
    """公司名归一化：单元格里常有换行（「郑州东生标识设计制作\\n有限公司」），先去干净。"""
    from bidsummary.scanner import normalize_bidder

    return normalize_bidder(re.sub(r"[\s　]+", "", name or ""))


RATE_LABEL_KEYS = ("折扣率", "下浮率", "优惠率", "费率", "报价率", "折扣", "下浮",
                   "优惠", "加价率", "让利", "百分数")


def _grid_rate_header(grid: list[list]) -> bool:
    """表头行里出现「…率(%)」这类列时，连列映射都别做，直接让路给模式 D。"""
    for c in (grid[0] if grid else []):
        s = _s(c)
        if ("%" in s or "％" in s) and any(k in s for k in ("报价", "率", "折扣", "优惠")):
            return True
    return False


def _is_rate_quotation(lot: LotResult) -> bool:
    """报价列是百分比 / 折扣率时，必须交给模式 D，不能按金额评标。

    PDF 开标一览表进入模式 B 后，苏州「优惠率/下浮率(%)」、周口「投标报价（含税，折扣率）
    市场价格的(%)」都会被当成报价列直读成 7.86 元 / 74.5 元，算出来的价格分完全失真。
    这类项目的正确链路是 discount.py：把百分数还原成评审价，再按招标文件里的
    折扣率评分规则计算，并校验 Σ限价×预估量。
    """
    if lot.pricing != "single" or not lot.parts:
        return False
    label = lot.parts[0].label or ""
    if any(k in label for k in RATE_LABEL_KEYS) or "%" in label or "％" in label:
        return True
    vals = [r.total for r in lot.rows if r.total is not None and r.total > 0]
    # 报价列偶尔只写「报价(%)」之外的模糊词：全部是 0~1 的小数也说明是折扣率
    if len(vals) >= 2 and max(vals) <= 1.0:
        return True
    return False


def _looks_like_bidders(rows: list[BidRow]) -> bool:
    """兜底嗅探的表必须真的是一堆公司，否则可能是投标人自己的分项报价表。"""
    if len(rows) < 2:
        return False
    named = sum(1 for r in rows if any(h in r.name for h in COMPANY_HINT))
    priced = sum(1 for r in rows if r.total is not None)
    return named >= max(2, len(rows) // 2) and priced >= max(2, len(rows) // 2)


def extract_lots(items: list[FileItem], log=print) -> list[LotResult]:
    """从开标文件夹里的 Excel / PDF 表格中抽取标段。

    不依赖文件名是否写了「开标」：先按内容嗅探，规则认出「投标人 + 报价」多行表才采纳。
    PDF 开标一览表（文本型、跨页重复表头）同样走这条链路；招标文件 PDF 里的
    「投标文件格式」空表必须排除，否则会被误当开标记录。
    """
    lots: list[LotResult] = []

    for it in items:
        ext = it.ext.lower()
        if ext not in (".xls", ".xlsx", ".xlsm", ".pdf"):
            continue
        official = it.kind == "OPENING_RECORD"
        if ext == ".pdf" and not official:
            if it.kind in ("TENDER_DOC", "PUBLICITY", "BID_FILE", "QUALIFICATION"):
                continue
            if not any(k in it.name for k in ("开标", "一览", "唱标", "记录")):
                continue
        # 已归属某投标人的表多半是它自己的报价表，不参与开标记录嗅探
        if not official and (it.bidder or it.kind in ("PUBLICITY", "TENDER_DOC")):
            continue
        try:
            grids = read_grids(Path(it.path))
        except Exception as e:
            log(f"  [警告] 读取 {it.name} 失败：{e}")
            continue

        got = 0
        lots_here: list[LotResult] = []
        file_scores: dict[str, float] = {}
        for sheet, grid in grids:
            if ext == ".pdf" and _grid_rate_header(grid):
                log(f"  [让路] {it.name}／{sheet} 表头为费率 / 折扣率报价，交给模式 D 处理")
                continue
            for nm, sc in _parse_score_table(grid).items():
                file_scores.setdefault(_name_key(nm), sc)
            lot = parse_sheet(grid, sheet, it.rel,
                              allow_ai=official, min_rows=1 if official else 2)
            if not lot:
                continue
            if _is_rate_quotation(lot):
                log(f"  [让路] {it.name}／{sheet} 报价列是「{lot.parts[0].label}」，"
                    "属于折扣率 / 下浮率报价，交给模式 D 处理（不按金额评标）")
                continue
            if not official and not _looks_like_bidders(lot.rows):
                continue

            merged = False
            for i, x in enumerate(lots):
                rel = _relation(lot, x)
                if rel is None:
                    continue
                if rel == "subset":
                    log(f"  [去重] {sheet} 是 {x.sheet} 的截断副本（{len(lot.rows)}/{len(x.rows)} 行），已忽略")
                elif rel == "equal":
                    if _generic(x.lot_name) and not _generic(lot.lot_name):
                        lots[i] = lot
                        log(f"  [去重] {x.sheet} 与 {sheet} 数据一致，保留 {lot.lot_name}")
                    else:
                        log(f"  [去重] {sheet} 与 {x.sheet} 数据一致，已忽略")
                else:                      # superset：lot 更完整
                    lots[i] = lot
                    log(f"  [去重] {x.sheet} 是 {sheet} 的截断副本，改用更完整的 {sheet}")
                merged = True
                break
            if merged:
                continue

            lots.append(lot)
            lots_here.append(lot)
            got += 1
            log(f"  [标段] {lot.lot_name}  {len(lot.rows)} 家投标人  "
                f"{'单一总报价' if lot.pricing == 'single' else str(len(lot.parts)) + ' 个报价区间'}"
                f"（{lot.priced_by}）")

        # PDF 的「项目名称 / 项目编号 / 开标时间」在表格上方的正文里，网格里没有；
        # 从该文件的 Markdown 头部回填，保证标段编号与开标时间能显示在报表上。
        if ext == ".pdf" and lots_here and it.markdown:
            head = it.markdown[:2000]
            m_no = re.search(r"项目编号\s*[：:]\s*([A-Za-z0-9/_\-]+)", head)
            m_tm = re.search(r"开标时间\s*[：:]\s*([^\n]{6,40})", head)
            for lot in lots_here:
                if not lot.lot_no and m_no:
                    lot.lot_no = m_no.group(1).strip()
                if not lot.open_time and m_tm:
                    lot.open_time = m_tm.group(1).strip()

        if got and not official:
            it.kind = "OPENING_RECORD"
            log(f"  [识别] {it.name} 文件名不含「开标」，但内容是一张开标记录表，已按开标记录处理")

        # ---- 同一文件里若有招标人自算价格分表，只在「表内没有自算得分」时补进来 ----
        if file_scores:
            for lot in lots_here:
                if any(r.organizer_score is not None for r in lot.rows):
                    continue          # 表内右侧已带排名缺，别拿别的包件/别处的分数来串台
                pairs = [(_name_key(r.name), r) for r in lot.rows]
                hits = [(file_scores[k], r) for k, r in pairs if k in file_scores]
                if len(hits) >= max(2, len(lot.rows) // 2):
                    for sc, r in hits:
                        r.organizer_score = sc
                    log(f"  [对照] {lot.lot_name}：读到招标人自算价格分 {len(hits)} 条，已并入对照列")

    # ---- 招标人自算价格分已在上面的文件循环内按标段补齐 ----
    return lots
