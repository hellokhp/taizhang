/* 模式 B · 开标记录表（单一报价 / 多区间 / 多品类分项）
   价格分来自 bidsummary/scoring.py 确定性代码；
   该模式 AI 工具未输出「最高限价」字段，因此折扣率只给相对评标基准价的对照比例，
   并明确标注「基准价 ≠ 最高限价」，绝不把对照比例冒充结算折扣率。 */
import {
  fmtMoney, fmtPct, fmtNum, fmtSigned, fmtScore, deviationPct,
  scoreCell, assertBaseline,
} from './currency.js'
import { openingPairs, summarizePairs } from './dimensions.js'

const CALIBER_BY_BEHAVIOR = {
  ratio: 'score.opening.ratio.v1',
  deviation: 'score.opening.deviation.v1',
  proportional: 'score.opening.ratio.v1',
  full_deviation: 'score.opening.full_deviation.v1',
  itemized_deviation: 'score.opening.itemized.v1',
}

export function select(raw) {
  const lots = raw?.lots
  return !!(Array.isArray(lots) && lots.length && lots.some(l => (l.rows || []).length))
}

export function adapt(summary) {
  const raw = typeof summary?.raw_result === 'string'
    ? (() => { try { return JSON.parse(summary.raw_result) } catch (e) { return {} } })()
    : (summary?.raw_result || {})
  const lots = raw.lots || []
  const rule = raw.rule || {}
  const behavior = String(rule.behavior || '')
  const caliber = CALIBER_BY_BEHAVIOR[behavior] || 'score.opening.ratio.v1'
  const ruleRef = (rule.source_file || '招标文件') + (rule.source_page ? ' P' + rule.source_page : '')

  // 多标段时压平成一张主表：每行带标段名，分组由 lot 决定
  const flat = []
  for (const lot of lots) {
    for (const r of (lot.rows || [])) flat.push({ lot, r })
  }
  const first = lots[0] || {}
  const parts = first.parts || []
  const multiLot = lots.length > 1
  const itemized = !!first.itemized
  const useNet = !!first.use_net
  const tax = useNet ? 'exc' : 'incl'
  const priceBasis = first.priced_by || (useNet ? '不含税单价合计' : '含税单价合计')

  const pairGroups = flat.map(({ lot, r }) => openingPairs(lot, r, tax))
  const quality = summarizePairs(pairGroups.map((g, i) => ({ bidder: flat[i].r.name, pairs: g.pairs })))

  const columns = [
    { key: 'rank', label: '排名', dtype: 'int', formatter: 'int', group: '开标结果', locked: true },
    { key: 'bidder_name', label: '投标单位', dtype: 'text', formatter: 'text', group: '开标结果', locked: true },
    { key: 'quote_primary', label: '评标价', sub: priceBasis, dtype: 'money', formatter: 'money', group: '开标结果' },
    { key: 'deviation', label: '偏差', sub: '相对评标基准价', dtype: 'pct', formatter: 'signed', group: '开标结果' },
    { key: 'total_score', label: '价格分', sub: '满分 ' + (rule.total_score ?? '—'), dtype: 'score', formatter: 'score', group: '开标结果', caliberKey: caliber },
    { key: 'status', label: '状态 / 标记', dtype: 'text', formatter: 'text', group: '开标结果' },
  ]
  if (multiLot) columns.splice(1, 0, { key: 'lot_name', label: '标段', dtype: 'text', formatter: 'text', group: '开标结果' })
  if (!itemized && parts.length > 1) {
    for (let i = 0; i < parts.length; i++) {
      columns.push({ key: 'part:' + i + ':quote', label: parts[i].label || '区间' + (i + 1), sub: priceBasis, dtype: 'money', formatter: 'money', group: '分项报价' })
    }
  }
  if (itemized) {
    for (let i = 0; i < parts.length; i++) {
      const c = first.part_labels?.[i] || parts[i].label || '分项' + (i + 1)
      columns.push(
        { key: 'part:' + i + ':net', label: c + ' 不含税报价', dtype: 'money', formatter: 'money', group: c },
        { key: 'part:' + i + ':incl', label: c + ' 含税报价', dtype: 'money', formatter: 'money', group: c + ' · 对照' },
        { key: 'part:' + i + ':score', label: c + ' 得分', sub: '满分 ' + (parts[i].score ?? '—'), dtype: 'score', formatter: 'score', group: c, caliberKey: caliber },
      )
    }
  }
  columns.push(
    { key: 'bench_ratio', label: '评标价 ÷ 评标基准价', sub: '对照比例，非折扣率', dtype: 'pct', formatter: 'pct4', group: '基准价对照' },
    { key: 'incl_total', label: '含税总价（对照）', dtype: 'money', formatter: 'money', group: '金额（元）' },
    { key: 'tax_rate', label: '税率', dtype: 'pct', formatter: 'pct2', group: '金额（元）' },
    { key: 'duration', label: '工期 / 服务期', dtype: 'text', formatter: 'text', group: '其他' },
  )

  const rows = flat.map(({ lot, r }, idx) => {
    const g = pairGroups[idx]
    const benchPair = g.pairs[0]
    const cell = {}
    cell.rank = { value: r.rank, display: String(r.rank ?? '—') }
    cell.bidder_name = { value: r.name, display: r.name }
    if (multiLot) cell.lot_name = { value: lot.lot_name, display: lot.lot_name || lot.sheet || '—' }
    cell.quote_primary = {
      value: r.total, display: fmtMoney(r.total),
      evidence: { file: lot.source_file, page: lot.sheet, quote: (lot.priced_by || priceBasis) + ' · 原表第 ' + ((r.src_row || 0) + 1) + ' 行' },
    }
    cell.deviation = {
      value: deviationPct(r.deviation, 'ratio'),
      display: fmtSigned(deviationPct(r.deviation, 'ratio')),
    }
    cell.total_score = {
      ...scoreCell(r.price_score, caliber, ruleRef + ' · ' + (rule.formula || '')),
      display: fmtScore(r.price_score),
      evidence: { file: lot.source_file, page: lot.sheet, quote: (lot.lot_name || '') + ' · 第 ' + ((r.src_row || 0) + 1) + ' 行' },
    }
    cell.status = { value: (r.flags || []).join('；') || r.status, display: (r.flags || []).join('；') || r.status || '正常' }
    if (!itemized && parts.length > 1) {
      for (let i = 0; i < parts.length; i++) {
        cell['part:' + i + ':quote'] = { value: r.parts?.[i] ?? null, display: fmtMoney(r.parts?.[i]) }
      }
    }
    if (itemized) {
      for (let i = 0; i < parts.length; i++) {
        const vals = r.net_parts?.length ? r.net_parts : (r.parts || [])
        cell['part:' + i + ':net'] = { value: vals?.[i] ?? null, display: fmtMoney(vals?.[i]) }
        cell['part:' + i + ':incl'] = { value: r.incl_parts?.[i] ?? null, display: fmtMoney(r.incl_parts?.[i]) }
        cell['part:' + i + ':score'] = {
          ...scoreCell(r.part_scores?.[i], caliber, ruleRef + ' · ' + (parts[i].label || '')),
          display: fmtScore(r.part_scores?.[i]),
        }
      }
    }
    cell.bench_ratio = {
      value: benchPair?.pct ?? null,
      display: benchPair?.consistent ? fmtPct(benchPair.pct, 4) : '—',
      consistent: benchPair?.consistent === true,
      pair: benchPair || null, reason: benchPair?.reason || '',
      formulaRef: '评标价 ÷ 评标基准价（基准价来自招标文件评审办法，非最高限价）',
    }
    cell.incl_total = { value: r.incl_total ?? r.total, display: fmtMoney(r.incl_total ?? r.total) }
    cell.tax_rate = { value: r.tax_rate != null ? Number(r.tax_rate) * 100 : null, display: fmtPct(r.tax_rate != null ? Number(r.tax_rate) * 100 : null, 2) }
    cell.duration = { value: r.duration, display: r.duration || '—' }
    return { bidderId: null, name: r.name, cells: cell }
  })

  const rules = [
    { label: '计价口径', value: priceBasis + '（' + (useNet ? '按不含税价评标' : '按含税价评标') + '）' },
    { label: '价格分算法', value: rule.formula || '—', ref: ruleRef },
    { label: '评标基准价', value: rule.benchmark || '—', ref: ruleRef },
    { label: '满分 / 基本分', value: (rule.total_score ?? '—') + ' 分 / ' + (rule.base_score ?? '—') + ' 分' },
    { label: '去高去低', value: (rule.trim_guard ?? '—') + ' 家门槛，去高 ' + (rule.trim_high ?? 0) + ' / 去低 ' + (rule.trim_low ?? 0) },
    { label: '分项满分', value: parts.map(p => (p.label || '分项') + ' ' + (p.score ?? '—')).join(' / ') },
    { label: '最高限价', value: 'AI 工具未从开标记录表解析出最高限价字段，故不计算结算折扣率；请以招标文件与评审结果为准。' },
    { label: '规则原文', value: (rule_text_of(raw) || '').slice(0, 3000), ref: ruleRef },
    { label: '计算来源', value: 'bidsummary/scoring.py 确定性代码；价格分不经模型估算' },
  ]

  const sideTables = []
  if (parts.length) {
    sideTables.push({
      id: 'parts',
      title: '各分项基准价与满分（对照）',
      columns: [{ key: 'label', label: '分项' }, { key: 'score', label: '满分' },
        { key: 'min', label: '最低报价' }, { key: 'avg', label: '平均报价' }, { key: 'bench', label: '评标基准价' }],
      rows: parts.map(p => ({
        label: p.label, score: p.score, min: fmtMoney(p.min_price),
        avg: fmtMoney(p.avg_price), bench: fmtMoney(p.benchmark),
      })),
    })
  }

  const vm = {
    ok: true,
    layoutId: itemized ? 'opening.itemized.v1' : 'opening.table.v1',
    mode: 'opening',
    title: '开标结果 · ' + (itemized ? '分类分项计分' : '开标记录表') + '价格评审排名',
    columns, rows, rules, sideTables,
    footnotes: [
      '价格分由确定性代码按招标文件规则计算，前端仅投影；公式与原文页码见 ⓘ。',
      '本模式未解析出最高限价，「评标价 ÷ 评标基准价」是对照比例，不是结算折扣率，不可与最高限价混用。',
      (pairGroups[0]?.note || ''),
      '金额单位：元；计价口径：' + priceBasis,
    ],
    quality: {
      ...quality,
      calibers: [caliber],
      checks: [
        { label: '价格分齐全', ok: rows.length > 0 && rows.every(r => r.cells.total_score.value !== null) },
        { label: '未把基准价当最高限价', ok: true },
      ],
    },
    raw: first,
  }
  vm.errors = assertBaseline(vm)
  return vm
}

function rule_text_of(raw) {
  return raw?.rule?.rule_text || raw?.rule?.formula || ''
}

export default { select, adapt }
