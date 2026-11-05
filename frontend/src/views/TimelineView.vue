<script setup>
/* ================================================================
   标源总览：年 → 月 → 标书 三级展示
   左侧年列表（含筛选项匹配数量），右侧 1-12 月行可展开出当月标书；
   标书行展示 项目名 / 开标时间 / 招标方式 / 负责人 / 开标结果，
   项目名可点击复用全局标段详情弹窗（openDetail）。
   ================================================================ */
import { ref, reactive, computed, watch, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ST, recMonth, openDetail, leadOpts, methodOpts } from '../stores/bid'
import { RESULT_OPTS } from '../lib/fields'
import { canSee, isMasked, can } from '../stores/perm'

/* 本页独立筛选项（不占用投标管理页的 ST.f，两页互不影响） */
const f = reactive({kw:'', lead:'', result:'', method:''});
function resetFilter(){ Object.assign(f, {kw:'', lead:'', result:'', method:''}); }

/* 「未中标 / 废流标」是多状态合并项：看板跳转时会带这个组合值 */
const RESULT_MULTI = {'未中标|废标|流标': ['未中标','废标','流标']};

/* 筛选语义与台账 scopedRows 保持一致：结果为空按「待开标」算 */
function hit(r){
  const kw = f.kw.trim().toLowerCase();
  if(kw && ![r.projectName, r.bidNo, r.owner, r.winner, r.lead, r.remark, r.pkg]
    .some(v => String(v||'').toLowerCase().includes(kw))) return false;
  if(f.lead && (r.lead||'') !== f.lead) return false;
  if(f.result){
    const want = RESULT_MULTI[f.result] || [f.result];
    if(want.indexOf(r.result || '待开标') < 0) return false;
  }
  if(f.method && (r.method||'') !== f.method) return false;
  return true;
}

/* 归月口径：与看板统计一致（ST.basis：开标 / 交标），保证跳转后的数量对得上 */
const ymOfOpen = r => recMonth(r);
const basisWord = computed(() => ST.basis === 'deadline' ? '交标' : '开标');
const matched = computed(() => ST.rows.filter(hit));
const noTime = computed(() => matched.value.filter(r => !ymOfOpen(r)).length);

/* 年列表（降序）：年 + 该年命中数 */
const years = computed(() => {
  const map = new Map();
  matched.value.forEach(r => {
    const ym = ymOfOpen(r);
    if(!ym) return;
    const y = ym.slice(0, 4);
    map.set(y, (map.get(y) || 0) + 1);
  });
  return [...map.entries()].sort((a,b) => b[0].localeCompare(a[0])).map(([y, n]) => ({y, n}));
});
/* 默认选中：看板跳转指定的年（若有）→ 今年（若有数据）→ 数据里最近的一年 */
const selYear = ref('');
const wantYear = ref('');       // 跳转指定的年份，数据到达后仍优先命中
function pickYear(y){ wantYear.value = ''; selYear.value = y; }
watch(years, list => {
  if(wantYear.value && list.some(x => x.y === wantYear.value)){ selYear.value = wantYear.value; return; }
  if(!list.some(x => x.y === selYear.value)){
    const cur = String(new Date().getFullYear());
    selYear.value = list.some(x => x.y === cur) ? cur : (list[0] ? list[0].y : '');
  }
}, {immediate:true});

/* 选中年的 1-12 月行（无数据的月也列出，便于对照） */
const months = computed(() => {
  const y = selYear.value;
  const byM = Array.from({length:12}, (_, i) => ({m:i+1, ym:y + '-' + String(i+1).padStart(2,'0'), rows:[]}));
  matched.value.forEach(r => {
    const ym = ymOfOpen(r);
    if(y && ym && ym.slice(0,4) === y) byM[+ym.slice(5,7) - 1].rows.push(r);
  });
  byM.forEach(x => x.rows.sort((a,b) => String(a.openTime||'').localeCompare(String(b.openTime||''))));
  return byM;
});

