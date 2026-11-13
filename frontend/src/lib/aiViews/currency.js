/* ================================================================
   开标结果 · 计算内核（纯函数，无 Vue 依赖）

   铁律（用户 2026-11-12 明确要求）：
   1. 价格分一律取 AI 工具「确定性代码」已算好的值（raw_result），
     前端只投影、只校验、只格式化，绝不重算、绝不猜公式。
   2. 折扣率必须同维度配对：税口径 / 范围 / 量纲 / 单位 四项全同才相除。
     任一不同 → ratio = null + reason，宁可显示「—」也不算错。

   维度四元组：
     tax      含税 incl / 不含税 exc / 未标注 unknown
     scope    全项目 project / 某类别 cat:<i> / 某分项 part:<i>
     measure  含预估量的总额 total_with_qty / 单价合计 unit_sum /
              加权合计 weighted / 每百元基数 per_100_base
     unit     元 CNY / 无量纲 RATIO

   例：
     est_total ÷ cap_total          → 元·含税·全项目·含预估量 ÷ 同 → OK（结算折扣）
     eval_prices[i] ÷ cap_weighted[i] → 元·含税·类别i·加权 ÷ 同 → OK（分类折扣）
     raw_sums[i] ÷ cap_unit[i]       → 元·含税·类别i·单价合计 ÷ 同 → OK（分类单价折扣）
     分类报价 ÷ 全项目最高限价        → scope 不同 → 拒绝，返回 null + 原因
     不含税报价 ÷ 含税限价            → tax 不同 → 拒绝，返回 null + 原因
   ================================================================ */

export const DIM = {
  projectTotal: (tax) => ({ tax, scope: 'project', measure: 'total_with_qty', unit: 'CNY' }),
  projectUnit: (tax) => ({ tax, scope: 'project', measure: 'unit_sum', unit: 'CNY' }),
  catWeighted: (i, tax) => ({ tax, scope: 'cat:' + i, measure: 'weighted', unit: 'CNY' }),
  catUnit: (i, tax) => ({ tax, scope: 'cat:' + i, measure: 'unit_sum', unit: 'CNY' }),
  projectWeighted: (tax) => ({ tax, scope: 'project', measure: 'weighted', unit: 'CNY' }),
  partTotal: (i, tax) => ({ tax, scope: 'part:' + i, measure: 'total_with_qty', unit: 'CNY' }),
  ratio: () => ({ tax: 'n/a', scope: 'n/a', measure: 'ratio', unit: 'RATIO' }),
}

const DIM_LABEL = {
  tax: { incl: '含税', exc: '不含税', unknown: '税口径未标注', 'n/a': '比例' },
  measure: {
    total_with_qty: '金额（含预估量）', unit_sum: '单价合计',
    weighted: '加权合计（单价×权重）', ratio: '比例',
  },
  scope: (s) => (s === 'project' ? '全项目' : s.startsWith('cat:') ? '分类 ' + (Number(s.slice(4)) + 1) : s.startsWith('part:') ? '分项 ' + (Number(s.slice(5)) + 1) : s),
}

export function dimText(d) {
  if (!d) return '未知'
  const tax = DIM_LABEL.tax[d.tax] || d.tax
  const scope = DIM_LABEL.scope(d.scope)
  const measure = DIM_LABEL.measure[d.measure] || d.measure
  return [tax, scope, measure, d.unit].join(' · ')
}

export function sameDim(a, b) {
  if (!a || !b) return false
  return a.tax === b.tax && a.scope === b.scope &&
    a.measure === b.measure && a.unit === b.unit
}

function num(v) {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null
  const n = typeof v === 'number' ? v : Number(String(v).replace(/,/g, '').replace(/元/g, '').trim())
  return Number.isFinite(n) ? n : null
}

export function sum(values) {
  let s = 0
  let has = false
  for (const v of values) {
    const n = num(v)
    if (n !== null) { s += n; has = true }
  }
  return has ? s : null
}

export function round(v, n = 2) {
  const x = num(v)
  if (x === null) return null
  const p = Math.pow(10, n)
  return Math.round((x + Number.EPSILON) * p) / p
}

/**
 * 同维度折扣率配对。分子分母维度不同时绝不计算。
 * @returns {key,label,num:{value,label,dim},den:{...},ratio,pct,consistent,reason,formulaRef}
 */
