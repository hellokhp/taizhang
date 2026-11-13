/* ================================================================
   开标汇总 AI · 前端状态与 SSE 订阅（含轮询降级）
   - 生成只发一次请求，进度由 SSE 推送；Redis 不可用自动切 3 秒轮询
   - EventSource 不能带 Authorization 头，用 ?token= 传会话令牌
   - 三色置信度 / 只看需复核 / 单元格编辑 / 定稿 都在这里收口
   ================================================================ */
import { reactive, computed } from 'vue'
import * as XLSX from 'xlsx'
import { api, getToken } from '../api/client'
import { toast } from './bid'
import { parseDiscount } from '../lib/aiDiscount'
import { adapt as adaptView } from '../lib/aiViews/index.js'

export const AI = reactive({
  projects: [],              // 当前已加载的项目页（搜索重置、滚动累加）
  projectQuery: '',          // 当前搜索词
  projectTotal: 0,           // 服务端命中的总数（跨所有页）
  projectIndexed: 0,         // 服务端索引内项目总数（诊断/展示规模）
  projectHasMore: false,     // 还有下一页
  projectLoading: false,
  selectedProject: null,     // 当前选中项目快照（翻页/搜索后仍可显示）
  rid: '',
  tasks: [],
  task: null,
  summaryId: null,
  summary: null,
  bidders: [],
  evidence: [],
  files: [],
  templates: { PROJECT: [], BIDDER: [] },
  loading: false,
  generating: false,
  degraded: false,
  onlyReview: false,
  resultTab: 'auto',          // 兼容旧状态；v2 起排名表是唯一主视图
  lotFilter: '',
  viewSpec: null,             // 后端 resolve 合并后的布局（含用户 diff）
  contracts: [],              // 字段契约，列设置用
  layoutWarning: '',
  layoutBusy: false,
  showRawFields: false,       // 「原始字段 · 复核」抽屉默认收起
  showEmptyCols: false,       // 主表全空列默认隐藏
  err: '',
  msg: '本表由 AI 辅助生成，最终以评审结果为准',
})

let _es = null
let _poll = null
let _lastSeq = 0
let _errCount = 0

export const aiCols = computed(() => (AI.templates.BIDDER || []).map(f => ({
  key: f.key, label: f.label, type: f.type || 'text',
})))

/** 模式 D（下浮率/折扣）结构化结果视图模型；保留供旧组件/回归使用 */
export const discountVm = computed(() => parseDiscount(AI.summary))

/**
 * v2 唯一主视图：跨所有模式的「开标结果 · 价格评审排名」。
 * adapter 只投影 raw_result 里已算好的价格分与折扣率，绝不重算；
 * viewSpec 只改列属性（隐藏/排序/改名），不改任何数值。
 */
export const resultVm = computed(() => {
  if (!AI.summary) return null
  try {
    return adaptView(AI.summary, AI.viewSpec)
  } catch (e) {
    return { ok: false, title: '开标结果 · 价格评审排名', columns: [], rows: [],
             rules: [], sideTables: [], footnotes: [], quality: { checks: [], issues: [] },
             errors: ['视图适配异常：' + (e?.message || String(e))] }
  }
})

/** 后端布局 mode/variant 需要与 adapter 的选择口径一致 */
function specQueryOf(summary) {
  const raw = summary?.raw_result
  const d = typeof raw === 'string' ? (() => { try { return JSON.parse(raw) } catch (e) { return {} } })() : (raw || {})
  let variant = ''
  if (d?.shortlist?.variant) variant = String(d.shortlist.variant)
  else if (Array.isArray(d?.lots) && d.lots.length && d.lots[0].itemized) variant = 'itemized'
  return { rid: summary?.rid || AI.rid || '', mode: String(summary?.mode || ''), variant }
}

export const evMap = computed(() => {
  const m = {}
  for (const e of AI.evidence) {
    const k = (e.scope === 'PROJECT' ? 'P:' : 'B:' + (e.bidder_id || 0) + ':') + e.field_key
    if (!m[k] || e.manual) m[k] = e
  }
  return m
})

export function evOf(bidderId, key) {
  const b = evMap.value['B:' + bidderId + ':' + key]
  if (b) return b
  return evMap.value['P:' + key] || null
}

function levelOf(e) {
  if (!e) return 'red'
  if (e.conflict || e.value_text == null || e.value_text === '') return 'red'
  const c = Number(e.confidence || 0)
  return c >= 0.85 ? 'green' : (c >= 0.6 ? 'yellow' : 'red')
}
export { levelOf }