/* 展开状态（以 年-月 为 key，切年保留） */
const expanded = reactive(new Set());
function toggle(ym){ expanded.has(ym) ? expanded.delete(ym) : expanded.add(ym); }
const allOpen = computed(() => months.value.every(x => expanded.has(x.ym)));
function toggleAll(){
  const every = allOpen.value;
  months.value.forEach(x => { every ? expanded.delete(x.ym) : (x.rows.length && expanded.add(x.ym)); });
}

/* 列显隐跟随字段权限；hidden 不渲染，masked 原样展示后端返回值 */
const COLS = [
  {k:'projectName', l:'项目名称', w:'minmax(220px,2.2fr)'},
  {k:'openTime',    l:'开标时间', w:'118px'},
  {k:'method',      l:'招标方式', w:'118px'},
  {k:'lead',        l:'负责人',   w:'100px'},
  {k:'result',      l:'开标结果', w:'92px'}
];
const visCols = computed(() => COLS.filter(c => canSee(c.k)));
const gridCols = computed(() => visCols.value.map(c => c.w).join(' '));

const RESULT_PILL = {'中标':'p-win','未中标':'p-lose','待开标':'p-wait','废标':'p-fail','流标':'p-fail','不投':'p-na','放弃':'p-na'};
function openDay(v){ return String(v||'').replace(/^\d{4}-/, ''); }   // 年月已在层级里，日够用
function resultOf(r){ return r.result || '待开标'; }

/* ==================== 从看板跳转带入的条件 ====================
   ?y=2026&m=11&result=中标&lead=张三
   单月：选中该年并展开该月并滚动定位；整年：只选中年份，不展开月份 */
const route = useRoute();
const router = useRouter();
const canManage = computed(() => can('view.manage'));
const hitKey = ref('');       // 本次定位到的高亮月份

/* 「另有 N 条未填…」→ 投标管理页，只列这批没进树的标段（带当前筛选条件） */
function goNoTime(){
  if(!canManage.value) return;
  router.push({path:'/manage', query:{
    noTime: '1',
    kw: f.kw || '',
    lead: f.lead || '',
    result: f.result || '',
    method: f.method || ''
  }});
}

function applyQuery(){
  const q = route.query;
  const y = String(q.y || '').slice(0, 4);
  f.kw = String(q.kw || '');
  f.lead = String(q.lead || '');
  f.method = String(q.method || '');
  /* 结果值不在下拉项里时（兼容手改 URL）回退为全部 */
  const rs = String(q.result || '');
  f.result = (!rs || rs === '全部') ? '' : rs;
  if(/^\d{4}$/.test(y)){ wantYear.value = y; selYear.value = y; }
  const m = String(q.m || '');
  if(/^\d{1,2}$/.test(m) && +m >= 1 && +m <= 12 && /^\d{4}$/.test(y)){
    const key = y + '-' + String(+m).padStart(2, '0');
    expanded.add(key);
    focusMonth(key);
  }
}
async function focusMonth(key){
  await nextTick();
  const el = document.querySelector('.tl-month[data-ym="' + key + '"]');
  if(!el) return;
  el.scrollIntoView({block:'center', behavior:'smooth'});
  hitKey.value = key;
  clearTimeout(focusMonth.tm);
  focusMonth.tm = setTimeout(() => { hitKey.value = ''; }, 2400);
}
onMounted(applyQuery);
watch(() => route.query, applyQuery);
</script>

