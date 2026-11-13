"""阶段 2：内容归一化 —— 把 PDF / Word / Excel / 图片统一转成带页码锚点的 Markdown。

这是整套方案的地基：
  * 大模型对 Markdown 表格的理解远好于裸文本；
  * `<!-- page:3 -->` 锚点让「来源可追溯」成为可能；
  * 人类可读，出问题能直接打开排查。
"""
from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bidsummary import llm
from bidsummary.config import CONFIG, Config
from bidsummary.models import FileItem


class ParseAborted(Exception):
    """解析被主动中止：用户取消任务，或单文件解析超时。

    单独一个异常类型，是为了让上层能区分「这个文件没解析完是因为主动中止」
    与「解析真的失败」。中止的文件标记 SKIPPED，不污染 FAILED 统计。
    """

try:                                   # PyMuPDF >= 1.24 推荐用 pymupdf
    import pymupdf as fitz
except ImportError:                    # pragma: no cover
    import fitz                        # type: ignore

MAX_ROWS = 300
MAX_COLS = 40


# ------------------------------------------------------------------ 表格
def table_to_md(table: list[list]) -> str:
    rows = []
    for row in table or []:
        rows.append(["" if c is None else str(c).replace("\n", " ").replace("|", "/").strip()
                     for c in row])
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def grid_to_md(grid: list[list]) -> str:
    return table_to_md(grid)


def _trim_grid(grid: list[list]) -> list[list]:
    grid = [r[:MAX_COLS] for r in grid[:MAX_ROWS]]
    while grid and not any(c not in (None, "") for c in grid[-1]):
        grid.pop()
    return grid


# ------------------------------------------------------------------ PDF
# 视觉识别页预算：按文件类型给不同上限。
# 真实事故：一份 57 页「中标公告」扫描 PDF（无文字层）逐页串行调视觉模型，
# 上限 40 页 × 每页 5~9 秒 ≈ 5 分钟，全程进度条不动、取消按钮无效，
# 看起来就是「卡死在解析文件」。公示类文件真正有用的只有前几页的候选人名单，
# 给它 40 页预算纯属烧钱又堵路。
_SCAN_BUDGET_BY_KIND = {
    'PUBLICITY': ('AI_SCAN_PAGES_PUBLICITY', 6),
    'QUALIFICATION': ('AI_SCAN_PAGES_QUALIFICATION', 4),
    'PRICE_SHEET': ('AI_SCAN_PAGES_PRICE_SHEET', 8),
    'BID_FILE': ('AI_SCAN_PAGES_BID_FILE', 12),
}


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, '')).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def scan_budget(cfg: Config, kind: str = '') -> int:
    """单份 PDF 允许走视觉模型的页数。"""
    cap = _env_int('AI_SCAN_MAX_PAGES', cfg.max_pages_scan)
    hit = _SCAN_BUDGET_BY_KIND.get((kind or '').upper())
    if not hit:
        return cap
    return max(1, min(cap, _env_int(hit[0], hit[1])))


