"""价格评分条款的宽口径定位器（反"适配范围太狭隘"）。

真实项目里「价格评分标准」的写法极不统一，实测出现过：

  * 标题型：「投标报价评分标准」「价格评审」「评标办法前附表」「评分标准附件」
  * 表格单元格型：「价格部分（50分）| 价格 | 最低价法 计算方式：1.价格总分50分…」
  * 正文型：「评标基准价为…」「报价得分＝…」「每高于基准价1%扣0.6分」
  * 折扣型：「下浮率」「优惠率」「折扣率」「结算金额＝市场价格×入围折扣率×数量」

早期实现按固定几个词去切片（如 `投标报价评分标准|价格分（N|评标基准价`），
只要招标文件把标题写成「价格部分（50分）」就整段返回空串 —— 明明原文里
有完整的价格评分标准，模型却根本看不到。这里改成：

  1. 用一组覆盖各种写法的锚点词找出所有候选位置；
  2. 对每个候选窗口按「价格词 + 分值词 + 公式词」加权打分；
  3. 取密度最高的窗口，并允许跨文件挑选（招标文件 / 开标文件 / 补遗都可能写规则）；
  4. 顺带用正则从原文直接抠分值、阈值、税率口径等确定性参数。

定位只负责"把原文送到模型面前"；数字与公式仍由代码计算，模型不得改写。
"""
from __future__ import annotations

import re
from typing import Iterable

from bidsummary.models import FileItem
from bidsummary.textutil import page_of

__all__ = ["locate_price_rule", "locate_in_files", "regex_hints", "grounded"]


# ------------------------------------------------------------------ 词表
# 锚点：命中即产生一个候选窗口。刻意放宽到「章节名 / 表格行名 / 公式片段」。
ANCHORS: tuple[str, ...] = (
    "投标报价评分标准", "报价评分标准", "价格评分标准", "价格评审标准",
    "价格评审", "价格部分", "价格分", "价格总分", "报价得分", "报价评分",
    "评标基准价", "评标办法", "评标方法", "评审办法", "评分办法",
    "评分标准", "评审标准", "评分因素", "评分细则", "分值构成",
    "最低价法", "最低评标价法", "合理低价", "综合评估法", "综合评分法",
    "分项价格分", "每项标的得分", "分项权重", "偏离惩罚", "扣分",
    "评标价", "基准价", "得分=", "得分＝", "下浮率", "优惠率", "折扣率",
    "评审价", "满分", "扣完为止", "去高去低", "算术平均",
)

# 打分词：窗口里出现得越多、越像「价格评分条款」。
PRICE_WORDS = ("价格", "报价", "评标价", "评审价", "基准价", "最低价", "平均价",
               "单价", "含税", "税率", "权重", "分项", "标的", "折扣", "下浮")
SCORE_WORDS = ("分", "分值", "满分", "总分", "得分", "权重", "扣", "加", "封顶")
FORMULA_WORDS = ("最低价法", "每项标的得分", "分项价格分", "得分=", "得分＝",
                 "评标基准价", "基准价", "×分项", "×分值", "每高于", "每低于",
                 "每高", "每低", "扣完为止", "算术平均", "去掉一个", "去极值",
                 "X/Y", "偏离值", "平均价", "÷", "／", "比值", "结算金额")

# 找「N分」这类分值证据
SCORE_PAT = re.compile(r"(?:\d+(?:\.\d+)?)\s*分|满分\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*分\)")


def _window_score(seg: str) -> int:
    s = 0
    for w in PRICE_WORDS:
        if w in seg:
            s += 1
    for w in SCORE_WORDS:
        if w in seg:
            s += 1
    for w in FORMULA_WORDS:
        if w in seg:
            s += 3
    if SCORE_PAT.search(seg):
        s += 4
    # 目录页 / 表格说明页（大量点线、极少"分"）降权
    if seg.count("…") > 20 or seg.count("....") > 5:
        s -= 6
    # 同一窗口里出现多个公式片段，说明命中了真正的规则段
    s += 2 * sum(1 for w in FORMULA_WORDS if seg.count(w) >= 2)
    return s


