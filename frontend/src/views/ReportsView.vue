<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import { auth } from '../stores/auth'
import { staff, loadStaff, toast } from '../stores/bid'
import { can } from '../stores/perm'

/* ============ 基础 ============ */
const TODAY = new Date();
const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const today = iso(TODAY);
const me = auth.user || {};

const selUser = ref(me.username);
const year = ref(TODAY.getFullYear());
const years = [TODAY.getFullYear(), TODAY.getFullYear() - 1];
const openMonth = ref(TODAY.getMonth() + 1);            // 0 = 全部收起
const loading = ref(false);
const rep = ref({ user: null, reports: {} });           // reports: 'YYYY-MM-DD' -> 行
const selDate = ref(today);
const dlg = reactive({ open: false, rdate: '', content: '', plan: '', saving: false });

/* 左侧人员：自己置顶（可写自己的日报）；是否展示其他人员由角色权限 report.viewOthers 控制（管理员恒可看） */
const seeOthers = computed(() => can('report.viewOthers'));
const people = computed(() => {
  const list = [{ username: me.username, displayName: me.displayName || me.username, self: true }];
  if(!seeOthers.value) return list;
  (staff.list || []).forEach(p => {
    if(p.username !== me.username) list.push({ username: p.username, displayName: p.displayName });
  });
  return list;
});

async function load(){
  loading.value = true;
  try{
    const d = await api('/api/reports?user=' + encodeURIComponent(selUser.value) + '&ym=' + year.value);
    const map = {};
    (d.reports || []).forEach(r => { map[r.rdate] = r; });
    rep.value = { user: d.user, reports: map };
  }catch(e){ toast(e.message || '日报加载失败'); }
  loading.value = false;
}
watch([selUser, year], () => { selDate.value = today; load(); });
/* 权限中途被收回时，若正停留在他人视图则切回本人，避免后续 403 */
watch(seeOthers, v => { if(!v && selUser.value !== me.username) selUser.value = me.username; });
onMounted(() => { loadStaff(); load(); });

/* ============ 月度列表 ============ */
const months = computed(() => {
  const arr = [];
  for(let m = 1; m <= 12; m++){
    const pre = year.value + '-' + String(m).padStart(2, '0');
    let cnt = 0;
    for(const k in rep.value.reports) if(k.indexOf(pre) === 0) cnt++;
    arr.push({ m, pre, cnt });
  }
  return arr;
});

/* ============ 日历（周一起始） ============ */
const WEEK = ['一', '二', '三', '四', '五', '六', '日'];
const cal = computed(() => {
  const lead = (new Date(year.value, openMonth.value - 1, 1).getDay() + 6) % 7;
  const days = new Date(year.value, openMonth.value, 0).getDate();
  const cells = [];
  for(let i = 0; i < lead; i++) cells.push(null);
  for(let d = 1; d <= days; d++)
    cells.push(year.value + '-' + String(openMonth.value).padStart(2, '0') + '-' + String(d).padStart(2, '0'));
  return cells;
});

/* ============ 详情（选中某天） ============ */
const selInfo = computed(() => {
  const r = rep.value.reports[selDate.value];
  return {
    row: r || null,
    future: selDate.value > today,
    editable: (selUser.value === me.username || me.isAdmin) && selDate.value <= today
  };
});

function openDlg(){
  const r = rep.value.reports[selDate.value];
  dlg.rdate = selDate.value;
  dlg.content = r ? r.content : '';
  dlg.plan = r ? r.plan : '';
  dlg.open = true;
}
async function saveDlg(){
  if(dlg.saving) return;
  dlg.saving = true;
  try{
    await api('/api/reports', { method:'PUT', body: JSON.stringify({
      user: selUser.value, rdate: dlg.rdate, content: dlg.content, plan: dlg.plan }) });
    rep.value.reports[dlg.rdate] = { rdate: dlg.rdate, content: dlg.content.trim(),
      plan: dlg.plan.trim(), updatedAt: Date.now() / 1000 };
    dlg.open = false;
    toast('日报已保存');
  }catch(e){ toast(e.message || '保存失败'); }
  dlg.saving = false;
}
async function writeToday(){
  if(selUser.value !== me.username) selUser.value = me.username;
  year.value = TODAY.getFullYear();
  openMonth.value = TODAY.getMonth() + 1;
  selDate.value = today;
  await load();
  openDlg();
}

const fmtDate = s => {
  const [y, m, d] = s.split('-').map(Number);
  const w = ['日', '一', '二', '三', '四', '五', '六'][new Date(y, m - 1, d).getDay()];
  return y + '年' + m + '月' + d + '日 · 周' + w;
};
</script>

