<script setup>
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import BidTable from '../components/BidTable.vue'
import TableConfigDialog from '../components/TableConfigDialog.vue'
import { ST, S, leadOpts, methodOpts, filteredCnt, pageCnt, pagedRows,
  resetFilter, pickImportFile, exportXlsx, exportTemplate, openEdit,
  setQuickMonths, setPage, setPageSize, selPageAll, batchDelete,
  openCfg, setRowHeight, setOnlyNoTime, setFund } from '../stores/bid'
import { RESULT_OPTS } from '../lib/fields'
import { fundLabel } from '../lib/fund'
import { can } from '../stores/perm'

const route = useRoute();

/* 来自标源总览「另有 N 条未填…」的跳转：只列这批未填统计口径时间的标段
   来自看板「资金状态」叶子（投标/履约保证金、三项开销）的跳转：只列该口径下的标段 */
function applyQuery(){
  const q = route.query;
  if(String(q.noTime || '') === '1'){
    ST.f.kw = String(q.kw || '');
    ST.f.lead = String(q.lead || '');
    ST.f.result = String(q.result || '');
    ST.f.method = String(q.method || '');
    setOnlyNoTime(true);
    return;
  }
  const code = String(q.fund || '');
  if(!code) return;
  ST.f.kw = String(q.kw || '');
  ST.f.lead = String(q.lead || '');
  ST.f.result = String(q.result || '');
  ST.f.method = String(q.method || '');
  /* 日期区间跟随饼图的「本期 / 全部」口径：本期 = 看板统计区间的首末日，全部 = 不限日期 */
  if(String(q.sc || '') === 'cur' && S.value.ymFrom && S.value.ymTo){
    const [y, m] = S.value.ymTo.split('-').map(Number);
    ST.dateFrom = S.value.ymFrom + '-01';
    ST.dateTo = S.value.ymTo + '-' + String(new Date(y, m, 0).getDate()).padStart(2, '0');
  }else{
    ST.dateFrom = '';
    ST.dateTo = '';
  }
  setFund(code);
}
onMounted(applyQuery);
watch(() => route.query, applyQuery);
onUnmounted(() => {
  if(ST.onlyNoTime) setOnlyNoTime(false);
  if(ST.fund) setFund('');
});

const fundLbl = computed(() => fundLabel(ST.fund));
const basisWord = computed(() => ST.basis === 'deadline' ? '交标' : '开标');

/* 当前生效的命名配置（默认视图时为空） */
const activeCfgName = computed(() => {
  const c = ST.tableCfg.list.find(x => x.id === ST.tableCfg.activeId);
  return c ? c.name : '';
});

/* 行高拖拽：上下拖动工具栏手柄，实时调整全部行高 */
let rhDrag = null;
function startRowDrag(e){
  e.preventDefault();
  rhDrag = {y:e.clientY, h:ST.tableCfg.rowH};
  document.body.style.cursor = 'row-resize';
  document.body.style.userSelect = 'none';
  window.addEventListener('pointermove', onRowDrag);
  window.addEventListener('pointerup', endRowDrag, {once:true});
}
function onRowDrag(e){
  if(rhDrag) setRowHeight(rhDrag.h + (e.clientY - rhDrag.y));
}
function endRowDrag(){
  rhDrag = null;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  window.removeEventListener('pointermove', onRowDrag);
}

/* 分页显示窗口：当前页前2后2 + 两端省略 */
const visiblePages = computed(() => {
  const pc = pageCnt.value, cur = ST.page;
  if(pc <= 7) return Array.from({length: pc}, (_, i) => i + 1);
  const arr = [1];
  const lo = Math.max(2, cur - 2), hi = Math.min(pc - 1, cur + 2);
  if(lo > 2) arr.push('...');
  for(let i = lo; i <= hi; i++) arr.push(i);
  if(hi < pc - 1) arr.push('...');
  arr.push(pc);
  return arr;
});
</script>

