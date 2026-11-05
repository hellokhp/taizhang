<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { S, toast } from '../stores/bid'
import { fmtMoney } from '../lib/format'
import { canAgg, can } from '../stores/perm'
import { FUND_META, fundAllow, fundStat, fundCats } from '../lib/fund'

/* ============ 数据：本期 / 全部 两套口径 ============ */
const allow = computed(() => fundAllow(canAgg));
const scope = ref('cur');                                  // 'cur' 本期 | 'all' 全部
const stat = computed(() => fundStat(scope.value === 'cur' ? S.value.cur : S.value.live, allow.value));
const cats = computed(() => fundCats(stat.value, allow.value));

/* ============ 下钻：open 为空 = 顶层四类；否则为某一类的明细 ============ */
const router = useRouter();
const open = ref('');
const hot = ref('');
function drill(k){
  /* 已在下钻层：k 就是明细项（投标/履约保证金、三项开销）→ 跳投标管理看对应项目 */
  if(open.value){
    const it = view.value.items.find(x => x.key === k);
    if(it && it.v > 0) jump(open.value + '.' + k);            // 金额为 0 的明细没有项目可看
    return;
  }
  const c = cats.value.find(x => x.key === k);
  if(!c) return;
  if(c.parts.length > 1){ open.value = k; hot.value = ''; }   // 有多个明细 → 下钻
  else if(c.parts.length === 1) jump(k + '.' + c.parts[0].k); // 只有一个明细 → 直接跳列表
}
/* 带资金口径跳转投标管理：sc 跟随本卡的「本期 / 全部」口径 */
function jump(code){
  if(!can('view.manage')){ toast('当前角色没有「投标管理」权限，无法查看项目明细'); return; }
  router.push({ path:'/manage', query:{ fund: code, sc: scope.value } });
}
function back(){ open.value = ''; hot.value = ''; }

/* 当前层展示的数据（顶层 = 四类；下钻层 = 该类明细） */
const view = computed(() => {
  if(!open.value){
    const t = stat.value.total;
    return {
      title:'资金状态', up:'', total:t,
      items: cats.value.map(c => ({key:c.key, l:c.l, c:c.c, v:c.sum, deep:c.parts.length > 1,
        p: t > 0 ? c.sum / t * 100 : 0}))
    };
  }
  const c = cats.value.find(x => x.key === open.value);
  if(!c) return {title:'资金状态', up:'', total:stat.value.total, items:[]};
  return {
    title:c.l, up:'资金状态', total:c.sum,
    items: c.parts.map(p => ({key:p.k, l:p.l, c:p.c, v:stat.value[open.value][p.k], deep:false,
      p: c.sum > 0 ? stat.value[open.value][p.k] / c.sum * 100 : 0}))
  };
});

/* ============ 环形图几何 ============ */
const R = 54, SW = 21, CX = 70, CY = 70;
const C = 2 * Math.PI * R;                                 // 周长
const GAP = 3;                                             // 相邻扇块之间的留白（弧长）
const segs = computed(() => {
  const items = view.value.items.filter(i => i.v > 0);
  const tot = items.reduce((a, i) => a + i.v, 0);
  if(!tot) return [];
  let acc = 0;
  return items.map(i => {
    const frac = i.v / tot;
    const len = Math.max(0.6, frac * C - (items.length > 1 ? GAP : 0));
    const s = Object.assign({}, i, {
      frac, len, p: frac * 100,
      dash: len + ' ' + (C - len + 1),
      off: -acc * C
    });
    acc += frac;
    return s;
  });
});
const empty = computed(() => !segs.value.length);

/* 圆心文案：默认显示当前层合计，悬停时显示该扇块 */
const center = computed(() => {
  if(hot.value){
    const s = segs.value.find(x => x.key === hot.value);
    if(s) return {l:s.l, v:fmtMoney(s.v), p:s.p.toFixed(1) + '%', c:s.c};
  }
  return {l: view.value.title === '资金状态' ? '本期占用' : view.value.title,
    v: fmtMoney(view.value.total), p: '', c:''};
});
const pctTxt = v => v.toFixed(1) + '%';
</script>

<template>
  <div class="card">
    <div class="card-h fund-head">
      <h2>
        <button v-if="view.up" class="fd-back" @click="back" title="返回资金状态总览">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 3.5 5 8l4.5 4.5"/></svg>
        </button>
        {{ view.title }}
      </h2>
      <div class="seg fund-seg">
        <button :class="{on: scope==='cur'}" @click="scope='cur'; open=''">本期</button>
        <button :class="{on: scope==='all'}" @click="scope='all'; open=''">全部</button>
      </div>
    </div>

    <div class="card-b">
      <div class="fund-wrap" :class="{empty: empty}">
        <div class="fund-pie">
          <svg viewBox="0 0 140 140" preserveAspectRatio="xMidYMid meet" role="img"
            :aria-label="view.title + '构成：' + view.items.map(i => i.l + ' ¥' + fmtMoney(i.v)).join('，')">
            <circle class="fd-track" :cx="CX" :cy="CY" :r="R" :stroke-width="SW"/>
            <g transform="rotate(-90 70 70)">
              <circle v-for="s in segs" :key="s.key" class="fd-seg"
                :class="{dim: hot && hot !== s.key, on: hot === s.key}"
                :cx="CX" :cy="CY" :r="R" :stroke="s.c" :stroke-width="hot === s.key ? SW + 5 : SW"
                :stroke-dasharray="s.dash" :stroke-dashoffset="s.off"
                @mouseenter="hot = s.key" @mouseleave="hot = ''" @click="drill(s.key)"/>
            </g>
          </svg>
          <div class="fd-center">
            <b :style="{color: center.c || 'var(--pri)'}">{{ center.v }}</b>
            <span>{{ center.l }}</span>
            <em v-if="center.p">{{ center.p }}</em>
          </div>
        </div>

        <div v-if="empty" class="fund-empty">该范围内暂无资金占用记录</div>
        <div v-else class="fund-list">
          <button v-for="i in view.items" :key="i.key" class="fd-item"
            :class="{click: i.deep || i.v > 0, act: hot === i.key}"
            :title="i.deep ? '查看「' + i.l + '」的构成'
              : (i.v > 0 ? '查看含「' + i.l + '」的投标项目' : i.l)"
            @mouseenter="hot = i.key" @mouseleave="hot = ''" @click="drill(i.key)">
            <span class="dot" :style="{background: i.c}"></span>
            <span class="nm">{{ i.l }}</span>
            <span class="pct">{{ pctTxt(i.p) }}</span>
            <b class="amt">¥{{ fmtMoney(i.v) }}</b>
            <i class="fd-bar"><s :style="{width: Math.max(1.5, i.p) + '%', background: i.c}"></s></i>
          </button>
          <div v-if="open" class="fund-tip">点击费用查看对应的投标项目 ›</div>
        </div>
      </div>
    </div>
  </div>
</template>
