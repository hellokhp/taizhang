<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ST, recMonth } from '../stores/bid'
import { TODAY, fmtYM, ymMonths, ymLabel } from '../lib/format'
import { isNoBid } from '../lib/fields'

/* ============ 数据：绘制统计区间内的每个月 ============ */
const ms = computed(() => ymMonths(ST.ymFrom, ST.ymTo));
const trend = computed(() => {
  const ds = ms.value.map(m => {
    const rr = ST.rows.filter(r => recMonth(r) === m && !isNoBid(r));
    return { m, n: rr.length, w: rr.filter(r => r.result === '中标').length };
  });
  const totalN = ds.reduce((a,d) => a + d.n, 0);
  const totalW = ds.reduce((a,d) => a + d.w, 0);
  const peak = ds.reduce((a,d) => d.n > a.n ? d : a, ds[0] || {m:'', n:0});
  return { ds, totalN, totalW, peak, curM: fmtYM(TODAY), rate: totalN ? totalW/totalN : 0, label: ymLabel(ST.ymFrom, ST.ymTo) };
});

/* ============ 交互状态 ============ */
const type = ref('bar');                       // 'bar' | 'line'
const show = ref({n:true, w:true});            // 系列显隐（点击图例切换）
const hover = ref(-1);
const bothHidden = computed(() => !show.value.n && !show.value.w);
const emptyRange = computed(() => !trend.value.totalN && !trend.value.totalW);
const n = computed(() => Math.max(1, trend.value.ds.length));

function toggle(k){ show.value[k] = !show.value[k]; hover.value = -1; }

/* 鼠标横向定位：不依赖 SVG 内部命中矩形，整块绘图区都能触发 */
function onMove(e){
  const box = e.currentTarget.getBoundingClientRect();
  const x = (e.clientX - box.left) / box.width * mw.value;
  const i = Math.floor((x - PL) / band.value);
  hover.value = (i >= 0 && i < n.value) ? i : -1;
}

/* ============ 尺寸测量 ============ */
const wrap = ref(null);
const W = ref(0);
let ro = null;
onMounted(() => {
  ro = new ResizeObserver(es => { W.value = Math.round(es[0].target.getBoundingClientRect().width); });
  ro.observe(wrap.value);
});
onBeforeUnmount(() => { if(ro) ro.disconnect(); });

/* ============ 几何 ============ */
const H = 252, PT = 22, PB = 30, PL = 44, PR = 16;
const innerH = H - PT - PB;
const mw = computed(() => Math.max(320, W.value || 720));
const innerW = computed(() => mw.value - PL - PR);
const band = computed(() => innerW.value / n.value);

function niceMax(v){
  if(v <= 4) return 4;
  const step = Math.max(1, Math.ceil(v / 4));
  return step * 4;
}
const maxV = computed(() => niceMax(Math.max(1, ...trend.value.ds.map(d => Math.max(d.n, d.w)))));
const yOfV = v => PT + innerH - (v / maxV.value) * innerH;
const cxOf = i => PL + band.value * (i + 0.5);
const ticks = computed(() => [0,1,2,3,4].map(k => ({ v: maxV.value * k / 4, y: yOfV(maxV.value * k / 4) })));

const series = computed(() => [
  show.value.n ? {key:'n', color:'var(--pri-m)'} : null,
  show.value.w ? {key:'w', color:'var(--win)'} : null
].filter(Boolean));

const bw = computed(() => Math.max(8, Math.min(36, band.value * 0.28)));
const gap = computed(() => (n.value > 14 ? 3 : 4));

const ds = computed(() => trend.value.ds.map((d, i) => {
  const keys = series.value.map(s => s.key);
  const totalW = bw.value * keys.length + gap.value * (keys.length - 1);
  const x0 = cxOf(i) - totalW / 2;
  const bars = {};
  keys.forEach((k, j) => {
    const v = d[k];
    const hh = (v / maxV.value) * innerH;
    bars[k] = { x: x0 + j * (bw.value + gap.value), y: hh < 2 ? PT + innerH - 2 : yOfV(v), hh: Math.max(2, hh) };
  });
  return { ...d, i, cx: cxOf(i), bars };
}));

