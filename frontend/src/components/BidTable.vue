<script setup>
import { computed, onBeforeUnmount } from 'vue'
import { ST, pagedRows, toggleSort, openEdit, openDetail, openBidUpload, removeRow, toggleSel, isSelected, selPageAll, setColWidth } from '../stores/bid'
import { num, fmtN, dd } from '../lib/format'
import { bondState, shortType, bidStatusOf, bidEffortDays } from '../lib/fields'
import { TABLE_MAP } from '../lib/tableCols'
import { can, canSee, isMasked } from '../stores/perm'

const props = defineProps({ readonly: {type:Boolean, default:false} });

/* 依据 ST.basis 动态决定"时间列"的字段键和列名（与看板统计口径保持一致） */
const TIME_K = computed(() => ST.basis === 'deadline' ? 'deadline' : 'openTime');
const TIME_L = computed(() => ST.basis === 'deadline' ? '交标时间' : '开标时间');

/* 列由「列配置」生成：未配置的字段不渲染，字段无权限时整列剔除 */
function colVisible(d){
  if(d.k === 'project') return canSee('projectName');
  if(d.k === 'time') return canSee(TIME_K.value);
  if(d.k === 'amount') return canSee('ourPrice') || canSee('winAmount');
  if(d.k === 'bondStatus') return canSee('bondStatus');
  return canSee(d.k);
}
const cols = computed(() => {
  const out = [];
  for(const k of ST.tableCfg.cols){
    const d = TABLE_MAP[k];
    if(!d || !colVisible(d)) continue;
    out.push(Object.assign({}, d, {
      l: d.k === 'time' ? TIME_L.value : d.l,
      sk: d.k === 'time' ? TIME_K.value : (d.sk || d.k),
      w: ST.tableCfg.widths[k] || d.w || 120,
      sortable: d.sortable !== false && !d.der
    }));
  }
  return out;
});
/* 可勾选（批量删除）与可显示操作列 */
const canSel = computed(() => !props.readonly && can('bid.delete'));
const canUp = computed(() => !props.readonly && can('view.files'));
const canOp = computed(() => !props.readonly && (can('bid.edit') || can('bid.delete') || canUp.value));
const OP_W = computed(() => {
  if(!canOp.value) return 0;
  let w = 0;
  if(can('bid.edit')) w += 28;
  if(canUp.value) w += 28;
  if(can('bid.delete')) w += 28;
  return Math.max(36, w + 8);
});
const colspan = computed(() => cols.value.length + (canSel.value ? 1 : 0) + (canOp.value ? 1 : 0));
const canEdit = computed(() => can('bid.edit'));
const canDel = computed(() => can('bid.delete'));

const tableWidth = computed(() => {
  const sum = cols.value.reduce((a, c) => a + c.w, 0);
  return sum + (canSel.value ? 42 : 0) + (canOp.value ? OP_W.value : 0);
});
const rowPad = computed(() => Math.max(2, Math.min(11, Math.round((ST.tableCfg.rowH - 18) / 2))));
const tableStyle = computed(() => ({
  /* 列宽总和小于容器时铺满容器，大于容器时保持各列宽度并横向滚动 */
  width: 'max(100%, ' + tableWidth.value + 'px)',
  '--row-h': ST.tableCfg.rowH + 'px',
  '--row-pad': rowPad.value + 'px'
}));

/* ---------- 列宽拖拽（拖动结束才写入配置，过程直接改响应式宽度） ---------- */
let rz = null;
let justResized = false;
function startResize(e, c){
  e.preventDefault();
  e.stopPropagation();
  rz = {k:c.k, x:e.clientX, w:c.w};
  justResized = true;
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';
  window.addEventListener('pointermove', onResize);
  window.addEventListener('pointerup', endResize, {once:true});
}
function onResize(e){
  if(!rz) return;
  setColWidth(rz.k, rz.w + (e.clientX - rz.x));
}
function endResize(){
  rz = null;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  window.removeEventListener('pointermove', onResize);
  setTimeout(() => { justResized = false; }, 0);
}
onBeforeUnmount(() => { if(rz) endResize(); });
function headClick(c){
  if(!c.sortable || justResized) return;
  toggleSort(c.sk);
}

/* 表头全选三态 */
const isPageAll = computed(() => !!pagedRows.value.length && pagedRows.value.every(r => isSelected(r.id)));
const isHalfSel = computed(() => {
  const c = pagedRows.value.filter(r => isSelected(r.id)).length;
  return c > 0 && c < pagedRows.value.length;
});

