<script setup>
/* 开标结果 · 价格评审排名 通用渲染器
   - 输入是 adapter 产出的 view_model，组件不解析文档、不重算价格分/折扣率
   - 列来自 resolve_layout（隐藏/排序/改名只影响视图，不改数值）
   - ⓘ 面板展示「值 + 公式/计分口径 + 同维度配对 + 原文证据」
*/
import { ref, computed } from 'vue'
import { AI, resultVm, visibleColumns, saveLayoutPref } from '../stores/aiSummary'
import { adapt as adaptView } from '../lib/aiViews/index.js'
import { dimText } from '../lib/aiViews/currency'

const props = defineProps({ vm: { type: Object, default: null } })

const showRules = ref(true)
const showQuality = ref(false)
const showSettings = ref(false)
const sideOpen = ref({})
const sideSearch = ref({})
const detail = ref(null)
const draftHidden = ref([])
const draftLabels = ref({})
const draftOrder = ref([])
const saving = ref(false)

const vm = computed(() => props.vm || resultVm.value || {})
const cols = computed(() => visibleColumns(vm.value))
const rows = computed(() => vm.value.rows || [])
const allCols = computed(() => {
  if (!AI.summary) return vm.value.columns || []
  try {
    const base = adaptView(AI.summary, null)
    return (base && base.columns && base.columns.length) ? base.columns : (vm.value.columns || [])
  } catch (e) { return vm.value.columns || [] }
})
const checks = computed(() => vm.value.quality?.checks || [])
const failedChecks = computed(() => checks.value.filter(c => !c.ok))
const issues = computed(() => vm.value.quality?.issues || [])
const calibers = computed(() => vm.value.quality?.calibers || [])
/** 规则面板拆两类：短指标进网格，长文本（规则原文等）单独成块，否则会把栅格撑坏 */
const SHORT_RULE_MAX = 60
const shortRules = computed(() => (vm.value.rules || [])
  .filter(r => String(r.value ?? '').length <= SHORT_RULE_MAX))
const longRules = computed(() => (vm.value.rules || [])
  .filter(r => String(r.value ?? '').length > SHORT_RULE_MAX))
/** ref 与 value 重复时不再展示，避免同一段公式出现两次 */
function showRef(r) {
  const ref = String(r.ref || '').trim()
  if (!ref) return false
  const val = String(r.value || '').trim()
  if (val && ref.includes(val)) return false
  return true
}
const meta = computed(() => AI.viewSpec?.meta || {})
const baseLabels = computed(() => Object.fromEntries(allCols.value.map(c => [c.key, c.label])))

function cellOf(r, c) { return r.cells?.[c.key] || null }
function displayOf(r, c) {
  const cell = cellOf(r, c)
  if (!cell) return '—'
  return cell.display === null || cell.display === undefined || cell.display === '' ? '—' : cell.display
}
function isNum(c) { return ['money', 'num', 'int', 'score', 'pct', 'bool'].includes(c.dtype) }
/** 列级 class：动态列 key 含冒号，需转义后才能当 CSS 钩子（不依赖列顺序） */
function colCls(c) { return 'ai-col-' + String(c.key).replace(/[^a-zA-Z0-9_]/g, '_') }
function hasInfo(cell) {
  return !!(cell && (cell.formulaRef || cell.pair || cell.evidence || cell.reason || cell.note || cell.caliberKey))
}
function openInfo(r, c) {
  const cell = cellOf(r, c)
  if (!cell) return
  detail.value = { name: r.name, label: c.label, caliberKey: c.caliberKey || cell.caliberKey || '', cell }
}
function cellCls(r, c) {
  const cell = cellOf(r, c)
  const out = []
  if (isNum(c)) out.push('ai-r-num')
  if (c.key === 'rank') out.push('ai-r-rank')
  if (c.key === 'bidder_name') out.push('ai-r-name')
  if (c.dtype === 'score') out.push('ai-r-score')
  if (cell && cell.consistent === false) out.push('ai-r-bad')
  if (cell && (cell.reason || (cell.pair && !cell.pair.consistent))) out.push('ai-r-warn')
  return out
}
function rowCls(r, i) {
  const rank = Number(r.cells?.rank?.value)
  return [rank === 1 || (rank !== rank && i === 0) ? 'ai-r-top' : '',
    String(r.cells?.status?.display || '').includes('剔除') ? 'ai-r-trim' : '']
}