<template>
  <div class="card" style="margin-bottom:0;">
    <div class="card-h" style="padding-bottom:0">
      <h2>标源总览</h2>
      <span class="hint">按{{ basisWord }}时间分级：年 → 月 → 标书；点月份行展开当月标书，点项目名查看详情</span>
    </div>
    <div class="toolbar tbar">
      <input type="search" placeholder="搜索项目 / 招标编号 / 业主 / 中标单位" style="width:240px;" v-model="f.kw">
      <select v-model="f.lead">
        <option value="">全部负责人</option>
        <option v-for="v in leadOpts" :key="v" :value="v">{{ v }}</option>
      </select>
      <select v-model="f.result">
        <option value="">全部结果</option>
        <option v-for="v in RESULT_OPTS" :key="v" :value="v">{{ v }}</option>
        <option value="未中标|废标|流标">未中标 / 废流标</option>
      </select>
      <select v-model="f.method">
        <option value="">全部招标方式</option>
        <option v-for="v in methodOpts" :key="v" :value="v">{{ v }}</option>
      </select>
      <span class="tb-sep"></span>
      <button class="btn ghost" @click="resetFilter()">重置筛选</button>
      <span class="cnt">筛选项匹配 {{ matched.length }} 条</span>
    </div>

    <div class="tl-layout">
      <!-- 左：年 -->
      <aside class="tl-years">
        <button v-for="x in years" :key="x.y" class="tl-year" :class="{on: x.y === selYear}" @click="pickYear(x.y)">
          <b>{{ x.y }}年</b>
          <em>{{ x.n }} 条</em>
        </button>
        <div v-if="!years.length" class="tl-none">暂无匹配数据</div>
      </aside>

      <!-- 右：月 → 标书 -->
      <div class="tl-main" v-if="selYear">
        <div class="tl-main-h">
          <span class="lbl">{{ selYear }}年 · 按月分布</span>
          <button v-if="noTime" class="tl-note" :class="{off: !canManage}"
            :title="canManage ? '在投标管理中查看这些未填' + basisWord + '时间的标段' : '无投标管理权限，无法跳转'"
            @click="goNoTime()">另有 {{ noTime }} 条未填{{ basisWord }}时间，未列入<i v-if="canManage">去查看 →</i></button>
          <button class="chip" style="margin-left:auto" @click="toggleAll()">{{ allOpen ? '全部收起' : '展开有标月份' }}</button>
        </div>
        <div v-for="x in months" :key="x.ym" class="tl-month" :data-ym="x.ym" :class="{hit: hitKey === x.ym}">
          <div class="tl-month-h" :class="{empty: !x.rows.length}" @click="x.rows.length && toggle(x.ym)">
            <i class="tl-arrow" :class="{on: expanded.has(x.ym)}">▶</i>
            <b>{{ x.m }}月</b>
            <span class="tl-cnt" :class="{zero: !x.rows.length}">{{ x.rows.length }} 条</span>
          </div>
          <div v-if="expanded.has(x.ym) && x.rows.length" class="tl-bids">
            <div class="tl-row tl-head" :style="{gridTemplateColumns: gridCols}">
              <span v-for="c in visCols" :key="c.k">{{ c.l }}</span>
            </div>
            <div v-for="r in x.rows" :key="r.id" class="tl-row" :style="{gridTemplateColumns: gridCols}">
              <template v-for="c in visCols" :key="c.k">
                <span v-if="c.k === 'projectName'" class="tl-proj" title="查看详情" @click="openDetail(r.id)">{{ r.projectName || '未命名项目' }}</span>
                <span v-else-if="c.k === 'openTime'" class="tl-time">{{ openDay(r.openTime) }}</span>
                <span v-else-if="c.k === 'method'">{{ r.method || '—' }}</span>
                <span v-else-if="c.k === 'lead'">{{ r.lead || '—' }}</span>
                <span v-else-if="c.k === 'result'">
                  <span v-if="!isMasked('result')" class="pill" :class="RESULT_PILL[resultOf(r)] || 'p-na'">{{ resultOf(r) }}</span>
                  <template v-else>{{ resultOf(r) }}</template>
                </span>
              </template>
            </div>
          </div>
        </div>
      </div>
      <div class="tl-main" v-else><div class="empty">暂无匹配的标源数据，请调整上方筛选项。</div></div>
    </div>
  </div>
</template>
