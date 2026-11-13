"""从招标文件里抽取「价格评分规则」，交给 scoring.py 用代码算分。

原则：**规则由模型读、分数由代码算**。
模型只负责读懂规则里的参数（总分、权重、基准价算法、系数），不负责做算术，
这样既准确又可复核——表里每一列都能手动验算。
"""
from __future__ import annotations

import json
import re

from bidsummary import llm
from bidsummary.config import CONFIG
from bidsummary.models import PriceRule
from bidsummary.textutil import locate, page_at, page_for

def _norm(s: str) -> str:
    """归一化：去空白、统一全角符号，便于做原文核对。"""
    s = re.sub(r"[\s　]+", "", s or "")
    for a, b in (("／", "/"), ("（", "("), ("）", ")"), ("，", ","), ("：", ":"),
                 ("；", ";"), ("－", "-"), ("—", "-"), ("～", "~"), ("“", '"'),
                 ("”", '"'), ("【", "["), ("】", "]")):
        s = s.replace(a, b)
    return s


def _grounded(text: str, doc: str, chunk: int = 8, ratio: float = 0.7) -> bool:
    """校验引用句是否真的出现在原文里：按 8 字滑窗计算命中率，>=70% 视为可信。

    宁可误判为「未确认」让用户核一遍，也不能让模型编的公式冒充原文。
    """
    t, d = _norm(text), _norm(doc)
    if not t or not d:
        return False
    if t in d:
        return True
    pieces = [t[i:i + chunk] for i in range(0, len(t), chunk)]
    pieces = [p for p in pieces if len(p) >= 6]
    if not pieces:
        return False
    return sum(1 for p in pieces if p in d) / len(pieces) >= ratio


def _score_corroborated(ev_formula: str, total: float, doc: str, window: int = 700) -> bool:
    """证据旁证：分值数字必须出现在「公式原句」附近。

    模型的 evidence_score 常会写成「价格:价格得分计算方法：...标准分值 50.0」这种带省略号的
    转述，天然过不了逐字核对。但真正要防的是「凭空编公式」，公式既然已核对通过，
    分值只要能在同一段落里找到数字，就足以互证。这里用代码定位，不信任模型的转述。
    """
    if not ev_formula or not total:
        return False
    t, d = _norm(ev_formula), _norm(doc)
    pos = d.find(t)
    if pos < 0:                     # 原文有换行差异时，用分片定位
        for i in range(0, max(1, len(t) - 8), 4):
            pos = d.find(t[i:i + 8])
            if pos >= 0:
                break
    if pos < 0:
        return False
    seg = d[max(0, pos - window): pos + len(t) + window]
    num = f"{total:g}"
    return num in seg


def _regex_hints(text: str) -> dict:
    """用确定性规则从原文里抠出几个关键参数，作为模型结果的兜底/纠偏。"""
    t = re.sub(r"[\s　]+", "", text or "")
    h: dict = {}
    # 评分方式：出现「(B/A)×分值」即比值法（不高于基准价得满分）
    if re.search(r"[（(]?B\s*/\s*A[）)]?\s*[×x*]", t) or "B/A" in t:
        h["behavior"] = "ratio"
    # 基本分 + 偏差率增减分（等于基准价得基本分 X 分，每低/高 1% 加/扣 Y 分）
    m = re.search(r"基本分[为是]?(\d{1,3}(?:\.\d+)?)分", t)
    if m:
        h["base_score"] = float(m.group(1))
        h.setdefault("behavior", "deviation")   # 原文已写出 (B/A) 公式时以公式为准
    m = re.search(r"每[高低](\d{1,2}(?:\.\d+)?)%[^。；]{0,20}?([加扣增减])(\d{1,3}(?:\.\d+)?)分", t)
    if m:
        h.setdefault("step_percent", float(m.group(3)))
        h.setdefault("behavior", "deviation")
    m = re.search(r"低于[^。；]{0,15}?基准价(\d{1,2}(?:\.\d+)?)%以上", t)
    if m:
        h["low_limit"] = float(m.group(1))
    # 系数：算术平均值的 95% / 平均价×0.9
    m = re.search(r"平均值的?(\d{2}(?:\.\d+)?)%", t)
    if m:
        h["factor"] = float(m.group(1)) / 100
    else:
        m = re.search(r"平均值[×*]?[【\[]?(0\.\d+)[】\]]?", t)
        if m:
            h["factor"] = float(m.group(1))
        elif re.search(r"算术平均值(?:作为|即为|为)评标基准价", t):
            h["factor"] = 1.0                 # 原文没乘系数，别让默认 0.9 混进来
    # 剔除家数：去掉一个最高价 / 最低价
    if "去掉一个最高价" in t or "剔除一个最高价" in t:
        h["trim_high"] = 1
    if "去掉一个最低价" in t or "剔除一个最低价" in t:
        h["trim_low"] = 1
    # 门槛家数：优先匹配带 ≥/>/「以上」的写法，避免误取文中先出现的「3家」「5家」
    m = (re.search(r"[≥>]=?\s*(\d{1,3})\s*家", t)
         or re.search(r"(\d{1,3})\s*家(?:以上|及以上|或以上)", t))
    if m and (h.get("trim_high") or h.get("trim_low")):
        n = int(m.group(1))
        if 2 <= n <= 200:                     # 排除「1家」「0家」这类噪声
            h["trim_guard"] = n
    return h


