<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ST, bidFiles, filesState } from '../stores/bid'
import { buildAlerts, sevOf } from '../lib/alerts'
import { can, canAgg } from '../stores/perm'

const router = useRouter();

/* 计算口径统一在 lib/alerts.js（详情页共用）。
   字段不可见/脱敏时对应预警无意义，直接不出（读到的不是真实值）。
   「标书未上传」依赖标书文件数：需同时具备标书状态可聚合 + 文件查看权限，且文件数已拉取成功。 */
const alerts = computed(() => buildAlerts(ST.rows, {
  bid: canAgg('bidStatus'),
  bond: canAgg('bondStatus'),
  perf: canAgg('perfStatus'),
  minor: true,
  bidFile: canAgg('bidStatus') && can('view.files') && filesState.loaded,
  files: bidFiles
})
  /* 面板只保留逾期超过 30 天的待退费，避免长期未退的存量把列表刷满；
     完整清单见「需要处理」详情页 */
  .filter(o => o.type !== 'refund' || o.daysPast > 30));

const top = computed(() => alerts.value.slice(0,12).map(o => ({...o, name: o.r.projectName || '未命名项目'})));
const moreCount = computed(() => Math.max(0, alerts.value.length - 12));
/* 按视觉等级统计：标书未准备 sev=2 → 计入「紧急」（与红色标签一致） */
const lvCount = computed(() => ({
  1: alerts.value.filter(o => sevOf(o) === 1).length,
  2: alerts.value.filter(o => sevOf(o) === 2).length,
  3: alerts.value.filter(o => sevOf(o) === 3).length
}));
function clsOf(o){ return {1:'a-info', 2:'a-red', 3:'a-warn'}[sevOf(o)]; }
/* 点「紧急 / 提醒 / 关注」直接进详情页并按该等级筛选 */
function goLevel(lv){
  if(!can('view.alerts')) return;
  router.push('/alerts?lv=' + lv);
}
</script>

<template>
  <div class="card">
    <div class="card-h">
      <h2>需要处理</h2>
      <div class="alert-sum">
        <button class="as" :class="{go: can('view.alerts')}" :disabled="!can('view.alerts')"
          title="含：保证金未缴、标书未准备、标书未上传（点击查看）" @click="goLevel(2)">
          <i style="background:var(--win)"></i>紧急 <b>{{ lvCount[2] }}</b>
        </button>
        <button class="as" :class="{go: can('view.alerts')}" :disabled="!can('view.alerts')"
          title="含：7 天内即将开标（点击查看）" @click="goLevel(1)">
          <i style="background:var(--info)"></i>提醒 <b>{{ lvCount[1] }}</b>
        </button>
        <button class="as" :class="{go: can('view.alerts')}" :disabled="!can('view.alerts')"
          title="含：信息缺失、待补金额、保函到期、待退费（点击查看）" @click="goLevel(3)">
          <i style="background:var(--warn)"></i>关注 <b>{{ lvCount[3] }}</b>
        </button>
        <button v-if="can('view.alerts')" class="as-link" title="打开「需要处理」详情页，可按类型筛选与分页"
          @click="router.push('/alerts')">全部 ›</button>
      </div>
    </div>
    <div class="card-b">
      <div class="alerts" v-if="top.length">
        <div class="alert" v-for="(o,i) in top" :key="i" :class="clsOf(o)">
          <span class="tag">{{ o.tag }}</span>
          <div class="body">{{ o.name }}<em>{{ o.sub }}</em></div>
        </div>
        <div v-if="moreCount" class="alerts-more">… 另有 {{ moreCount }} 项待处理</div>
      </div>
      <div class="empty" v-else>暂无待处理事项</div>
    </div>
  </div>
</template>
