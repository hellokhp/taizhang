<script setup>
import { computed } from 'vue'
import { ST, setQuickRange, setYmRange } from '../stores/bid'
import { TODAY, pad, ymLabel } from '../lib/format'
import KpiRow from '../components/KpiRow.vue'
import TrendCard from '../components/TrendCard.vue'
import BondPanel from '../components/BondPanel.vue'
import PeoplePanel from '../components/PeoplePanel.vue'
import AlertsPanel from '../components/AlertsPanel.vue'
import { canAgg } from '../stores/perm'

/* 字段不可见 / 脱敏时，对应看板模块一并隐藏（否则只会显示 0 或算错，误导） */
const showBond = computed(() => canAgg('bondAmount') || canAgg('bondStatus'));
const showPeople = computed(() => canAgg('lead'));
const showAlerts = computed(() => canAgg('lead') && canAgg('bondStatus'));

const curY = TODAY.getFullYear(), curM = pad(TODAY.getMonth() + 1);
const isMonth = computed(() => ST.ymFrom === ST.ymTo && ST.ymTo === curY + '-' + curM);
const isYear = computed(() => ST.ymFrom === curY + '-01' && ST.ymTo === curY + '-12');
const isPrevYear = computed(() => ST.ymFrom === (curY - 1) + '-01' && ST.ymTo === (curY - 1) + '-12');
const rangeText = computed(() => ymLabel(ST.ymFrom, ST.ymTo));

/* 起止任一改动都走同一入口：自动排序、非法值回退 */
function onFrom(e){ const v = e.target.value; if(v) setYmRange(v, ST.ymTo); e.target.value = ST.ymFrom; }
function onTo(e){ const v = e.target.value; if(v) setYmRange(ST.ymFrom, v); e.target.value = ST.ymTo; }
</script>

<template>
  <div class="dash">
    <div class="card dash-ctrl">
      <div class="ctrl-grp">
        <span class="ctrl-lbl">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.3 1.4"/></svg>
          统计口径
        </span>
        <div class="seg">
          <button :class="{on: ST.basis==='openTime'}" @click="ST.basis='openTime'">按开标时间</button>
          <button :class="{on: ST.basis==='deadline'}" @click="ST.basis='deadline'">按交标时间</button>
        </div>
      </div>
      <span class="tb-sep"></span>
      <div class="ctrl-grp">
        <span class="ctrl-lbl">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2.2" y="3" width="11.6" height="10.6" rx="1.8"/><path d="M2.2 6.4h11.6M5.4 1.9v2.4M10.6 1.9v2.4"/></svg>
          统计区间
        </span>
        <input type="month" :value="ST.ymFrom" @change="onFrom" title="起始月份">
        <span class="range-dash">~</span>
        <input type="month" :value="ST.ymTo" @change="onTo" title="结束月份">
        <span class="quick-row">
          <button class="chip" :class="{on: isMonth}" @click="setQuickRange('month')">本月</button>
          <button class="chip" :class="{on: isYear}" @click="setQuickRange('year')">本年</button>
          <button class="chip" :class="{on: isPrevYear}" @click="setQuickRange('prevYear')">去年</button>
        </span>
      </div>
      <span class="dash-hint"><b class="range-tag">{{ rangeText }}</b>· 口径与区间作用于本页全部图表</span>
    </div>

    <KpiRow />

    <div class="grid2" :class="{one: !showBond}">
      <TrendCard />
      <BondPanel v-if="showBond" />
    </div>

    <div v-if="showPeople || showAlerts" class="grid2 b" :class="{one: !(showPeople && showAlerts)}">
      <PeoplePanel v-if="showPeople" />
      <AlertsPanel v-if="showAlerts" />
    </div>
  </div>
</template>