def parse_pdf(path: Path, cfg: Config, log=print, progress=None,
              should_stop=None, kind: str = '') -> tuple[str, int, bool]:
    """PDF -> Markdown。

    三个约束（都是真金白银换来的）：
      · 文字层优先，只有「取不到文字也没有表格」的页才走视觉模型；
      · 视觉页预算按 kind 分档，公示/资质类不需要整篇 OCR；
      · 视觉调用按页并发（scan_concurrency），否则几十页扫描件会把任务拖成假死。
    progress(done, total, note) —— 每完成一页回调一次，让前端进度条真的会动。
    should_stop() -> bool     —— 返回 True 时尽快中止（用户取消 / 单文件超时）。
    """
    import pdfplumber

    doc = fitz.open(path)
    n = len(doc)
    chunks = [''] * n            # 按页占位，并发完成后仍按原页序拼接
    scan_jobs: list[tuple[int, bytes]] = []
    over_budget = 0
    budget = scan_budget(cfg, kind)

    try:
        with pdfplumber.open(path) as pdf:
            for i in range(n):
                page_no = i + 1
                try:
                    text = doc[i].get_text("text") or ""
                except Exception:
                    text = ""
                tables: list[str] = []
                try:
                    for t in (pdf.pages[i].extract_tables() or []):
                        md = table_to_md(t)
                        if md:
                            tables.append(md)
                except Exception:
                    pass

                if len(text.strip()) < 20 and not tables:
                    # 判定为扫描页 -> 视觉模型（渲染放在主线程：PyMuPDF 的
                    # Document 不是线程安全的，只有 HTTP 请求放进线程池）
                    if len(scan_jobs) >= budget:
                        over_budget += 1
                        chunks[i] = (f"<!-- page:{page_no} -->\n"
                                     f"[扫描页，超出本类文件的视觉识别预算 {budget} 页，跳过]")
                        continue
                    try:
                        pix = doc[i].get_pixmap(dpi=cfg.scan_page_dpi)
                        scan_jobs.append((i, pix.tobytes("png")))
                    except Exception as e:
                        chunks[i] = f"<!-- page:{page_no} -->\n[扫描页渲染失败：{e}]"
                    continue

                body = text
                if tables:
                    body += "\n\n" + "\n\n".join(tables)
                chunks[i] = f"<!-- page:{page_no} -->\n{body}"
    finally:
        doc.close()

    total = len(scan_jobs)
    if total:
        if progress:
            progress(0, total, f"待识别 {total} 页扫描件")
        done = 0
        pool = ThreadPoolExecutor(max_workers=max(1, _env_int(
            'AI_SCAN_CONCURRENCY', getattr(cfg, 'scan_concurrency', 4))))
        try:
            futs = {pool.submit(llm.vision_markdown, png): idx for idx, png in scan_jobs}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    md = fut.result()
                    chunks[i] = f"<!-- page:{i + 1} -->\n[扫描页·视觉识别]\n{md}"
                except ParseAborted:
                    raise
                except Exception as e:
                    chunks[i] = f"<!-- page:{i + 1} -->\n[扫描页识别失败：{e}]"
                done += 1
                if progress:
                    progress(done, total, "")
                if should_stop and should_stop():
                    for f in futs:
                        f.cancel()
                    raise ParseAborted(f"扫描页识别到第 {done}/{total} 页时被中止")
        finally:
            # 不等待剩余在途请求，取消来不及开始的页，避免取消后还要干等
            pool.shutdown(wait=False, cancel_futures=True)

    if over_budget:
        log(f"  [提示] {Path(path).name}：{over_budget} 页扫描件超出「{kind or '默认'}」"
            f"类目预算 {budget} 页未识别（可用 AI_SCAN_PAGES_* 调整）")

    return "\n\n".join(c for c in chunks if c), n, total > 0