def _parse_full_deviation(text: str) -> dict:
    """确定性识别「满分基础扣分 + 按家数分档去极值」口径，并从原文抠出全部参数。

    典型原文（山东烟草）：
      评标参考值 N：≤5 家取全部算术平均；6~9 家去掉一个最高、一个最低；
      >9 家去掉一个最高、一个次高、一个最低后取算术平均。
      报价 P 与 N 相等得满分 53 分；每高于 N 值 1% 减 0.6 分；每低于 N 值 1% 减 0.3 分，
      不足 1% 按插入法，扣完为止。

    与 deviation（基本分加/减分）不同：这里**等于参考值即满分，两侧都只扣分**，
    且高、低两侧步长不同、去极值家数按分档变化。参数一律由正则从原文取，不信任模型转述。
    """
    t = re.sub(r"[\s　]+", "", text or "")
    m_full = re.search(r"得满分(\d{1,3}(?:\.\d+)?)分", t)
    m_hi = re.search(r"每高[于过][^。；]{0,30}?1%[^。；]{0,30}?(?:减|扣)(\d{1,3}(?:\.\d+)?)分", t)
    m_lo = re.search(r"每低[于过][^。；]{0,30}?1%[^。；]{0,30}?(?:减|扣)(\d{1,3}(?:\.\d+)?)分", t)
    if not (m_full and m_hi and m_lo):
        return {}

    # 证据取同一页里的连续原句，避免跨页锚点导致逐字核对失败
    ev = ""
    m_ev = re.search(r"(投标报价得分[：:]投标人的?报价P值与N值相等的得满分.{0,200}?扣完为止)", t)
    if m_ev:
        ev = m_ev.group(1)
    else:
        s = t.find("投标报价得分")
        if s >= 0:
            seg = t[s:s + 220]
            k = seg.find("扣完为止")
            ev = seg[:k + 4] if k >= 0 else seg[:120]

    # 评标参考值 N 的分档去极值
    tiers: list[dict] = []
    m_seg = re.search(r"评标参考值的确定[：:](.+?)(?:3[.、]投标报价得分|扣完为止|$)", t)
    if m_seg:
        for clause in re.split(r"[；;]", m_seg.group(1)):
            if "平均" not in clause:
                continue
            hi = len(re.findall(r"最高值", clause)) + len(re.findall(r"次高值", clause))
            lo = len(re.findall(r"最低值", clause)) + len(re.findall(r"次低值", clause))
            m_le = re.search(r"[≤<]\s*(\d{1,3})\s*家", clause)
            m_gt = re.search(r"[＞>]\s*(\d{1,3})\s*家", clause)
            if m_le:
                max_n = int(m_le.group(1))
            elif m_gt:
                max_n = 10 ** 6
            else:
                continue
            if 2 <= max_n <= 10 ** 6:
                tiers.append({"max_n": max_n, "high": hi, "low": lo})
    tiers.sort(key=lambda x: x["max_n"])
    dedup: list[dict] = []
    for x in tiers:
        if not dedup or dedup[-1]["max_n"] != x["max_n"]:
            dedup.append(x)

    return {
        "behavior": "full_deviation",
        "total_score": float(m_full.group(1)),
        "step_high": float(m_hi.group(1)),
        "step_low": float(m_lo.group(1)),
        "trim_tiers": dedup,
        "evidence": ev,
    }


