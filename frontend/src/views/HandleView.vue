<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ST, leadOpts, openDetail, openEdit, bidFiles, filesState } from '../stores/bid'
import { ALERT_TYPES, buildAlerts, countByType, sevOf } from '../lib/alerts'
import { can, canAgg, canSee } from '../stores/perm'
import { bondState, bidStatusOf } from '../lib/fields'
import { dd, num } from '../lib/format'

const route = useRoute();

/* 字段四档：隐藏/脱敏的字段不参与汇总，对应事项也不出（读到的不是真实值） */
const capBid  = computed(() => canAgg('bidStatus'));
const capBond = computed(() => canAgg('bondStatus'));
const capPerf = computed(() => canAgg('perfStatus'));
const capAmt  = computed(() => canAgg('bondAmount'));
/* 「标书未上传」需要标书状态可聚合 + 文件查看权限 + 文件数已拉取成功 */
const capFile = computed(() => can('view.files') && filesState.loaded);

/* 可用的筛选项：按权限收敛（关注级三类同样按字段可见性收敛） */
const types = computed(() => ALERT_TYPES.filter(t => {
  if(t.k === 'prep') return capBid.value;
  if(t.k === 'bidfile') return capBid.value && capFile.value;
  if(t.k === 'pay' || t.k === 'refund' || t.k === 'guar') return capBond.value || capPerf.value;
  if(t.k === 'info') return canSee('lead') || canSee('bidNo') || canSee('openTime');
  if(t.k === 'amount') return canAgg('winAmount');
  return true;
}));

/* 支持从看板汇总「紧急 / 提醒 / 关注」点进来直接筛等级：/alerts?lv=3 */
const f = reactive({
  type: '',
  lv: /^[123]$/.test(String(route.query.lv || '')) ? String(route.query.lv) : '',
  kw: '', lead: '', sort: 'urgent'
});
const page = ref(1);
const pageSize = ref(20);

/* minor: true → 同时纳入「关注」级事项（信息缺失 / 待补金额 / 保函到期），
   与看板「关注」计数同源，保证面板数字点进来都能找到对应条目 */
const all = computed(() => buildAlerts(ST.rows, {
  bid: capBid.value, bond: capBond.value, perf: capPerf.value, minor: true,
  bidFile: capBid.value && capFile.value, files: bidFiles
}));

/* 关键词 / 负责人筛选（等级筛选之前，用于给等级下拉带全局数量） */
const base = computed(() => all.value.filter(o => {
  const r = o.r;
  if(f.lead && (r.lead || '') !== f.lead) return false;
  const kw = f.kw.trim().toLowerCase();
  if(kw && ![r.projectName, r.bidNo, r.owner, r.pkg, r.lead]
    .some(v => String(v || '').toLowerCase().includes(kw))) return false;
  return true;
}));
/* 等级筛选后的集合：类型卡片数字与列表严格一致 */
const filtered = computed(() => f.lv ? base.value.filter(o => sevOf(o) === +f.lv) : base.value);
const counts = computed(() => countByType(filtered.value));
/* 各等级数量（与看板汇总同口径：sev 优先） */
const lvCount = computed(() => {
  const m = {1:0, 2:0, 3:0};
  base.value.forEach(o => { m[sevOf(o)] = (m[sevOf(o)] || 0) + 1; });
  return m;
});

const list = computed(() => {
  const arr = filtered.value.filter(o => !f.type || o.type === f.type);
  const copy = arr.slice();
  if(f.sort === 'date'){
    copy.sort((a, b) => (a.d === null) - (b.d === null) || (a.d || 0) - (b.d || 0));
  } else if(f.sort === 'past'){
    copy.sort((a, b) => b.daysPast - a.daysPast || ((a.d === null) - (b.d === null)) || (a.d || 0) - (b.d || 0));
  }
  return copy;   /* 默认 urgent：lib/alerts.js 已按 等级 → 开标时间 → 逾期天数 排好 */
});

const total = computed(() => list.value.length);
const pageCnt = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const paged = computed(() => list.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));

watch([() => f.type, () => f.lv, () => f.kw, () => f.lead, () => f.sort, pageSize], () => { page.value = 1; });
watch(pageCnt, n => { if(page.value > n) page.value = n; });
/* 权限变化后当前筛选项可能已不可用 */
watch(types, ts => { if(f.type && !ts.some(t => t.k === f.type)) f.type = ''; });

function go(p){ page.value = Math.min(Math.max(1, p), pageCnt.value); }
function resetFilter(){ Object.assign(f, { type: '', lv: '', kw: '', lead: '', sort: 'urgent' }); }

