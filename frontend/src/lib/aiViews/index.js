/* 开标结果 · 统一适配入口
   raw_result → view_model；不重算价格分、不改 raw_result、不落库。

   view_model = { ok, layoutId, mode, title, columns, rows, rules, sideTables,
                  footnotes, quality, errors }
   columns: [{ key,label,sub,dtype,formatter,group,locked,caliberKey,permGroup }]
   rows:    [{ bidderId, name, cells:{ key:{value,display,evidence,level,formulaRef,pair,reason} } }]
*/
import { assertBaseline } from './currency.js'
import discount from './discount.js'
import shortlist from './shortlist.js'
import opening from './opening.js'
import bidderFolder from './bidderFolder.js'

const ADAPTERS = [discount, shortlist, opening, bidderFolder]

export function rawOf(summary) {
  if (summary?.raw_result && typeof summary.raw_result === 'object') return summary.raw_result
  if (typeof summary?.raw_result === 'string') {
    try { return JSON.parse(summary.raw_result) } catch (e) { return {} }
  }
  return {}
}

export function selectAdapter(summary) {
  const raw = rawOf(summary)
  for (const a of ADAPTERS) {
    try { if (a.select(raw)) return a } catch (e) { /* 跳过坏 adapter */ }
  }
  return null
}

/** 兜底：任何未知口径都给一张只有锚点列的排名表，绝不退回满屏空值的「标准字段」 */
function fallback(summary) {
  const raw = rawOf(summary)
  const names = (raw?.discount?.bidders || raw?.shortlist?.bidders || []).map(b => b.name)
  const rows = names.map((n, i) => ({
    bidderId: null, name: n,
    cells: {
      rank: { value: i + 1, display: String(i + 1) },
      bidder_name: { value: n, display: n },
      quote_primary: { value: null, display: '—', reason: '未知口径，未投影到报价字段' },
      deviation: { value: null, display: '—', reason: '未知口径，未投影到偏差字段' },
      total_score: { value: null, display: '—', reason: '未知口径，未投影到价格分' },
      status: { value: '', display: '需人工配置布局' },
    },
  }))
  return {
    ok: false,
    layoutId: 'result.ranking.v1',
    mode: String(summary?.mode || 'unknown'),
    title: '开标结果 · 价格评审排名（基线）',
    columns: [
      { key: 'rank', label: '排名', dtype: 'int', formatter: 'int', group: '开标结果', locked: true },
      { key: 'bidder_name', label: '投标单位', dtype: 'text', formatter: 'text', group: '开标结果', locked: true },
      { key: 'quote_primary', label: '报价 / 下浮率 / 加权报价', dtype: 'text', formatter: 'text', group: '开标结果' },
      { key: 'deviation', label: '偏差', dtype: 'pct', formatter: 'signed', group: '开标结果' },
      { key: 'total_score', label: '价格分', dtype: 'score', formatter: 'score', group: '开标结果' },
      { key: 'status', label: '状态 / 标记', dtype: 'text', formatter: 'text', group: '开标结果' },
    ],
    rows,
    rules: [{ label: '提示', value: '本口径暂无专用视图，已展示可识别的价格评审结果；可在「列设置」中配置布局。' }],
    sideTables: [],
    footnotes: ['未知口径只展示基线锚点列，不猜测口径与公式。'],
    quality: { pairs: 0, rejected: 0, issues: [], consistent: false, calibers: [], checks: [] },
    errors: [],
    raw,
  }
}

/** 应用布局覆盖（用户/项目 diff），只改视图属性，不改数据、不重算任何值 */
export function applyViewSpec(vm, spec) {
  if (!vm || !spec) return vm
  const ov = spec.overrides || {}
  let cols = (vm.columns || []).map(c => ({ ...c }))
  if (Array.isArray(ov.hidden) && ov.hidden.length) {
    // locked 锚点列（排名/投标单位/报价/价格分等）任何用户偏好都不能删
    cols = cols.filter(c => c.locked || !ov.hidden.includes(c.key))
  }
  if (Array.isArray(ov.order) && ov.order.length) {
    const rank = new Map(ov.order.map((k, i) => [k, i]))
    cols.sort((a, b) => {
      const ra = rank.has(a.key) ? rank.get(a.key) : 1e6
      const rb = rank.has(b.key) ? rank.get(b.key) : 1e6
      return ra - rb
    })
  }
  cols = cols.map(c => ({
    ...c,
    label: ov.labels?.[c.key] || c.label,
    width: ov.widths?.[c.key] || c.width,
    group: ov.groups?.[c.key] || c.group,
  }))
  const hiddenGroups = new Set((vm.sideTables || []).filter(t => ov.side_tables?.hidden?.includes(t.id)).map(t => t.id))
  const sideTables = (vm.sideTables || []).filter(t => !hiddenGroups.has(t.id))
  const rules = ov.rules_panel?.length
    ? (vm.rules || []).filter(r => ov.rules_panel.includes(r.label))
    : (vm.rules || [])
  return { ...vm, columns: cols, sideTables, rules, appliedSpec: { layoutId: spec.layout_id || spec.layoutId, version: spec.version, baseVersion: spec.base_version, appliedPrefs: spec.appliedPrefs || [] } }
}

export function adapt(summary, viewSpec = null) {
  const a = selectAdapter(summary)
  let vm
  try {
    vm = a ? a.adapt(summary) : fallback(summary)
  } catch (e) {
    vm = fallback(summary)
    vm.errors = (vm.errors || []).concat('专用适配器异常，已回退基线：' + (e?.message || String(e)))
  }
  // 锚点列硬校验（布局也不能删）
  const errs = assertBaseline(vm)
  if (errs.length) {
    const base = fallback(summary)
    vm = { ...base, errors: errs.concat(base.errors || []) }
  }
  return applyViewSpec(vm, viewSpec)
}

export function listAdapters() {
  return ADAPTERS.map(a => a.adapt ? (a.default?.layoutId || a.name) : a.name)
}

export default { adapt, selectAdapter, rawOf, applyViewSpec }
