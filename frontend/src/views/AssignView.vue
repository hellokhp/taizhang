<script setup>
/* ================================================================
   项目分配表：行 = 开标时间（由近到远、含今天），列 = 负责人，
   格子 = 该负责人在该开标时间的项目名（可点看详情）。
   调节方式与「投标管理」的行高一致：
   · 整体：拖工具栏的虚线手柄（行高上下拖 / 列宽左右拖）
   · 单列：拖表头右边缘；单行：拖时间格底部
   内容随格子大小自动换行，永远收在对应格子里。
   ================================================================ */
import { ref, reactive, computed, watch } from 'vue'
import { ST, openDetail, staff, leadOpts } from '../stores/bid'
import { TODAY, pad } from '../lib/format'
import { canAgg, canSee } from '../stores/perm'

const showAll = ref(false);      // 默认从今天起；打开后含更早的历史安排
const todayISO = TODAY.getFullYear() + '-' + pad(TODAY.getMonth() + 1) + '-' + pad(TODAY.getDate());

/* 字段权限：负责人被隐藏/脱敏、或时间/项目名不可见时，分配表会失真，整体不展示 */
const blocked = computed(() => !canAgg('lead') || !canSee('openTime') || !canSee('projectName'));

const DATE_RE = /^\d{4}-\d{2}-\d{2}/;
function dayOf(v){ const m = String(v||'').match(DATE_RE); return m ? m[0] : ''; }
/* 时间拆两行：日期 + 时分（窄列也不挤成乱码） */
function timeParts(v){
  const m = String(v||'').match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?/);
  if(!m) return {d:String(v||''), tm:''};
  return {
    d: (+m[1]) + '年' + (+m[2]) + '月' + (+m[3]) + '日',
    tm: m[4] !== undefined ? ('0' + m[4]).slice(-2) + ':' + m[5] : ''
  };
}

/* 可见范围内的行（未填开标时间的不进表） */
const pool = computed(() => ST.rows.filter(r => {
  const t = String(r.openTime||'').trim();
  if(!DATE_RE.test(t)) return false;
  if(!showAll.value && dayOf(t) < todayISO) return false;
  return true;
}));

/* 列：员工名单顺序优先；数据里出现过的其他负责人（含「未指定」）追加在后 */
const persons = computed(() => {
  const base = staff.list.length ? staff.list.map(p => p.displayName).filter(Boolean) : [...leadOpts.value];
  const have = new Set(base);
  const extras = new Set();
  pool.value.forEach(r => {
    const v = (r.lead||'').trim() || '未指定';
    if(!have.has(v)){ have.add(v); extras.add(v); }
  });
  return base.concat([...extras].sort((a,b) => a.localeCompare(b, 'zh')));
});

/* 行：按开标时间聚合，升序（由近到远）；格子内按项目名排序 */
const slots = computed(() => {
  const map = new Map();
  pool.value.forEach(r => {
    const t = String(r.openTime||'').trim();
    if(!map.has(t)) map.set(t, new Map());
    const m = map.get(t);
    const p = (r.lead||'').trim() || '未指定';
    if(!m.has(p)) m.set(p, []);
    m.get(p).push(r);
  });
  return [...map.keys()].sort().map(t => {
    const cells = map.get(t);
    cells.forEach(arr => arr.sort((a,b) =>
      String(a.projectName||'').localeCompare(String(b.projectName||''), 'zh')));
    const tp = timeParts(t);
    return { t, d: tp.d, tm: tp.tm, isToday: dayOf(t) === todayISO, cells };
  });
});
function cellOf(s, p){ return s.cells.get(p) || []; }

/* ==================== 列宽 / 行高（整体可拖，单列单行可拖，本地记忆） ==================== */
const CFG_KEY = 'workbuddy_assign_cfg_v1';
const DEF = {timeW:176, colW:170, rowH:44};
const MIN_W = {time:120, col:90}, MAX_W = {time:360, col:520}, MIN_RH = 28, MAX_RH = 260;
const cfg = reactive({timeW:DEF.timeW, colW:DEF.colW, rowH:DEF.rowH, widths:{}, heights:{}});
try{
  const o = JSON.parse(localStorage.getItem(CFG_KEY) || 'null');
  if(o) Object.assign(cfg, {
    timeW: +o.timeW || DEF.timeW, colW: +o.colW || DEF.colW, rowH: +o.rowH || DEF.rowH,
    widths: o.widths || {}, heights: o.heights || {}
  });
}catch(e){}
let saveTm = null;
watch(cfg, () => {
  clearTimeout(saveTm);
  saveTm = setTimeout(() => {
    try{ localStorage.setItem(CFG_KEY, JSON.stringify({
      timeW: cfg.timeW, colW: cfg.colW, rowH: cfg.rowH, widths: cfg.widths, heights: cfg.heights
    })); }catch(e){}
  }, 200);
}, {deep:true});

const clamp = (v, a, b) => Math.min(b, Math.max(a, Math.round(v)));
const colWOf = p => clamp(cfg.widths[p] ?? cfg.colW, MIN_W.col, MAX_W.col);
const rowHOf = t => clamp(cfg.heights[t] ?? cfg.rowH, MIN_RH, MAX_RH);
/* 内边距随行高走（与台账一致）：行越高、留白越多；行变矮时先收内边距再收内容 */
const rowPadOf = t => Math.max(3, Math.min(12, Math.round((rowHOf(t) - 26) / 2)));
const totalW = computed(() => cfg.timeW + persons.value.reduce((s, p) => s + colWOf(p), 0));