const visiblePages = computed(() => {
  const pc = pageCnt.value, cur = page.value;
  if(pc <= 7) return Array.from({length: pc}, (_, i) => i + 1);
  const arr = [1];
  const lo = Math.max(2, cur - 2), hi = Math.min(pc - 1, cur + 2);
  if(lo > 2) arr.push('...');
  for(let i = lo; i <= hi; i++) arr.push(i);
  if(hi < pc - 1) arr.push('...');
  arr.push(pc);
  return arr;
});

const typeMeta = k => ALERT_TYPES.find(t => t.k === k) || { l: k, hint: '' };
/* 空态占位列数（按字段权限动态算） */
const colCount = computed(() => 4
  + (canSee('openTime') ? 1 : 0) + (canSee('lead') ? 1 : 0) + (canSee('result') ? 1 : 0)
  + (capBid.value ? 1 : 0) + ((capBond.value || capPerf.value) ? 1 : 0));
const RESULT_PILL = {'中标':'p-win','未中标':'p-lose','待开标':'p-wait','废标':'p-fail','流标':'p-fail','不投':'p-na','放弃':'p-na'};
const BOND_PILL = {'已退':'p-pay','已缴':'p-wait','未缴':'p-nopay','无需缴纳':'p-na','不予退还':'p-fail','待退':'p-fail','未填':'p-na'};
const BID_PILL = {'未开始':'p-na','进行中':'p-wait','已结束':'p-pay'};
const bondPill = v => BOND_PILL[v] || 'p-na';

/* 开标时间的相对描述 */
function relTxt(r){
  const d = dd(r.openTime);
  if(d === null) return '';
  if(d === 0) return '今天';
  return d > 0 ? d + ' 天后' : '已过 ' + (-d) + ' 天';
}
function bondTxt(r){
  const s = bondState(r);
  const n = num(r.bondAmount);
  return { s, amt: capAmt.value && n > 0 ? n.toLocaleString('zh-CN', {maximumFractionDigits:2}) + ' 万' : '' };
}
function perfAmt(r){
  const n = num(r.perfAmount);
  return capAmt.value && n > 0 ? n.toLocaleString('zh-CN', {maximumFractionDigits:2}) + ' 万' : '';
}
</script>

