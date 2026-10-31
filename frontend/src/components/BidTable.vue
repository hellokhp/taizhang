<script setup>
import { computed } from 'vue'
import { ST, pagedRows, toggleSort, openEdit, removeRow, toggleSel, isSelected, selPageAll } from '../stores/bid'
import { num, fmtN, dd } from '../lib/format'
import { bondState, shortType } from '../lib/fields'
import { can, canSee } from '../stores/perm'

const props = defineProps({ readonly: {type:Boolean, default:false} });

/* 依据 ST.basis 动态决定"时间列"的字段键和列名（与看板统计口径保持一致） */
const TIME_K = computed(() => ST.basis === 'deadline' ? 'deadline' : 'openTime');
const TIME_L = computed(() => ST.basis === 'deadline' ? '交标时间' : '开标时间');

/* 列按「字段权限」动态生成：不可见字段整列不渲染 */
const cols = computed(() => {
  const list = [];
  if(canSee('projectName')) list.push({k:'projectName', l:'项目 / 招标编号', sortable:true});
  if(canSee(TIME_K.value)) list.push({k:TIME_K.value, l:TIME_L.value, sortable:true});
  if(canSee('method')) list.push({k:'method', l:'招标方式', sortable:true});
  if(canSee('lead')) list.push({k:'lead', l:'负责人', sortable:true});
  if(canSee('result')) list.push({k:'result', l:'开标结果', sortable:true});
  if(canSee('winner')) list.push({k:'winner', l:'中标单位', sortable:true});
  if(canSee('ourPrice') || canSee('winAmount')) list.push({k:'amount', l:'金额（万元）', sortable:false, num:true});
  if(canSee('bondStatus')) list.push({k:'bondStatus', l:'保证金', sortable:true});
  return list;
});
/* 可勾选（批量删除）与可显示操作列 */
const canSel = computed(() => !props.readonly && can('bid.delete'));
const canOp = computed(() => !props.readonly && (can('bid.edit') || can('bid.delete')));
const colspan = computed(() => cols.value.length + (canSel.value ? 1 : 0) + (canOp.value ? 1 : 0));
const canEdit = computed(() => can('bid.edit'));
const canDel = computed(() => can('bid.delete'));

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
</script>

<template>
  <table>
    <thead>
      <tr>
        <th v-if="canSel" class="col-cb">
          <input type="checkbox" class="th-cb"
            :checked="isPageAll" :indeterminate="isHalfSel" :disabled="!pagedRows.length"
            @change="selPageAll">
        </th>
        <th v-for="c in cols" :key="c.k" :class="{num: c.num, sortable: c.sortable}"
          @click="c.sortable && toggleSort(c.k)">
          {{ c.l }}<span class="ar" v-if="ST.sort.k === c.k">{{ ST.sort.d > 0 ? '▲' : '▼' }}</span>
        </th>
        <th v-if="canOp" class="col-op-h">操作</th>
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
        <td v-if="canSee('projectName')" class="pj">
          <b>{{ r.projectName || '—' }}</b>
          <span>{{ r.bidNo || '无编号' }}<template v-if="r.pkg"> · {{ r.pkg }}</template><template v-if="r.owner"> · {{ r.owner }}</template><template v-if="r.agency"> · 代理 {{ r.agency }}</template></span>
        </td>
        <td v-if="canSee(TIME_K)" class="td-center">
          {{ (rowTime(r) || '—').slice(5) }}
          <span v-if="openSoon(r)" class="soon">{{ openSoonTxt(r) }}</span>
        </td>
        <td v-if="canSee('method')" class="td-center">{{ r.method || '—' }}</td>
        <td v-if="canSee('lead')">
          <span v-if="r.lead" class="lead-cell"><i class="avatar">{{ r.lead.slice(0,1) }}</i>{{ r.lead }}</span>
          <span v-else style="color:var(--warn)">未指定</span>
        </td>
        <td v-if="canSee('result')" class="td-center"><span class="pill" :class="resultPill(r.result)">{{ r.result || '待开标' }}</span></td>
        <td v-if="canSee('winner')">
          <span style="display:inline-flex;align-items:center;gap:2px;max-width:100%">
            <b v-if="r.winner === '本公司'" style="color:var(--win)">本公司</b>
            <span v-else-if="r.winner" :title="r.winner" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ r.winner }}</span>
            <span v-else style="color:var(--sub)">—</span>
            <a v-if="r.noticeUrl" :href="r.noticeUrl" target="_blank" rel="noopener" title="打开中标公告原文" style="color:var(--pri);text-decoration:none;font-size:11px;flex:none">↗</a>
          </span>
        </td>
        <td v-if="canSee('ourPrice') || canSee('winAmount')" class="num">
          <b v-if="r.result === '中标' && num(r.winAmount)" style="color:var(--win)">{{ fmtN(r.winAmount) }}</b>
          <template v-else-if="num(r.ourPrice)">{{ fmtN(r.ourPrice) }}</template>
          <span v-else style="color:var(--sub)">—</span>
        </td>
        <td v-if="canSee('bondStatus')" class="td-center">
          <span class="pill" :class="bondPillCls(r)">{{ bondState(r) }}</span><span v-if="bondTag(r)" class="wtag">{{ bondTag(r) }}</span>
        </td>
        <td v-if="canOp" class="col-op">
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
