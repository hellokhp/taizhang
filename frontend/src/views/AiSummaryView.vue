<script setup>
import { onMounted, onUnmounted, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { toast } from '../stores/bid'
import { can } from '../stores/perm'
import AiTaskProgress from '../components/AiTaskProgress.vue'
import AiSummaryTable from '../components/AiSummaryTable.vue'
import AiResultTable from '../components/AiResultTable.vue'
import AiProjectPicker from '../components/AiProjectPicker.vue'
import {
  AI, loadProjects, loadTasks, loadSummary, generate, confirm, exportXlsx,
  openTask, dispose, reviewCount, resultVm,
} from '../stores/aiSummary'
import { fmtTime, fmtCell } from '../lib/aiSummaryCols'

const router = useRouter()
const canGen = computed(() => can('ai.generate'))
const canConfirm = computed(() => can('ai.confirm'))
const curProject = computed(() => (AI.selectedProject && AI.selectedProject.rid === AI.rid)
  ? AI.selectedProject : (AI.projects.find(p => p.rid === AI.rid) || null))
const warnings = computed(() => {
  const w = AI.summary?.warnings
  if (!w) return []
  return Array.isArray(w) ? w : (() => { try { return JSON.parse(w) } catch (e) { return [String(w)] } })()
})
const zeroTable = computed(() => warnings.value.some(w => String(w).includes('0 个可解析开标表')))
const failedFiles = computed(() => AI.files.filter(f => f.status === 'FAILED' || f.status === 'SKIPPED'))
const budgetText = computed(() => AI.summary?.budget == null ? '—' : fmtCell(AI.summary.budget, 'money') + ' 元')
/** 概况压成一行；关键指标从 adapter 的规则面板取，跨口径通用 */
const headKpis = computed(() => (resultVm.value?.rules || [])
  .filter(r => r.value !== null && r.value !== undefined && String(r.value).length <= 60)
  .slice(0, 8).map(r => ({ label: r.label, value: String(r.value) })))

async function onRidChange() {
  AI.summary = null; AI.summaryId = null; AI.bidders = []; AI.task = null
  AI.viewSpec = null; AI.showRawFields = false
  await loadTasks()
  const done = AI.tasks.find(t => t.summary_id && ['DONE', 'PARTIAL'].includes(t.status))
  if (done) await loadSummary(done.summary_id)
}
function gotoFiles() { router.push('/files') }
async function doConfirm() {
  if (reviewCount.value && !window.confirm(`仍有 ${reviewCount.value} 行含需复核项，确认定稿？定稿后不可修改。`)) return
  await confirm()
}
async function openTaskRow(t) {
  if (t.summary_id) await loadSummary(t.summary_id)
  if (!['DONE', 'PARTIAL', 'FAILED', 'CANCELED'].includes(t.status)) openTask(t.id)
}
onMounted(async () => { await loadProjects(); await loadTasks(); await onRidChange() })
onUnmounted(dispose)
</script>

<template>
  <div class="ai-page">
    <div class="card">
      <div class="card-h">
        <h2>开标汇总（AI）</h2>
        <span class="hint">针对项目，读取其「招标文件」目录生成结构化开标汇总，数据统一存 PostgreSQL</span>
      </div>
      <div class="toolbar tbar ai-tbar">
        <AiProjectPicker v-model="AI.rid" :disabled="AI.generating" @change="onRidChange" />
        <button class="btn pri" :disabled="!canGen || AI.generating || !AI.rid" @click="generate(false)">
          {{ AI.generating ? '生成中…' : '生成开标汇总' }}
        </button>
        <button class="btn ghost" :disabled="!canGen || AI.generating || !AI.rid" title="文件变化后强制重跑，忽略缓存" @click="generate(true)">强制重跑</button>
        <span class="tb-sep"></span>
        <button class="btn ghost" :disabled="!AI.summary" @click="exportXlsx()">导出 Excel</button>
        <button class="btn ghost" :disabled="!canConfirm || !AI.summary || AI.summary.status === 'CONFIRMED'" @click="doConfirm()">定稿</button>
        <span class="cnt">{{ AI.msg }}</span>
      </div>
      <div v-if="curProject && !curProject.hasTenderDir" class="ai-tip ai-tip-warn">
        该项目尚未建立「招标文件」目录。请先在投标管理中打开该项目，或到文件存储页上传招标文件。
      </div>
    </div>

    <AiTaskProgress />

    <div v-if="zeroTable" class="ai-tip ai-tip-warn">
      检测到 0 个可解析开标表，请确认开标一览表是否放在「招标文件」目录。
      <button class="btn ghost" @click="gotoFiles()">去文件存储页</button>
    </div>

    <div v-if="AI.summary" class="card ai-head-card">
      <div class="card-h"><h2>项目概况</h2><span class="hint">模式 {{ AI.summary.mode }} · 任务 #{{ AI.summary.task_id }}</span>
        <div class="spacer"></div>
        <span class="pill" :class="AI.summary.status==='CONFIRMED'?'ai-pill-warn':'ai-pill-live'">{{ AI.summary.status }}</span>
      </div>
      <div class="ai-head-inline">
        <span><i>项目</i>{{ AI.summary.project_name || '—' }}</span>
        <span><i>编号</i>{{ AI.summary.project_code || '—' }}</span>
        <span><i>招标人</i>{{ AI.summary.tenderer || '—' }}</span>
        <span><i>代理</i>{{ AI.summary.agency || '—' }}</span>
        <span><i>开标</i>{{ AI.summary.open_time_raw || '—' }}</span>
        <span><i>地点</i>{{ AI.summary.open_place || '—' }}</span>
        <span><i>最高限价</i>{{ budgetText }}</span>
        <span><i>评标办法</i>{{ AI.summary.eval_method || '—' }}</span>
        <span><i>投标人</i>{{ AI.summary.bidder_count ?? AI.bidders.length }} 家</span>
      </div>
      <div v-if="headKpis.length" class="ai-head-kpis">
        <span v-for="(k,i) in headKpis" :key="i">{{ k.label }} <b>{{ k.value }}</b></span>
      </div>
      <div v-if="warnings.length" class="ai-warns">
        <div v-for="(w,i) in warnings" :key="i">· {{ w }}</div>
      </div>
    </div>

    <!-- 唯一主视图：跨所有模式的「开标结果 · 价格评审排名」 -->
    <AiResultTable v-if="AI.summary" :vm="resultVm" />

    <!-- 原始字段降级为默认收起的复核抽屉：编辑 / 冲突裁决 / 证据追溯仍需入口 -->
    <details v-if="AI.summary" class="ai-raw-drawer" :open="AI.showRawFields"
             @toggle="AI.showRawFields = $event.target.open">
      <summary>
        <b>原始字段 · 复核</b>
        <span class="hint">单元格编辑、多来源冲突裁决、证据追溯、定稿前核对（默认隐藏全空列）</span>
      </summary>
      <AiSummaryTable />
    </details>

    <div v-if="failedFiles.length" class="card ai-fail-card">
      <div class="card-h"><h2>失败 / 跳过文件</h2><span class="hint">单文件失败不影响整体结果</span></div>
      <div v-for="f in failedFiles" :key="f.id" class="ai-fail-row">
        <b>{{ f.rel_path || f.name }}</b><span>{{ f.status }}</span><span>{{ f.error_msg || '—' }}</span>
      </div>
    </div>

    <div class="card ai-hist">
      <div class="card-h"><h2>历史任务</h2><button class="btn ghost" @click="loadTasks()">刷新</button></div>
      <!-- 不复用 .tbl-wrap：它的全局 table-layout:fixed + tbody td{white-space:nowrap}
           会把「阶段」长文本顶进「进度」列。这里用专属 wrapper + colgroup + 两行截断。 -->
      <div class="ai-hist-wrap">
        <table class="ai-hist-tbl">
          <colgroup>
            <col style="width:72px">
            <col style="width:104px">
            <col>
            <col style="width:176px">
            <col style="width:92px">
            <col style="width:150px">
          </colgroup>
          <thead><tr><th>任务</th><th>状态</th><th>阶段</th><th>进度</th><th>投标人</th><th>创建时间</th></tr></thead>
          <tbody>
            <tr v-for="t in AI.tasks" :key="t.id" class="ai-hist-row" @click="openTaskRow(t)">
              <td>#{{ t.id }}</td>
              <td><span class="pill" :class="t.status==='FAILED'?'ai-pill-bad':'ai-pill-live'">{{ t.status }}</span></td>
              <td class="ai-hist-stage"><span class="ai-hist-stage-txt" :title="t.stage_label || t.stage || '—'">{{ t.stage_label || t.stage || '—' }}</span></td>
              <td class="ai-hist-progress">{{ t.progress || 0 }}% · {{ t.parsed_files || 0 }}/{{ t.total_files || 0 }} 文件</td>
              <td>{{ t.summary_id ? '已生成' : '—' }}</td>
              <td class="ai-hist-time">{{ fmtTime(t.created_at) }}</td>
            </tr>
            <tr v-if="!AI.tasks.length"><td colspan="6" class="ai-empty">暂无任务</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
