"""把 Excel / PDF 读成二维网格（保留合并单元格填充），供「开标记录表」逐行抽取使用。

为什么要单独读原始网格？
  开标记录表里的数字是**结构化的确定值**，直接读单元格比让大模型"抄一遍"准确得多。
  大模型只用来判断「哪一行是表头、哪几列是报价」，数值本身由代码直读。

PDF 开标一览表同样是文本型表格：pdfplumber 能把每页的表格线还原成二维网格，
但这类表常跨 7~10 页、每页重复一次表头。这里按表头签名分组，把跨页续表合并成
一张连续网格（只保留一次表头），否则下游会退化成「一家投标人」的空表。
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_ROWS = 500
MAX_COLS = 40


def to_number(v) -> float | None:
    """把 '￥43.25元'、'1,201.00'、'13%' 之类转成 float。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("￥", "").replace("¥", "").replace("元", "").replace(",", "").replace("，", "")
    s = s.replace(" ", "").replace("　", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_grids(path: Path) -> list[tuple[str, list[list]]]:
    """返回 [(sheet 名, 二维网格)]。"""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path)
    if ext == ".xls":
        return _read_xls(path)
    if ext == ".pdf":
        return _read_pdf(path)
    return []


def _clean_cell(v) -> str:
    """PDF 单元格常把中文词组按列宽硬换行：「山东尚邦建设集团\n有限公司」。

    去换行而不是换成空格，才能还原公司全称；长句同样能拼回原文。
    """
    if v is None:
        return ""
    s = str(v).replace("\r", "").replace("\n", "")
    s = re.sub(r"[ \t\u3000]+", " ", s).strip()
    return s.replace("|", "/")


def _row_sig(row: list) -> str:
    return re.sub(r"[\s　]+", "", "|".join(_clean_cell(c) for c in row))


def _is_header_row(row: list) -> bool:
    s = _row_sig(row)
    return any(k in s for k in ("序号", "投标人", "投标单位", "供应商", "报价", "名称")) \
        and len(s) <= 220


def _read_pdf(path: Path) -> list[tuple[str, list[list]]]:
    """把 PDF 里的表格按表头签名分组，跨页续表合并成一张网格。

    · 同一份开标记录往往每页重复表头 → 只保留第一份表头，数据行顺序拼接，
      这样下游 `_locate_blocks` 看到的是「一个表头 + 全部投标人」的干净网格；
    · 表头不同的表格（如评分标准表）另开一个 sheet，不与开标表串在一起。
    """
    import pdfplumber

    stem = path.stem
    m = re.search(r"标段\s*[0-9一二三四五六]+", stem)
    base = m.group(0).replace(" ", "") if m else stem

    groups: dict[str, list[list]] = {}
    order: list[str] = []
    last_sig = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    continue
                for t in tables:
                    rows = [[_clean_cell(c) for c in row] for row in t]
                    rows = [r[:MAX_COLS] for r in rows if any(c for c in r)]
                    if len(rows) < 2:
                        continue
                    head_sig = _row_sig(rows[0])
                    if _is_header_row(rows[0]) or not groups:
                        sig = head_sig
                        if sig not in groups:
                            groups[sig] = [rows[0]]
                            order.append(sig)
                        body = rows[1:]
                    elif last_sig:
                        # 续页表格没有表头：整块接到上一张表后面
                        sig = last_sig
                        body = rows
                    else:
                        continue
                    last_sig = sig
                    for r in body:
                        if _row_sig(r) == sig:      # 重复表头行，跳过
                            continue
                        groups[sig].append(r)
    except Exception:
        return []

    out = []
    for i, sig in enumerate(order):
        grid = groups[sig]
        if len(grid) < 2:
            continue
        name = base if len(order) == 1 else f"{base}#{i + 1}"
        out.append((name, grid))
    return out


def _read_xlsx(path: Path) -> list[tuple[str, list[list]]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            fill: dict[tuple[int, int], object] = {}
            for rng in ws.merged_cells.ranges:
                v = ws.cell(rng.min_row, rng.min_col).value
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        fill[(r, c)] = v
            grid = [[fill.get((ri + 1, ci + 1), cell) for ci, cell in enumerate(row)]
                    for ri, row in enumerate(grid)]
        except Exception:
            pass
        out.append((ws.title, _clip(grid)))
    return out


def _read_xls(path: Path) -> list[tuple[str, list[list]]]:
    import xlrd

    book = xlrd.open_workbook(str(path))
    out = []
    for sh in book.sheets():
        grid = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
        out.append((sh.name, _clip(grid)))
    return out


def _clip(grid: list[list]) -> list[list]:
    grid = [r[:MAX_COLS] for r in grid[:MAX_ROWS]]
    while grid and not any(str(c).strip() for c in grid[-1]):
        grid.pop()
    return grid