<template>
  <div class="card" style="margin-bottom:0;">
    <div class="card-h" style="padding-bottom:0">
      <h2>项目台账</h2>
      <span class="hint">点击行尾「编辑」修改，表头可排序；勾选行后可用批量操作</span>
    </div>
    <div class="toolbar tbar">
      <input type="search" id="fKw" placeholder="搜索项目 / 招标编号 / 业主 / 中标单位" style="width:260px;" v-model="ST.f.kw">
      <select v-model="ST.f.lead">
        <option value="">全部负责人</option>
        <option v-for="v in leadOpts" :key="v" :value="v">{{ v }}</option>
      </select>
      <select v-model="ST.f.result">
        <option value="">全部结果</option>
        <option v-for="v in RESULT_OPTS" :key="v" :value="v">{{ v }}</option>
        <option value="未中标|废标|流标">未中标 / 废流标</option>
      </select>
      <select v-model="ST.f.method">
        <option value="">全部招标方式</option>
        <option v-for="v in methodOpts" :key="v" :value="v">{{ v }}</option>
      </select>

      <span class="tb-sep"></span>
      <span class="lbl">时间：</span>
      <input type="date" v-model="ST.dateFrom">
      <span style="color:var(--sub)">~</span>
      <input type="date" v-model="ST.dateTo">
      <span class="quick-row">
        <button class="chip" :class="{on: !ST.dateFrom && !ST.dateTo}" @click="setQuickMonths(0)">全部</button>
        <button class="chip" @click="setQuickMonths(3)">近三月</button>
        <button class="chip" @click="setQuickMonths(6)">近半年</button>
        <button class="chip" @click="setQuickMonths(12)">一年</button>
      </span>

      <span class="tb-sep"></span>
      <button class="btn ghost" @click="resetFilter()">重置筛选</button>
      <button v-if="ST.onlyNoTime" class="chip on no-time-chip"
        :title="'当前仅列出未填' + basisWord + '时间的标段，日期区间在此模式下不生效；点击退出'"
        @click="setOnlyNoTime(false)">仅看未填{{ basisWord }}时间 ✕</button>
      <button v-if="ST.fund" class="chip on no-time-chip fund-chip"
        :title="'当前仅列出「' + fundLbl + '」口径下的标段（来自看板资金状态），可再叠加其它筛选；点击退出'"
        @click="setFund('')">{{ fundLbl }} ✕</button>
      <span class="cnt">{{ filteredCnt }} 条</span>

      <span class="tb-sep"></span>
      <button class="btn ghost" @click="openCfg()">列设置<template v-if="activeCfgName"> · {{ activeCfgName }}</template></button>
      <span class="rowh-grip" title="上下拖动调整行高" @pointerdown="startRowDrag"><i></i>行高 {{ ST.tableCfg.rowH }}px</span>

      <span class="tb-sep"></span>
      <button v-if="can('bid.import')" class="btn" @click="pickImportFile()">导入台账</button>
      <button v-if="can('bid.export')" class="btn" @click="exportXlsx()">导出 Excel</button>
      <button v-if="can('bid.export')" class="btn" @click="exportTemplate()">下载模板</button>
      <button v-if="can('bid.edit')" class="btn pri" @click="openEdit(null)">＋ 新增标段</button>
    </div>

    <!-- 批量操作条 -->
    <div v-if="can('bid.delete') && ST.selIds.length" class="batch-bar">
      <input type="checkbox"
        :checked="pagedRows.length && pagedRows.every(r => ST.selIds.indexOf(r.id) >= 0)"
        @change="selPageAll" style="flex:none">
      <span class="sel-info">已选 <b>{{ ST.selIds.length }}</b> 条
        <button class="chip ghost" @click="ST.selIds=[]">清空选择</button>
      </span>
      <button class="btn danger" @click="batchDelete(ST.selIds.slice())">批量删除</button>
    </div>

    <div class="tbl-wrap">
      <BidTable />
    </div>

    <!-- 底部分页 -->
    <div class="pager">
      <select v-model.number="ST.pageSize" @change="setPageSize(ST.pageSize)">
        <option :value="10">10 条/页</option>
        <option :value="20">20 条/页</option>
        <option :value="50">50 条/页</option>
        <option :value="100">100 条/页</option>
      </select>
      <span class="pg-info">第 <b>{{ ST.page }}</b> / {{ pageCnt }} 页 · 共 {{ filteredCnt }} 条</span>
      <div class="pg-btns">
        <button class="pg-btn" :disabled="ST.page<=1" @click="setPage(1)">«</button>
        <button class="pg-btn" :disabled="ST.page<=1" @click="setPage(ST.page-1)">‹</button>
        <template v-for="p in visiblePages" :key="p">
          <button v-if="p !== '...'" class="pg-btn" :class="{on: p===ST.page}" @click="setPage(p)">{{ p }}</button>
          <span v-else class="pg-dots">…</span>
        </template>
        <button class="pg-btn" :disabled="ST.page>=pageCnt" @click="setPage(ST.page+1)">›</button>
        <button class="pg-btn" :disabled="ST.page>=pageCnt" @click="setPage(pageCnt)">»</button>
      </div>
      <span class="jump">跳转至
        <input type="number" min="1" :max="pageCnt"
          :value="ST.page" @change="setPage(+($event.target.value || 1))">
        页
      </span>
    </div>
  </div>
  <TableConfigDialog />
</template>