<template>
  <div class="card hd-card">
    <div class="card-h" style="padding-bottom:0">
      <h2>需要处理</h2>
      <span class="hint">按事项类型筛选；点项目名看详情，点「去处理」直接改这条记录</span>
    </div>

    <!-- 类型筛选（数字随关键词/负责人筛选实时变化） -->
    <div class="hd-tabs">
      <button class="hd-tab" :class="{on: !f.type}" @click="f.type = ''">
        <span class="hd-tab-l">全部事项</span>
        <b>{{ counts.all }}</b>
      </button>
      <template v-for="(t, ti) in types" :key="t.k">
        <span v-if="t.minor && (ti === 0 || !types[ti - 1].minor)" class="hd-sep"
          title="关注级：数据完整性提示（不阻塞推进），看板「关注」计数即这三类">关注</span>
        <button class="hd-tab" :class="['k-' + t.k, {on: f.type === t.k}]"
          :title="t.hint" @click="f.type = f.type === t.k ? '' : t.k">
          <span class="hd-tab-l">{{ t.l }}</span>
          <b>{{ counts[t.k] || 0 }}</b>
        </button>
      </template>
    </div>

    <div class="toolbar">
      <input type="search" placeholder="搜索项目 / 招标编号 / 业主 / 负责人" style="width:250px;" v-model="f.kw">
      <select v-model="f.lead">
        <option value="">全部负责人</option>
        <option v-for="v in leadOpts" :key="v" :value="v">{{ v }}</option>
      </select>
      <select v-model="f.lv" title="按处理等级筛选（与看板「紧急 / 提醒 / 关注」同口径）">
        <option value="">全部等级（{{ base.length }}）</option>
        <option value="2">紧急（{{ lvCount[2] }}）</option>
        <option value="1">提醒（{{ lvCount[1] }}）</option>
        <option value="3">关注（{{ lvCount[3] }}）</option>
      </select>
      <select v-model="f.sort" title="排序方式">
        <option value="urgent">按紧迫度</option>
        <option value="date">按开标时间</option>
        <option value="past">按逾期天数</option>
      </select>
      <span class="tb-sep"></span>
      <button class="btn ghost" @click="resetFilter()">重置筛选</button>
      <span class="cnt">{{ total }} 项</span>
    </div>

    <div v-if="!canSee('projectName')" class="empty">当前账号看不到「项目名称」字段，无法展示处理清单。</div>

    <template v-else>
      <div class="tbl-wrap hd-wrap">
        <table class="hd-tbl">
          <!-- 百分比列宽（总和 100%）：表格恒等于容器宽度并随窗口缩放，内容在格内换行 -->
          <colgroup>
            <col style="width:8%">
            <col style="width:20%">
            <col v-if="canSee('openTime')" style="width:9.5%">
            <col v-if="canSee('lead')" style="width:7.5%">
            <col v-if="canSee('result')" style="width:8%">
            <col v-if="capBid" style="width:8%">
            <col v-if="capBond || capPerf" style="width:13%">
            <col style="width:19%">
            <col style="width:7%">
          </colgroup>
          <thead>
            <tr>
              <th>事项类型</th>
              <th>项目名称</th>
              <th v-if="canSee('openTime')">开标时间</th>
              <th v-if="canSee('lead')">负责人</th>
              <th v-if="canSee('result')">开标结果</th>
              <th v-if="capBid">标书状态</th>
              <th v-if="capBond || capPerf" title="投标保证金 / 履约保证金">保证金</th>
              <th>事项说明</th>
              <th style="text-align:right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(o, i) in paged" :key="o.type + '-' + o.r.id + '-' + i" :class="'r-' + o.type">
              <td>
                <span class="hd-tag" :class="'k-' + o.type" :title="typeMeta(o.type).hint">{{ o.tag }}</span>
              </td>
              <td>
                <a class="hd-name" :title="(o.r.projectName || '未命名项目') + '（点击查看详情）'" @click="openDetail(o.r.id)">
                  {{ o.r.projectName || '未命名项目' }}
                </a>
                <div v-if="o.r.pkg || o.r.bidNo" class="hd-sub">{{ [o.r.pkg, o.r.bidNo].filter(Boolean).join(' · ') }}</div>
              </td>
              <td v-if="canSee('openTime')" class="hd-time">
                <template v-if="o.r.openTime">
                  {{ (o.r.openTime || '').slice(5) }}
                  <em :class="{past: (o.d !== null && o.d < 0), today: o.d === 0}">{{ relTxt(o.r) }}</em>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td v-if="canSee('lead')">
                <span v-if="o.r.lead">{{ o.r.lead }}</span>
                <span v-else class="muted">未指定</span>
              </td>
              <td v-if="canSee('result')">
                <span class="pill" :class="RESULT_PILL[o.r.result || '待开标'] || 'p-na'">{{ o.r.result || '待开标' }}</span>
              </td>
              <td v-if="capBid">
                <span class="pill" :class="BID_PILL[bidStatusOf(o.r)] || 'p-na'">{{ bidStatusOf(o.r) }}</span>
              </td>
              <td v-if="capBond || capPerf" class="hd-bond">
                <div v-if="capBond" class="hd-bond-row">
                  <em>投标</em>
                  <span class="pill" :class="bondPill(bondTxt(o.r).s)">{{ bondTxt(o.r).s }}</span>
                  <i v-if="bondTxt(o.r).amt">{{ bondTxt(o.r).amt }}</i>
                </div>
                <div v-if="capPerf && o.r.perfStatus && o.r.perfStatus !== '无需缴纳'" class="hd-bond-row">
                  <em>履约</em>
                  <span class="pill" :class="bondPill(o.r.perfStatus)">{{ o.r.perfStatus }}</span>
                  <i v-if="perfAmt(o.r)">{{ perfAmt(o.r) }}</i>
                </div>
              </td>
              <td class="hd-reason">{{ o.reason }}</td>
              <td class="hd-ops">
                <button v-if="can('bid.edit')" class="btn pri hd-mini" title="打开编辑弹窗处理这条记录"
                  @click="openEdit(o.r.id)">去处理</button>
                <span v-else class="muted">—</span>
              </td>
            </tr>
            <tr v-if="!paged.length">
              <td :colspan="colCount">
                <div class="empty">当前筛选下没有需要处理的事项</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <select v-model.number="pageSize">
          <option :value="10">10 条/页</option>
          <option :value="20">20 条/页</option>
          <option :value="50">50 条/页</option>
          <option :value="100">100 条/页</option>
        </select>
        <span class="pg-info">第 <b>{{ page }}</b> / {{ pageCnt }} 页 · 共 {{ total }} 项</span>
        <div class="pg-btns">
          <button class="pg-btn" :disabled="page <= 1" @click="go(1)">«</button>
          <button class="pg-btn" :disabled="page <= 1" @click="go(page - 1)">‹</button>
          <template v-for="p in visiblePages" :key="p">
            <button v-if="p !== '...'" class="pg-btn" :class="{on: p === page}" @click="go(p)">{{ p }}</button>
            <span v-else class="pg-dots">…</span>
          </template>
          <button class="pg-btn" :disabled="page >= pageCnt" @click="go(page + 1)">›</button>
          <button class="pg-btn" :disabled="page >= pageCnt" @click="go(pageCnt)">»</button>
        </div>
        <span class="jump">跳转至
          <input type="number" min="1" :max="pageCnt" :value="page" @change="go(+($event.target.value || 1))">
          页
        </span>
      </div>
    </template>
  </div>
</template>