export const reviewRows = computed(() => {
  if (!AI.onlyReview) return AI.bidders
  return AI.bidders.filter(b => {
    for (const c of aiCols.value) {
      const e = evOf(b.id, c.key)
      const lv = levelOf(e)
      if (lv !== 'green' || (e && e.conflict)) return true
    }
    return false
  })
})

export const reviewCount = computed(() => AI.bidders.length - reviewRows.value.length)

/* ---------------- 项目 / 任务 ---------------- */
const PROJECT_PAGE = 50
let _projectSeq = 0            // 竞态序号：只接受最后一次请求的结果

/**
 * 服务端模糊搜索 + 分页加载项目。
 * @param {string} q      搜索词（多关键词空格分隔）；不变则用于翻页
 * @param {boolean} append true=在现有列表后追加下一页；false=重新搜索并重置
 */
export async function loadProjects(q = '', append = false) {
  const seq = ++_projectSeq
  const offset = append ? AI.projects.length : 0
  const query = append ? AI.projectQuery : String(q || '')
  if (!append) {
    AI.projectQuery = query
    AI.projects = []
    AI.projectHasMore = false
  }
  AI.projectLoading = true
  try {
    const r = await api('/api/ai-summary/projects?q=' + encodeURIComponent(query)
      + '&limit=' + PROJECT_PAGE + '&offset=' + offset)
    if (seq !== _projectSeq) return null            // 过期响应丢弃，避免覆盖新搜索
    const page = r.projects || []
    AI.projects = append ? AI.projects.concat(page) : page
    AI.projectTotal = r.total || 0
    AI.projectIndexed = r.indexed || 0
    AI.projectHasMore = !!r.hasMore
    if (!AI.rid && AI.projects.length) {
      const first = AI.projects.find(p => p.hasTenderDir) || AI.projects[0]
      AI.rid = first.rid
      AI.selectedProject = first
    }
    return r
  } catch (e) {
    if (seq === _projectSeq) AI.err = e.message
    return null
  } finally {
    if (seq === _projectSeq) AI.projectLoading = false
  }
}

/** 选中项目时记录快照：搜索/翻页把该 rid 挤出当前页后，仍可显示名称 */
export function setSelectedProject(p) { AI.selectedProject = p || null }

export async function loadTasks() {
  try {
    const r = await api('/api/ai-summary/tasks?limit=30' + (AI.rid ? '&rid=' + encodeURIComponent(AI.rid) : ''))
    AI.tasks = r.tasks || []
  } catch (e) { AI.err = e.message }
}

export async function loadSummary(id) {
  AI.loading = true
  try {
    const r = await api('/api/ai-summary/summaries/' + id)
    AI.summary = r.summary
    AI.bidders = r.bidders || []
    AI.evidence = r.evidence || []
    AI.files = r.files || []
    AI.templates = { PROJECT: [], BIDDER: [], ...(r.templates || {}) }
    if (!aiCols.value.length) {
      AI.templates.BIDDER = [
        { key: 'bidder_name', label: '投标人名称' },
        { key: 'bid_price', label: '投标报价（元）', type: 'money' },
        { key: 'duration', label: '工期/服务期' },
        { key: 'remark', label: '备注' },
      ]
    }
    AI.summaryId = id
    AI.viewSpec = null
    AI.showRawFields = false
    await loadViewSpec()
    await loadContracts()
  } catch (e) { toast(e.message) }
  AI.loading = false
}

/** 读时合并布局：失败不阻塞主视图，仅记一条 warning */
export async function loadViewSpec() {
  if (!AI.summary) return
  const q = specQueryOf(AI.summary)
  try {
    const r = await api('/api/ai-summary/layouts/resolve?rid=' + encodeURIComponent(q.rid)
      + '&mode=' + encodeURIComponent(q.mode) + '&variant=' + encodeURIComponent(q.variant))
    AI.viewSpec = r
    AI.layoutWarning = r.layoutWarning || ''
  } catch (e) {
    AI.viewSpec = null
    AI.layoutWarning = '布局服务不可用，已使用内置基线：' + e.message
  }
}

export async function loadContracts() {
  try {
    const r = await api('/api/ai-summary/field-contracts')
    AI.contracts = r.contracts || []
  } catch (e) { AI.contracts = [] }
}

