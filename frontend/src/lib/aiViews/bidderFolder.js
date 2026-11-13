/* 模式 A · 按投标人文件夹解析（兜底视图）
   没有开标结果表时也给出排名主表骨架，绝不静默出空表。 */
import { fmtMoney, fmtScore, scoreCell, assertBaseline } from './currency.js'

export function select(raw) {
  return !!(raw?.rows && raw.rows.length && !raw?.lots && !raw?.discount && !raw?.shortlist)
}

export function adapt(summary) {
  const raw = typeof summary?.raw_result === 'string'
    ? (() => { try { return JSON.parse(summary.raw_result) } catch (e) { return {} } })()
    : (summary?.raw_result || {})
  const rowsRaw = raw.rows || []
  const v = (row, key) => row.fields?.[key]?.value ?? null
  const columns = [
    { key: 'rank', label: '排名', dtype: 'int', formatter: 'int', group: '开标结果', locked: true },
    { key: 'bidder_name', label: '投标单位', dtype: 'text', formatter: 'text', group: '开标结果', locked: true },
    { key: 'quote_primary', label: '投标报价', sub: '元', dtype: 'money', formatter: 'money', group: '开标结果' },
    { key: 'deviation', label: '偏差', dtype: 'pct', formatter: 'signed', group: '开标结果' },
    { key: 'total_score', label: '价格分', dtype: 'score', formatter: 'score', group: '开标结果' },
    { key: 'status', label: '状态 / 标记', dtype: 'text', formatter: 'text', group: '开标结果' },
    { key: 'duration', label: '工期 / 服务期', dtype: 'text', formatter: 'text', group: '其他' },
    { key: 'deposit', label: '投标保证金', dtype: 'money', formatter: 'money', group: '金额（元）' },
    { key: 'qualification', label: '资格响应', dtype: 'text', formatter: 'text', group: '其他' },
  ]
  const rows = rowsRaw.map((r, i) => ({
    bidderId: null,
    name: r.bidder || '（未识别）',
    cells: {
      rank: { value: i + 1, display: String(i + 1) },
      bidder_name: { value: r.bidder, display: r.bidder || '（未识别）' },
      quote_primary: { value: v(r, 'bid_price'), display: fmtMoney(v(r, 'bid_price')) },
      deviation: { value: null, display: '—', reason: '该模式未提供评标基准价，无偏差率' },
      total_score: { ...scoreCell(v(r, 'price_score'), 'score.unknown.v1'), display: fmtScore(v(r, 'price_score')) },
      status: { value: (r.flags || []).join('；'), display: (r.flags || []).join('；') || '正常' },
      duration: { value: v(r, 'duration'), display: v(r, 'duration') || '—' },
      deposit: { value: v(r, 'deposit'), display: fmtMoney(v(r, 'deposit')) },
      qualification: { value: v(r, 'qualification'), display: v(r, 'qualification') || '—' },
    },
  }))
  const vm = {
    ok: true,
    layoutId: 'bidder_folder.v1',
    mode: 'bidder_folder',
    title: '开标结果 · 投标文件解析排名（原始字段）',
    columns, rows,
    rules: [{ label: '计算来源', value: '该模式只有投标文件字段解析，未识别到开标一览表，无法计算价格分与折扣率' }],
    sideTables: [],
    footnotes: ['本口径未识别到开标结果表，只提供原始字段排名骨架；请确认开标一览表是否放在「招标文件」目录。'],
    quality: { pairs: 0, rejected: 0, issues: [], consistent: false, calibers: [], checks: [] },
    raw,
  }
  vm.errors = assertBaseline(vm)
  return vm
}

export default { select, adapt }
