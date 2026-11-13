"""文本定位工具：按关键词在 Markdown 里切出相关片段（省 token 的核心）。"""
from __future__ import annotations

import re

PAGE_ANCHOR = re.compile(r"<!--\s*page:(\d+)\s*-->")


def locate(md: str, aliases: list[str], window: int = 1200) -> str:
    """按别名定位片段，合并重叠区间，避免重复喂料。"""
    hits: list[tuple[int, int]] = []
    for a in aliases:
        for m in re.finditer(re.escape(a), md):
            hits.append((max(0, m.start() - window), min(len(md), m.end() + window * 2)))
    if not hits:
        return ""
    hits.sort()
    merged = [list(hits[0])]
    for s, e in hits[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return "\n\n…（略）…\n\n".join(md[s:e] for s, e in merged)


def locate_balanced(md: str, groups: list[list[str]], window: int = 1200,
                    max_chars: int | None = None) -> str:
    """按「字段组」均衡采样：保证每个字段都有一段原文证据。

    `locate()` 会把所有别名的命中窗口合并，长招标文件（100+ 页）动辄拼出 8 万字；
    `build_context` 再按字符数从头截断时，靠后章节的稀有字段（如「评标办法：综合评估法」
    在第 11 页、「价格评分标准」在第 69 页）就被整段丢掉了。

    这里改为每个字段组单独取前 2 个命中点的小窗口，再按组拼接：
      · 每个字段至少有一段证据，不会因位置靠后而消失；
      · 超预算时压缩当前组片段，而不是砍掉后面的字段。
    """
    blocks: list[str] = []
    for aliases in groups:
        spans: list[tuple[int, int]] = []
        n = 0
        for a in aliases:
            for m in re.finditer(re.escape(a), md):
                spans.append((max(0, m.start() - window // 4),
                              min(len(md), m.end() + window // 2)))
                n += 1
                if n >= 2:
                    break
            if n >= 2:
                break
        if not spans:
            continue
        spans.sort()
        merged = [list(spans[0])]
        for s, e in spans[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        blocks.append("\n…\n".join(md[s:e] for s, e in merged))
    if not blocks:
        return ""

    if not max_chars:
        return "\n\n…（略）…\n\n".join(blocks)
    # 每组预留等额配额，而不是从头累加到耗尽——否则排在最后的字段
    # （如 eval_method「评标办法」）会被整组丢掉。
    each = max(300, max_chars // max(1, len(blocks)))
    return "\n\n…（略）…\n\n".join(b[:each] for b in blocks)


def page_of(md: str, pos: int) -> str:
    last = ""
    for m in PAGE_ANCHOR.finditer(md, 0, pos):
        last = m.group(1)
    return last


def page_at(md: str, keyword: str) -> str:
    """关键词所在页码。"""
    i = md.find(keyword)
    return page_of(md, i) if i >= 0 else ""


def _squeeze(doc: str) -> tuple[str, list[int]]:
    """去掉所有空白并保留每个字符在原文中的下标，用于跨换行定位。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(doc):
        if ch in " \t\r\n\u3000":
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def split_cell(text: str) -> str:
    """取一段文字里最长的不含空白片段（用于在原文中做鲁棒定位）。"""
    parts = re.split(r"[\s\u3000]+", text or "")
    return max(parts, key=len) if parts else ""


def page_for(doc: str, needle: str) -> str:
    """一段引用文字在原文中的页码，容忍换行/空白差异。

    招标文件表格里的长句常被换行切断，直接 str.find 会失败；
    这里先去掉空白再找，再映射回原下标取页码。
    """
    needle = (needle or "").strip()
    if not needle:
        return ""
    flat, idx = _squeeze(doc)
    key = re.sub(r"[\s\u3000]+", "", needle)
    pos = flat.find(key)
    if pos < 0:
        # 逐段退化：取够长的前缀片段，避免因个别字差异全盘失败
        for n in (len(key), 24, 16, 10, 8):
            if n > len(key):
                continue
            pos = flat.find(key[:n])
            if pos >= 0:
                break
    if pos < 0:
        longest = split_cell(needle)
        pos = doc.find(longest) if longest else -1
        return page_of(doc, pos) if pos >= 0 else ""
    return page_of(doc, idx[pos])


def project_similarity(a: str, b: str) -> float:
    """两个项目名称的相似度，用于识别混进来的其他项目文件。"""
    import difflib

    def norm(s: str) -> str:
        s = re.sub(r"[\s（）()【】\[\]“”\"'·.,，。、:：;；\-—_]", "", s or "")
        for w in ("项目", "采购", "招标", "有限公司", "公司"):
            s = s.replace(w, "")
        return s

    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