def _parse_itemized(text: str) -> dict:
    """确定性识别「分项独立基准价 + 基本分 Fn 双向偏差扣分」口径（安徽工行一类）。

    典型原文（招标文件 P103）：
      投标报价满分 F 为40分（其中 F1:门楣发光字类价格占10分，F2:灯箱类价格占10分；
      F3:标识标牌类价格占15分；F4:配件类价格占5分）……投标报价得分Dn＝Fn－偏差率*100*E1；
      投标报价得分Dn＝Fn＋偏差率*100*E2。E1=0.5；E2=0.3
      进入评标基准价计算的投标人投标报价的算术平均值作为评标基准价。

    与 full_deviation 不同：这里价格分按**多个报价分项**各自独立算基准价、各自按 Fn
    与偏差率计分后加总；E1/E2 是两侧系数（高于扣 E1、低于扣分 E2），Fn 是各分项分值。
    全部数值用正则从原文直取，不信任模型转述。
    """
    t = re.sub(r"[\s　]+", "", text or "")
    m_full = re.search(r"投标报价满分F(?:为|是|为是)?(\d{1,3}(?:\.\d+)?)分", t)
    m_e1 = re.search(r"E1[＝=](\d{1,3}(?:\.\d+)?)", t)
    m_e2 = re.search(r"E2[＝=](\d{1,3}(?:\.\d+)?)", t)
    # 分项分值：F1:门楣发光字类价格占10分 / F2：灯箱类价格占10分 …
    parts: list[dict] = []
    for m in re.finditer(r"F([1-9])[:：]([^，,；;F]{1,24}?)价格?占(\d{1,3}(?:\.\d+)?)分", t):
        label = re.sub(r"价格$", "", m.group(2)).strip()
        parts.append({"label": label, "score": float(m.group(3)), "_no": int(m.group(1))})
    parts.sort(key=lambda x: x["_no"])
    for p in parts:
        p.pop("_no", None)
    # 只有当「满分 + 两侧系数」齐全，或「满分 + ≥2 个分项分值」时才认定为该口径
    if not m_full:
        return {}
    has_coef = bool(m_e1 and m_e2)
    if not has_coef and len(parts) < 2:
        return {}
    # 证据：取连续原句（归一后仍须在原文滑窗核对通过）
    def ev_near(anchor: str, back: int, fwd: int) -> str:
        i = t.find(anchor)
        return t[max(0, i - back): i + fwd] if i >= 0 else ""

    ev_formula = ev_near("①如果投标人的投标报价＞评标基准价", 0, 105)
    if not ev_formula:
        ev_formula = ev_near("投标报价得分Dn", 0, 100)
    if not ev_formula:
        ev_formula = ev_near("E1=", 80, 40)
    ev_score = ev_near("投标报价满分F", 0, 95)
    # 基准价：算术平均值作为评标基准价（无系数、无去极值）
    benchmark = ""
    m_bench = re.search(r"进入评标基准价计算的?投标人?投标报价的?算术平均值作为评标基准价", t)
    if m_bench:
        benchmark = m_bench.group(0)
    note_bits = ["各分项独立按算术平均值计算评标基准价（无系数、无去极值）"]
    if "价格分以投标报价（不含税）" in t or "价格分以投标报价(不含税)" in t:
        note_bits.append("价格分以投标报价（不含税）计算")
    if "得分为负时，均按0分计算" in t or "得分为负时均按0分计算" in t:
        note_bits.append("每个分项扣完为止、最低 0 分（某项为负不冲抵其它分项）")
    return {
        "behavior": "itemized_deviation",
        "total_score": float(m_full.group(1)),
        "e1": float(m_e1.group(1)) if m_e1 else 0.0,
        "e2": float(m_e2.group(1)) if m_e2 else 0.0,
        "item_parts": parts,
        "benchmark": benchmark,
        "benchmark_factor": 1.0,
        "evidence_formula": ev_formula,
        "evidence_score": ev_score,
        "note": "；".join(note_bits),
        "benchmark_scope": "商务技术部分得分前60%入围者参与基准价计算" if "前60%" in t else "",
    }


# 规则核心锚点：这些词出现在价格评分条款里，用于把 P103 一类的关键段前置，
# 避免 locate() 拼接出的长片段在 [:30000] 截断时把整套公式切掉。
FOCUS_ANCHORS = [
    "投标报价满分", "投标报价得分Dn", "E1=", "E1＝", "偏差率*100*E1",
    "价格占10分", "价格占15分", "分值范围", "报价部分", "偏差率=",
]


def _rule_focus(md: str, back: int = 900, fwd: int = 1500) -> str:
    """按规则核心锚点取若干小窗口并合并，保证评分条款不被整段截断。"""
    spans: list[tuple[int, int]] = []
    for a in FOCUS_ANCHORS:
        start = 0
        while True:
            i = md.find(a, start)
            if i < 0:
                break
            spans.append((max(0, i - back), min(len(md), i + fwd)))
            start = i + len(a)
    if not spans:
        return ""
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return "\n…\n".join(md[s:e] for s, e in merged)


ALIASES = [
    "价格评分", "价格部分", "合理低价", "价格分", "评标基准价", "基准价",
    "评分标准", "价格评审", "评标办法", "报价得分", "评标参考值", "投标报价得分",
    "投标报价满分", "偏差率", "分项价格分",
]