export function ratioPair(key, label, numerator, denominator, formulaRef = '') {
  const n = num(numerator.value)
  const d = num(denominator.value)
  const out = {
    key, label,
    num: { value: n, label: numerator.label, dim: numerator.dim, path: numerator.path || '' },
    den: { value: d, label: denominator.label, dim: denominator.dim, path: denominator.path || '' },
    ratio: null, pct: null, consistent: false, reason: '', formulaRef,
  }
  if (n === null) { out.reason = '分子缺失或不合法：' + numerator.label; return out }
  if (d === null) { out.reason = '分母缺失或不合法：' + denominator.label; return out }
  if (d === 0) { out.reason = '分母为 0（无可比分母）'; return out }
  if (!sameDim(numerator.dim, denominator.dim)) {
    out.reason = '维度不一致，拒绝计算：分子[' + dimText(numerator.dim) +
      '] ≠ 分母[' + dimText(denominator.dim) + ']'
    return out
  }
  out.consistent = true
  out.ratio = round(n / d, 8)
  out.pct = round(n / d * 100, 4)
  return out
}

/** 自洽校验：同一口径下多个独立推导的折扣率必须相等，否则数据可疑 */
export function crossCheck(pairs, tolerance = 1e-6) {
  const ok = pairs.filter(p => p && p.consistent && p.ratio !== null && p.ratio > 0)
  const issues = []
  if (ok.length < 2) return { consistent: ok.length > 0, issues }
  const base = ok[0]
  for (const p of ok.slice(1)) {
    if (Math.abs(p.ratio - base.ratio) > tolerance) {
      issues.push(base.label + ' = ' + (base.ratio * 100).toFixed(4) +
        '% 与 ' + p.label + ' = ' + (p.ratio * 100).toFixed(4) + '% 不一致，请核对限价与报价口径')
    }
  }
  return { consistent: issues.length === 0, issues }
}

/* ---------------------------------------------------------------- 价格分 */

/**
 * 从 raw_result 投影已算好的价格分，附口径与公式出处；缺失时说明原因，不重算。
 * @param value AI 工具算出的分数
 * @param caliberKey 口径键（score.deduct.v1 等）
 * @param formulaRef 公式与原文页码
 */
export function scoreCell(value, caliberKey, formulaRef = '', note = '') {
  const v = num(value)
  return {
    value: v,
    caliberKey: caliberKey || 'unknown',
    formulaRef: formulaRef || '',
    note: note || (v === null ? '该口径未能算出价格分，请以招标文件与评审结果为准' : ''),
    source: 'raw_result（确定性代码计算，非模型估算）',
  }
}

/** 偏差率统一成「百分点」展示。不同模式的原始单位不同，必须由 adapter 显式声明。 */
export function deviationPct(value, unit = 'pct') {
  const v = num(value)
  if (v === null) return null
  return round(unit === 'ratio' ? v * 100 : v, 4)
}

/** 锚点列断言：任何模式都必须能给出排名主表的 6 个锚点（缺失也算给出，标 null + 原因） */
export const ANCHOR_KEYS = ['rank', 'bidder_name', 'quote_primary', 'deviation', 'total_score', 'status']

export function assertBaseline(viewModel) {
  const errs = []
  if (!viewModel || !Array.isArray(viewModel.columns)) return ['view_model 无 columns']
  for (const k of ANCHOR_KEYS) {
    if (!viewModel.columns.some(c => c.key === k)) errs.push('缺锚点列 ' + k)
  }
  if (!Array.isArray(viewModel.rows)) errs.push('view_model 无 rows')
  return errs
}

/* ---------------------------------------------------------------- 格式化 */

export function fmtMoney(v, dash = '—') {
  const n = num(v)
  if (n === null) return dash
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
export function fmtPct(v, digits = 2, dash = '—') {
  const n = num(v)
  if (n === null) return dash
  return n.toFixed(digits) + '%'
}
export function fmtNum(v, digits = 4, dash = '—') {
  const n = num(v)
  if (n === null) return dash
  return n.toFixed(digits)
}
export function fmtSigned(v, digits = 2, dash = '—') {
  const n = num(v)
  if (n === null) return dash
  return (n > 0 ? '+' : '') + n.toFixed(digits) + '%'
}
export function fmtScore(v, dash = '—') {
  const n = num(v)
  if (n === null) return dash
  return n.toFixed(2)
}