def locate_price_rule(md: str, *, window: int = 3000, before: int = 700) -> tuple[str, int]:
    """在一份文件的 Markdown 里找最像「价格评分标准」的窗口。

    返回 (窗口原文, 起始下标)；找不到返回 ("", -1)。
    """
    if not md:
        return "", -1
    positions: list[int] = []
    for kw in ANCHORS:
        positions.extend(m.start() for m in re.finditer(re.escape(kw), md))
    if not positions:
        # 兜底：连锚点都没有时，至少给「评分/评审」附近的内容
        positions = [m.start() for m in re.finditer(r"评分|评审|分值", md)][:40]
    if not positions:
        return "", -1
    # 相近位置去重，避免 30 个关键词在同一段反复开窗
    positions.sort()
    uniq: list[int] = []
    for p in positions:
        if not uniq or p - uniq[-1] > 400:
            uniq.append(p)

    best_seg, best_pos, best_score = "", -1, -10**9
    for p in uniq:
        s = max(0, p - before)
        e = min(len(md), p + window)
        seg = md[s:e]
        sc = _window_score(seg)
        # 同样分数时取靠前的（通常是评标办法章节的表格）
        if sc > best_score:
            best_seg, best_pos, best_score = seg, p, sc
    return best_seg, best_pos


def locate_in_files(files: Iterable[FileItem], *, window: int = 3000,
                    before: int = 700) -> tuple[str, str, str]:
    """跨全部已解析文件挑最佳价格条款窗口。

    返回 (窗口原文, 来源文件 rel, 页码)。招标文件/开标文件略加分，
    但不再假设"只有 TENDER_DOC 才写规则"——补遗、报价表说明页也常有。
    """
    best = ("", "", "", -10**9)
    for it in files:
        if not it.markdown:
            continue
        seg, pos = locate_price_rule(it.markdown, window=window, before=before)
        if not seg:
            continue
        sc = _window_score(seg)
        if it.kind in ("TENDER_DOC", "OPENING_RECORD") or "招标" in it.name:
            sc += 3
        if sc > best[3]:
            page = page_of(it.markdown, pos) if pos >= 0 else ""
            best = (seg, it.rel, page, sc)
    return best[0], best[1], best[2]


# ------------------------------------------------------------------ 确定性参数
def regex_hints(text: str) -> dict:
    """从价格条款原文直取参数（不经模型转写）。

    覆盖三种常见写法：整包分值、分项加权最低价法、基准价偏差扣分。
    """
    t = re.sub(r"[\s　]+", "", text or "")
    h: dict = {}

    # 满分：价格总分50分 / 价格部分（50分）/ 价格分50分 / 得满分53分
    m = (re.search(r"价格总分(\d+(?:\.\d+)?)分", t)
         or re.search(r"价格部分[（(](\d+(?:\.\d+)?)分[)）]", t)
         or re.search(r"价格分(?:为|满分)?(\d+(?:\.\d+)?)分", t)
         or re.search(r"得满分(\d+(?:\.\d+)?)分", t)
         or re.search(r"报价部分[（(]?(\d+(?:\.\d+)?)分", t))
    if m:
        h["full_score"] = float(m.group(1))

    # 分项价格分 = 50 * 分项权重
    m = re.search(r"分项价格分[=＝](\d+(?:\.\d+)?)\s*[*×]\s*分项权重", t)
    if m:
        h["weight_base"] = float(m.group(1))

    # 每项标的得分 =（所有投标人中最低价格/投标人投标价格）×分项价格分
    m = re.search(r"每项标的得分[=＝][（(]([^）)]{2,40})[)）]\s*[*×]\s*分项价格分", t)
    if m:
        h["item_formula"] = m.group(0)
        h["min_over_price"] = m.group(1)

    # 偏离惩罚：X/Y＜0.5、X/Y≥1.5（含各种全半角符号）
    m = re.search(r"X/Y\s*[＜<]\s*(\d+(?:\.\d+)?)", t)
    if m:
        h["deviation_low"] = float(m.group(1))
    m = re.search(r"X/Y\s*[≥>＝=]\s*(\d+(?:\.\d+)?)", t)
    if m:
        h["deviation_high"] = float(m.group(1))
    if "偏离惩罚" in t:
        h["deviation_rule"] = True

    # 税率口径
    m = re.search(r"税率相同时[^。；]{0,40}含税价[^。；]{0,20}否则[^。；]{0,20}不含税", t)
    if m:
        h["tax_rule"] = m.group(0)

    # 基准价 / 去极值 / 步长（供其他口径复用）
    m = re.search(r"评标基准价为([^。；]{4,60})", t)
    if m:
        h["benchmark_rule"] = m.group(1)
    m = re.search(r"每高于[^，。；]{0,20}1%扣(\d+(?:\.\d+)?)分", t)
    if m:
        h["above_deduct"] = float(m.group(1))
    m = re.search(r"每低于[^，。；]{0,20}1%扣(\d+(?:\.\d+)?)分", t)
    if m:
        h["below_deduct"] = float(m.group(1))
    return h


def grounded(quote: str, corpus: str) -> bool:
    """证据必须能在原文里逐字找到（去空白后子串匹配）。"""
    q = re.sub(r"[\s　]+", "", quote or "")
    if len(q) < 8:
        return True
    return q in re.sub(r"[\s　]+", "", corpus or "")