PROMPT_TMPL = """请从下方招标文件片段中，解析出「价格分」的计算规则，输出可编程执行的参数。

【本项目的报价构成】（顺序固定，来自开标记录表表头，请勿改动顺序、请勿增删）
{labels}

【输出结构】严格按此 JSON 结构输出，键名不得增删改名：
{{
  "total_score": 价格分满分（数字，如 40 / 50）。
                 填「价格部分权重×100」还是绝对分值，以原文写法为准；原文只给权重比例时填 null。
  "method": "评分方法原文名称，如「合理低价优先法」「基准价比值法」「最低评标价法」",
  "formula": "价格分公式，逐字照抄原文（如「价格分=（P基准／P）×A」）；原文未写出公式则填空字符串",
  "benchmark": "基准价（评标基准价）的定义，逐字照抄原文；未写出则填空字符串",
  "benchmark_factor": 基准价里平均值前面的系数（数字）。
                       「平均值×0.9」→ 0.9；「算术平均值的95%」→ 0.95；「平均值的98%」→ 0.98；
                       原文没有乘系数则填 1。
  "behavior": 五选一：ratio / deviation / proportional / full_deviation / itemized_deviation：
              ratio = 报价不高于基准价即得满分、高于基准价按 (基准价/报价)×满分 折算（比值法）；
              deviation = 以「基本分」为基准，按偏差率每 1% 加/减固定分值，
                          例如「等于基准价得基本分45分，每低1%加1分、每高1%扣1分」；
              proportional = 只有扣分、没有基本分，以基准价为 100% 逐档扣分；
              full_deviation = **单一总报价**、等于参考值即得满分，高于/低于都只扣分，两侧步长不同，
                          例如「P 与 N 相等得满分53分；每高于 N 值1%减0.6分；每低于 N 值1%减0.3分」；
              itemized_deviation = **多个报价分项，每个分项各自算基准价、各自按 Fn 与偏差率加减分**，
                          例如「满分F=40（F1门楣10分、F2灯箱10分、F3标识15分、F4配件5分），
                          Dn=Fn−偏差率*100*E1（高于）或 Fn+偏差率*100*E2（低于），E1=0.5,E2=0.3，
                          投标报价得分=D1+D2+D3+D4，某项为负按 0 分、不冲抵其它分项」。
                          本口径 parts 必须逐项给出 Fn 分值。
  "step_high": full_deviation 下，报价高于参考值每 1% 扣的分（数字）；其他 behavior 填 null。
  "step_low": full_deviation 下，报价低于参考值每 1% 扣的分（数字）；其他 behavior 填 null。
  "itemized_e1": itemized_deviation 下，分项报价高于分项基准价每 1% 的扣分系数 E1（如 0.5）；其他填 null。
  "itemized_e2": itemized_deviation 下，分项报价低于分项基准价每 1% 的系数 E2（如 0.3）；其他填 null。
  "trim_tiers": full_deviation 下，按有效投标人家数分档计算参考值的去极值规则，数组形如
                [{{"max_n": 5, "high": 0, "low": 0}}, {{"max_n": 9, "high": 1, "low": 1}},
                 {{"max_n": 999999, "high": 2, "low": 1}}]；
                max_n 为该档家数上限，high/low 为该档去掉的最高/最低报价个数；
                原文没分档则返回空数组 []。
  "base_score": 报价等于基准价时的基本分（数字）；原文「得基本分45分」→ 45；
                behavior 不是 deviation 时填 null。
  "step_percent": 偏差率每 1% 对应的加/减分值（数字）；「每低1%加1分」→ 1；
                 「每高1%扣0.5分」→ 0.5；未提及填 null。
  "low_limit": 报价低于基准价超过该百分比后，改为在满分基础上扣分（数字）；
               「低于基准价5%以上（不含）时按每再低1%在满分50分基础上扣1分」→ 5；
               未提及填 0。
  "trim_high": 计算基准价时剔除的最高报价家数（整数）；「去掉一个最高价」→ 1；未提及填 0。
  "trim_low": 计算基准价时剔除的最低报价家数（整数）；「去掉一个最低价」→ 1；未提及填 0。
  "trim_guard": 触发上述剔除所需的有效投标人家数（整数）；「合格投标人≥10家时」→ 10；
               原文未设门槛（即始终剔除）填 0。
  "parts": [按上面报价构成的顺序，逐个区间给出分值：{{"label": "区间原文标题", "score": 17}}]。
           区间与报价构成数量必须一致；区间只有整体分值、不分区间的，返回单条。
           原文未分配区间分值则返回空数组 []。
  "note": "其他影响计算的口径，如「税率不一致时按不含税价评标」「价格分保留两位小数」；没有则 null",
  "evidence_formula": "支撑 formula 的原文句子，**逐字照抄连续片段**（标点一致，≤60 字）；
                        原文没有公式则填空字符串",
  "evidence_score": "支撑 total_score 或区间分值的原文片段，**逐字照抄连续片段**（≤60 字）；
                     原文没有则填空字符串",
  "confidence": 0~1 的数字
}}

【证据引用的硬性要求】
evidence_formula 与 evidence_score 会被程序拿回招标文件做逐字比对，**不通过则整条规则判为未确认**。
· 必须是连续的一整段原文，不得用「…」「...」「（略）」「等」省略中间内容
· 不要拼接两个不连续的句子，不要改写字词、不要调整标点
· 长度控制在 60 字以内：宁可只截取包含关键数字/公式的最短片段，也不要为了完整而省略中间部分
· 表格类内容（如「标准分值」列）请照抄同一行的文字，不要把整张表拼在一起

【判定要点】
1. 先判断 behavior：原文出现「(B/A)×满分」「A>B 时按…折算」「不高于基准价得满分」即 ratio；
   原文出现「基本分 XX 分」+「每低/高 1% 加/扣 X 分」即 deviation（要先取基本分、步长、低幅上限）；
   只有「每高于基准价 1% 扣 X 分」而没有基本分，才是 proportional；
   原文出现「与参考值相等得满分 XX 分」且高于、低于两侧**都写「减/扣 X 分」**时，
   是 full_deviation，要把两侧步长分别填进 step_high / step_low，并把按家数分档的
   去极值规则填进 trim_tiers。注意它**没有基本分**，base_score 必须为 null。
   原文出现「满分 F 为 XX 分（其中 F1:X 类价格占 a 分、F2:Y 类价格占 b 分…）」+「D1+D2+…」
   即**多个报价分项各自独立计分**，是 itemized_deviation：把各分项分值填进 parts，
   E1/E2 填 itemized_e1/itemized_e2，base_score 为 null。注意它与 full_deviation 的区别：
   itemized 是**每个分项各自算一个基准价**，full_deviation 是单一总报价算一个基准价。
2. 再判断基准价的组成：是否剔除最高/最低价、是否乘系数、是否设家数门槛。
   三个参数必须分开填，不要合并成一句话。若「≤5 家不去、6~9 家去 1 高 1 低、>9 家去 2 高 1 低」
   这类按家数分档，填 trim_tiers，不要把某一档套进 trim_high/trim_low。
3. parts 的分值要能加总为 total_score。若原文写「价格分 40 分，其中区间一 17 分、区间二 13 分、
   区间三 10 分」，则 parts 三条分别为 17/13/10，total_score 为 40。
   单一总报价且只给一个总分时，parts 返回一条 {{"label": "投标报价", "score": 该总分}}。
4. 原文若写「不足 1% 按插入法计算」，说明偏差率按连续值线性折算，不要按整数百分比取整。
5. 若原文只写了「综合评分法」而没有价格分公式、分值、基准价定义中的任何一项，
   说明本节不含可计算规则：formula 与 benchmark 填空字符串、evidence_formula 填空字符串、
   confidence 填 0.3 —— **不要用行业惯例补一个公式**。程序会把这种情况标为「规则未确认」。

【示例 A：比值法】
原文「若合格投标人≥10家，去掉一个最高价、一个最低价，剩余评标价格的算术平均值的95%为基准价；
若(A-B)≤0，价格分=50；若(A-B)>0，价格分=(B/A)×50。」
→ {{"total_score":50,"method":"基准价比值法","formula":"(B/A)×50","benchmark":"去掉一个最高价、
一个最低价后剩余评标价格算术平均值的95%","benchmark_factor":0.95,"behavior":"ratio",
"trim_high":1,"trim_low":1,"trim_guard":10,"parts":[{{"label":"投标报价","score":50}}],
"evidence_formula":"若（A－B）>0，则价格分＝（B/A）×50"}}

【示例 B：逐区间合理低价优先法】
原文「价格分=（P基准／P）×A，A 为价格权重分。区间一 17 分、区间二 13 分、区间三 10 分。」
→ {{"total_score":40,"method":"合理低价优先法","formula":"价格分=（P基准／P）×A",
"benchmark":"取最低有效报价与平均有效报价×0.9 中的较高者","benchmark_factor":0.9,
"behavior":"ratio","parts":[{{"label":"区间一","score":17}},{{"label":"区间二","score":13}},
{{"label":"区间三","score":10}}],"evidence_formula":"价格分=（P基准／P）×A，A 为价格权重分"}}

【示例 C：基本分 + 偏差率增减分】
原文「以所有有效投标人投标报价的算术平均值作为评标基准价。当有效投标报价超过5家（不含）时，
去掉一个最高价和一个最低价后计算算术平均值。投标人有效投标报价与评标基准价相等的，得基本分45分；
低于基准价的按每低1%在基本分45分的基础上加1分，加至满分50分为止；若低于基准价5%以上（不含）时，
按每再低1%在满分50分的基础上扣1分；高于基准价的按每高1%扣1分。」
→ {{"total_score":50,"method":"基准价偏差率加减分法","formula":"基本分45分±偏差率×1分/1%",
"benchmark":"去掉一个最高价、一个最低价后剩余有效投标报价的算术平均值","benchmark_factor":1,
"behavior":"deviation","base_score":45,"step_percent":1,"low_limit":5,
"trim_high":1,"trim_low":1,"trim_guard":5,
"parts":[{{"label":"投标报价","score":50}}],
"evidence_formula":"投标人有效投标报价与评标基准价相等的，得基本分45分"}}

【示例 D：满分基础扣分 + 按家数分档去极值】
原文「2.评标参考值的确定：当投标人数量≤5家时，取所有有效投标人评标价格的算术平均值为
评标参考值N；当5家＜有效投标人数量≤9家时，去掉一个评标价格最高值和一个评标价格最低值后，
取剩余评标价格的算术平均值为评标参考值N；投标人数量＞9家时，去掉一个评标价格最高值、
一个次高值和一个评标价格最低值后，取剩余评标价格的算术平均值为评标参考值N。
3.投标报价得分：投标人的报价P值与N值相等的得满分53分；每高于N值1%，在53分的基础上
减0.6分；每低于N值1%，在53分的基础上减0.3分。扣完为止。」
→ {{"total_score":53,"method":"评标参考值偏差扣分法","formula":"P=N 得满分53分，每高1%减0.6分、每低1%减0.3分",
"benchmark":"按家数分档去极值后剩余评标价格的算术平均值","benchmark_factor":1,"behavior":"full_deviation",
"step_high":0.6,"step_low":0.3,"base_score":null,"step_percent":null,"low_limit":0,
"trim_high":0,"trim_low":0,"trim_guard":0,
"trim_tiers":[{{"max_n":5,"high":0,"low":0}},{{"max_n":9,"high":1,"low":1}},{{"max_n":999999,"high":2,"low":1}}],
"parts":[{{"label":"投标报价","score":53}}],
"evidence_formula":"投标人的报价P值与N值相等的得满分53分；每高于N值1%，在53分的基础上减0.6分",
"evidence_score":"价格 评标 价格 53"}}

【示例 E：分项独立基准价 + 基本分 Fn 双向偏差扣分】
原文「报价部分（40分）。投标报价得分Dn＝Fn－偏差率*100*E1（投标报价＞评标基准价）；
Dn＝Fn＋偏差率*100*E2（投标报价≤评标基准价）。E1=0.5；E2=0.3。
投标报价满分F为40分（其中F1:门楣发光字类价格占10分，F2:灯箱类价格占10分；
F3:标识标牌类价格占15分；F4:配件类价格占5分）。D1为门楣发光字类价格得分……
投标报价得分=D1+D2+D3+D4。当投标报价得分为负时，均按0分计算。
进入评标基准价计算的投标人投标报价的算术平均值作为评标基准价。」
→ {{"total_score":40,"method":"分项基准价偏差率加减分法",
"formula":"Dn=Fn±偏差率*100*E；投标报价得分=D1+D2+D3+D4","benchmark":"各分项投标报价的算术平均值",
"benchmark_factor":1,"behavior":"itemized_deviation","itemized_e1":0.5,"itemized_e2":0.3,
"base_score":null,"step_percent":null,"step_high":null,"step_low":null,"low_limit":0,
"trim_high":0,"trim_low":0,"trim_guard":0,
"parts":[{{"label":"门楣发光字类","score":10}},{{"label":"灯箱类","score":10}},
{{"label":"标识标牌类","score":15}},{{"label":"配件类","score":5}}],
"note":"价格分以投标报价（不含税）计算；各分项扣完为止、最低0分，某项为负不冲抵其它分项",
"evidence_formula":"投标报价得分Dn＝Fn－偏差率*100*E1",
"evidence_score":"投标报价满分F为40分（其中F1:门楣发光字类价格占10分"}}

【招标文件片段】
{snippet}"""