function resultPill(v){
  const m = {'中标':'p-win','未中标':'p-lose','待开标':'p-wait','废标':'p-fail','流标':'p-fail','不投':'p-na','放弃':'p-na'};
  return m[v] || 'p-na';
}
function bondPillCls(r){
  const m = {'已退':'p-pay','已缴':'p-wait','未缴':'p-nopay','待退':'p-nopay','无需缴纳':'p-na','不予退还':'p-fail','未填':'p-na'};
  return m[bondState(r)] || 'p-na';
}
function bondTag(r){ const t = shortType(r.bondType); return (t && t !== '转账') ? t : ''; }
/* 标书状态胶囊：未开始=灰、进行中=蓝、已结束=绿（与开标结果胶囊同一套色板） */
const BID_PILL = {'未开始':'p-na', '进行中':'p-wait', '已结束':'p-pay'};
function bidPill(r){ return BID_PILL[bidStatusOf(r)] || 'p-na'; }

/* 按 ST.basis 取当前时间字段值（展示 + 标识都用同一个） */
function rowTime(r){ return r[TIME_K.value] || r.openTime || r.deadline || ''; }
/* 距当前时间还有几天（返回 null 表示无时间或已过期负数） */
function daysFromNow(r){
  const d = dd(rowTime(r));
  return (d === null) ? null : d;
}
/* 近期标识：只对「待开标」且时间在 [0, 7] 天内的行显示 */
function openSoon(r){
  if(r.result !== '待开标') return false;
  const d = daysFromNow(r);
  return d !== null && d >= 0 && d <= 7;
}
function openSoonTxt(r){
  const d = daysFromNow(r);
  if(d === 0) return '今天';
  if(d === 1) return '明天';
  if(d === null || d < 0) return '';
  return d + '天';
}
function cellClass(c){
  return {
    num: c.t === 'num' || c.t === 'amount',
    'td-center': c.t === 'time' || c.t === 'result' || c.t === 'bond' || c.k === 'method'
  };
}
</script>

