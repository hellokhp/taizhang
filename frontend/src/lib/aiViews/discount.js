/* 模式 D · 下浮率 / 折扣率报价
   价格分、基准价、排名全部来自 raw_result 里由 bidsummary/discount.py 算好的值；
   本模块只做投影 + 折扣率同维度配对 + 自洽校验，不重算价格分。 */
import {
  fmtMoney, fmtNum, fmtPct, fmtSigned, fmtScore, round,
  deviationPct, scoreCell, assertBaseline,
} from './currency.js'
import { discountPairs, summarizePairs } from './dimensions.js'

const CALIBER = {
  deduct: 'score.deduct.v1',
  deviation: 'score.deviation.v1',
}

export function select(raw) {
  const d = raw?.discount
  return !!(d && Array.isArray(d.bidders) && d.bidders.length)
}

export function adapt(summary) {
  const raw = typeof summary?.raw_result === 'string'
    ? (() => { try { return JSON.parse(summary.raw_result) } catch (e) { return {} } })()
    : (summary?.raw_result || {})
  const d = raw.discount || {}
  const rowsRaw = d.bidders || []
  const isRate = d.rate_kind === 'discount_rate'
  const caliber = CALIBER[d.score_kind] || CALIBER.deduct
  const ruleRef = (d.rule_source_file || '招标文件') + ' P' + (d.rule_source_page || '?')
  const formulaRef = ruleRef + ' · ' + (d.formula || '')
  const pairs = discountPairs(d, 'incl')
  const quality = summarizePairs(pairs)

  const columns = [
    { key: 'rank', label: '排名', sub: '', dtype: 'int', formatter: 'int', group: '开标结果', locked: true },
    { key: 'bidder_name', label: '投标单位', sub: '', dtype: 'text', formatter: 'text', group: '开标结果', locked: true, permGroup: '' },
    { key: 'quote_primary', label: isRate ? '评审价（折扣率）' : '评审价（折扣）', sub: isRate ? '报价折扣率 ÷ 100' : '1 − 下浮率 ÷ 100', dtype: 'num', formatter: 'num4', group: '开标结果', caliberKey: d.rate_kind },
    { key: 'deviation', label: '偏差', sub: '相对评标基准价', dtype: 'pct', formatter: 'signed', group: '开标结果' },
    { key: 'total_score', label: '价格分', sub: '满分 ' + (d.full_score ?? '—'), dtype: 'score', formatter: 'score', group: '开标结果', caliberKey: caliber },
    { key: 'status', label: '标记', sub: '', dtype: 'text', formatter: 'text', group: '开标结果' },
    { key: 'rate', label: isRate ? '报价折扣率' : '下浮率', sub: '开标一览表原文', dtype: 'pct', formatter: 'pct2', group: '报价与折扣' },
    { key: 'per_100', label: '每 100 元市场价结算', sub: '元', dtype: 'money', formatter: 'money', group: '报价与折扣' },
    { key: 'discount_project', label: '结算现价 ÷ 项目最高限价', sub: '同维度：含税·全项目·含预估量', dtype: 'pct', formatter: 'pct4', group: '折扣率（同维度）' },
    { key: 'discount_unit', label: '折算单价合计 ÷ 单价限价合计', sub: '同维度：含税·全项目·单价合计', dtype: 'pct', formatter: 'pct4', group: '折扣率（同维度）' },
    { key: 'unit_sum', label: '折算单价合计', sub: '元 · Σ(限价 × 折扣)，不含量', dtype: 'money', formatter: 'money', group: '金额（元）' },
    { key: 'est_total', label: '预估结算现价', sub: '元 · Σ(限价 × 预估量 × 折扣)', dtype: 'money', formatter: 'money', group: '金额（元）' },
    { key: 'in_benchmark', label: '参与基准价', sub: '剔除者仍参与评分', dtype: 'bool', formatter: 'bool', group: '报价与折扣' },
  ]

  const rows = rowsRaw.map((b, i) => {
    const p = pairs[i] || { pairs: [] }
    const dp = p.pairs.find(x => x.key.startsWith('discount_project_'))
    const du = p.pairs.find(x => x.key.startsWith('discount_unit_'))
    const flags = [b.in_benchmark ? '' : '基准价剔除']
      .concat(b.over_cap ? ['超最高折扣限价'] : [])
      .concat(b.flags || [])
      .filter(Boolean)
    return {
      bidderId: null,
      name: b.name,
      cells: {
        rank: { value: b.rank, display: String(b.rank ?? '—') },
        bidder_name: { value: b.name, display: b.name },
        quote_primary: {
          value: b.discount,
          display: fmtNum(b.discount, 4),
          evidence: { file: d.opening_file, page: '', quote: isRate ? '报价折扣率 ' + b.rate + '%' : '下浮率 ' + b.rate + '%' },
        },
        deviation: { value: deviationPct(b.deviation, 'pct'), display: fmtSigned(deviationPct(b.deviation, 'pct')) },
        total_score: {
          ...scoreCell(b.price_score, caliber, formulaRef),
          display: fmtScore(b.price_score),
          evidence: { file: d.rule_source_file, page: d.rule_source_page, quote: (d.formula || '').slice(0, 200) },
        },
        status: { value: flags.join('；'), display: flags.join('；') || '正常' },
        rate: { value: b.rate, display: fmtPct(b.rate, 2) },
        per_100: { value: b.per_100, display: fmtMoney(b.per_100) },
        discount_project: {
          value: dp?.pct ?? null,
          display: dp?.consistent ? fmtPct(dp.pct, 4) : '—',
          consistent: dp?.consistent === true,
          reason: dp?.reason || '',
          pair: dp || null,
          formulaRef: '同维度：预估结算现价 ÷ Σ(单价最高限价×预估量)',
        },
        discount_unit: {
          value: du?.pct ?? null,
          display: du?.consistent ? fmtPct(du.pct, 4) : '—',
          consistent: du?.consistent === true,
          reason: du?.reason || '',
          pair: du || null,
          formulaRef: '同维度：Σ(限价×折扣) ÷ Σ限价（均不含量）',
        },
        unit_sum: { value: b.unit_sum, display: fmtMoney(b.unit_sum) },
        est_total: { value: b.est_total, display: fmtMoney(b.est_total) },
        in_benchmark: { value: !!b.in_benchmark, display: b.in_benchmark ? '是' : '否（仍计分）' },
      },
    }
  })

  const capUnit = (d.products || []).reduce((s, p) => s + (Number(p.cap) || 0), 0)
  const rules = [
    { label: '报价口径', value: isRate ? '直接报折扣率；评审价 = 折扣率 ÷ 100' : (d.discount_def || '折扣 = 1 − 下浮率 ÷ 100') },
    { label: '严禁二次换算', value: isRate ? '报价折扣率不得再按「1 − 下浮率」换算（AI 工具 discount.py 明确约束）' : '折扣只换算一次：折扣 = 1 − 下浮率/100' },
    { label: '价格分公式', value: d.formula || '—', ref: formulaRef },
    { label: '评标基准价', value: d.benchmark != null ? fmtNum(d.benchmark, 6) + '（' + (d.benchmark_n ?? '—') + ' 家参与）' : '—', ref: d.benchmark_rule ? ruleRef + ' · ' + d.benchmark_rule : '' },
    { label: '满分 / 扣分系数', value: (d.full_score ?? '—') + ' 分；高于基准价每 1% 扣 ' + (d.above_deduct ?? '—') + ' 分，低于每 1% 扣 ' + (d.below_deduct ?? '—') + ' 分', ref: ruleRef },
    { label: '去高去低', value: '门槛 ' + (d.trim_guard ?? '—') + ' 家；去高 ' + (d.trim_high ?? 0) + ' / 去低 ' + (d.trim_low ?? 0) + '（被剔除者仍参与评分）' },
    { label: '最高限价（Σ限价×预估量）', value: fmtMoney(d.cap_total) + ' 元', ref: '招标文件写明 ' + fmtMoney(d.stated_cap) + ' 元' },
    { label: '单价限价合计（Σ限价，不含量）', value: fmtMoney(capUnit) + ' 元（' + (d.products || []).length + ' 项）', ref: '仅用于「折算单价合计 ÷ 单价限价合计」同维度对照' },
    { label: '结算价口径', value: d.settlement_def || d.unit_price_def || '结算金额 = 市场价格 × 折扣 × 数量' },
    { label: '规则原文', value: (d.rule_text || '').slice(0, 3000), ref: ruleRef },
    { label: '计算来源', value: 'bidsummary/discount.py 确定性代码；价格分不经模型估算' },
  ]

  const sideTables = []
  if ((d.products || []).length) {
    sideTables.push({
      id: 'products',
      title: '产品单价限价清单（' + d.products.length + ' 项）',
      columns: [
        { key: 'seq', label: '序号' }, { key: 'name', label: '产品' },
        { key: 'spec', label: '规格' }, { key: 'unit', label: '单位' },
        { key: 'cap', label: '单价限价（元）' }, { key: 'qty', label: '预估量' },
        { key: 'amount', label: '限价金额（元）' }, { key: 'source_page', label: '页码' },
      ],
      rows: d.products.map(p => ({
        seq: p.seq, name: p.name, spec: p.spec, unit: p.unit,
        cap: fmtMoney(p.cap), qty: p.qty == null ? '—' : String(p.qty),
        amount: fmtMoney((Number(p.cap) || 0) * (Number(p.qty) || 0)),
        source_page: p.source_page,
      })),
      total: 'Σ限价×预估量 = ' + fmtMoney(d.cap_total) + ' 元',
    })
  }
  if ((d.penalty_items || []).length) {
    sideTables.push({
      id: 'penalty_items', title: '偏离惩罚明细',
      columns: [{ key: 'seq', label: '序号' }, { key: 'name', label: '产品' },
        { key: 'bidder', label: '投标人' }, { key: 'price', label: '报价' },
        { key: 'avg', label: '均价' }, { key: 'kind', label: '判定' }],
      rows: d.penalty_items.map(x => ({ ...x, price: fmtMoney(x.price), avg: fmtMoney(x.avg) })),
    })
  }

  const vm = {
    ok: true,
    layoutId: 'discount.v1',
    mode: 'discount',
    title: isRate ? '开标结果 · 折扣率报价价格评审排名' : '开标结果 · 下浮率报价价格评审排名',
    columns, rows,
    rules, sideTables,
    footnotes: [
      '价格分由确定性代码按招标文件规则计算，前端仅投影；公式与原文页码见 ⓘ。',
      '折扣率仅在分子分母同维度（税口径/范围/量纲/单位全同）时计算，否则显示「—」并给出原因。',
      '金额单位：元。',
    ],
    quality: {
      ...quality,
      calibers: [caliber],
      checks: [
        { label: '价格分齐全', ok: rows.length > 0 && rows.every(r => r.cells.total_score.value !== null) },
        { label: '折扣率同维度', ok: quality.rejected === 0 },
        { label: 'Σ限价×预估量 与写明限价一致', ok: d.stated_cap == null || Math.abs(Number(d.cap_total) - Number(d.stated_cap)) < 1 },
      ],
    },
    raw: d,
  }
  vm.errors = assertBaseline(vm)
  return vm
}

export default { select, adapt }
