<script setup>
/* ================================================================
   全局「上传任务」悬浮栏
   · 常驻：上传完成后不自动消失，直到手动清空 / 隐藏（解决「传完就找不到队列」）
   · 可拖动：按住标题栏拖动，位置写入 localStorage，带视口边界约束
   · 可折叠：整体收成一行胶囊；单条任务可再展开文件明细
   · 不绑定任何页面/弹窗生命周期：切页、关窗，任务与进度都在
   ================================================================ */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { LIVE, liveTasks } from '../../lib/fsUploader'
import { humanSize } from '../../lib/fsPerm'

const POS_KEY = 'wb_upload_dock_pos';
const W = 340, H_EST = 240;
const box = ref(null);
const tick = ref(0);
const collapsed = ref(false);
const hidden = ref(false);            // 用户主动隐藏整个栏；新批次到来会自动重现
const dismissed = reactive({});       // 单条任务隐藏
const taskOpen = reactive({});        // 单条任务文件明细展开
const pos = ref({ x: 0, y: 0, ready: false });
const dragging = ref(false);
let dragStart = null;
let justDragged = false;
const lastSeq = {};                  // 队列 _seq 变化 = 来了新的一批文件

/* ---------------- 位置：读取 / 边界约束 / 持久化 ---------------- */
function clamp(x, y){
  const w = box.value ? box.value.offsetWidth : W;
  const h = box.value ? box.value.offsetHeight : H_EST;
  const maxX = Math.max(8, window.innerWidth - w - 8);
  const maxY = Math.max(8, window.innerHeight - h - 8);
  return { x: Math.min(Math.max(8, x), maxX), y: Math.min(Math.max(8, y), maxY) };
}
function loadPos(){
  let p = null;
  try{ p = JSON.parse(localStorage.getItem(POS_KEY) || 'null'); }catch(e){}
  const base = (p && Number.isFinite(p.x) && Number.isFinite(p.y))
    ? clamp(p.x, p.y)
    : clamp(window.innerWidth - W - 18, window.innerHeight - H_EST - 18);
  pos.value = { x: base.x, y: base.y, ready: true };
}
function savePos(){
  try{ localStorage.setItem(POS_KEY, JSON.stringify({ x: pos.value.x, y: pos.value.y })); }catch(e){}
}
function onResize(){
  if(!pos.value.ready) return;
  const p = clamp(pos.value.x, pos.value.y);
  pos.value = { x: p.x, y: p.y, ready: true };
}

/* ---------------- 拖动：标题栏即手柄，点击按钮不触发拖动 ---------------- */
function onDown(e){
  if(e.button !== 0) return;
  if(e.target && e.target.closest && e.target.closest('button')) return;
  dragStart = { mx: e.clientX, my: e.clientY, x: pos.value.x, y: pos.value.y, moved: false };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}