# ------------------------------------------------------------------ Word
def parse_docx(path: Path) -> tuple[str, int]:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(str(path))
    parts: list[str] = []
    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            t = Paragraph(child, d).text.strip()
            if t:
                parts.append(t)
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, d)
            grid = [[c.text.strip() for c in row.cells] for row in tbl.rows]
            md = grid_to_md(_trim_grid(grid))
            if md:
                parts.append(md)
    txt = "\n\n".join(parts)
    return txt, max(1, len(txt) // 1800)


def convert_doc_to_docx(path: Path) -> Path | None:
    """.doc 旧格式 -> .docx（需要本机装 LibreOffice）。"""
    out_dir = CONFIG.temp_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    for cmd in ("soffice", "libreoffice"):
        try:
            subprocess.run([cmd, "--headless", "--convert-to", "docx",
                            "--outdir", str(out_dir), str(path)],
                           timeout=120, capture_output=True, check=False)
            target = out_dir / (path.stem + ".docx")
            if target.exists():
                return target
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ------------------------------------------------------------------ Excel
def parse_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    sheets: list[str] = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        # 合并单元格填充：值只在左上角，需要向下/向右补齐
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
        grid = _trim_grid(grid)
        if not grid:
            continue
        md = grid_to_md(grid)
        sheets.append(f"<!-- sheet:{ws.title} -->\n{md}")
    return "\n\n".join(sheets)


def parse_xls(path: Path) -> str:
    import xlrd

    book = xlrd.open_workbook(str(path))
    sheets = []
    for sh in book.sheets():
        grid = [[sh.cell_value(r, c) for c in range(min(sh.ncols, MAX_COLS))]
                for r in range(min(sh.nrows, MAX_ROWS))]
        md = grid_to_md(_trim_grid(grid))
        if md:
            sheets.append(f"<!-- sheet:{sh.name} -->\n{md}")
    return "\n\n".join(sheets)


# ------------------------------------------------------------------ 纯文本 / CSV
def parse_text(path: Path) -> str:
    for enc in ("utf-8", "gbk", "utf-16", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
    return ""


def parse_csv(path: Path) -> str:
    import csv

    rows: list[list[str]] = []
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with path.open("r", encoding=enc, errors="ignore", newline="") as f:
                rows = [r for r in csv.reader(f)]
            break
        except Exception:
            continue
    return grid_to_md(_trim_grid(rows))


def parse_image(path: Path) -> str:
    return llm.vision_markdown(path.read_bytes())


# ------------------------------------------------------------------ 去噪
NOISE_LINE = re.compile(r"(第\s*\d+\s*页\s*(/|共|，)?\s*(共\s*)?\d*\s*页?)|(^\s*\d+\s*$)")


def denoise(md: str) -> str:
    """去掉页眉页脚、空行压实。"""
    out = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) < 30 and NOISE_LINE.search(s):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


# ------------------------------------------------------------------ 统一入口
def parse_file(item: FileItem, cfg: Config | None = None, log=print,
               progress=None, should_stop=None) -> FileItem:
    cfg = cfg or CONFIG
    ext = item.ext.lower()
    try:
        if ext == ".doc":
            converted = convert_doc_to_docx(item.path)
            if converted is None:
                raise RuntimeError("旧版 .doc 需要本机安装 LibreOffice 才能解析，请转为 .docx 后重试")
            item.markdown, item.page_count = parse_docx(converted)
        elif ext == ".docx":
            item.markdown, item.page_count = parse_docx(item.path)
        elif ext == ".pdf":
            item.markdown, item.page_count, item.is_scanned = parse_pdf(
                item.path, cfg, log, progress=progress, should_stop=should_stop,
                kind=item.kind)
        elif ext in (".xlsx", ".xlsm"):
            item.markdown = parse_xlsx(item.path)
            item.page_count = 1
        elif ext == ".xls":
            item.markdown = parse_xls(item.path)
        elif ext == ".csv":
            item.markdown = parse_csv(item.path)
        elif ext == ".txt":
            item.markdown = parse_text(item.path)
        elif ext in (".jpg", ".jpeg", ".png"):
            item.markdown = parse_image(item.path)
            item.is_scanned = True
        else:
            raise RuntimeError(f"暂不支持的格式：{ext}")

        item.markdown = denoise(item.markdown or "")
        if not item.markdown.strip():
            raise RuntimeError("未能提取到任何文本内容（可能是空白页或解析失败）")
        item.status = "PARSED"
    except ParseAborted as e:
        # 主动中止（取消 / 单文件超时）：不记为失败，避免污染重试与失败统计
        item.status = "SKIPPED"
        item.error = str(e) or "解析被中止"
        log(f"  [中止] {item.name}：{item.error}")
    except Exception as e:
        item.status = "FAILED"
        item.error = str(e)
        log(f"  [失败] {item.name}：{e}")
    return item