/* ---------------- 列设置（个人 diff，免 ai.layout） ---------------- */
function openSettings() {
  const ov = AI.viewSpec?.overrides || {}
  draftHidden.value = [...(ov.hidden || [])].filter(k => !isLocked(k))
  draftLabels.value = { ...(ov.labels || {}) }
  const ordered = (vm.value.columns || []).map(c => c.key)
  for (const c of allCols.value) if (!ordered.includes(c.key)) ordered.push(c.key)
  draftOrder.value = ordered
  showSettings.value = true
}
function isLocked(key) { return !!allCols.value.find(c => c.key === key)?.locked }
function toggleHide(c) {
  if (c.locked) return
  const i = draftHidden.value.indexOf(c.key)
  if (i >= 0) draftHidden.value.splice(i, 1)
  else draftHidden.value.push(c.key)
}
function move(c, delta) {
  const i = draftOrder.value.indexOf(c.key)
  const j = i + delta
  if (i < 0 || j < 0 || j >= draftOrder.value.length) return
  const arr = draftOrder.value.slice()
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  draftOrder.value = arr
}
function labelDraft(c) { return draftLabels.value[c.key] ?? c.label }
function setLabel(c, v) {
  const next = { ...draftLabels.value }
  if (!v || v === baseLabels.value[c.key]) delete next[c.key]
  else next[c.key] = v
  draftLabels.value = next
}
async function saveSettings() {
  saving.value = true
  try {
    await saveLayoutPref({ hidden: draftHidden.value, labels: draftLabels.value, order: draftOrder.value })
    showSettings.value = false
  } catch (e) { /* toast 已在 store 里给 */ }
  finally { saving.value = false }
}
async function resetSettings() {
  saving.value = true
  try {
    await saveLayoutPref({ hidden: [], labels: {}, order: [] })
    openSettings()
  } catch (e) {}
  finally { saving.value = false }
}

/* ---------------- 副表（产品清单 / 惩罚明细 / 规则原文） ---------------- */
function sideRows(t) {
  const q = String(sideSearch.value[t.id] || '').trim().toLowerCase()
  const rs = t.rows || []
  if (!q) return rs
  return rs.filter(r => (t.columns || []).some(c => String(r[c.key] ?? '').toLowerCase().includes(q)))
}
function toggleSide(id) { sideOpen.value = { ...sideOpen.value, [id]: !sideOpen.value[id] } }
</script>

