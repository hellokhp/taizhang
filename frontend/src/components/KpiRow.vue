<script setup>
import { computed } from 'vue'
import { ST, S, recMonth } from '../stores/bid'
import { fmtMoney, ymShift, ymMonths, ymLabel } from '../lib/format'
import { canSee, canAgg } from '../stores/perm'
import { isNoBid } from '../lib/fields'

/* 保证金金额若为「脱敏」档，汇总会失真，直接不展示该 KPI */
const showBond = computed(() => (canSee('bondAmount') || canSee('bondStatus')) && canAgg('bondAmount'));

/* 统计区间文案：单月=本月，整年=全年，其余=区间 */
const months = computed(() => ymMonths(ST.ymFrom, ST.ymTo));
const periodWord = computed(() => {
  if(months.value.length === 1) return '本月';
  const fullYear = ST.ymFrom.slice(5) === '01' && ST.ymTo.slice(5) === '12'
    && ST.ymFrom.slice(0,4) === ST.ymTo.slice(0,4);
  return fullYear ? '全年' : '区间';
});

const rateTxt = computed(() => S.value.decided.length ? (S.value.rate*100).toFixed(1) + '%' : '—');
const ytdRateTxt = computed(() => S.value.ytd.length ? (S.value.ytdRate*100).toFixed(1) + '%' : '—');
const nextOpenD = computed(() => S.value.nextOpen ? (S.value.nextOpen.openTime||'').slice(5) : '—');
function pct(a,b){ return b > 0 ? Math.max(2, Math.min(100, a/b*100)) : 0; }

/* 环比：与「等长的上一周期」对比（整年对上年整年，1–3 月对上年 10–12 月） */
const prevFrom = computed(() => ymShift(ST.ymFrom, -months.value.length));
const prevTo = computed(() => ymShift(ST.ymFrom, -1));
const prevTxt = computed(() => ymLabel(prevFrom.value, prevTo.value));
const prevCnt = computed(() => ST.rows.filter(r => {
  const m = recMonth(r); return m && m >= prevFrom.value && m <= prevTo.value && !isNoBid(r);
}).length);
const prevWon = computed(() => ST.rows.filter(r => {
  const m = recMonth(r); return m && m >= prevFrom.value && m <= prevTo.value && !isNoBid(r) && r.result === '中标';
}).length);
const dCnt = computed(() => S.value.cur.length - prevCnt.value);
const dWon = computed(() => S.value.won.length - prevWon.value);
function deltaText(v){ return v === 0 ? '持平' : (v > 0 ? '↑ ' : '↓ ') + Math.abs(v); }
</script>

<template>
  <div class="kpi-row" :class="{four: !showBond}">
    <div class="kpi">
      <div class="kpi-top"><span class="kpi-ic"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8 2 5.2l6 3.4 6-3.4-6-3.4Z"/><path d="M2 10.4 8 13.8l6-3.4"/></svg></span><span class="lbl">{{ periodWord }}投标标段</span></div>
      <div class="kpi-mid">
        <div class="val">{{ S.cur.length }}<small>个</small></div>
        <span class="delta" :class="dCnt > 0 ? 'up' : dCnt < 0 ? 'down' : 'flat'" :title="'较上一周期（' + prevTxt + '）'">{{ deltaText(dCnt) }}</span>
      </div>
      <div class="kpi-bar"><i :style="{width: pct(S.cur.length, S.ytd.length || S.cur.length) + '%'}"></i></div>
      <div class="foot"><b>{{ S.projCount }}</b> 个项目 · 年度累计 <b>{{ S.ytd.length }}</b> 个<template v-if="S.noBidCount"> · 另 <b>{{ S.noBidCount }}</b> 个报名不投（未计入）</template></div>
    </div>
    <div class="kpi w">
      <div class="kpi-top"><span class="kpi-ic"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5.2 2h5.6v3.1a2.8 2.8 0 0 1-5.6 0V2Z"/><path d="M5.2 2.9H3.1v1a2 2 0 0 0 2 2M10.8 2.9h2.1v1a2 2 0 0 1-2 2"/><path d="M8 8.2v2.9M5.6 14h4.8M8 11.1V14"/></svg></span><span class="lbl">{{ periodWord }}中标</span></div>
      <div class="kpi-mid">
        <div class="val">{{ S.won.length }}<small>个</small></div>
        <span class="delta" :class="dWon > 0 ? 'up' : dWon < 0 ? 'down' : 'flat'" :title="'较上一周期（' + prevTxt + '）'">{{ deltaText(dWon) }}</span>
      </div>
      <div class="kpi-bar"><i :style="{width: pct(S.won.length, S.decided.length) + '%'}"></i></div>
      <div class="foot">中标率 <b>{{ rateTxt }}</b>（已开标 {{ S.decided.length }} 个）· 年度 <b>{{ ytdRateTxt }}</b></div>
    </div>
    <div class="kpi l">
      <div class="kpi-top"><span class="kpi-ic"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M5.6 8h4.8"/></svg></span><span class="lbl">未中标 / 废流标</span></div>
      <div class="val">{{ S.lost.length + S.fail.length }}<small>个</small></div>
      <div class="kpi-bar"><i :style="{width: pct(S.lost.length + S.fail.length, S.cur.length) + '%'}"></i></div>
      <div class="foot">未中标 <b>{{ S.lost.length }}</b> · 废标流标 <b>{{ S.fail.length }}</b></div>
    </div>
    <div class="kpi i">
      <div class="kpi-top"><span class="kpi-ic"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 4.4V8l2.5 1.5"/></svg></span><span class="lbl">待开标</span></div>
      <div class="val">{{ S.waiting.length }}<small>个</small></div>
      <div class="kpi-bar"><i :style="{width: pct(S.waiting.length, S.cur.length) + '%'}"></i></div>
      <div class="foot">最近开标 <b>{{ nextOpenD }}</b><template v-if="S.nextOpen && S.nextOpen.lead"> · {{ S.nextOpen.lead }}</template></div>
    </div>
    <div v-if="showBond" class="kpi a">
      <div class="kpi-top"><span class="kpi-ic"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="1.9" y="3.8" width="12.2" height="9" rx="2.2"/><path d="M1.9 6.9h12.2"/><circle cx="11.4" cy="10.2" r=".9"/></svg></span><span class="lbl">保证金占用（现金）</span></div>
      <div class="val">¥{{ fmtMoney(S.fundAmt) }}</div>
      <div class="kpi-bar"><i :style="{width: pct(S.fundAmt, S.fundAmt + S.guarAmt) + '%'}"></i></div>
      <div class="foot">保函/保单 <b>{{ S.guarRows.length }}</b> 笔 ¥{{ fmtMoney(S.guarAmt) }} · 待缴 <b>{{ S.nopay.length }}</b> · 逾期 <b class="red">{{ S.over.length }}</b></div>
    </div>
  </div>
</template>
