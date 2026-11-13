/* ================================================================
   折扣率维度一致性判定（纯函数）

   用户硬要求：折扣率的分子/分母必须同维度。
     · 结算现价 ↔ 最高限价（同项目、同含税口径、同数量口径）
     · 单商品类汇总报价 ↔ 单商品类汇总最高限价（同类、同口径）
   不允许拿「单类报价」除以「项目总限价」，也不允许含税/不含税混算。

   注意：本模块只做「比例计算」，价格分绝不在这里重算。
   ================================================================ */

import { ratioPair, crossCheck, DIM, round } from './currency.js'

export { DIM, ratioPair, crossCheck, dimText, sameDim } from './currency.js'

/** 税口径：AI 工具已在 sheetquote._choose_price_basis() 统一，展示只读取；未标注则返回 unknown */
export function taxOf(priceBasis) {
  const s = String(priceBasis || '')
  if (s.includes('不含税')) return 'exc'
  if (s.includes('含税')) return 'incl'
  return 'unknown'
}

/**
 * 模式 D：下浮率 / 折扣率报价
 * 分子分母都来自同一产品清单（cap_total = Σ单价最高限价×预估量），
 * 且都是「投标折扣口径」，因此 est_total / cap_total 才是合法的结算折扣率。
 * 注意：rate_kind=discount_rate 时 discount = rate/100，绝不能按 1−下浮率 再换一次。
 */
export function discountPairs(d, tax) {
  if (!d || !d.bidders) return []
  const taxDim = tax === 'exc' ? 'exc' : 'incl'   // 模式 D 产品限价清单为含税
  const capTotal = d.cap_total
  const capUnit = (d.products || []).reduce((s, p) => s + (Number(p.cap) || 0), 0)
  return d.bidders.map((b, i) => {
    const project = ratioPair(
      'discount_project_' + i, '结算现价 ÷ 项目最高限价',
      { value: b.est_total, label: '预估结算现价', dim: DIM.projectTotal(taxDim) },
      { value: capTotal, label: '最高限价（Σ限价×预估量）', dim: DIM.projectTotal(taxDim) },
      (d.rule_source_file || '招标文件') + ' P' + (d.rule_source_page || '?') + ' · ' + (d.formula || '')
    )
    const unit = ratioPair(
      'discount_unit_' + i, '折算单价合计 ÷ 单价限价合计',
      { value: b.unit_sum, label: '折算单价合计', dim: DIM.projectUnit(taxDim) },
      { value: capUnit, label: '单价限价合计（Σ限价，不含量）', dim: DIM.projectUnit(taxDim) },
      '各产品折算单价 = 单价最高限价 × 折扣；与「限价合计」同为单价维度'
    )
    return { bidder: b.name, pairs: [project, unit] }
  })
}

/**
 * 模式 C / E：单商品类汇总报价 ↔ 单商品类汇总最高限价
 * AI 工具已分别给出：
 *   eval_prices[i] = Σ(该类单价 × 权重) / 100   ← 评标口径（加权）
 *   cap_weighted[i] = Σ(该类限价 × 权重) / 100  ← 同口径的限价参照
 *   raw_sums[i] = Σ(该类单价)                    ← 未加权对照
 * 因此合法的折扣率只有「加权 ↔ 加权限价」与「未加权 ↔ 未加权限价」两对，
 * 且必须逐类配对（scope = cat:i），不可跨类、不可跨项目。
 */
