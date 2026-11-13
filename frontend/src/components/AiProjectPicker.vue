<script setup>
/* 开标汇总 · 可搜索项目选择器（服务端模糊搜索 + 分页）
   - 命中判定/排序/高亮区间全在后端 lib ai_project_index 完成，口径与 lib/fuzzy.js 一致
   - 组件只负责：防抖输入、分页加载、键盘导航、高亮渲染
   - v-model 绑定 rid；「未建招标文件目录」的项目仍可选（保留原语义，只是提示） */
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { AI, loadProjects, setSelectedProject } from '../stores/aiSummary'

const props = defineProps({
  modelValue: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'change'])

const root = ref(null)
const inputEl = ref(null)
const listEl = ref(null)
const open = ref(false)
const query = ref('')
const active = ref(0)
let debounce = null

const selected = computed(() => AI.selectedProject
  && AI.selectedProject.rid === props.modelValue ? AI.selectedProject
  : AI.projects.find(p => p.rid === props.modelValue) || null)
const label = computed(() => selected.value
  ? selected.value.name + (selected.value.hasTenderDir ? '' : '（未建招标文件目录）')
  : '— 选择项目 —')
const rows = computed(() => AI.projects)
const cap = computed(() => AI.projectTotal
  ? `${AI.projects.length} / ${AI.projectTotal}` : `${AI.projects.length}`)

watch(rows, () => { if (active.value >= rows.value.length) active.value = 0 })

function onDocClick(e) {
  if (open.value && root.value && !root.value.contains(e.target)) close()
}
function onDocKey(e) {
  if (open.value && e.key === 'Escape') { e.stopPropagation(); close() }
}
onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  document.addEventListener('keydown', onDocKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocClick)
  document.removeEventListener('keydown', onDocKey)
  if (debounce) clearTimeout(debounce)
})

function toggle() { if (props.disabled) return; open.value ? close() : show() }
function show() {
  if (props.disabled) return
  open.value = true
  query.value = AI.projectQuery || ''
  active.value = Math.max(0, rows.value.findIndex(p => p.rid === props.modelValue))
  // 首次打开且没有数据时加载第一页（项目页在 onMounted 已预取，这里兜底）
  if (!rows.value.length && !AI.projectLoading) loadProjects(query.value, false)
  nextTick(() => {
    if (inputEl.value) { inputEl.value.focus(); inputEl.value.select() }
    scrollActive()
  })
}
function close() { open.value = false; if (debounce) { clearTimeout(debounce); debounce = null } }

function choose(p) {
  if (!p) return
  setSelectedProject(p)
  if (p.rid !== props.modelValue) {
    emit('update:modelValue', p.rid)
    emit('change', p.rid)
  }
  close()
}
function clear(e) {
  if (e) { e.preventDefault(); e.stopPropagation() }
  setSelectedProject(null)
  if (props.modelValue) {
    emit('update:modelValue', '')
    emit('change', '')
  }
  close()
}

function onInput() {
  active.value = 0
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => {
    debounce = null
    loadProjects(query.value, false)     // 服务端搜索，重置分页
  }, 260)
}
function onKeydown(e) {
  if (!open.value) {
    if (['ArrowDown', 'Enter', ' '].includes(e.key)) { e.preventDefault(); show() }
    return
  }
  const n = rows.value.length
  if (e.key === 'ArrowDown') { e.preventDefault(); active.value = n ? (active.value + 1) % n : 0; scrollActive() }
  else if (e.key === 'ArrowUp') { e.preventDefault(); active.value = n ? (active.value - 1 + n) % n : 0; scrollActive() }
  else if (e.key === 'Enter') { e.preventDefault(); choose(rows.value[active.value]) }
  else if (e.key === 'Escape') { e.preventDefault(); close() }
  else if (e.key === 'Tab') { close() }
}
function onScroll(e) {
  if (!AI.projectHasMore || AI.projectLoading) return
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) {
    loadProjects(AI.projectQuery, true)  // 触底追加下一页
  }
}
function scrollActive() {
  const list = listEl.value
  const el = list && list.querySelector('.app-opt.active')
  if (!list || !el) return
  const lr = list.getBoundingClientRect()
  const er = el.getBoundingClientRect()
  if (er.top < lr.top) list.scrollTop -= (lr.top - er.top + 4)
  else if (er.bottom > lr.bottom) list.scrollTop += (er.bottom - lr.bottom + 4)
}
</script>

<template>
  <div ref="root" class="app-picker" :class="{ 'is-open': open, 'is-disabled': disabled }">
    <button type="button" class="app-picker-trigger" :disabled="disabled"
            :title="label" @click="toggle">
      <span class="app-picker-val" :class="{ 'is-ph': !selected }">{{ label }}</span>
      <span v-if="modelValue && !disabled" class="app-picker-x" title="清除选择" @click.stop="clear">×</span>
      <svg class="app-picker-arrow" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>

    <div v-if="open" class="app-pop">
      <div class="app-pop-h">
        <input ref="inputEl" v-model="query" type="text" class="app-pop-q"
               placeholder="输入项目名 / 编号 / 关键词，空格分隔多个词" @input="onInput" @keydown="onKeydown" />
        <span class="app-pop-cnt">{{ cap }}</span>
      </div>
      <div ref="listEl" class="app-pop-list" @scroll="onScroll">
        <div v-for="(p, i) in rows" :key="p.rid" class="app-opt"
             :class="{ active: i === active, sel: p.rid === modelValue }"
             @mouseenter="active = i" @click="choose(p)">
          <span class="app-opt-main">
            <span class="app-opt-name"><template v-for="(seg, k) in p.nameParts" :key="'n'+k"><em v-if="seg.hit">{{ seg.text }}</em><template v-else>{{ seg.text }}</template></template></span>
            <span class="app-opt-rid"><template v-for="(seg, k) in p.ridParts" :key="'r'+k"><em v-if="seg.hit">{{ seg.text }}</em><template v-else>{{ seg.text }}</template></template></span>
          </span>
          <span v-if="!p.hasTenderDir" class="app-opt-tag">未建招标文件目录</span>
          <svg v-if="p.rid === modelValue" class="app-opt-ok" viewBox="0 0 14 14" aria-hidden="true"><path d="M2.5 7.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div v-if="AI.projectLoading && !rows.length" class="app-pop-empty">加载中…</div>
        <div v-else-if="!rows.length" class="app-pop-empty">没有匹配的项目</div>
        <div v-if="AI.projectLoading && rows.length" class="app-pop-more">加载更多…</div>
        <div v-else-if="AI.projectHasMore" class="app-pop-more">下滑加载更多（共 {{ AI.projectTotal }} 项）</div>
      </div>
      <div class="app-pop-f">↑↓ 选择 · Enter 确认 · Esc 关闭 · 可在投标管理新建项目<template v-if="AI.projectIndexed"> · 索引 {{ AI.projectIndexed }} 项</template></div>
    </div>
  </div>
</template>