<template>
  <table :style="tableStyle">
    <colgroup>
      <col v-if="canSel" style="width:42px">
      <col v-for="c in cols" :key="c.k" :style="{width: c.w + 'px'}">
      <col v-if="canOp" :style="{width: OP_W + 'px'}">
    </colgroup>
    <thead>
      <tr>
        <th v-if="canSel" class="col-cb">
          <input type="checkbox" class="th-cb"
            :checked="isPageAll" :indeterminate="isHalfSel" :disabled="!pagedRows.length"
            @change="selPageAll">
        </th>
        <th v-for="c in cols" :key="c.k" :class="{num: c.t === 'num' || c.t === 'amount', sortable: c.sortable}"
          :style="{width: c.w + 'px'}" @click="headClick(c)">
          {{ c.l }}<span class="ar" v-if="ST.sort.k === c.sk">{{ ST.sort.d > 0 ? '▲' : '▼' }}</span>
          <span class="col-resize" title="拖动调整列宽"
            @pointerdown="startResize($event, c)" @click.stop></span>
        </th>
        <th v-if="canOp" class="col-op-h" :style="{width: OP_W + 'px'}">操作</th>
      </tr>
    </thead>
    <tbody>
      <tr v-if="!pagedRows.length">
        <td :colspan="colspan" class="col-empty"><div class="empty">当前条件下没有记录。可切换上方时间范围，或点「＋ 新增标段」录入。</div></td>
      </tr>
      <tr v-for="r in pagedRows" :key="r.id" :class="{sel: canSel && isSelected(r.id)}">
        <td v-if="canSel" class="col-cb">
          <input type="checkbox" :checked="isSelected(r.id)" @change="toggleSel(r.id)" class="row-cb">
        </td>
        <template v-for="c in cols" :key="c.k">
          <td v-if="c.k === 'project'" class="pj">
            <b class="pj-link" title="查看标段详情" @click.stop="openDetail(r.id)">{{ r.projectName || '—' }}</b>
            <span>{{ r.bidNo || '无编号' }}<template v-if="r.pkg"> · {{ r.pkg }}</template><template v-if="r.owner"> · {{ r.owner }}</template><template v-if="r.agency"> · 代理 {{ r.agency }}</template></span>
          </td>
          <td v-else-if="c.k === 'time'" class="td-center">
            {{ (rowTime(r) || '—').slice(5) }}
            <span v-if="openSoon(r)" class="soon">{{ openSoonTxt(r) }}</span>
          </td>
          <td v-else-if="c.k === 'lead'">
            <span v-if="r.lead" class="lead-cell"><i class="avatar">{{ r.lead.slice(0,1) }}</i>{{ r.lead }}</span>
            <span v-else style="color:var(--warn)">未指定</span>
          </td>
          <td v-else-if="c.k === 'result'" class="td-center"><span class="pill" :class="resultPill(r.result)">{{ r.result || '待开标' }}</span></td>
          <td v-else-if="c.k === 'winner'">
            <span style="display:inline-flex;align-items:center;gap:2px;max-width:100%">
              <b v-if="r.winner === '本公司'" style="color:var(--win)">本公司</b>
              <span v-else-if="r.winner" :title="r.winner" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ r.winner }}</span>
              <span v-else style="color:var(--sub)">—</span>
              <a v-if="r.noticeUrl" :href="r.noticeUrl" target="_blank" rel="noopener" title="打开中标公告原文" style="color:var(--pri);text-decoration:none;font-size:11px;flex:none">↗</a>
            </span>
          </td>
          <td v-else-if="c.k === 'amount'" class="num">
            <b v-if="r.result === '中标' && num(r.winAmount)" style="color:var(--win)">{{ fmtN(r.winAmount) }}</b>
            <template v-else-if="num(r.ourPrice)">{{ fmtN(r.ourPrice) }}</template>
            <span v-else style="color:var(--sub)">—</span>
          </td>
          <td v-else-if="c.k === 'bondStatus'" class="td-center">
            <span class="pill" :class="bondPillCls(r)">{{ bondState(r) }}</span><span v-if="bondTag(r)" class="wtag">{{ bondTag(r) }}</span>
          </td>
          <td v-else-if="c.k === 'bidStatus'" class="td-center"
              :title="'未开始 ' + (r.bidWaitAt || '—') + ' · 进行中 ' + (r.bidStartAt || '—') + ' · 已结束 ' + (r.bidEndAt || '—')">
            <span class="pill" :class="bidPill(r)">{{ bidStatusOf(r) }}</span>
          </td>
          <td v-else-if="c.k === 'bidEffort'" class="num"
              :title="r.bidStartAt ? ('进行中 ' + r.bidStartAt + ' → ' + (r.bidEndAt || '至今')) : '尚未进入「进行中」，无法计算'">
            <span v-if="isMasked('bidEffort')" style="color:var(--sub)">***</span>
            <span v-else-if="bidEffortDays(r)">{{ bidEffortDays(r) }}<small style="color:var(--sub);font-size:10px;margin-left:1px">天</small></span>
            <span v-else style="color:var(--sub)">—</span>
          </td>
          <td v-else :class="cellClass(c)">
            <template v-if="c.t === 'num'">
              <span v-if="num(r[c.k])">{{ fmtN(r[c.k]) }}</span>
              <span v-else style="color:var(--sub)">—</span>
            </template>
            <span v-else-if="!r[c.k]" style="color:var(--sub)">—</span>
            <span v-else :title="String(r[c.k])" style="display:block;overflow:hidden;text-overflow:ellipsis">{{ r[c.k] }}</span>
          </td>
        </template>
        <td v-if="canOp" class="col-op">
          <button v-if="canUp" class="btnb act-up" title="上传项目文件" @click="openBidUpload(r.id)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.2 5.9 9.9a1.9 1.9 0 0 0 2.7 2.7l4.9-4.9a3.2 3.2 0 0 0-4.5-4.5L3.8 8.4a4.5 4.5 0 0 0 6.4 6.4l3.8-3.8"/></svg>
          </button>
          <button v-if="canEdit" class="btnb act-edit" title="编辑" @click="openEdit(r.id)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11 2.5a2.1 2.1 0 013 3L5.5 14l-3 .7.7-3L11 2.5z"/></svg>
          </button>
          <button v-if="canDel" class="btnb act-del" title="删除" @click="removeRow(r.id)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h10M6 4V2.5h4V4M4 4l.8 9.5a1.5 1.5 0 001.5 1.4h3.4a1.5 1.5 0 001.5-1.4L12 4"/></svg>
          </button>
        </td>
      </tr>
    </tbody>
  </table>
</template>
