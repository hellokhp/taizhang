<script setup>
import { computed, watch, onUnmounted } from 'vue'
import { ST, detailDlg, closeDetail } from '../stores/bid'
import { F, bondState, shortType, bidEffortDays } from '../lib/fields'
import { fmtN, num } from '../lib/format'
import { canSee, isMasked } from '../stores/perm'

/* 详情数据来自 ST.rows（唯一数据源），轮询更新后弹窗内同步刷新
   两侧统一转字符串比较：后端 rid 为文本，导入/本地缓存的历史数据可能是数字 */
const row = computed(() => {
  const key = String(detailDlg.id ?? '');
  return ST.rows.find(r => String(r.id ?? '') === key) || null;
});

const RESULT_PILL = {'中标':'p-win','未中标':'p-lose','待开标':'p-wait','废标':'p-fail','流标':'p-fail','不投':'p-na','放弃':'p-na'};
const BOND_PILL = {'已退':'p-pay','已缴':'p-wait','未缴':'p-nopay','无需缴纳':'p-na','不予退还':'p-fail','待退':'p-nopay','未填':'p-na'};
const BID_PILL = {'未开始':'p-na','进行中':'p-wait','已结束':'p-pay'};
function resultPill(v){ return RESULT_PILL[v] || 'p-na'; }
function bondPill(v){ return BOND_PILL[v] || 'p-na'; }
function bidPill(v){ return BID_PILL[v] || 'p-na'; }

/* 按 fields.js 的分组顺序输出可见字段；projectName 已在顶部标题展示，不重复 */
const groups = computed(() => {
  if(!row.value) return [];
  const out = [];
  let cur = null;
  F.forEach(f => {
    if(f.k === 'projectName') return;
    if(!canSee(f.k)) return;
    if(!cur || cur.title !== f.g){ cur = {title: f.g, fields: []}; out.push(cur); }
    cur.fields.push(f);
  });
  return out;
});

/* 顶部关键指标：同样遵循字段权限；脱敏字段直接展示后端返回值 */
const kpis = computed(() => {
  const r = row.value;
  if(!r) return [];
  const out = [];
  if(canSee('ourPrice')) out.push({l:'我方报价', v: amount('ourPrice'), k:'ourPrice'});
  if(canSee('winAmount')) out.push({l:'中标金额', v: amount('winAmount'), k:'winAmount', win:true});
  if(canSee('bondAmount')) out.push({l:'投标保证金', v: amount('bondAmount'), k:'bondAmount'});
  if(canSee('bondStatus')){
    const masked = isMasked('bondStatus');
    out.push({
      l:'保证金状态',
      k:'bondStatus',
      v: masked ? String(r.bondStatus || '') : bondState(r),
      pill: masked ? '' : bondPill(bondState(r))
    });
  }
  return out;
});

function amount(k){
  const v = row.value ? row.value[k] : '';
  if(isMasked(k)) return String(v || '');
  const n = num(v);
  return n ? fmtN(n) + ' 万元' : '';
}
/* 派生字段（如标书投入时间）不在 row 里，取值时现算 */
function fieldVal(f){
  if(!row.value) return '';
  if(f.der) return f.k === 'bidEffort' ? bidEffortDays(row.value) : '';
  return row.value[f.k];
}
function hasVal(f){
  const v = fieldVal(f);
  return v !== undefined && v !== null && String(v).trim() !== '';
}
function valueText(f){
  if(!hasVal(f)) return '';
  const v = fieldVal(f);
  if(f.der) return isMasked(f.k) ? '***' : v + ' 天';
  if(isMasked(f.k)) return String(v);          // 脱敏值由后端返回，前端不再二次处理
  if(f.t === 'num') return num(v) ? fmtN(v) : '—';
  return String(v);
}

const metaText = computed(() => {
  const r = row.value;
  if(!r) return '';
  const bits = [];
  if(hasVal({k:'bidNo'})) bits.push(r.bidNo);
  if(hasVal({k:'pkg'})) bits.push(r.pkg);
  if(canSee('owner') && hasVal({k:'owner'})) bits.push(r.owner);
  if(canSee('region') && hasVal({k:'region'})) bits.push(r.region);
  return bits.join(' · ');
});
const bondTag = computed(() => {
  const r = row.value;
  if(!r || !canSee('bondType') || isMasked('bondType')) return '';
  return shortType(r.bondType);
});
const updatedInfo = computed(() => {
  const r = row.value;
  if(!r || !r._updatedBy) return '';
  const t = r._updatedAt ? new Date(r._updatedAt * 1000).toLocaleString('zh-CN', {hour12:false}) : '';
  return '最近由 ' + r._updatedBy + ' 更新于 ' + t;
});