<template>
  <div class="ai-r">
    <div class="card ai-r-card">
      <div class="card-h">
        <h2>{{ vm.title || '开标结果 · 价格评审排名' }}</h2>
        <span class="pill ai-pill-live">{{ vm.mode || AI.summary?.mode || '—' }}</span>
        <span class="hint">{{ rows.length }} 家投标人 · {{ cols.length }} 列</span>
        <div class="spacer"></div>
        <span v-if="meta.baseVersion" class="hint">布局 {{ AI.viewSpec.layout_id }} · {{ meta.baseVersion }}</span>
        <button class="btn ghost" :class="{ on: AI.showEmptyCols }" @click="AI.showEmptyCols = !AI.showEmptyCols">
          {{ AI.showEmptyCols ? '隐藏空列' : '显示空列' }}
        </button>
        <button class="btn ghost" @click="openSettings()">列设置</button>
      </div>

      <div class="ai-r-bar">
        <span v-for="(c,i) in checks" :key="i" class="ai-r-check" :class="c.ok ? 'ok' : 'bad'">
          {{ c.ok ? '✓' : '!' }} {{ c.label }}
        </span>
        <span v-if="!checks.length" class="hint">价格分与折扣率均取自 AI 工具确定性计算结果</span>
        <button v-if="failedChecks.length || issues.length" class="ai-r-link" @click="showQuality = !showQuality">
          数据质量（{{ failedChecks.length }} 项未通过）
        </button>
        <span v-if="calibers.length" class="ai-r-caliber">计分口径：{{ calibers.join('、') }}</span>
      </div>

      <div v-if="AI.layoutWarning" class="ai-tip ai-tip-warn">{{ AI.layoutWarning }}</div>
      <div v-if="(vm.errors || []).length" class="ai-warns">
        <div v-for="(e,i) in vm.errors" :key="i">· {{ e }}</div>
      </div>
      <div v-if="showQuality" class="ai-r-quality">
        <div v-for="(c,i) in checks" :key="i">{{ c.ok ? '✓' : '✗' }} {{ c.label }}</div>
        <div v-for="(x,i) in issues" :key="'q'+i" class="ai-r-bad">· {{ x }}</div>
        <div v-if="!checks.length && !issues.length">暂无校验记录</div>
      </div>

      <div class="ai-r-wrap">
        <table class="ai-r-tbl">
          <thead>
            <tr>
              <th v-for="c in cols" :key="c.key" :class="colCls(c)"
                  :style="c.width ? { width: c.width + 'px' } : {}"
                  :title="c.sub || c.label">
                <div>{{ c.label }}<span v-if="c.locked" class="ai-r-lock" title="锚点列，不可隐藏">锁</span></div>
                <small v-if="c.sub">{{ c.sub }}</small>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r,i) in rows" :key="r.name + i" :class="rowCls(r, i)">
              <td v-for="c in cols" :key="c.key" :class="[cellCls(r, c), colCls(c)]">
                <span class="ai-r-val" :title="displayOf(r, c)">{{ displayOf(r, c) }}</span>
                <button v-if="hasInfo(cellOf(r,c))" class="ai-info" title="公式 / 口径 / 来源" @click="openInfo(r,c)">i</button>
                <span v-else-if="cellOf(r,c)?.consistent === false" class="ai-r-mark" :title="cellOf(r,c)?.reason || '维度不一致'">!</span>
              </td>
            </tr>
            <tr v-if="!rows.length"><td :colspan="Math.max(1, cols.length)" class="ai-empty">
              暂无开标汇总数据，请先点「生成开标汇总」
            </td></tr>
          </tbody>
        </table>
      </div>

      <div class="ai-r-foot" v-if="(vm.footnotes || []).length">
        <span v-for="(f,i) in vm.footnotes" :key="i">· {{ f }}</span>
      </div>
    </div>

    <!-- 评审规则与计分口径 -->
    <div v-if="(vm.rules || []).length" class="card ai-r-rules">
      <div class="card-h" @click="showRules = !showRules" style="cursor:pointer">
        <h2>评标办法与计分口径</h2><div class="spacer"></div>
        <span class="hint">{{ showRules ? '收起' : '展开' }}</span>
      </div>
      <div v-if="showRules">
        <div class="ai-r-rulegrid">
          <div v-for="(r,i) in shortRules" :key="'s'+i">
            <span>{{ r.label }}</span><b>{{ r.value }}</b>
            <small v-if="showRef(r)" class="ai-r-ru-ref">{{ r.ref }}</small>
          </div>
        </div>
        <div v-for="(r,i) in longRules" :key="'l'+i" class="ai-r-ruleblock">
          <span>{{ r.label }}</span>
          <p>{{ r.value }}</p>
          <small v-if="showRef(r)">{{ r.ref }}</small>
        </div>
      </div>
    </div>

    <!-- 折叠副表：产品清单 / 惩罚明细 / 规则原文 -->
    <div v-for="t in (vm.sideTables || [])" :key="t.id" class="card ai-r-side">
      <div class="card-h" @click="toggleSide(t.id)" style="cursor:pointer">
        <h2>{{ t.title }}</h2><span class="hint">{{ (t.rows || []).length }} 行</span>
        <div class="spacer"></div><span class="hint">{{ sideOpen[t.id] ? '收起' : '展开' }}</span>
      </div>
      <div v-if="sideOpen[t.id]">
        <input v-if="(t.rows || []).length > 8" class="ai-r-search" v-model="sideSearch[t.id]" placeholder="搜索…">
        <div class="ai-r-wrap" style="max-height:420px">
          <table class="ai-r-tbl ai-r-side-tbl">
            <thead><tr><th v-for="c in (t.columns || [])" :key="c.key">{{ c.label }}</th></tr></thead>
            <tbody>
              <tr v-for="(r,i) in sideRows(t)" :key="i">
                <td v-for="c in (t.columns || [])" :key="c.key">{{ r[c.key] ?? '—' }}</td>
              </tr>
              <tr v-if="!sideRows(t).length"><td :colspan="(t.columns||[]).length" class="ai-empty">无匹配数据</td></tr>
            </tbody>
          </table>
        </div>
        <div v-if="t.total" class="ai-r-foot">{{ t.total }}</div>
      </div>
    </div>

    <!-- 单元格口径追溯 -->
    <div v-if="detail" class="mask on" @click.self="detail = null">
      <div class="dlg ai-dlg">
        <div class="dlg-h"><b>{{ detail.name }} · {{ detail.label }}</b><button class="btn ghost" @click="detail=null">关闭</button></div>
        <div class="ai-dlg-body">
          <div class="ai-kv"><span>当前值</span><b>{{ detail.cell.display ?? '—' }}</b></div>
          <div class="ai-kv" v-if="detail.caliberKey"><span>计分口径</span><b>{{ detail.caliberKey }}</b></div>
          <div class="ai-kv" v-if="detail.cell.source"><span>数据来源</span><b>{{ detail.cell.source }}</b></div>
          <div v-if="detail.cell.formulaRef" class="ai-quote">公式 / 口径：{{ detail.cell.formulaRef }}</div>
          <template v-if="detail.cell.pair">
            <div class="ai-sub">同维度配对校验</div>
            <div class="ai-kv"><span>分子</span><b>{{ detail.cell.pair.num?.label }} = {{ detail.cell.pair.num?.value ?? '—' }}</b></div>
            <div class="ai-kv"><span>分子维度</span><b>{{ dimText(detail.cell.pair.num?.dim) }}</b></div>
            <div class="ai-kv"><span>分母</span><b>{{ detail.cell.pair.den?.label }} = {{ detail.cell.pair.den?.value ?? '—' }}</b></div>
            <div class="ai-kv"><span>分母维度</span><b>{{ dimText(detail.cell.pair.den?.dim) }}</b></div>
            <div class="ai-kv"><span>结果</span><b>{{ detail.cell.pair.consistent ? '维度一致，可计算' : '维度不一致，拒绝计算' }}</b></div>
          </template>
          <div v-if="detail.cell.evidence" class="ai-quote">
            来源：{{ detail.cell.evidence.file || '—' }} {{ detail.cell.evidence.page ? 'P' + detail.cell.evidence.page : '' }}<br>
            原文：{{ detail.cell.evidence.quote || '—' }}
          </div>
          <div v-if="detail.cell.reason || detail.cell.pair?.reason" class="ai-warns">
            {{ detail.cell.reason || detail.cell.pair?.reason }}
          </div>
          <div v-if="detail.cell.note" class="ai-kv"><span>备注</span><b>{{ detail.cell.note }}</b></div>
        </div>
      </div>
    </div>

    <!-- 列设置：只写 diff，锚点列锁定 -->
    <div v-if="showSettings" class="mask on" @click.self="showSettings = false">
      <div class="dlg ai-dlg ai-r-settings">
        <div class="dlg-h"><b>列设置 · {{ AI.viewSpec?.layout_id || vm.layoutId }}</b>
          <button class="btn ghost" @click="showSettings=false">关闭</button></div>
        <div class="ai-dlg-body">
          <p class="hint">个人偏好只保存差异；系统布局升版后会自动继承新列。带「锁」的锚点列不可隐藏。</p>
          <div class="ai-r-setlist">
            <div v-for="(c,i) in allCols" :key="c.key" class="ai-r-setrow">
              <label class="imp-chk">
                <input type="checkbox" :checked="!draftHidden.includes(c.key)" :disabled="c.locked" @change="toggleHide(c)">
                <span>{{ c.label }}</span>
              </label>
              <input class="ai-r-setlabel" :value="labelDraft(c)" @input="setLabel(c, $event.target.value)" placeholder="显示名">
              <span v-if="c.locked" class="ai-r-lock">锁</span>
              <button class="btn ghost ai-r-mv" :disabled="i===0" @click="move(c,-1)">↑</button>
              <button class="btn ghost ai-r-mv" :disabled="i===allCols.length-1" @click="move(c,1)">↓</button>
            </div>
          </div>
        </div>
        <div class="dlg-f">
          <button class="btn ghost" :disabled="saving" @click="resetSettings()">恢复系统默认</button>
          <div class="spacer"></div>
          <button class="btn pri" :disabled="saving" @click="saveSettings()">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