/** 个人列偏好免 ai.layout；项目级/角色级需要权限（后端校验） */
export async function saveLayoutPref(overrides, scope = 'user') {
  AI.layoutBusy = true
  try {
    const layoutId = AI.viewSpec?.layout_id || resultVm.value?.layoutId || 'result.ranking.v1'
    const r = await api('/api/ai-summary/layout-prefs', {
      method: 'PUT',
      body: JSON.stringify({ scope, rid: AI.rid, layout_id: layoutId, overrides }),
    })
    if ((r.warnings || []).length) toast('部分定制已忽略：' + r.warnings.join('；'))
    await loadViewSpec()
    return r
  } catch (e) { toast('保存列设置失败：' + e.message); throw e }
  finally { AI.layoutBusy = false }
}

/* ---------------- 生成 + 进度 ---------------- */
function closeStream() {
  if (_es) { try { _es.close() } catch (e) {} _es = null }
  if (_poll) { clearInterval(_poll); _poll = null }
}

function startPolling(id) {
  AI.degraded = true
  closeStream()
  const tick = async () => {
    try {
      const t = await api('/api/ai-summary/tasks/' + id)
      AI.task = t
      if (t.summary_id) await loadSummary(t.summary_id)
      if (['DONE', 'PARTIAL', 'FAILED', 'CANCELED'].includes(t.status)) {
        closeStream(); AI.generating = false
        toast(t.status === 'FAILED' ? ('生成失败：' + (t.error_msg || '未知错误')) : '开标汇总已生成')
      }
    } catch (e) { /* 轮询偶发失败继续等 */ }
  }
  tick()
  _poll = setInterval(tick, 3000)
}

function startStream(id) {
  closeStream()
  _lastSeq = 0
  AI.degraded = false
  const qs = '?token=' + encodeURIComponent(getToken() || '') + '&last_seq=0'
  let es
  try { es = new EventSource('/api/ai-summary/tasks/' + id + '/stream' + qs) }
  catch (e) { startPolling(id); return }
  _es = es
  es.onmessage = async (ev) => {
    let d
    try { d = JSON.parse(ev.data) } catch (e) { return }
    if (ev.lastEventId) _lastSeq = Math.max(_lastSeq, Number(ev.lastEventId) || 0)
    if (d.type === 'degraded') { startPolling(id); return }
    if (d.type === 'progress') {
      // Worker 的进度 payload 除了 stage/progress，还带 current_file / parsed_files
      // / total_files。旧实现只合并三个字段，导致「当前：xxx 文件 2/3」永远不更新，
      // 用户只看到一个不动的百分比。
      const { type, ...rest } = d
      AI.task = { ...(AI.task || {}), ...rest }
      return
    }
    if (d.type === 'state') {
      AI.task = d
      if (d.summary_id && d.summary_id !== AI.summaryId) await loadSummary(d.summary_id)
      if (['DONE', 'PARTIAL', 'FAILED', 'CANCELED'].includes(d.status)) {
        closeStream(); AI.generating = false
        toast(d.status === 'FAILED' ? ('生成失败：' + (d.error_msg || '未知错误')) : '开标汇总已生成')
      }
    }
  }
  es.onerror = () => {
    _errCount++
    if (_errCount >= 2) { _errCount = 0; startPolling(id) }
  }
  es.addEventListener('open', () => { _errCount = 0 })
}

export async function generate(force = false) {
  if (!AI.rid) { toast('请先选择项目'); return }
  AI.generating = true
  AI.err = ''
  AI.task = null
  try {
    const r = await api('/api/ai-summary/generate', {
      method: 'POST', body: JSON.stringify({ rid: AI.rid, force_refresh: !!force }),
    })
    if (r.reused) {
      AI.generating = false
      toast('文件未变化，复用已有汇总结果')
      if (r.summary_id) await loadSummary(r.summary_id)
      await loadTasks()
      return r
    }
    AI.task = { id: r.task_id, status: 'PENDING', progress: 0, total_files: r.total_files }
    startStream(r.task_id)
    await loadTasks()
    return r
  } catch (e) {
    AI.generating = false
    toast(e.message)
    throw e
  }
}

export async function cancelTask() {
  if (!AI.task || !AI.task.id) return
  try { await api('/api/ai-summary/tasks/' + AI.task.id + '/cancel', { method: 'POST' }); toast('已请求取消') }
  catch (e) { toast(e.message) }
}

export async function retryTask() {
  await generate(true)
}