const lineN = computed(() => ds.value.map(d => (d.cx + ',' + yOfV(d.n))).join(' '));
const lineW = computed(() => ds.value.map(d => (d.cx + ',' + yOfV(d.w))).join(' '));
const areaN = computed(() => {
  const base = PT + innerH;
  const pts = ds.value.map(d => (d.cx + ',' + yOfV(d.n))).join(' L ');
  return `M ${ds.value[0].cx},${base} L ${pts} L ${ds.value[ds.value.length - 1].cx},${base} Z`;
});
const curIdx = computed(() => trend.value.ds.findIndex(d => d.m === trend.value.curM));

/* X 轴标签：跨年时标注年份；月份过多时隔位显示（1 月必显） */
const multiYear = computed(() => {
  const a = trend.value.ds[0], b = trend.value.ds[trend.value.ds.length - 1];
  return !!a && !!b && a.m.slice(0,4) !== b.m.slice(0,4);
});
const xTicks = computed(() => trend.value.ds.map((d, i) => {
  const isJan = d.m.slice(5) === '01';
  const yearFirst = multiYear.value && (isJan || i === 0);
  return {
    key: d.m, i, cx: cxOf(i), cur: d.m === trend.value.curM,
    lab: yearFirst ? d.m.slice(2,4) + '年' + +d.m.slice(5) + '月' : +d.m.slice(5) + '月',
    show: n.value <= 14 || isJan || i === 0 || d.m === trend.value.curM || i % 2 === 0
  };
}).filter(t => t.show));

const tipLeft = computed(() => {
  if(hover.value < 0) return '0px';
  const x = cxOf(hover.value);
  return Math.min(Math.max(x, 96), mw.value - 96) + 'px';
});
const hoverData = computed(() => hover.value >= 0 ? trend.value.ds[hover.value] : null);
const rateTxt = computed(() => trend.value.totalN ? (trend.value.rate * 100).toFixed(1) + '%' : '—');
const peakLab = computed(() => {
  const p = trend.value.peak;
  if(!p || !p.m) return '—';
  return (multiYear.value ? p.m.slice(0,4) + '年' : '') + +p.m.slice(5) + '月';
});
const fmtTick = v => Number.isInteger(v) ? v : v.toFixed(1);
</script>

