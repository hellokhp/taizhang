"""价格分计算（确定性代码，不交给大模型算）。

支持「合理低价优先法」：
    价格分 = (P基准 / P) × A
    P基准 = max(所有有效报价中的最低者, 所有有效报价平均值 × 系数)
    价格分最高得满分（封顶）

当招标文件把价格分拆到多个报价区间（如 17 / 13 / 10 分）时，逐区间分别计算再相加。
"""
from __future__ import annotations

from statistics import mean

from bidsummary.models import LotResult, PriceRule


def _assign_weights(lot: LotResult, rule: PriceRule) -> list[float]:
    n = len(lot.parts)
    if n == 0:
        return []
    weights = [p.get("score") for p in (rule.parts or [])]
    if len(weights) == n and all(isinstance(w, (int, float)) and w > 0 for w in weights):
        return [float(w) for w in weights]
    # 规则没对上：平均分配，并在提示里说明
    if rule.total_score > 0:
        return [round(rule.total_score / n, 4)] * n
    return [0.0] * n


def _score_deviation(a: float, b: float, rule: PriceRule) -> tuple[float, float]:
    """基本分 + 偏差率增减分。返回 (得分, 偏差率百分数)。

    原文口径（以安阳项目为例）：
      报价 = 基准价 → 得基本分 45 分；
      报价低于基准价 → 每低 1% 加 step 分，最高到满分 50；
      低于超过 low_limit%（如 5%）后 → 改为每再低 1% 在满分基础上扣 step 分；
      报价高于基准价 → 每高 1% 扣 step 分。不足 1% 按插入法（即连续值）。
    """
    full = rule.total_score or 50.0
    base = rule.base_score or 0.0
    step = rule.step_percent or 1.0
    if not b:
        return 0.0, 0.0
    dev = (a - b) / b * 100.0
    if abs(dev) < 1e-9:
        raw = base
    elif dev < 0:
        d = -dev
        lim = rule.low_limit or 0.0
        raw = (base + d * step) if (not lim or d <= lim) else (full - (d - lim) * step)
    else:
        raw = base - dev * step
    return max(0.0, min(full, raw)), dev


def _score_full_deviation(lot: LotResult, rule: PriceRule) -> None:
    """满分基础扣分法：报价 = 评标参考值 N 得满分，两侧按不同步长扣分。

    原文口径（山东烟草）：
      N 按有效投标人家数分档去极值后取算术平均（≤5 家不去；6~9 家去 1 高 1 低；
      >9 家去 2 高 1 低）；P 高于 N 每 1% 扣 step_high 分，低于 N 每 1% 扣 step_low 分，
      不足 1% 按插入法连续折算，扣完为止（下限 0 分）。
    被剔除的投标人**仍参与评分**，只是不参与 N 的计算。
    """
    full = rule.total_score or 50.0
    part = lot.parts[0]
    part.score = full

    valid = [r for r in lot.rows if r.total is not None and r.total > 0]
    if not valid:
        _rank_only(lot)
        return

    ordered = sorted(valid, key=lambda r: r.total)
    tiers = sorted(rule.trim_tiers or [], key=lambda x: x.get("max_n", 0)) or \
        [{"max_n": 10 ** 6, "high": 0, "low": 0}]
    tier = next((x for x in tiers if len(ordered) <= x.get("max_n", 0)), tiers[-1])
    n_low, n_high = int(tier.get("low", 0)), int(tier.get("high", 0))
    if n_low + n_high >= len(ordered):
        n_low = n_high = 0
    end = len(ordered) - n_high if n_high else len(ordered)
    pool = ordered[n_low:end]
    trimmed = ordered[:n_low] + (ordered[end:] if n_high else [])
    for r in trimmed:
        r.status = "参与评分（未参与评标参考值计算）"

    avg = sum(r.total for r in pool) / len(pool)
    bench = round(avg, 2)
    part.min_price = min(r.total for r in valid)
    part.avg_price = avg
    part.benchmark = bench
    lot.trimmed = [r.name for r in trimmed]
    lot.bench_n = len(pool)
    lot.bench_factor_used = 1.0

    for r in valid:
        dev = (r.total - bench) / bench * 100.0 if bench else 0.0
        raw = full - dev * rule.step_high if dev >= 0 else full - (-dev) * rule.step_low
        sc = max(0.0, min(full, raw))
        r.part_scores = [sc]
        r.price_score = round(sc, 2)
        r.deviation = dev / 100.0

    for r in valid:
        if r.organizer_score is None:
            continue
        if abs(r.organizer_score - r.price_score) > 0.011:
            r.flags.append(f"与招标人自算得分不一致（表内 {r.organizer_score:g}，本表 {r.price_score:g}）")

    _rank(lot)