def extract_rule(tender_md: str, part_labels: list[str], source_file: str = "",
                 log=print) -> PriceRule:
    if not tender_md:
        return PriceRule(note="未找到招标文件，无法确定评分规则",
                         basis="未找到招标文件，价格分未计算")

    snippet = locate(tender_md, ALIASES, window=CONFIG.snippet_window)
    if not snippet:
        snippet = tender_md[:20000]
    # 规则核心条款（如 P103 的 Dn/Fn/E1/E2）可能落在长片段末端被 30000 字截断，
    # 这里把命中锚点的小窗口前置，确保千问一定看到完整公式与分项分值。
    focus = _rule_focus(tender_md)
    if focus:
        snippet = focus + "\n\n…（略）…\n\n" + snippet
    snippet = snippet[:30000]

    labels_block = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(part_labels)) or "（无，单一总报价）"

    prompt = PROMPT_TMPL.format(labels=labels_block, snippet=snippet)

    try:
        d = llm.chat_json(prompt, system=llm.SYS_RULE_PARSER, max_tokens=2500)
    except Exception as e:
        log(f"  [警告] 评分规则抽取失败：{e}")
        return PriceRule(note=f"规则抽取失败：{e}",
                         basis="规则抽取失败，价格分未计算")

    parts = []
    for i, p in enumerate(d.get("parts") or []):
        if not isinstance(p, dict):
            continue
        label = part_labels[i] if i < len(part_labels) else str(p.get("label", ""))
        sc = p.get("score")
        try:
            sc = float(sc) if sc is not None else None
        except (TypeError, ValueError):
            sc = None
        parts.append({"label": label, "score": sc})

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    hints = _regex_hints(snippet)
    fd = _parse_full_deviation(snippet)
    # 分项口径在**完整原文**上跑确定性识别，避免被 30000 字截断误伤
    im = _parse_itemized(tender_md)

    ev_formula = str(d.get("evidence_formula") or "").strip()
    ev_score = str(d.get("evidence_score") or "").strip()
    ev_formula = ev_formula if ev_formula.lower() not in ("null", "none") else ""
    ev_score = ev_score if ev_score.lower() not in ("null", "none") else ""

    rule = PriceRule(
        total_score=_f(d.get("total_score")),
        method=str(d.get("method") or ""),
        formula=str(d.get("formula") or ""),
        benchmark=str(d.get("benchmark") or ""),
        benchmark_factor=_f(d.get("benchmark_factor"), 0.9) or 0.9,
        parts=parts,
        note=str(d.get("note") or ""),
        source_file=source_file,
        # 页码取证据原句所在页；证据过不了原样匹配时再退回关键词定位
        source_page=page_for(tender_md, ev_formula)
                    or page_at(tender_md, "价格评分") or page_at(tender_md, "合理低价"),
        confidence=_f(d.get("confidence"), 0.7),
        behavior=str(d.get("behavior") or ""),
        base_score=_f(d.get("base_score"), 0.0),
        step_percent=_f(d.get("step_percent"), 1.0) or 1.0,
        low_limit=_f(d.get("low_limit"), 0.0),
        step_high=_f(d.get("step_high"), 0.0),
        step_low=_f(d.get("step_low"), 0.0),
        itemized_e1=_f(d.get("itemized_e1"), 0.0),
        itemized_e2=_f(d.get("itemized_e2"), 0.0),
        trim_tiers=[x for x in (d.get("trim_tiers") or []) if isinstance(x, dict)],
        trim_low=int(_f(d.get("trim_low"), 0)),
        trim_high=int(_f(d.get("trim_high"), 0)),
        trim_guard=int(_f(d.get("trim_guard"), 0)),
    )

    # 正则抠到的参数更可靠，直接覆盖模型结果
    if hints.get("behavior"):
        rule.behavior = hints["behavior"]
    if hints.get("factor"):
        rule.benchmark_factor = hints["factor"]
    for k in ("trim_high", "trim_low", "trim_guard"):
        if hints.get(k):
            setattr(rule, k, hints[k])
    for k in ("base_score", "step_percent", "low_limit"):
        if hints.get(k):
            setattr(rule, k, hints[k])
    # 基本分只对 deviation 有意义；比值法/扣分法下清掉，免得表格里写出矛盾口径
    if rule.behavior in ("ratio", "proportional"):
        rule.base_score = 0.0
    elif rule.behavior == "deviation" and not rule.base_score and rule.total_score:
        rule.base_score = round(rule.total_score * 0.9, 2)

    # ---- 原文确定性识别：满分基础扣分 + 分档去极值（山东烟草一类口径） ----
    # 模型只负责「读懂这是哪种玩法」，全部数值与分档一律用正则从原文抠，避免模型转写出错。
    if fd:
        rule.behavior = "full_deviation"
        rule.total_score = fd["total_score"]
        rule.step_high = fd["step_high"]
        rule.step_low = fd["step_low"]
        rule.trim_tiers = fd["trim_tiers"]
        rule.base_score = 0.0
        rule.benchmark_factor = 1.0
        rule.trim_high = rule.trim_low = rule.trim_guard = 0
        if not rule.method or "推定" in rule.method:
            rule.method = "评标参考值偏差扣分法"
        if not rule.formula or "推定" in rule.formula:
            rule.formula = (f"P=N 得满分 {fd['total_score']:g} 分；每高 1% 减 {fd['step_high']:g} 分、"
                            f"每低 1% 减 {fd['step_low']:g} 分（不足 1% 按插入法，扣完为止）")
        if not rule.benchmark or "推定" in rule.benchmark:
            rule.benchmark = "按有效投标人家数分档去极值后，剩余评标价格的算术平均值"
        if not any(p.get("score") for p in rule.parts):
            rule.parts = [{"label": part_labels[0] if part_labels else "投标报价",
                           "score": fd["total_score"]}]
        if fd.get("evidence"):
            ev_formula = fd["evidence"]
            rule.source_page = page_for(tender_md, ev_formula) or rule.source_page
        if _grounded(ev_formula, tender_md):
            # 参数来自原文正则、不是模型推断，置信度按已核对原文处理
            rule.confidence = max(rule.confidence, 0.92)
        log(f"  [规则] 原文识别为「满分扣分 + 分档去极值」：满分 {rule.total_score:g}，"
            f"每高 1% 扣 {rule.step_high:g} 分 / 每低 1% 扣 {rule.step_low:g} 分，"
            f"分档 {len(rule.trim_tiers)} 档")

    # ---- 原文确定性识别：分项独立基准价 + 基本分 Fn 双向偏差扣分（安徽工行一类口径） ----
    # 模型容易把 Dn=Fn±偏差率×E1/E2 误读成单一 total_score 的 deviation，
    # 这里以原文正则为准：分项分值、两侧系数、基准价取法全部直取，证据逐字核对。
    if im and not fd:
        rule.behavior = "itemized_deviation"
        rule.total_score = im["total_score"]
        rule.itemized_e1 = im["e1"]
        rule.itemized_e2 = im["e2"]
        rule.base_score = 0.0                 # 分项口径没有统一「基本分」
        rule.step_high = rule.step_low = 0.0
        rule.step_percent = 1.0
        rule.low_limit = 0.0
        rule.trim_high = rule.trim_low = rule.trim_guard = 0
        rule.trim_tiers = []
        rule.benchmark_factor = 1.0
        rule.benchmark_scope = im.get("benchmark_scope", "")
        if im.get("benchmark") and ("推定" in rule.benchmark or not rule.benchmark):
            rule.benchmark = im["benchmark"]
        if not rule.method or "推定" in rule.method:
            rule.method = "分项基准价偏差率加减分法"
        if not rule.formula or "推定" in rule.formula:
            rule.formula = (f"Dn=Fn±偏差率×100×E（高于用 E1={im['e1']:g}，"
                            f"低于用 E2={im['e2']:g}）；每项扣完为止（0≤Dn≤Fn）；"
                            f"投标报价得分=ΣDn")
        if im.get("note") and ("推定" in rule.note or not rule.note):
            rule.note = im["note"]
        # 分项分值：按原文 F1..F4 的顺序，映射到开标表的分项（part_labels）
        if im.get("item_parts"):
            scored = [p for p in im["item_parts"] if p.get("score")]
            if len(scored) == len(part_labels):
                rule.parts = [{"label": part_labels[i], "score": p["score"]}
                              for i, p in enumerate(scored)]
            else:
                rule.parts = [{"label": p["label"], "score": p["score"]} for p in scored]
        if im.get("evidence_formula"):
            ev_formula = im["evidence_formula"]
            rule.source_page = page_for(tender_md, ev_formula) or rule.source_page
        if im.get("evidence_score"):
            ev_score = im["evidence_score"]
        if _grounded(ev_formula, tender_md) and any(p.get("score") for p in rule.parts):
            rule.confidence = max(rule.confidence, 0.92)
        log(f"  [规则] 原文识别为「分项独立基准价 + 基本分双向偏差扣分」：满分 {rule.total_score:g}，"
            f"{len(rule.parts)} 个分项 {[p['score'] for p in rule.parts]}，"
            f"E1={rule.itemized_e1:g} E2={rule.itemized_e2:g}")

    # ---- 护栏：公式/基准价没有明确写出时，不许默认当成本项目的规则 ----
    ok_formula = bool(ev_formula) and _grounded(ev_formula, tender_md)
    ok_score = (bool(ev_score) and _grounded(ev_score, tender_md)) or \
               _score_corroborated(ev_formula, rule.total_score, tender_md)
    has_weights = bool(rule.total_score) and any(p.get("score") for p in rule.parts)

    # 分项口径：分值 E1/E2/Fn 均由原文正则直取，只要 Dn 公式能在原文核对、
    # 且分项分值之和 == 满分，就判为已确认（模型对分项口径的转述不可靠，不作为否决依据）。
    if rule.behavior == "itemized_deviation" and im:
        parts_sum = sum(p["score"] for p in rule.parts if p["score"])
        coherent = abs(parts_sum - rule.total_score) <= 0.01 if parts_sum else False
        rule.confirmed = bool(ok_formula and coherent and rule.itemized_e1 > 0
                              and rule.itemized_e2 > 0)
    else:
        rule.confirmed = bool(ok_formula and (ok_score or not rule.parts) and has_weights
                              and rule.confidence >= 0.6)
    rule.evidence = ev_formula
    if rule.confirmed:
        rule.basis = "招标文件明确（原文正则提取参数 + 原文核对，分数由代码计算）" \
            if (fd or im) else "招标文件明确（已核对原文原句）"
    elif ev_formula and not _grounded(ev_formula, tender_md):
        rule.basis = f"规则原文核对未通过（引用「{ev_formula[:30]}…」在招标文件中未找到），价格分仅供参考"
    else:
        rule.basis = "招标文件未明确价格分公式，按「合理低价优先法」推定，须人工核对"
        if not rule.method:
            rule.method = "合理低价优先法（推定）"
        if not rule.formula:
            rule.formula = "价格分 =（P基准 ÷ P）× A（推定）"
        if not rule.benchmark:
            rule.benchmark = "max(最低有效报价, 平均有效报价 × 0.9)（推定）"

    # 一致性自检：各区间分值之和应等于价格总分
    got = [p["score"] for p in rule.parts if p["score"]]
    if got and rule.total_score and abs(sum(got) - rule.total_score) > 0.01:
        rule.note = (rule.note + "；" if rule.note else "") + (
            f"注意：各区间分值合计 {sum(got):g} 与价格总分 {rule.total_score:g} 不一致，请核对")
    return rule


def rule_to_json(rule: PriceRule) -> str:
    return json.dumps({
        "total_score": rule.total_score, "method": rule.method,
        "formula": rule.formula, "benchmark": rule.benchmark,
        "factor": rule.benchmark_factor, "parts": rule.parts,
        "note": rule.note, "page": rule.source_page,
    }, ensure_ascii=False, indent=2)
