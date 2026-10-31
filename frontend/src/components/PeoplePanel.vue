<script setup>
import { computed } from 'vue'
import { S } from '../stores/bid'

const list = computed(() => {
  const map = new Map();
  S.value.cur.forEach(r => {
    const k = (r.lead || '未指定').trim();
    if(!map.has(k)) map.set(k, {name:k, n:0, w:0});
    const o = map.get(k); o.n++; if(r.result === '中标') o.w++;
  });
  return [...map.values()].sort((a,b) => b.n - a.n);
});
const max = computed(() => Math.max(...list.value.map(p => p.n), 1));
const total = computed(() => list.value.reduce((a,p) => ({n:a.n + p.n, w:a.w + p.w}), {n:0, w:0}));
const rate = v => v.n ? Math.round(v.w / v.n * 100) : 0;
</script>

<template>
  <div class="card">
    <div class="card-h"><h2>负责人承担情况</h2><span class="hint">{{ S.cur.length ? '本期 ' + list.length + ' 人 · ' + total.n + ' 标 / ' + total.w + ' 中' : '' }}</span></div>
    <div class="card-b">
      <div class="people" v-if="list.length">
        <div class="p-row" v-for="(p, i) in list" :key="p.name">
          <span class="rank">{{ i + 1 }}</span>
          <div class="nm" :title="p.name">{{ p.name }}</div>
          <div class="p-track" :title="p.name + '：' + p.n + ' 标 · 中标 ' + p.w">
            <i :style="{width: p.n/max*100 + '%', background:'#C6D5E7'}">
              <i :style="{display:'block', height:'100%', width: (p.n ? p.w/p.n*100 : 0) + '%', background:'var(--win)'}"></i>
            </i>
          </div>
          <div class="p-num"><b>{{ p.n }}</b><em>标</em><b class="w">{{ p.w }}</b><em>中</em><i>{{ rate(p) }}%</i></div>
        </div>
        <div class="p-total"><span>合计</span><b>{{ total.n }}</b><em>标</em><b class="w">{{ total.w }}</b><em>中</em><i>{{ rate(total) }}%</i></div>
      </div>
      <div class="empty" v-else>本期暂无数据</div>
    </div>
  </div>
</template>