def score_single(lot: LotResult, rule: PriceRule) -> None:
    """单一总报价评分。

    两种口径（由 rules 判定，代码只负责算）：
      · 比值法：A≤B 得满分，A>B 得 (B/A)×满分；
      · 偏差率增减分：等于基准价得基本分，每低/高 1% 加/扣固定分值（见 _score_deviation）。
    基准价 B：有效投标人 ≥ trim_guard 家时，去掉 trim_high 个最高、trim_low 个最低，
    剩余报价算术平均值 × 系数；不足该家数时取全部报价的算术平均值（不再乘系数）。
    full_deviation 走独立分支（按家数分档去极值 + 满分两侧扣分）。
    """
    if rule.behavior == "full_deviation" and rule.step_high > 0 and rule.step_low > 0:
        _score_full_deviation(lot, rule)
        return
    full = rule.total_score or 50.0
    factor = rule.benchmark_factor or 1.0
    part = lot.parts[0]
    part.score = full

    valid = [r for r in lot.rows if r.total is not None and r.total > 0]
    if not valid:
        _rank_only(lot)
        return

    ordered = sorted(valid, key=lambda r: r.total)
    guard = rule.trim_guard or 0
    t_low, t_high = rule.trim_low, rule.trim_high
    use_trim = bool(t_low or t_high) and (guard == 0 or len(ordered) >= guard)
    pool = ordered
    trimmed: list = []
    if use_trim and (t_low + t_high) < len(ordered):
        low_rm = ordered[:t_low]
        high_rm = ordered[len(ordered) - t_high:] if t_high else []
        trimmed = low_rm + high_rm
        pool = ordered[t_low:len(ordered) - t_high]
        for r in trimmed:
            r.status = "参与评分（未参与基准价计算）"
    eff_factor = factor if use_trim else 1.0

    pool_sum = sum(r.total for r in pool)
    avg = pool_sum / len(pool)
    bench = round(avg * eff_factor, 2)
    part.min_price = min(r.total for r in valid)
    part.avg_price = avg
    part.benchmark = bench
    lot.trimmed = [r.name for r in trimmed]
    lot.bench_n = len(pool)
    lot.bench_factor_used = eff_factor

    deviation_mode = rule.behavior == "deviation" and rule.base_score > 0
    for r in valid:
        if deviation_mode:
            sc, dev = _score_deviation(r.total, bench, rule)
            r.part_scores = [sc]
            r.price_score = round(sc, 2)
            r.deviation = dev / 100.0
            continue
        if r.total <= bench:
            r.part_scores = [full]
        else:
            r.part_scores = [round(bench / r.total * full, 4)]
        r.price_score = round(sum(r.part_scores), 2)
        r.price_score = max(0.0, min(full, r.price_score))
        r.deviation = (r.total - bench) / bench if bench else None

    # 与招标人自算得分对照
    for r in valid:
        if r.organizer_score is None:
            continue
        if abs(r.organizer_score - r.price_score) > 0.011:
            r.flags.append(f"与招标人自算得分不一致（表内 {r.organizer_score:g}，本表 {r.price_score:g}）")

    _rank(lot)


