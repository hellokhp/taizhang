<script setup>
import { computed } from 'vue'
import BidTable from '../components/BidTable.vue'
import { ST, leadOpts, methodOpts, filteredCnt, pageCnt, pagedRows,
  resetFilter, pickImportFile, exportXlsx, exportTemplate, openEdit,
  setQuickMonths, setPage, setPageSize, selPageAll, batchDelete } from '../stores/bid'
import { RESULT_OPTS } from '../lib/fields'
import { can } from '../stores/perm'

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
      <span class="cnt">{{ filteredCnt }} 条</span>

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
</template>