/* ---------------- 复核 / 定稿 ---------------- */
export async function editCell(bidder, key, value, reason = '') {
  try {
    await api('/api/ai-summary/summaries/' + AI.summaryId + '/cells', {
      method: 'PATCH',
      body: JSON.stringify({ entity: 'BIDDER', entity_id: bidder.id, field_key: key, value, reason }),
    })
    await loadSummary(AI.summaryId)
    toast('已保存，并沉淀为优化样本')
  } catch (e) { toast(e.message); throw e }
}

export async function confirm() {
  try {
    const r = await api('/api/ai-summary/summaries/' + AI.summaryId + '/confirm', {
      method: 'POST', body: JSON.stringify({ write_back: false }),
    })
    await loadSummary(AI.summaryId)
    toast('已定稿冻结，共 ' + (r.bidder_count || 0) + ' 家投标人')
  } catch (e) { toast(e.message) }
}

function _sheetName(s, used) {
  let n = String(s || 'Sheet').replace(/[\\/?*\[\]:]/g, '_').slice(0, 28)
  if (used.has(n)) { let i = 2; while (used.has(n + i)) i++; n = n + i }
  used.add(n)
  return n
}

/** 导出按 resolved columns 生成：隐藏的列不导，自定义列照导，不改任何数值 */
export async function exportXlsx() {
  try {
    const wb = XLSX.utils.book_new()
    const used = new Set()
    const vm = resultVm.value
    if (vm && (vm.columns || []).length) {
      const cols = visibleColumns(vm)
      XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
        cols.map(c => c.label),
        ...(vm.rows || []).map(r => cols.map(c => r.cells?.[c.key]?.display ?? '')),
      ]), _sheetName('开标结果', used))

      if ((vm.rules || []).length) {
        XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
          ['项目', '内容'],
          ...vm.rules.map(r => [r.label, r.value]),
        ]), _sheetName('评审规则', used))
      }
      // 计算口径与同维度配对明细：价格分公式、折扣率分子/分母与维度
      const cal = []
      for (const c of cols) {
        for (const r of (vm.rows || [])) {
          const cell = r.cells?.[c.key]
          if (!cell || (!cell.formulaRef && !cell.pair)) continue
          cal.push([r.name, c.label, cell.display ?? '', cell.formulaRef || '',
            cell.pair?.num?.label || '', String(cell.pair?.num?.value ?? ''),
            cell.pair?.den?.label || '', String(cell.pair?.den?.value ?? ''),
            cell.pair?.consistent === false ? '维度不一致，未计算' : '',
            cell.pair?.reason || cell.reason || ''])
        }
      }
      if (cal.length) {
        XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
          ['投标单位', '列', '值', '公式/口径', '分子', '分子值', '分母', '分母值', '状态', '说明'], ...cal,
        ]), _sheetName('计算口径', used))
      }
      for (const t of (vm.sideTables || [])) {
        XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
          (t.columns || []).map(c => c.label),
          ...(t.rows || []).map(r => (t.columns || []).map(c => r[c.key] ?? '')),
        ]), _sheetName(t.title, used))
      }
      const q = vm.quality || {}
      if ((q.checks || []).length || (q.issues || []).length) {
        XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([
          ['校验项', '结果', '说明'],
          ...(q.checks || []).map(c => [c.label, c.ok ? '通过' : '未通过', '']),
          ...(q.issues || []).map(i => ['维度/口径告警', '需复核', i]),
        ]), _sheetName('数据质量', used))
      }
    }
    // 原始字段 Sheet 始终保留（人工复核 / 修正后的值）
    const head = aiCols.value.map(c => c.label)
    const data = AI.bidders.map(b => aiCols.value.map(c => {
      const e = evOf(b.id, c.key)
      return e ? (e.value_text ?? '') : (b[c.key] ?? '')
    }))
    XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([head, ...data]),
      _sheetName('原始字段', used))
    XLSX.writeFile(wb, (AI.summary?.project_name || '开标汇总') + '.xlsx')
  } catch (e) { toast('导出失败：' + e.message) }
}

/** 主表列可见性：锚点 locked 恒定可见；全空非锚点列默认隐藏 */
export function visibleColumns(vm) {
  const cols = vm?.columns || []
  if (AI.showEmptyCols) return cols
  return cols.filter(c => c.locked || (vm.rows || []).some(r => {
    const cell = r.cells?.[c.key]
    if (!cell) return false
    const v = cell.display
    return v !== null && v !== undefined && v !== '' && v !== '—'
  }))
}

export function dispose() { closeStream() }

export function openTask(id) { startStream(id) }
