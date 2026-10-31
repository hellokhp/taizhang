<script setup>
import { computed } from 'vue'
import { ST } from '../stores/bid'
import { dd, num, fmtN } from '../lib/format'
import { isNoBid, bondState, bondKind, shortType } from '../lib/fields'

const alerts = computed(() => {
  const out = [];
  ST.rows.forEach(r => {
    if(isNoBid(r)) return;
    const d = dd(r.openTime);
    if(r.bondStatus === '未缴' && r.result === '待开标'){
      out.push({lv:2, tag:'保证金未缴', r, sub:'保证金 ' + fmtN(r.bondAmount) + ' 万未缴纳' + (d !== null ? (d < 0 ? '，开标已过 ' : d === 0 ? '，今天开标 ' : '，距开标 ' + d + ' 天') : '') + ' · ' + (r.lead || '未指定负责人')});
    }
    if(r.result === '待开标' && d !== null && d >= 0 && d <= 7){
      out.push({lv:1, tag:'即将开标', r, sub:(r.openTime||'').slice(5) + ' 开标' + (d === 0 ? '（今天）' : '（' + d + ' 天后）') + ' · ' + (r.lead || '未指定负责人')});
    }
    if(bondState(r) === '待退' && d !== null && -d > 30){
      out.push({lv:3, tag:'保证金逾期', r, sub:'已开标 ' + (-d) + ' 天' + (bondKind(r) === 'guar' ? '仍未解保' : '仍未退回') + ' · ' + fmtN(r.bondAmount) + ' 万'});
    }
    if(!r.lead || !r.bidNo || !r.openTime){
      const miss = [!r.lead && '负责人', !r.bidNo && '招标编号', !r.openTime && '开标时间'].filter(Boolean).join('、');
      out.push({lv:3, tag:'信息缺失', r, sub:'缺少：' + miss});
    }
    if(r.result === '中标' && !num(r.winAmount)){
      out.push({lv:3, tag:'待补金额', r, sub:'已中标但未填中标金额'});
    }
    if(bondKind(r) === 'guar' && r.bondStatus === '已缴' && bondState(r) !== '待退'){
      const ddue = dd(r.bondDue);
      if(ddue !== null && ddue <= 30){
        out.push({lv: ddue < 0 ? 2 : 3, tag: ddue < 0 ? '保函已过期' : '保函将到期', r,
          sub: shortType(r.bondType) + '有效期至 ' + (r.bondDue||'').slice(5) + (ddue < 0 ? '（已过期 ' + (-ddue) + ' 天）' : '（' + ddue + ' 天后）') + ' · 需延期或解保'});
      }
    }
  });
  out.sort((a,b) => a.lv - b.lv);
  return out;
});
const top = computed(() => alerts.value.slice(0,12).map(o => ({...o, name: o.r.projectName || '未命名项目'})));
const moreCount = computed(() => Math.max(0, alerts.value.length - 12));
const lvCount = computed(() => ({
  1: alerts.value.filter(o => o.lv === 1).length,
  2: alerts.value.filter(o => o.lv === 2).length,
  3: alerts.value.filter(o => o.lv === 3).length
}));
function clsOf(lv){ return {1:'a-info', 2:'a-red', 3:'a-warn'}[lv]; }
</script>

<template>
  <div class="card">
    <div class="card-h">
      <h2>需要处理</h2>
      <div class="alert-sum">
        <span class="as"><i style="background:var(--win)"></i>紧急 <b>{{ lvCount[2] }}</b></span>
        <span class="as"><i style="background:var(--info)"></i>提醒 <b>{{ lvCount[1] }}</b></span>
        <span class="as"><i style="background:var(--warn)"></i>关注 <b>{{ lvCount[3] }}</b></span>
      </div>
    </div>
    <div class="card-b">
      <div class="alerts" v-if="top.length">
        <div class="alert" v-for="(o,i) in top" :key="i" :class="clsOf(o.lv)">
          <span class="tag">{{ o.tag }}</span>
          <div class="body">{{ o.name }}<em>{{ o.sub }}</em></div>
        </div>
        <div v-if="moreCount" class="alerts-more">… 另有 {{ moreCount }} 项待处理</div>
      </div>
      <div class="empty" v-else>暂无待处理事项</div>
    </div>
  </div>
</template>