/* Esc 关闭；仅在弹窗打开时挂监听，避免与其他弹窗互相干扰 */
function onKey(e){
  if(e.key === 'Escape' && detailDlg.open) closeDetail();
}
watch(() => detailDlg.open, v => {
  if(v) document.addEventListener('keydown', onKey);
  else document.removeEventListener('keydown', onKey);
});
onUnmounted(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <div class="mask" :class="{on: detailDlg.open}" @click.self="closeDetail()">
    <div class="dlg detail-dlg">
      <div class="dlg-h">
        <h3>标段详情</h3>
        <span class="act" style="color:var(--mut)" @click="closeDetail()">关闭 ✕</span>
      </div>

      <template v-if="row">
        <div class="dlg-b">
          <!-- 标题区：项目名 + 编号/业主 + 结果 -->
          <div class="detail-hero">
            <div class="detail-hero-main">
              <h4>{{ row.projectName || '未命名项目' }}</h4>
              <div class="detail-meta" v-if="metaText">{{ metaText }}</div>
            </div>
            <span class="pill" :class="resultPill(row.result)" v-if="hasVal({k:'result'}) && !isMasked('result')">{{ row.result }}</span>
            <span class="detail-masked" v-else-if="hasVal({k:'result'})">{{ row.result }}</span>
          </div>

          <!-- 关键指标 -->
          <div class="detail-kpis" v-if="kpis.length">
            <div v-for="x in kpis" :key="x.k" class="detail-kpi">
              <span class="dk-l">{{ x.l }}</span>
              <span class="dk-v" :class="{win: x.win}">
                <span v-if="x.pill && x.v" class="pill" :class="x.pill">{{ x.v }}</span>
                <template v-else-if="x.v">{{ x.v }}</template>
                <span v-else class="dt-empty">—</span>
              </span>
            </div>
          </div>

          <!-- 全部字段（按字段分组，只显示有权限的字段） -->
          <div class="detail-secs">
            <section v-for="g in groups" :key="g.title" class="detail-sec">
              <div class="detail-sec-title">{{ g.title }}</div>
              <div class="detail-grid">
                <div v-for="f in g.fields" :key="f.k" class="detail-row" :class="{wide: f.span === 2 || f.span === 3}">
                  <span class="dt-l">
                    {{ f.l }}<em v-if="isMasked(f.k)" class="ro-tag masked">脱敏</em>
                  </span>
                  <span class="dt-v">
                    <template v-if="f.k === 'result' && hasVal(f) && !isMasked(f.k)">
                      <span class="pill" :class="resultPill(row.result)">{{ row.result }}</span>
                    </template>
                    <template v-else-if="f.k === 'winner' && hasVal(f)">
                      <b v-if="row.winner === '本公司'" class="red">本公司</b>
                      <template v-else>{{ row.winner }}</template>
                    </template>
                    <template v-else-if="(f.k === 'bondStatus' || f.k === 'perfStatus') && hasVal(f) && !isMasked(f.k)">
                      <span class="pill" :class="bondPill(row[f.k])">{{ row[f.k] }}</span>
                      <span v-if="f.k === 'bondStatus' && bondTag" class="wtag">{{ bondTag }}</span>
                    </template>
                    <template v-else-if="f.k === 'bidStatus'">
                      <span class="pill" :class="bidPill(row.bidStatus || '未开始')">{{ row.bidStatus || '未开始' }}</span>
                    </template>
                    <template v-else-if="f.k === 'noticeUrl' && hasVal(f)">
                      <a :href="row.noticeUrl" target="_blank" rel="noopener" class="detail-link">打开中标公告 ↗</a>
                    </template>
                    <template v-else-if="hasVal(f)">
                      <span :class="{remark: f.t2 === 'area'}">{{ valueText(f) }}</span>
                    </template>
                    <span v-else class="dt-empty">—</span>
                  </span>
                </div>
              </div>
            </section>
          </div>

          <div class="detail-foot" v-if="updatedInfo">{{ updatedInfo }}</div>
        </div>
        <div class="dlg-f">
          <button class="btn" @click="closeDetail()">关闭</button>
        </div>
      </template>

      <template v-else>
        <div class="dlg-b"><div class="empty">该标段已被删除或不存在，请刷新后重试。</div></div>
        <div class="dlg-f"><button class="btn" @click="closeDetail()">关闭</button></div>
      </template>
    </div>
  </div>
</template>
