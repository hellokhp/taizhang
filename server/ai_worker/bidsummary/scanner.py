"""阶段 1+3：文件采集 + 投标人归属。

职责：把一个文件夹变成一份干净、已分类、已归属的文件清单。
不做任何解析工作，保证重试成本低。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from pathlib import Path

from bidsummary.config import CONFIG, Config
from bidsummary.models import FileItem

# ------------------------------------------------------------------ 常量
SKIP_NAMES = {"Thumbs.db", ".DS_Store", "desktop.ini"}
SKIP_PREFIX = ("~$", ".")                      # Office 临时文件、隐藏文件
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".jpg", ".jpeg", ".png"}
ARCHIVE_EXT = {".zip", ".7z", ".rar"}

# 分类关键词（顺序即优先级：公示类最优先，避免被"开标/招标"误吞）
# 「中标公告」必须显式列出：从招标网站整页下载的公示 PDF，文件名常是
# 「××中标公告-全国招标公告公示搜索引擎-中国招标投标公共服务平台.pdf」，
# 里面含「招标公告」四个字，会被 TENDER_DOC 先命中，进而按招标文件
# 拿走 40 页视觉识别预算 —— 一份公示白烧 40 次 OCR，还拖死整个任务。
KIND_RULES: list[tuple[str, list[str]]] = [
    ("PUBLICITY",       ["中标人公示", "中标候选人", "中标公告", "中标公示", "中标结果",
                         "中标单位", "成交公告", "成交公示", "结果公示", "结果公告",
                         "入围公示", "定标公示", "候选人公示", "中标通知书"]),
    ("OPENING_RECORD",  ["开标记录", "开标一览", "开标情况", "唱标记录", "开标汇总"]),
    ("TENDER_DOC",      ["招标文件", "招标公告", "采购文件", "竞争性磋商", "询价文件", "投标邀请"]),
    ("PRICE_SHEET",     ["报价", "开标一览", "投标函", "投标总价", "分项报价", "工程量清单"]),
    ("QUALIFICATION",   ["营业执照", "资质", "资格", "许可证", "认证", "业绩", "授权委托", "法人"]),
]

# 目录名黑名单词：这些目录不是投标人
DIR_BLACKLIST = (
    "招标", "开标", "投标", "评标", "资料", "附件", "扫描", "电子", "商务标", "技术标",
    "资格审查", "报价", "文件", "定标", "补遗", "澄清", "图纸", "清单", "合同", "其他",
    "result", "output", "temp", "备份",
)

# 公司名特征词
COMPANY_HINT = ("公司", "集团", "中心", "研究院", "设计院", "医院", "大学", "学院",
                "厂", "合作社", "事务所", "联合体", "有限", "股份", "工程局", "bank")

# 序号前缀，如 "01_"、"1、"、"（一）"、"A."
NOISE_PREFIX = re.compile(r"^\s*[\(（【\[]?\s*[0-9一二三四五六七八九十]{1,3}\s*[\)）】\]\.、_\-]\s*")
# 投标人编号前缀，如 "甲公司_星辰科技有限公司"、"投标人1-某某公司"
LABEL_PREFIX = re.compile(
    r"^\s*(?:[甲乙丙丁戊己庚辛壬癸]|投标人\s*\d*|投标单位\s*\d*|供应商\s*\d*)公司\s*[_\-\s]+",
    re.I,
)


# ------------------------------------------------------------------ 工具
def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def classify(name: str, rel: str) -> str:
    text = f"{rel} {name}"
    for kind, keys in KIND_RULES:
        if any(k in text for k in keys):
            return kind
    return "BID_FILE" if ("投标" in text or "响应文件" in text) else "OTHER"


def clean_dir_name(s: str) -> str:
    s = NOISE_PREFIX.sub("", s).strip()
    s = LABEL_PREFIX.sub("", s).strip()
    s = re.sub(r"[（(]?(正本|副本|盖章|最终|正式|已签章)[）)]?", "", s).strip(" _-")
    return s


def looks_like_company(s: str) -> bool:
    if not s or len(s) > 40 or len(s) < 3:
        return False
    if any(b in s for b in DIR_BLACKLIST):
        return False
    return any(h in s for h in COMPANY_HINT)


def guess_bidder(rel: str, name: str) -> str:
    """按 目录分级 > 文件名前缀 判断投标人。"""
    parts = [p for p in Path(rel).parts[:-1]]          # 去掉文件名
    for p in reversed(parts):                          # 从最内层往外找
        c = clean_dir_name(p)
        if looks_like_company(c):
            return c
    # 文件名兜底：取第一个分隔符前的片段
    stem = Path(name).stem
    for sep in ("_", "-", "－", " "):
        if sep in stem:
            head = clean_dir_name(stem.split(sep)[0])
            if looks_like_company(head):
                return head
    return ""


def normalize_bidder(name: str) -> str:
    """投标人名称归一化，用于合并同一家的多种写法。"""
    s = re.sub(r"[（(].*?[)）]", "", name)
    s = s.replace(" ", "").replace("　", "")
    for w in ("有限责任公司", "有限公司", "股份有限公司", "有限", "公司"):
        s = s.replace(w, "")
    return s.strip()


# ------------------------------------------------------------------ 解压
def _safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as z:
            total = sum(i.file_size for i in z.infolist())
            if total > 2 * 1024 ** 3:
                raise ValueError("压缩包解压后体积超过 2GB，已跳过")
            for info in z.infolist():
                target = dest / info.filename
                if not str(target.resolve()).startswith(str(dest.resolve())):
                    continue                          # 防目录穿越
                z.extract(info, dest)
    else:
        try:
            import py7zr
            with py7zr.SevenZipFile(archive) as z:
                z.extractall(dest)
        except Exception:
            import rarfile
            with rarfile.RarFile(archive) as z:
                z.extractall(dest)


# ------------------------------------------------------------------ 主流程
def scan_folder(root: Path, cfg: Config | None = None, log=print) -> list[FileItem]:
    cfg = cfg or CONFIG
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"文件夹不存在：{root}")

    items: list[FileItem] = []
    seen_md5: dict[str, str] = {}
    archive_extracted = 0

    def walk(dirpath: Path, depth: int) -> None:
        nonlocal archive_extracted
        if depth > cfg.max_depth or len(items) >= cfg.max_files:
            return
        try:
            entries = sorted(dirpath.iterdir(), key=lambda p: p.name)
        except PermissionError:
            return
        for p in entries:
            if len(items) >= cfg.max_files:
                log(f"[提示] 文件数已达上限 {cfg.max_files}，停止扫描")
                return
            if p.is_dir():
                walk(p, depth + 1)
                continue
            name = p.name
            if name in SKIP_NAMES or name.startswith(SKIP_PREFIX):
                continue
            ext = p.suffix.lower()
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            if ext in ARCHIVE_EXT and depth <= cfg.archive_depth:
                dest = cfg.temp_dir / f"{p.stem}_{hashlib.md5(str(p).encode()).hexdigest()[:8]}"
                try:
                    _safe_extract(p, dest)
                    archive_extracted += 1
                    log(f"[解压] {name} -> {dest.name}")
                    walk(dest, depth + 1)
                except Exception as e:
                    log(f"[跳过] 压缩包 {name} 无法解压：{e}")
                continue
            if ext not in DOC_EXT:
                continue
            if size > cfg.max_file_mb * 1024 * 1024:
                log(f"[跳过] {name} 超过 {cfg.max_file_mb}MB")
                continue

            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = name
            digest = md5_of(p)
            if digest in seen_md5:
                log(f"[去重] {name} 与 {seen_md5[digest]} 内容相同，跳过")
                continue
            seen_md5[digest] = name

            items.append(
                FileItem(
                    path=p, rel=rel, name=name, ext=ext, size=size, md5=digest,
                    kind=classify(name, rel), bidder=guess_bidder(rel, name),
                )
            )

    walk(root, 0)

    # 归属校正：同一投标人多种写法 → 合并到出现次数最多的写法
    canonical: dict[str, dict[str, int]] = {}
    for it in items:
        if not it.bidder:
            continue
        k = normalize_bidder(it.bidder)
        canonical.setdefault(k, {}).setdefault(it.bidder, 0)
        canonical[k][it.bidder] += 1
    real_name = {k: max(v.items(), key=lambda x: x[1])[0] for k, v in canonical.items()}
    for it in items:
        if it.bidder:
            it.bidder = real_name.get(normalize_bidder(it.bidder), it.bidder)

    log(f"[采集] 共 {len(items)} 个文件，识别到 {len(real_name)} 家投标人"
        + (f"，解压 {archive_extracted} 个压缩包" if archive_extracted else ""))
    return items


def group_by_bidder(items: list[FileItem]) -> dict[str, list[FileItem]]:
    groups: dict[str, list[FileItem]] = {}
    for it in items:
        key = it.bidder or "（未识别投标人）"
        groups.setdefault(key, []).append(it)
    for v in groups.values():
        v.sort(key=lambda f: (f.priority, -len(f.markdown)))
    return groups