<template>
  <div class="card trend-card">
    <div class="card-h trend-head">
      <h2>投标与中标走势</h2>
      <div class="seg chart-seg">
        <button :class="{on: type==='line'}" @click="type='line'" title="切换为折线图">
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12.5 5.6 7.6 9 9.6l4.6-6"/><circle cx="2" cy="12.5" r=".9" fill="currentColor"/><circle cx="14" cy="3.6" r=".9" fill="currentColor"/></svg>
          折线
        </button>
        <button :class="{on: type==='bar'}" @click="type='bar'" title="切换为柱状图">
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 14V8.5M7.7 14V4M12.4 14V6.5"/></svg>
          柱状
        </button>
      </div>
    </div>

    <div class="card-b">
      <div class="trend-bar">
        <button class="lg-chip" :class="{off: !show.n}" :aria-pressed="show.n" @click="toggle('n')" title="点击隐藏 / 显示「投标标段数」">
          <i class="sw sw-n"></i>投标标段数 <b>{{ trend.totalN }}</b>
        </button>
        <button class="lg-chip" :class="{off: !show.w}" :aria-pressed="show.w" @click="toggle('w')" title="点击隐藏 / 显示「中标数」">
          <i class="sw sw-w"></i>中标数 <b>{{ trend.totalW }}</b>
        </button>
        <span class="trend-meta">
          {{ trend.label }} · 峰值 {{ trend.peak.n }} 个（{{ peakLab }}）
          <span class="dotsep">·</span>中标率（占投标）<b>{{ rateTxt }}</b>
        </span>
      </div>

      <div class="trend-wrap" ref="wrap">
        <svg :viewBox="'0 0 ' + mw + ' ' + H" :height="H" role="img" aria-label="月度投标量与中标量走势图"
          @mousemove="onMove" @mouseleave="hover = -1">
          <defs>
            <linearGradient id="areaN" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#3B628C" stop-opacity=".22"/>
              <stop offset="100%" stop-color="#3B628C" stop-opacity="0"/>
            </linearGradient>
            <linearGradient id="gradBarN" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#5A85B2"/>
              <stop offset="100%" stop-color="#2B5078"/>
            </linearGradient>
            <linearGradient id="gradBarW" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#CE6E63"/>
              <stop offset="100%" stop-color="#A63328"/>
            </linearGradient>
          </defs>

          <!-- 网格与 Y 轴刻度 -->
          <g class="trend-grid">
            <line v-for="t in ticks" :key="'g'+t.y" :x1="PL" :x2="PL + innerW" :y1="t.y" :y2="t.y" :class="{zero: t.v === 0}"/>
          </g>
          <g class="trend-axis">
            <text v-for="t in ticks" :key="'y'+t.y" :x="PL - 9" :y="t.y + 3.6" text-anchor="end">{{ fmtTick(t.v) }}</text>
          </g>

          <!-- 当前月份底纹 -->
          <rect v-if="curIdx >= 0" class="trend-cur-band" :x="PL + band * curIdx" :y="PT" :width="band" :height="innerH" rx="4"/>

          <!-- 柱状图 -->
          <g v-if="type === 'bar'" class="trend-bars">
            <template v-for="d in ds" :key="'b'+d.i">
              <rect v-for="s in series" :key="s.key"
                :class="['bar-' + s.key, {dim: hover >= 0 && hover !== d.i}]"
                :fill="s.key === 'n' ? 'url(#gradBarN)' : 'url(#gradBarW)'"
                :x="d.bars[s.key].x" :y="d.bars[s.key].y"
                :width="bw" :height="d.bars[s.key].hh"
                rx="3" :style="{animationDelay: (d.i * 34) + 'ms'}"/>
            </template>
          </g>

          <!-- 折线图 -->
          <g v-else class="trend-lines">
            <path v-if="show.n" class="area-n" fill="url(#areaN)" :d="areaN"/>
            <path v-if="show.n" class="line-n" :d="'M ' + lineN.split(' ').join(' L ')"/>
            <path v-if="show.w" class="line-w" :d="'M ' + lineW.split(' ').join(' L ')"/>
            <template v-if="show.n">
              <circle v-for="d in ds" :key="'dn'+d.i" class="dot-n" :cx="d.cx" :cy="yOfV(d.n)" r="2.6"
                :class="{cur: d.m === trend.curM, hot: hover === d.i}"/>
            </template>
            <template v-if="show.w">
              <circle v-for="d in ds" :key="'dw'+d.i" class="dot-w" :cx="d.cx" :cy="yOfV(d.w)" r="2.6"
                :class="{cur: d.m === trend.curM, hot: hover === d.i}"/>
            </template>
          </g>

          <!-- 悬浮高亮 -->
          <rect v-if="hover >= 0" class="trend-hl" :x="PL + band * hover" :y="PT" :width="band" :height="innerH" rx="4"/>

          <!-- 悬浮数值标签 -->
          <g v-if="hover >= 0 && hoverData" class="trend-flags">
            <text v-if="show.n" :x="cxOf(hover)" :y="Math.max(PT + 10, yOfV(hoverData.n) - 8)" text-anchor="middle" class="flag-n">{{ hoverData.n }}</text>
            <text v-if="show.w" :x="cxOf(hover)" :y="Math.max(PT + 10, yOfV(hoverData.w) - 8)" text-anchor="middle" class="flag-w">{{ hoverData.w }}</text>
          </g>

          <!-- X 轴月份 -->
          <g class="trend-x">
            <text v-for="t in xTicks" :key="'x'+t.key" :x="t.cx" :y="H - PB + 19" text-anchor="middle" :class="{cur: t.cur}">{{ t.lab }}</text>
            <circle v-if="curIdx >= 0" class="cur-dot" :cx="cxOf(curIdx)" :cy="H - PB + 27" r="2"/>
          </g>
        </svg>

        <div v-if="hoverData" class="trend-tip" :style="{left: tipLeft}">
          <div class="tt-m">{{ hoverData.m.slice(0,4) }} 年 {{ +hoverData.m.slice(5) }} 月</div>
          <div class="tt-row"><i class="sw sw-n"></i>投标标段 <b>{{ hoverData.n }}</b> 个</div>
          <div class="tt-row"><i class="sw sw-w"></i>中标 <b>{{ hoverData.w }}</b> 个</div>
          <div class="tt-rate">中标率（占投标）{{ hoverData.n ? (hoverData.w / hoverData.n * 100).toFixed(0) + '%' : '—' }}</div>
        </div>

        <div v-if="emptyRange || bothHidden" class="trend-empty">
          {{ emptyRange ? '该区间暂无投标数据' : '已隐藏全部数据系列，点击上方图例恢复' }}
        </div>
      </div>
    </div>
  </div>
</template>