export function shortlistPairs(sl, tax) {
  if (!sl || !sl.categories || !sl.bidders) return []
  const taxDim = tax
  const cats = sl.categories
  const catCapUnit = cats.map(c => (c.products || []).reduce((s, p) => s + (Number(p.cap) || 0), 0))
  return sl.bidders.map((b, bi) => {
    const pairs = []
    for (let i = 0; i < cats.length; i++) {
      const c = cats[i]
      pairs.push(ratioPair(
        'cat_weighted_' + bi + '_' + i,
        (c.name || '分类' + (i + 1)) + ' · 加权评标价 ÷ 加权限价',
        { value: b.eval_prices?.[i], label: '加权评标价（∑单价×权重）', dim: DIM.catWeighted(i, taxDim) },
        { value: c.cap_weighted, label: '限价加权合计（∑限价×权重）', dim: DIM.catWeighted(i, taxDim) },
        (sl.rule_source_file || '招标文件') + ' P' + (sl.rule_source_page || '?') + ' · ' + (sl.eval_price_rule || '')
      ))
      pairs.push(ratioPair(
        'cat_unit_' + bi + '_' + i,
        (c.name || '分类' + (i + 1)) + ' · 单价合计 ÷ 限价单价合计',
        { value: b.raw_sums?.[i], label: '未加权单价合计', dim: DIM.catUnit(i, taxDim) },
        { value: catCapUnit[i], label: '限价单价合计（∑限价）', dim: DIM.catUnit(i, taxDim) },
        '分子分母同为该类未加权单价合计，不含权重、不含数量'
      ))
    }
    // 全项目聚合：必须先把所有类别的加权值与加权限价分别求和，维度才一致
    pairs.push(ratioPair(
      'shortlist_project_' + bi, '全项目加权评标价 ÷ 全项目加权限价',
      { value: (b.eval_prices || []).reduce((s, v) => s + (Number(v) || 0), 0), label: '各类加权评标价之和', dim: DIM.projectWeighted(taxDim) },
      { value: cats.reduce((s, c) => s + (Number(c.cap_weighted) || 0), 0), label: '各类限价加权合计之和', dim: DIM.projectWeighted(taxDim) },
      '按类别逐项配对后再求和；缺类时该行不参与，避免虚假折扣'
    ))
    return { bidder: b.name, pairs }
  })
}

/**
 * 模式 B：开标记录表（单一报价 / 多区间 / 分项）
 * 该模式 AI 工具没有输出「限价」字段（PricePart 只有 min/avg/benchmark），
 * 因此**不能凭空算结算折扣率**；只给出相对评标基准价的对照比例，
 * 并明确标注「基准价 ≠ 最高限价」，避免把两者混为一谈。
 */
export function openingPairs(lot, bidder, tax) {
  if (!lot || !bidder) return { pairs: [], note: '' }
  const taxDim = String(lot.priced_by || '').includes('不含税') ? 'exc' : 'incl'
  const pairs = []
  if (lot.pricing === 'single') {
    const part = (lot.parts || [])[0]
    if (part && part.benchmark) {
      pairs.push(ratioPair(
        'opening_single_' + (bidder.seq || 0), '评标价 ÷ 评标基准价（对照，非折扣率）',
        { value: bidder.total, label: '评标价（' + (lot.priced_by || '报价') + '）', dim: DIM.projectTotal(taxDim) },
        { value: part.benchmark, label: '评标基准价（非最高限价）', dim: DIM.projectTotal(taxDim) },
        (lot.source_file || '开标记录表') + ' · ' + (lot.sheet || '')
      ))
    }
    return {
      pairs,
      note: '开标记录表未提供最高限价字段，无法计算「结算现价 ÷ 最高限价」；上表仅为相对评标基准价的对照比例，不得当作折扣率。',
    }
  }
  for (let i = 0; i < (lot.parts || []).length; i++) {
    const part = lot.parts[i]
    if (!part || !part.benchmark) continue
    const v = lot.use_net ? bidder.net_parts?.[i] : bidder.parts?.[i]
    pairs.push(ratioPair(
      'opening_part_' + (bidder.seq || 0) + '_' + i,
      (part.label || '分项' + (i + 1)) + ' · 分项报价 ÷ 分项基准价（对照）',
      { value: v, label: '分项报价（' + (lot.priced_by || '报价') + '）', dim: DIM.partTotal(i, taxDim) },
      { value: part.benchmark, label: '分项评标基准价（非最高限价）', dim: DIM.partTotal(i, taxDim) },
      (lot.source_file || '开标记录表') + ' · ' + (lot.sheet || '')
    ))
  }
  return {
    pairs,
    note: '开标记录表未提供分类最高限价，无法计算「单商品类汇总报价 ÷ 单商品类汇总最高限价」；上表仅为相对基准价的对照比例。',
  }
}

/** 对一组投标人的折扣率做横向一致性检查，结果给「数据质量」面板 */
export function summarizePairs(groups) {
  const issues = []
  let pairs = 0
  let rejected = 0
  for (const g of groups || []) {
    for (const p of g.pairs || []) {
      pairs++
      if (!p.consistent) rejected++
    }
    const ck = crossCheck((g.pairs || []).filter(p => p.key.startsWith('discount_')))
    for (const m of ck.issues) issues.push(g.bidder + '：' + m)
  }
  return { pairs, rejected, issues, consistent: rejected === 0 && issues.length === 0 }
}

export function discountOf(pair) {
  return pair && pair.consistent ? round(pair.ratio, 6) : null
}