function resetCfg(){
  cfg.timeW = DEF.timeW; cfg.colW = DEF.colW; cfg.rowH = DEF.rowH;
  cfg.widths = {}; cfg.heights = {};
}

/* —— 拖拽：整体（工具栏手柄）/ 单列（表头右缘）/ 单行（时间格底部） —— */
let drag = null;
function beginDrag(type, key, e){
  e.preventDefault();
  drag = {type, key, x:e.clientX, y:e.clientY,
    w: type === 'allcol' ? cfg.colW : (key === '__time' ? cfg.timeW : colWOf(key)),
    h: type === 'allrow' ? cfg.rowH : rowHOf(key),
    cleared:false};
  document.body.style.cursor = type === 'allrow' || type === 'row' ? 'row-resize' : 'col-resize';
  document.body.style.userSelect = 'none';
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', endDrag, {once:true});
}
function onMove(e){
  if(!drag) return;
  const isRow = drag.type === 'allrow' || drag.type === 'row';
  const delta = isRow ? (e.clientY - drag.y) : (e.clientX - drag.x);
  if(Math.abs(delta) < 1) return;

  if(drag.type === 'row'){
    cfg.heights[drag.key] = clamp(drag.h + delta, MIN_RH, MAX_RH);
  }else if(drag.type === 'col'){
    const w = clamp(drag.w + delta, drag.key === '__time' ? MIN_W.time : MIN_W.col,
                    drag.key === '__time' ? MAX_W.time : MAX_W.col);
    if(drag.key === '__time') cfg.timeW = w; else cfg.widths[drag.key] = w;
  }else if(drag.type === 'allrow'){
    if(!drag.cleared){ cfg.heights = {}; drag.cleared = true; }   // 整体调整覆盖单行微调
    cfg.rowH = clamp(drag.h + delta, MIN_RH, MAX_RH);
  }else{
    if(!drag.cleared){ cfg.widths = {}; drag.cleared = true; }    // 整体调整覆盖单列微调
    cfg.colW = clamp(drag.w + delta, MIN_W.col, MAX_W.col);
  }
}
function endDrag(){
  drag = null;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  window.removeEventListener('pointermove', onMove);
}
</script>

<template>
  <div class="card" style="margin-bottom:0;">
    <div class="card-h" style="padding-bottom:12px;">
      <h2>项目分配表</h2>
      <span class="hint">按开标时间由近到远排列（含今天）；点项目名查看详情</span>
      <div class="spacer"></div>
      <button class="chip" :class="{on: showAll}" @click="showAll = !showAll">{{ showAll ? '已含更早安排' : '含更早安排' }}</button>
    </div>

    <div class="toolbar asg-tools">
      <span class="lbl">整体行高</span>
      <span class="rowh-grip" title="上下拖动，统一调整所有行的高度（会清除单行微调）"
            @pointerdown="beginDrag('allrow', '', $event)"><i></i>{{ cfg.rowH }}px</span>
      <span class="rowh-grip asg-gx" title="左右拖动，统一调整所有负责人列的宽度（会清除单列微调）"
            @pointerdown="beginDrag('allcol', '', $event)"><i></i>{{ cfg.colW }}px</span>
      <button class="btn ghost" @click="resetCfg()">重置</button>
      <span class="tb-sep"></span>
      <span class="cnt">拖表头右边缘调单列 · 拖时间格底部调单行 · 内容随格子自动换行</span>
    </div>

    <div v-if="blocked" class="card-b"><div class="empty">当前账号无「标书负责人 / 开标时间 / 项目名称」的可见权限，无法生成分配表。</div></div>

    <div v-else-if="slots.length" class="asg-wrap">
      <table class="asg-tbl" :style="{width: 'max(100%, ' + totalW + 'px)'}">
        <colgroup>
          <col :style="{width: cfg.timeW + 'px'}">
          <col v-for="p in persons" :key="p" :style="{width: colWOf(p) + 'px'}">
        </colgroup>
        <thead>
          <tr>
            <th class="asg-time-h">开标时间<i class="asg-cg" title="左右拖动调整本列宽度" @pointerdown="beginDrag('col', '__time', $event)"></i></th>
            <th v-for="p in persons" :key="p"><span class="asg-hn">{{ p }}</span><i class="asg-cg" title="左右拖动调整本列宽度" @pointerdown="beginDrag('col', p, $event)"></i></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in slots" :key="s.t" :class="{today: s.isToday}"
              :style="{'--rh': rowHOf(s.t) + 'px', '--rhp': rowPadOf(s.t) + 'px'}">
            <td class="asg-time">
              <span class="asg-tdate"><span v-if="s.isToday" class="asg-today-tag">今天</span>{{ s.d }}</span>
              <span v-if="s.tm" class="asg-tm">{{ s.tm }}</span>
              <i class="asg-rh" title="上下拖动调整本行行高" @pointerdown="beginDrag('row', s.t, $event)"></i>
            </td>
            <td v-for="p in persons" :key="p" class="asg-cell"
                :title="cellOf(s, p).length ? cellOf(s, p).map(r => r.projectName || '未命名项目').join('\n') : ''">
              <a v-for="(r, i) in cellOf(s, p)" :key="r.id" class="asg-item"
                 :title="(r.result || '待开标') + ' · 负责人 ' + (r.lead || '未指定')"
                 @click="openDetail(r.id)">
                <em v-if="cellOf(s, p).length > 1">{{ i + 1 }}.</em>{{ r.projectName || '未命名项目' }}
              </a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="card-b">
      <div class="empty">今天起暂无待开标项目<template v-if="!showAll">，点右上「含更早安排」可查看历史。</template></div>
    </div>
  </div>
</template>