<template>
  <div class="rp-wrap">
    <!-- 左：人员列表 -->
    <div class="card rp-side">
      <div class="rp-side-h">人员</div>
      <div v-if="!seeOthers" class="rp-side-tip">当前角色仅可查看本人日报</div>
      <button v-for="p in people" :key="p.username" class="rp-person"
        :class="{on: selUser === p.username}" @click="selUser = p.username">
        <span class="nm">{{ p.displayName }}</span>
        <span v-if="p.self" class="me">我</span>
      </button>
    </div>

    <!-- 中：月度列表 + 日历概览 -->
    <div class="card rp-main">
      <div class="rp-mhead">
        <h2>工作日报</h2>
        <span v-if="rep.user" class="rp-who">{{ rep.user.displayName }} · {{ rep.user.roleName }}</span>
        <select v-model="year" style="margin-left:auto;">
          <option v-for="y in years" :key="y" :value="y">{{ y }} 年</option>
        </select>
        <button class="btn pri" @click="writeToday">写今日日报</button>
      </div>

      <div class="rp-months" :class="{loading}">
        <div v-for="mo in months" :key="mo.m" class="rp-month" :class="{open: openMonth === mo.m}">
          <button class="rp-month-h" @click="openMonth = openMonth === mo.m ? 0 : mo.m">
            <svg class="chev" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5 10.5 8 6 12.5"/></svg>
            <b>{{ mo.m }} 月</b>
            <span class="cnt">{{ mo.cnt ? '已填 ' + mo.cnt + ' 天' : '未填写' }}</span>
          </button>
          <div v-if="openMonth === mo.m" class="rp-cal">
            <span v-for="w in WEEK" :key="w" class="wd">{{ w }}</span>
            <template v-for="(d, i) in cal" :key="i">
              <span v-if="!d" class="rp-day blank"></span>
              <button v-else class="rp-day"
                :class="{has: rep.reports[d], today: d === today, future: d > today, sel: selDate === d}"
                @click="selDate = d">
                <span class="dn">{{ Number(d.slice(8)) }}</span>
                <span v-if="rep.reports[d]" class="mark">已填</span>
              </button>
            </template>
          </div>
        </div>
      </div>
    </div>

    <!-- 右：选中日期的详情 -->
    <div class="card rp-detail">
      <div class="rp-dhead">
        <b>{{ fmtDate(selDate) }}</b>
        <span v-if="selDate === today" class="rp-today-tag">今天</span>
      </div>

      <div class="sec">
        <div class="lbl">个人信息</div>
        <div class="rp-person-card">
          <b>{{ rep.user ? rep.user.displayName : '—' }}</b>
          <span>{{ rep.user ? rep.user.roleName + ' · ' + rep.user.username : '' }}</span>
        </div>
      </div>

      <div class="sec">
        <div class="lbl">当日工作内容</div>
        <div v-if="selInfo.row" class="txt">{{ selInfo.row.content || '（未填写）' }}</div>
        <div v-else class="empty">{{ selInfo.future ? '未来日期，暂不可填写' : '当日无日报' }}</div>
      </div>

      <div class="sec">
        <div class="lbl">次日工作计划</div>
        <div v-if="selInfo.row" class="txt">{{ selInfo.row.plan || '（未填写）' }}</div>
        <div v-else class="empty">—</div>
      </div>

      <button v-if="selInfo.editable" class="btn pri rp-edit-btn" @click="openDlg">
        {{ selInfo.row ? '编辑日报' : '填写日报' }}
      </button>
      <div v-else-if="!selInfo.row && selInfo.future" class="empty" style="text-align:center;">开始日期后即可填写</div>
    </div>

    <!-- 填写弹窗 -->
    <div class="mask" :class="{on: dlg.open}" @click.self="dlg.open = false">
      <div class="dlg" style="width:min(560px, calc(100vw - 48px));">
        <div class="dlg-h">
          <h3>{{ dlg.rdate }} 工作日报</h3>
          <span class="act" style="color:var(--mut);cursor:pointer;" @click="dlg.open = false">关闭 ✕</span>
        </div>
        <div class="dlg-b">
          <div class="fld" style="margin-bottom:12px;">
            <label>当日工作内容</label>
            <textarea v-model="dlg.content" rows="6" placeholder="今天做了什么（项目、进度、结果…）"></textarea>
          </div>
          <div class="fld">
            <label>次日工作计划</label>
            <textarea v-model="dlg.plan" rows="4" placeholder="明天打算做什么"></textarea>
          </div>
        </div>
        <div class="dlg-f">
          <button class="btn ghost" @click="dlg.open = false">取消</button>
          <button class="btn pri" :disabled="dlg.saving" @click="saveDlg">{{ dlg.saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
