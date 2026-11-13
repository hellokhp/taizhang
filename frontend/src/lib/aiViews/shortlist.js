/* 模式 C（aggregate）/ 模式 E（per_item）· 加权单价矩阵
   价格分全部来自 raw_result（sheetquote.py / shortlist.py 确定性代码）。
   折扣率按「单商品类汇总报价 ÷ 单商品类汇总最高限价」逐类同维度配对。 */
import {
  fmtMoney, fmtPct, fmtNum, fmtScore, round, scoreCell, assertBaseline,
} from './currency.js'
import { shortlistPairs, summarizePairs, taxOf } from './dimensions.js'

const CALIBER = {
  aggregate: 'score.shortlist.aggregate.v1',
  per_item: 'score.shortlist.per_item.v1',
}

export function select(raw) {
  const s = raw?.shortlist
  return !!(s && Array.isArray(s.categories) && s.categories.length && Array.isArray(s.bidders) && s.bidders.length)
}

export function adapt(summary) {
  const raw = typeof summary?.raw_result === 'string'
    ? (() => { try { return JSON.parse(summary.raw_result) } catch (e) { return {} } })()
    : (summary?.raw_result || {})
  const sl = raw.shortlist || {}
  const cats = sl.categories || []
  const rowsRaw = sl.bidders || []
  const variant = sl.variant === 'per_item' ? 'per_item' : 'aggregate'
  const perItem = variant === 'per_item'
  const tax = taxOf(sl.price_basis)
  const caliber = CALIBER[variant]
  const ruleRef = (sl.rule_source_file || '招标文件') + ' P' + (sl.rule_source_page || '?')
  const pairs = shortlistPairs(sl, tax)
  const quality = summarizePairs(pairs)

  const columns = [
    { key: 'rank', label: '排名', dtype: 'int', formatter: 'int', group: '开标结果', locked: true },
    { key: 'bidder_name', label: '投标单位', dtype: 'text', formatter: 'text', group: '开标结果', locked: true },
    { key: 'quote_primary', label: '加权评标价合计', sub: '∑(单价×权重)，全项目', dtype: 'money', formatter: 'money', group: '开标结果' },
    { key: 'deviation', label: '综合偏差', sub: '该口径按分类基准价逐类计分，无全项目统一基准价', dtype: 'pct', formatter: 'signed', group: '开标结果', optional: true },
    { key: 'total_score', label: '价格分', sub: '满分 ' + (sl.full_score ?? '—'), dtype: 'score', formatter: 'score', group: '开标结果', caliberKey: caliber },
    { key: 'status', label: '状态 / 标记', dtype: 'text', formatter: 'text', group: '开标结果' },
    { key: 'quoted', label: '报价产品数', sub: '共 ' + cats.reduce((s, c) => s + (c.products || []).length, 0) + ' 项', dtype: 'int', formatter: 'int', group: '报价完整度' },
    { key: 'missing', label: '缺报产品数', dtype: 'int', formatter: 'int', group: '报价完整度' },
    { key: 'over_cap_n', label: '超限价产品数', dtype: 'int', formatter: 'int', group: '报价完整度' },
  ]

  // 动态列：每个商品类 5 列，key 稳定为 cat:<i>:<metric>，显示名与 key 解耦
  for (let i = 0; i < cats.length; i++) {
    const c = cats[i]
    const nm = c.name || '分类' + (i + 1)
    columns.push(
      { key: 'cat:' + i + ':eval_price', label: nm + ' 加权评标价', sub: '∑单价×权重', dtype: 'money', formatter: 'money', group: nm },
      { key: 'cat:' + i + ':cap_ratio', label: nm + ' 占限价比例', sub: '同维度：加权↔加权限价', dtype: 'pct', formatter: 'pct4', group: nm },
      { key: 'cat:' + i + ':discount', label: nm + ' 分类折扣率', sub: '单价合计↔限价单价合计', dtype: 'pct', formatter: 'pct4', group: nm },
      { key: 'cat:' + i + ':raw_sum', label: nm + ' 未加权单价合计', sub: '元（对照）', dtype: 'money', formatter: 'money', group: nm },
      { key: 'cat:' + i + ':score', label: nm + ' 得分', sub: '满分 ' + (c.score ?? '—'), dtype: 'score', formatter: 'score', group: nm, caliberKey: caliber },
    )
  }
  if (perItem) {
    columns.push(
      { key: 'penalty_low', label: '偏离惩罚·偏低项', sub: '该项不得分', dtype: 'int', formatter: 'int', group: '异常与惩罚' },
      { key: 'penalty_high', label: '偏离惩罚·偏高项', sub: '该项不得分', dtype: 'int', formatter: 'int', group: '异常与惩罚' },
    )
  }

  const rows = rowsRaw.map((b, bi) => {
    const g = pairs[bi] || { pairs: [] }
    const byKey = (suffix, i) => g.pairs.find(p => p.key === suffix + bi + '_' + i)
    const projectPair = g.pairs.find(p => p.key === 'shortlist_project_' + bi)
    const cell = {}
    cell.rank = { value: b.rank, display: String(b.rank ?? '—') }
    cell.bidder_name = { value: b.name, display: b.name }
    cell.quote_primary = {
      value: (b.eval_prices || []).reduce((s, v) => s + (Number(v) || 0), 0),
      display: fmtMoney((b.eval_prices || []).reduce((s, v) => s + (Number(v) || 0), 0)),
      consistent: projectPair?.consistent === true,
      pair: projectPair || null,
      reason: projectPair?.reason || '',
    }
    cell.deviation = {
      value: null, display: '—',
      reason: '该评分口径按各商品类独立基准价计分，AI 工具未定义全项目统一基准价，故不给出综合偏差率（避免跨类误算）',
    }
    cell.total_score = {
      ...scoreCell(b.total_score, caliber, ruleRef + ' · ' + (sl.formula || sl.eval_price_rule || '')),
      display: fmtScore(b.total_score),
      evidence: { file: b.source_file || sl.rule_source_file, page: sl.rule_source_page, quote: (sl.formula || sl.eval_price_rule || '').slice(0, 200) },
    }
    cell.status = { value: (b.flags || []).join('；'), display: (b.flags || []).join('；') || '正常' }
    cell.quoted = { value: b.quoted, display: String(b.quoted ?? '—') }
    cell.missing = { value: (b.missing || []).length, display: String((b.missing || []).length) + ' 项' }
    cell.over_cap_n = { value: (b.over_cap || []).length, display: String((b.over_cap || []).length) + ' 项' }
    if (perItem) {
      cell.penalty_low = { value: b.penalty_low, display: String(b.penalty_low ?? 0) }
      cell.penalty_high = { value: b.penalty_high, display: String(b.penalty_high ?? 0) }
    }
    for (let i = 0; i < cats.length; i++) {
      const c = cats[i]
      const cw = byKey('cat_weighted_', i)
      const cu = byKey('cat_unit_', i)
      cell['cat:' + i + ':eval_price'] = {
        value: b.eval_prices?.[i] ?? null, display: fmtMoney(b.eval_prices?.[i]),
        pair: cw || null, reason: cw?.reason || '',
      }
      cell['cat:' + i + ':cap_ratio'] = {
        value: cw?.pct ?? null,
        display: cw?.consistent ? fmtPct(cw.pct, 4) : '—',
        consistent: cw?.consistent === true,
        pair: cw || null, reason: cw?.reason || '',
        formulaRef: '加权评标价 ÷ 限价加权合计（同类·同税口径·同权重）',
      }
      cell['cat:' + i + ':discount'] = {
        value: cu?.pct ?? null,
        display: cu?.consistent ? fmtPct(cu.pct, 4) : '—',
        consistent: cu?.consistent === true,
        pair: cu || null, reason: cu?.reason || '',
        formulaRef: '未加权单价合计 ÷ 限价单价合计（同类·同税口径）',
      }
      cell['cat:' + i + ':raw_sum'] = { value: b.raw_sums?.[i] ?? null, display: fmtMoney(b.raw_sums?.[i]) }
      cell['cat:' + i + ':score'] = {
        ...scoreCell(b.scores?.[i], caliber, ruleRef + ' · ' + (sl.formula || '')),
        display: fmtScore(b.scores?.[i]),
        evidence: { file: b.source_file || sl.rule_source_file, page: sl.rule_source_page, quote: (c.name || '') + ' · 满分 ' + (c.score ?? '—') },
      }
    }
    return { bidderId: null, name: b.name, cells: cell }
  })

  const rules = [
    { label: '计价口径', value: sl.price_basis || '未标注', ref: sl.tax_rule ? '原文：' + sl.tax_rule : '' },
    { label: '价格分算法', value: perItem
        ? '逐项得分 =（该项最低价 ÷ 本家单价）× 分项满分；总分 = Σ逐项得分（偏离项不得分）'
        : (sl.formula || '基准价 ÷ 评标价 × 类别分值'),
      ref: ruleRef },
    { label: '评标价构成', value: sl.eval_price_rule || '∑各产品投标单价 × 权重', ref: ruleRef },
    { label: '基准价取法', value: sl.benchmark_rule || '各类别取有效投标人评标价最低值', ref: ruleRef },
    { label: '价格分满分', value: (sl.full_score ?? '—') + ' 分；' + cats.map(c => (c.name || '') + ' ' + (c.score ?? '—')).join(' / ') },
    { label: '偏离惩罚阈值', value: perItem ? '低于均价 ' + fmtPct(sl.deviation_low * 100, 2) + ' / 不低于均价 ' + fmtPct(sl.deviation_high * 100, 2) : '不适用' },
    { label: '规则原文', value: (sl.rule_text || '').slice(0, 3000), ref: ruleRef },
    { label: '计算来源', value: perItem ? 'bidsummary/sheetquote.py 确定性代码' : 'bidsummary/shortlist.py 确定性代码；价格分不经模型估算' },
  ]

  const sideTables = [{
    id: 'products',
    title: '产品清单与权重 · 分类限价（' + cats.reduce((s, c) => s + (c.products || []).length, 0) + ' 项）',
    columns: [
      { key: 'cat', label: '商品类' }, { key: 'seq', label: '序号' },
      { key: 'name', label: '产品' }, { key: 'spec', label: '规格' },
      { key: 'unit', label: '单位' }, { key: 'cap', label: '单价限价（元）' },
      { key: 'weight', label: '权重' }, { key: 'cat_bench', label: '该类基准价' },
      { key: 'cat_capw', label: '该类限价加权合计' },
    ],
    rows: cats.flatMap((c, ci) => (c.products || []).map(p => ({
      cat: c.name || '分类' + (ci + 1), seq: p.seq, name: p.name, spec: p.spec,
      unit: p.unit, cap: fmtMoney(p.cap), weight: fmtPct(p.weight, 2),
      cat_bench: fmtMoney(c.benchmark), cat_capw: fmtMoney(c.cap_weighted),
    }))),
    total: '各类限价加权合计 = ' + fmtMoney(cats.reduce((s, c) => s + (Number(c.cap_weighted) || 0), 0)) + ' 元',
  }]
  if ((sl.penalty_items || []).length) {
    sideTables.push({
      id: 'penalty_items', title: '偏离惩罚明细',
      columns: [{ key: 'seq', label: '序号' }, { key: 'name', label: '产品' },
        { key: 'bidder', label: '投标人' }, { key: 'price', label: '报价' },
        { key: 'avg', label: '均价' }, { key: 'ratio', label: '比值' }, { key: 'kind', label: '判定' }],
      rows: sl.penalty_items.map(x => ({
        ...x, price: fmtMoney(x.price), avg: fmtMoney(x.avg),
        ratio: x.ratio == null ? '—' : fmtPct(Number(x.ratio) * 100, 2),
      })),
    })
  }

  const vm = {
    ok: true,
    layoutId: perItem ? 'shortlist.per_item.v1' : 'shortlist.aggregate.v1',
    mode: 'shortlist',
    variant,
    title: '开标结果 · ' + (perItem ? '逐项加权最低价' : '分类加权评标价') + '价格评审排名',
    columns, rows, rules, sideTables,
    footnotes: [
      '价格分由确定性代码按招标文件规则计算，前端仅投影；公式与原文页码见 ⓘ。',
      '分类折扣率按「单商品类汇总报价 ÷ 单商品类汇总最高限价」逐类同维度计算；缺报/超限价/缺限价时显示「—」。',
      '综合偏差率在该口径下无统一基准价，刻意留空，避免跨类误算。',
      '金额单位：元；计价口径：' + (sl.price_basis || '未标注'),
    ],
    quality: {
      ...quality,
      calibers: [caliber],
      checks: [
        { label: '价格分齐全', ok: rows.length > 0 && rows.every(r => r.cells.total_score.value !== null) },
        { label: '分类限价齐全', ok: cats.every(c => c.cap_weighted != null) },
        { label: '折扣率同维度', ok: quality.rejected === 0 },
      ],
    },
    raw: sl,
  }
  vm.errors = assertBaseline(vm)
  return vm
}

export default { select, adapt }