def _score_itemized(lot: LotResult, rule: PriceRule) -> None:
    """分项独立基准价 + 基本分 Fn 双向偏差扣分（安徽工行一类口径）。

    原文口径（招标文件 P103）：
      投标报价满分 F=40，其中 F1 门楣发光字类 10 分、F2 灯箱类 10 分、
      F3 标识标牌类 15 分、F4 配件类 5 分；
      每个分项各自算评标基准价（各投标人该分项报价的算术平均，无系数、无去极值）；
      偏差率 = (该分项报价 − 该分项基准价) / 该分项基准价 × 100；
      报价 > 基准价：Dn = Fn − 偏差率 × E1；
      报价 ≤ 基准价：Dn = Fn + 偏差率 × E2（偏差率为负，即扣分）；
      每个分项扣完为止：0 ≤ Dn ≤ Fn，某项为负按 0 计、不冲抵其它分项；
      投标报价得分 = D1+D2+D3+D4，保留两位小数。
    E1/E2 与 Fn 均由原文正则直取，分数由代码算。
    """
    n = len(lot.parts)
    if n == 0:
        _rank_only(lot)
        return

    # 归一化分项分值 Fn（按规则 parts 顺序；缺失则按 total_score 均分）
    fscores = _assign_weights(lot, rule)
    e1 = rule.itemized_e1
    e2 = rule.itemized_e2

    # 逐分项算基准价 = 该分项有效报价的算术平均（无系数、无去极值）
    benchmarks: list[float] = []
    for idx, part in enumerate(lot.parts):
        part.score = fscores[idx]
        vals = [v for v in (_value(lot, r, idx) for r in lot.rows)
                if v is not None and v > 0]
        bench = round(mean(vals), 6) if vals else 0.0
        part.min_price = min(vals) if vals else None
        part.avg_price = mean(vals) if vals else None
        part.benchmark = bench
        benchmarks.append(bench)

    # 逐家逐分项计分
    for r in lot.rows:
        scores: list[float] = []
        devs: list[float] = []
        for idx, part in enumerate(lot.parts):
            v = _value(lot, r, idx)
            bench = benchmarks[idx]
            if v is None or v <= 0 or not bench:
                scores.append(0.0)
                devs.append(0.0)
                continue
            dev = (v - bench) / bench * 100.0
            if dev > 0:
                dn = part.score - dev * e1
            else:
                dn = part.score + dev * e2
            # 每个分项都是独立评分项，扣完为止、下限 0 分：
            # 招标文件「当投标报价得分为负时，均按 0 分计算」中的「均」指 D1..Dn 各分项。
            # 不能让某项（如满分 5 分的配件类扣到 -18）去冲抵其它分项的正分。
            dn = max(0.0, min(part.score, dn))
            scores.append(dn)
            devs.append(dev)
        r.part_scores = [round(s, 4) for s in scores]
        # 各分项已封底在 [0, Fn]，总分自然 ≥ 0；max 仅作防御性兜底。
        r.price_score = round(max(0.0, sum(scores)), 2)
        # 展示用偏差率：以各分项基准价合计 vs 该家分项报价合计
        bench_total = sum(b for b in benchmarks if b)
        r.deviation = (r.total - bench_total) / bench_total if (r.total and bench_total) else None

    # 缺项检查
    for r in lot.rows:
        missing = [lot.parts[i].label for i in range(n)
                   if _value(lot, r, i) is None]
        if missing:
            r.flags.append("缺 " + str(len(missing)) + " 项报价：" + "、".join(missing))

    # 与招标人自算得分对照
    for r in lot.rows:
        if r.organizer_score is None or r.price_score is None:
            continue
        if abs(r.organizer_score - r.price_score) > 0.011:
            r.flags.append(f"与招标人自算得分不一致（表内 {r.organizer_score:g}，本表 {r.price_score:g}）")

    _rank(lot)


