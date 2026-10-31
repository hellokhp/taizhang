<script setup>
import { computed } from 'vue'
import { S } from '../stores/bid'
import { fmtMoney } from '../lib/format'

const rows = computed(() => [
  {n:'已缴 · 未出结果', v:S.value.pend.length, amt:S.value.pendAmt, c:'var(--pri-m)'},
  {n:'应缴未缴', v:S.value.nopay.length, amt:S.value.nopayAmt, c:'var(--warn)'},
  {n:'待退 / 待解保', v:S.value.toBack.length, amt:S.value.toBackAmt, c:'#C99A3F'},
  {n:'已退回 / 已解保', v:S.value.back.length, amt:S.value.backAmt, c:'var(--lose)'}
]);
const totalV = computed(() => Math.max(1, rows.value.reduce((a,r) => a + r.v, 0)));
const totalAmt = computed(() => rows.value.reduce((a,r) => a + r.amt, 0));
const stackRows = computed(() => rows.value.map(r => ({ ...r, p: r.v / totalV.value * 100 })));
const stacked = computed(() => stackRows.value.map(r => r.n + '：' + r.v + ' 笔').join(' / '));
</script>

<template>
  <div class="card">
    <div class="card-h"><h2>保证金状态</h2><span class="hint">在途 {{ totalV }} 笔 · ¥{{ fmtMoney(totalAmt) }}</span></div>
    <div class="card-b">
      <div class="bond-stack" role="img" :aria-label="'保证金状态构成：' + stacked" :title="stacked">
        <i v-for="r in stackRows" v-show="r.v" :key="r.n" :style="{width: r.p + '%', background: r.c}"></i>
      </div>
      <div class="bond-list">
        <div class="bond-item" v-for="r in rows" :key="r.n">
          <span class="dot" :style="{background: r.c}"></span>
          <span class="nm">{{ r.n }}</span>
          <b class="v">{{ r.v }}<em>笔</em></b>
          <span class="amt">¥{{ fmtMoney(r.amt) }}</span>
        </div>
      </div>
      <div class="bond-foot">
        <div class="bf-row"><span>资金占用（转账 / 现金）</span><b>¥{{ fmtMoney(S.fundAmt) }}</b></div>
        <div class="bf-row"><span>授信占用（保函 / 保单）</span><b>¥{{ fmtMoney(S.guarAmt) }}</b></div>
      </div>
    </div>
  </div>
</template>