function onMove(e){
  if(!dragStart) return;
  const dx = e.clientX - dragStart.mx, dy = e.clientY - dragStart.my;
  if(!dragStart.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
  dragStart.moved = true;
  dragging.value = true;
  const p = clamp(dragStart.x + dx, dragStart.y + dy);
  pos.value = { x: p.x, y: p.y, ready: true };
  e.preventDefault();
}
function onUp(){
  window.removeEventListener('mousemove', onMove);
  window.removeEventListener('mouseup', onUp);
  const moved = !!(dragStart && dragStart.moved);
  if(moved) savePos();
  dragStart = null;
  dragging.value = false;
  justDragged = moved;
  setTimeout(() => { justDragged = false; }, 0);   // 让本次 click 先被忽略
}
function setCollapsed(v){
  collapsed.value = v;
  nextTick(onResize);                              // 宽度变了，重新贴边约束
}
function toggleCollapse(){ if(!justDragged) setCollapsed(!collapsed.value); }

/* ---------------- 常驻：完成后保留，只有手动清空/隐藏才消失 ---------------- */
const timer = setInterval(() => {
  for(let i = LIVE.length - 1; i >= 0; i--){
    if(!LIVE[i].items.length) LIVE.splice(i, 1);  // 空队列不占位、不留内存
  }
  liveTasks().forEach(q => {
    const seq = q._seq || 0;
    if(seq > (lastSeq[q.id] || 0)){               // 新的一批文件
      lastSeq[q.id] = seq;
      hidden.value = false;                      // 之前隐藏过也重新出现
      if(dismissed[q.id]) delete dismissed[q.id];
    }
  });
  tick.value++;
}, 400);

const rows = computed(() => {
  tick.value;
  return liveTasks().map(q => ({ q, s: q.stats() })).filter(r => !dismissed[r.q.id]);
});
const total = computed(() => rows.value.reduce((n, r) => n + r.s.files, 0));
const failed = computed(() => rows.value.reduce((n, r) => n + r.s.fail, 0));
const busy = computed(() => rows.value.some(r => r.s.busy));
const doneCnt = computed(() => rows.value.reduce((n, r) => n + r.s.ok, 0));
const title = computed(() => busy.value ? '正在上传' : (failed.value ? '上传有失败' : '上传完成'));

/* 有任务时才显示；首次出现按实际高度重新贴边，避免撑出屏幕底部 */
watch(() => rows.value.length, (n, o) => { if(n && !o) nextTick(onResize); });

function openDock(){
  hidden.value = false;
  setCollapsed(false);
  Object.keys(dismissed).forEach(k => delete dismissed[k]);
}
function hideDock(){ hidden.value = true; }
function hideTask(q){
  if(q.stats().busy && !window.confirm('该任务还在上传，隐藏不会中断上传。确定隐藏？')) return;
  dismissed[q.id] = true;
}
function clearFinished(q){
  q.clearDone();                 // 只清已完成/已取消，失败项留着重试
  tick.value++;
}
function toggleTask(id){ taskOpen[id] = !taskOpen[id]; nextTick(onResize); }

const FILE_TXT = { wait:'待上传', hash:'校验中', run:'上传中', done:'已完成', fail:'失败', cancel:'已取消' };
function fileCls(s){ return FILE_TXT[s] || s; }
function filePct(it){ return it.size ? Math.min(100, Math.round(it.done * 100 / it.size)) : 0; }
function etaText(s){
  if(!s) return '';
  if(s < 60) return s + ' 秒';
  const m = Math.floor(s / 60), r = s % 60;
  return m < 60 ? m + ' 分' + (r ? ' ' + r + ' 秒' : '') : Math.floor(m / 60) + ' 小时';
}

onMounted(() => {
  loadPos();
  window.addEventListener('resize', onResize);
  window.addEventListener('wb-open-upload-dock', openDock);   // 文件存储页「上传任务」按钮
});
onUnmounted(() => {
  clearInterval(timer);
  window.removeEventListener('resize', onResize);
  window.removeEventListener('wb-open-upload-dock', openDock);
  window.removeEventListener('mousemove', onMove);
  window.removeEventListener('mouseup', onUp);
});
</script>

<template>
  <div class="up-dock" v-if="rows.length && !hidden && pos.ready" ref="box"
       :class="{ min: collapsed, dragging: dragging }"
       :style="{ left: pos.x + 'px', top: pos.y + 'px' }">
    <div class="ud-h" @mousedown="onDown" @click="toggleCollapse" title="按住可拖动位置；点击可折叠/展开">
      <span class="ud-grip">⠿</span>
      <span class="ud-dot" :class="{ fail: failed > 0, ok: !busy && !failed }"></span>
      <b>{{ title }}</b>
      <span class="ud-sum" v-if="!collapsed">
        {{ doneCnt }}/{{ total }} 个文件<template v-if="failed"> · {{ failed }} 个失败</template>
      </span>
      <span class="ud-sum" v-else>{{ doneCnt }}/{{ total }}</span>
      <span class="sp"></span>
      <button class="ud-ico" :title="collapsed ? '展开任务栏' : '折叠任务栏'"
              @click.stop="setCollapsed(!collapsed)">{{ collapsed ? '▴' : '▾' }}</button>
      <button class="ud-ico" title="隐藏任务栏（有新任务时自动出现）" @click.stop="hideDock()">✕</button>
    </div>

    <div class="ud-body" v-show="!collapsed">
      <div class="ud-task" v-for="r in rows" :key="r.q.id">
        <div class="ud-line">
          <span class="ud-nm" :title="(r.q.sub || '') + ' · ' + r.q.title">{{ r.q.title }}</span>
          <span class="ud-pct" :class="{ fail: r.s.fail && !r.s.busy }">{{ r.s.pct }}%</span>
          <button class="btnb act-del ud-x" title="隐藏该任务（不中断上传）" @click="hideTask(r.q)">✕</button>
        </div>
        <div class="ud-bar"><i :class="{ fail: r.s.fail && !r.s.busy }" :style="{ width: r.s.pct + '%' }"></i></div>
        <div class="ud-meta">
          <span>{{ r.s.ok }}/{{ r.s.files }} 完成<template v-if="r.s.fail"> · <em>{{ r.s.fail }} 失败</em></template></span>
          <span class="sp"></span>
          <template v-if="r.s.busy">
            <span v-if="r.s.bps || r.s.eta">{{ r.s.bps ? humanSize(r.s.bps) + '/s' : '' }}<template v-if="r.s.eta"> · 剩余 {{ etaText(r.s.eta) }}</template></span>
            <button class="btnb btnb-txt" @click="r.q.paused ? r.q.resume() : r.q.pause()">{{ r.q.paused ? '继续' : '暂停' }}</button>
          </template>
          <template v-else-if="r.s.fail">
            <button class="btnb btnb-txt ud-retry" @click="r.q.retryFailed()">全部重试</button>
          </template>
          <span v-else>已完成</span>
          <button class="btnb btnb-txt ud-detail" @click="toggleTask(r.q.id)">
            {{ taskOpen[r.q.id] ? '收起明细' : '明细 ' + r.q.items.length }}
          </button>
        </div>
        <div class="ud-err" v-if="r.s.fail && !r.s.busy">
          {{ (r.q.items.filter(i => i.status === 'fail')[0] || {}).error || '部分文件上传失败' }}
        </div>
        <div class="ud-sub" v-if="r.q.sub">{{ r.q.sub }}</div>

        <!-- 单条任务的文件明细：展开后可逐文件重试 / 取消 -->
        <div class="ud-files" v-if="taskOpen[r.q.id]">
          <div class="ud-file" :class="{ fail: it.status === 'fail' }" v-for="it in r.q.items" :key="it.key">
            <span class="nm" :title="it.rel">{{ it.rel }}</span>
            <span class="sz">{{ humanSize(it.size) }}</span>
            <span class="bar"><i :class="{ fail: it.status === 'fail' }" :style="{ width: filePct(it) + '%' }"></i></span>
            <span class="st" :class="it.status">{{ fileCls(it.status) }}</span>
            <span class="ops">
              <button class="btnb" v-if="it.status === 'fail'" title="重试该文件" @click="r.q.retry(it.key)">↻</button>
              <button class="btnb act-del" v-if="it.status !== 'done' && it.status !== 'cancel'"
                      title="取消该文件" @click="r.q.cancel(it.key)">✕</button>
            </span>
            <span class="sub" v-if="it.stage && (it.status === 'run' || it.status === 'hash')">{{ it.stage }}</span>
            <span class="err" v-if="it.error">失败原因：{{ it.error }}</span>
          </div>
          <div class="ud-file-ops">
            <button class="btnb btnb-txt" v-if="r.s.ok || r.s.cancel" @click="clearFinished(r.q)">清空已完成</button>
            <button class="btnb btnb-txt" v-if="r.s.busy" @click="r.q.cancelAll()">全部取消</button>
            <span class="sp"></span>
            <span class="hint">共 {{ r.q.items.length }} 个文件</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