def score_lot(lot: LotResult, rule: PriceRule) -> None:
    if lot.pricing == "single":
        score_single(lot, rule)
        return
    if lot.itemized and rule.behavior == "itemized_deviation" and rule.itemized_e1 > 0:
        _score_itemized(lot, rule)
        return
    weights = _assign_weights(lot, rule)
    for part, w in zip(lot.parts, weights):
        part.score = w

    if not weights or sum(weights) <= 0:
        # 没有评分规则：只排序不评分
        _rank_only(lot)
        return

    factor = rule.benchmark_factor or 0.9

    for idx, part in enumerate(lot.parts):
        vals = []
        for r in lot.rows:
            v = _value(lot, r, idx)
            if v is not None and v > 0:
                vals.append(v)
        if not vals:
            part.min_price = part.avg_price = part.benchmark = None
            continue
        part.min_price = min(vals)
        part.avg_price = mean(vals)
        part.benchmark = max(part.min_price, part.avg_price * factor)

    for r in lot.rows:
        scores: list[float] = []
        for idx, part in enumerate(lot.parts):
            v = _value(lot, r, idx)
            if v is None or v <= 0 or not part.benchmark:
                scores.append(0.0)
                continue
            s = part.benchmark / v * part.score
            scores.append(min(part.score, s))      # 最高得满分
        r.part_scores = scores
        r.price_score = round(sum(scores), 2) if scores else None

    # 基准价合计（仅用于展示偏差率）
    bench_total = sum(p.benchmark for p in lot.parts if p.benchmark)
    for r in lot.rows:
        if r.total and bench_total > 0:
            r.deviation = (r.total - bench_total) / bench_total

    # 缺项检查
    for r in lot.rows:
        missing = [lot.parts[i].label for i in range(len(lot.parts))
                   if _value(lot, r, i) is None]
        if missing:
            r.flags.append("缺 " + str(len(missing)) + " 项报价")

    _rank(lot)


def _value(lot: LotResult, row, idx: int):
    seq = row.net_parts if lot.use_net else row.parts
    return seq[idx] if idx < len(seq) else None


def _rank(lot: LotResult) -> None:
    # 价格分降序；同分者价格低者优先（招标文件 6.6 条）
    lot.rows.sort(key=lambda r: (
        -(r.price_score if r.price_score is not None else -1),
        r.total if r.total is not None else float("inf"),
    ))
    for i, r in enumerate(lot.rows, start=1):
        r.rank = i


def _rank_only(lot: LotResult) -> None:
    lot.rows.sort(key=lambda r: r.total if r.total is not None else float("inf"))
    for i, r in enumerate(lot.rows, start=1):
        r.rank = i
        r.price_score = None


def analyze(lot: LotResult, rule: PriceRule, budget: float | None = None) -> list[str]:
    """标段级提示：满分并列、总价超预算。"""
    out: list[str] = []
    full = rule.total_score or 0
    if full:
        top = [r for r in lot.rows if r.price_score is not None
               and abs(r.price_score - full) < 0.005]
        if len(top) >= 3:
            out.append(f"{lot.lot_name}：{len(top)} 家价格分并列满分（{full:g} 分），"
                       f"价格分无法区分排名，最终名次取决于技术、商务与服务部分得分；"
                       f"表中顺序按评标价由低到高排列，不代表中标顺序。")
    totals = [r.total for r in lot.rows if r.total]
    if budget and totals and max(totals) > budget:
        over = [r for r in lot.rows if r.total and r.total > budget]
        out.append(f"{lot.lot_name}：" + "、".join(f"{r.name}（{r.total:,.2f} 元）" for r in over)
                   + f" 报价高于三年期预算/最高限价 {budget:,.2f} 元，"
                     "招标文件限价条款针对单价，总价超预算是否否决须评标委员会认定。")
    return out


def summarize_lot(lot: LotResult) -> dict:
    totals = [r.total for r in lot.rows if r.total is not None]
    return {
        "count": len(lot.rows),
        "min": min(totals) if totals else None,
        "max": max(totals) if totals else None,
        "avg": mean(totals) if totals else None,
    }
