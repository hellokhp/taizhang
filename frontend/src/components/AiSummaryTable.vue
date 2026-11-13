<script setup>
import { ref, computed } from 'vue'
import { AI, aiCols, evOf, levelOf, reviewRows, reviewCount, editCell } from '../stores/aiSummary'
import { colsWithWidth, fmtCell } from '../lib/aiSummaryCols'

const props = defineProps({ hideEmpty: { type: Boolean, default: true } })
const showAll = ref(false)
const cols = computed(() => {
  const all = colsWithWidth(aiCols.value)
  if (!props.hideEmpty || showAll.value) return all
  // 原始字段抽屉：默认隐藏全空列，避免「满屏 —」
  return all.filter(c => c.key === 'bidder_name' || AI.bidders.some(b => {
    const e = evOf(b.id, c.key)
    const v = (e && e.value_text !== null && e.value_text !== undefined && e.value_text !== '')
      ? e.value_text : b[c.key]
    return v !== null && v !== undefined && v !== ''
  }))
})
const editing = ref(null)       // {id, key}
const draft = ref('')
const detail = ref(null)        // 来源追溯 / 冲突候选

function val(b, c) {
  const e = evOf(b.id, c.key)
  if (e && e.value_text !== null && e.value_text !== undefined) return e.value_text
  return b[c.key]
}
function cellCls(b, c) {
  const e = evOf(b.id, c.key)
  return 'ai-lv-' + levelOf(e) + (e && e.conflict ? ' ai-conflict' : '') + (e && e.manual ? ' ai-manual' : '')
}
function startEdit(b, c) {
  if (AI.summary?.status !== 'DRAFT') return
  editing.value = { id: b.id, key: c.key }
  draft.value = val(b, c) == null ? '' : String(val(b, c))
}
async function saveEdit(b, c) {
  if (!editing.value) return
  const v = draft.value
  editing.value = null
  if (String(val(b, c) ?? '') === v) return
  await editCell(b, c.key, v)
}
function showDetail(b, c) {
  const e = evOf(b.id, c.key)
  detail.value = { bidder: b, col: c, name: b.bidder_name, label: c.label, e }
}
function chooseCandidate(v) {
  const d = detail.value
  detail.value = null
  if (d && d.bidder && d.col) editCell(d.bidder, d.col.key, String(v ?? ''), '人工裁决多来源冲突')
}
</script>

<template>
  <div class="card ai-tbl-card">
    <div class="card-h">
      <h2>原始字段 · 复核</h2>
      <span class="hint">绿=高置信 · 黄=需复核 · 红=缺失或冲突；双击单元格可修正</span>
      <label class="imp-chk ai-chk"><input type="checkbox" v-model="showAll"><span>显示全部字段</span></label>
      <label class="imp-chk ai-chk"><input type="checkbox" v-model="AI.onlyReview"><span>只看需复核（{{ reviewCount }} 行）</span></label>
      <div class="spacer"></div>
      <span v-if="AI.summary?.status === 'CONFIRMED'" class="pill ai-pill-warn">已定稿 · 只读</span>
      <span v-else class="cnt">{{ reviewRows.length }} 家投标人</span>
    </div>
    <div class="ai-tbl-wrap">
      <table class="ai-tbl">
        <colgroup><col v-for="(c,i) in cols" :key="i" :style="{width:c.width}"></colgroup>
        <thead><tr><th v-for="c in cols" :key="c.key">{{ c.label }}</th></tr></thead>
        <tbody>
          <tr v-for="b in reviewRows" :key="b.id">
            <td v-for="c in cols" :key="c.key"
                :class="[cellCls(b,c), c.type === 'money' ? 'ai-num-cell' : '']"
                @dblclick="startEdit(b,c)">
              <template v-if="editing && editing.id===b.id && editing.key===c.key">
                <input class="ai-edit" v-model="draft" autofocus
                       @keydown.enter.prevent="saveEdit(b,c)" @keydown.esc="editing=null"
                       @blur="saveEdit(b,c)">
              </template>
              <template v-else>
                <span class="ai-val">{{ fmtCell(val(b,c), c.type) }}</span>
                <button class="ai-info" title="来源追溯 / 冲突候选" @click.stop="showDetail(b,c)">i</button>
                <span v-if="evOf(b.id,c.key)?.manual" class="ai-m-tag" title="人工修正">改</span>
              </template>
            </td>
          </tr>
          <tr v-if="!reviewRows.length"><td :colspan="cols.length" class="ai-empty">
            {{ AI.bidders.length ? '没有需要复核的行' : '暂无开标汇总数据，请先点「生成开标汇总」' }}
          </td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="detail" class="mask on" @click.self="detail=null">
      <div class="dlg ai-dlg">
        <div class="dlg-h"><b>{{ detail.name }} · {{ detail.label }}</b><button class="btn ghost" @click="detail=null">关闭</button></div>
        <div class="ai-dlg-body" v-if="detail.e">
          <div class="ai-kv"><span>采纳值</span><b>{{ detail.e.value_text ?? '—' }}</b></div>
          <div class="ai-kv"><span>置信度</span><b>{{ detail.e.confidence }}（{{ detail.e.level }}）</b></div>
          <div class="ai-kv"><span>来源文件</span><b>{{ detail.e.source_file || '—' }}</b></div>
          <div class="ai-kv"><span>页码 / 单元格</span><b>{{ detail.e.source_page || '—' }} {{ detail.e.source_cell || '' }}</b></div>
          <div class="ai-quote">原文片段：{{ detail.e.source_quote || '—' }}</div>
          <div v-if="detail.e.note" class="ai-kv"><span>备注</span><b>{{ detail.e.note }}</b></div>
          <template v-if="detail.e.conflict">
            <div class="ai-sub">多来源冲突，请选择采纳值：</div>
            <button v-for="(x,i) in (detail.e.candidates || [])" :key="i" class="btn ghost ai-cand"
                    @click="chooseCandidate(typeof x==='object' ? (x.value ?? x.v) : x)">
              {{ typeof x === 'object' ? JSON.stringify(x) : x }}
            </button>
          </template>
        </div>
        <div v-else class="ai-dlg-body">该单元格暂无抽取证据。</div>
      </div>
    </div>
  </div>
</template>
