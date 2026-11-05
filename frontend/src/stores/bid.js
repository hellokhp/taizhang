/* ================================================================
   台账主 Store：全局响应式状态 + 统计派生 + 数据操作 + 服务器同步
   ================================================================ */
import { reactive, computed, watch } from 'vue'
import * as XLSX from 'xlsx'
import { TODAY, pad, fmtYM, parseD, ymOf, yOf, dd, num, fmtMoney, fmtN, uid } from '../lib/format'
import { F, METHOD_OPTS, S0, SAMPLE, rowsFromMatrix, rowKey, cleanRow, isNoBid, isDecided, bondState, bondKind, stampBidStatus, bidEffortDays } from '../lib/fields'
import { defaultTableCfg, normalizeTableCfg, clampWidth, clampRowH } from '../lib/tableCols'
import { isoToday, rowInFund } from '../lib/fund'
import { api } from '../api/client'
import { auth } from './auth'
import { canWrite } from './perm'

/* ==================== 全局状态 ==================== */
const KEY = 'workbuddy_bid_dash_v1';
function toISODate(d){ return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()); }
function firstOfMonth(d){ return new Date(d.getFullYear(), d.getMonth(), 1); }
function lastOfMonth(d){ return new Date(d.getFullYear(), d.getMonth()+1, 0); }
function monthAgo(n){ return new Date(TODAY.getFullYear(), TODAY.getMonth() - n, 1); }

// 默认本月起止日期（YYYY-MM-DD）
const _curStart = toISODate(firstOfMonth(TODAY));
const _curEnd   = toISODate(lastOfMonth(TODAY));

export const ST = reactive({
  rows: [],
  ymFrom: fmtYM(TODAY),               // 统计区间起（YYYY-MM）
  ymTo:   fmtYM(TODAY),               // 统计区间止（YYYY-MM，含两端）
  basis: 'openTime',                  // 统计口径：openTime 开标 / deadline 交标（看板用，台账日期区间也用此字段）
  dateFrom: _curStart,                // 台账页：区间起止（含两端）
  dateTo:   _curEnd,
  f: {kw:'', lead:'', result:'', method:''},
  onlyNoTime: false,                  // 仅看未填统计口径时间的行（来自标源总览「未列入」跳转）
  fund: '',                           // 资金口径筛选（'back.bond' 等，来自看板「资金状态」叶子跳转；空 = 关闭）
  sort: {k:'openTime', d:-1},
  page: 1,
  pageSize: 20,
  selIds: [],                         // 选中行 id 列表（BidTable 多选）
  tableCfg: defaultTableCfg()         // 表格列配置：当前列/列宽/行高 + 已保存的命名配置
});
export const cfgDlg = reactive({open:false});   // 列配置弹窗
export const detailDlg = reactive({open:false, id:''});  // 标段详情弹窗（全局可复用）
/* 其他页面需要查看项目详情时直接调用 openDetail(rowId) */
export function openDetail(id){
  if(!id) return;
  detailDlg.id = String(id);
  detailDlg.open = true;
}
export function closeDetail(){ detailDlg.open = false; }
/* 标段附件上传弹窗：把文件存到该标段在文件存储里的项目目录（bidfile/年/月/项目名） */
export const upDlg = reactive({open:false, id:''});
export function openBidUpload(id){
  if(!id) return;
  upDlg.id = String(id);
  upDlg.open = true;
}
export function closeBidUpload(){ upDlg.open = false; }
export const dlg = reactive({open:false, id:null, base:null});  // 编辑弹窗；base = 打开时的服务器快照（算脏字段用）
export const imp = reactive({open:false, name:'', list:[], opt:{needLead:true, skipNo:true, overwrite:true}}); // 导入弹窗
export const toastBox = reactive({msg:'', on:false});
let toastTm = null;
export function toast(msg){
  toastBox.msg = msg; toastBox.on = true;
  clearTimeout(toastTm);
  toastTm = setTimeout(() => { toastBox.on = false; }, 2400);
}

/* ==================== 派生（纯函数） ==================== */
export function recMonth(r){ return ymOf(r[ST.basis] || r.openTime || r.deadline); }

/* 看板统计区间：起止颠倒自动交换，非法值忽略 */
function normYm(v){ return /^\d{4}-\d{2}$/.test(String(v || '')) ? String(v) : ''; }
export function setYmRange(a, b){
  const na = normYm(a), nb = normYm(b);
  if(!na || !nb) return;
  ST.ymFrom = na <= nb ? na : nb;
  ST.ymTo   = na <= nb ? nb : na;
}
/* 快捷区间：month 本月 / year 本年 / prevYear 去年整年 */
export function setQuickRange(kind){
  const y = TODAY.getFullYear(), m = pad(TODAY.getMonth() + 1);
  if(kind === 'month'){ setYmRange(y + '-' + m, y + '-' + m); }
  else if(kind === 'year'){ setYmRange(y + '-01', y + '-12'); }
  else if(kind === 'prevYear'){ setYmRange((y - 1) + '-01', (y - 1) + '-12'); }
}

/* ==================== 统计（computed） ==================== */
export const S = computed(() => {
  const from = ST.ymFrom, to = ST.ymTo;
  const live = ST.rows.filter(r => !isNoBid(r));
  const noBidRows = ST.rows.filter(isNoBid);
  const cur = live.filter(r => { const m = recMonth(r); return m && m >= from && m <= to; });
  /* 年度累计：区间所跨年份的整年数据（区间在本年内时即「今年累计」） */
  const yA = yOf(from) + '-01', yB = yOf(to) + '-12';
  const ytd = live.filter(r => { const m = recMonth(r); return m && m >= yA && m <= yB; });
  const won = cur.filter(r => r.result === '中标');
  const decided = cur.filter(isDecided);
  const waiting = cur.filter(r => r.result === '待开标');
  const lost = cur.filter(r => r.result === '未中标');
  const fail = cur.filter(r => r.result === '废标' || r.result === '流标');
  const projSet = new Set(cur.map(r => (r.projectName||'') + '|' + (r.pkg||'')));
  const bondIn = live.filter(r => r.bondStatus === '已缴');
  const pend = bondIn.filter(r => r.result === '待开标');
  const pendAmt = pend.reduce((s,r) => s + num(r.bondAmount), 0);
  const nopay = live.filter(r => r.bondStatus === '未缴');
  const nopayAmt = nopay.reduce((s,r) => s + num(r.bondAmount), 0);
  const back = live.filter(r => r.bondStatus === '已退');
  const backAmt = back.reduce((s,r) => s + num(r.bondAmount), 0);
  const toBack = bondIn.filter(r => r.result !== '待开标');
  const toBackAmt = toBack.reduce((s,r) => s + num(r.bondAmount), 0);
  const active = live.filter(r => ['无需缴纳','不予退还','已退'].indexOf(r.bondStatus) < 0);
  const fundRows = active.filter(r => bondKind(r) === 'fund');
  const guarRows = active.filter(r => bondKind(r) === 'guar');
  const fundAmt = fundRows.reduce((a,r) => a + num(r.bondAmount), 0);
  const guarAmt = guarRows.reduce((a,r) => a + num(r.bondAmount), 0);
  const over = toBack.filter(r => { const d = dd(r.openTime); return d !== null && -d > 30; });
  const nextOpen = live.filter(r => r.result === '待开标' && parseD(r.openTime))
    .sort((a,b) => parseD(a.openTime) - parseD(b.openTime))[0];
  const ytdWon = ytd.filter(r => r.result === '中标').length;
  const ytdDecided = ytd.filter(isDecided).length;
  return {ymFrom:from, ymTo:to, cur, ytd, won, decided, waiting, lost, fail, live, noBidCount:noBidRows.length,
    projCount:projSet.size, rate: decided.length ? won.length/decided.length : 0,
    ytdRate: ytdDecided ? ytdWon/ytdDecided : 0,
    fundRows, guarRows, fundAmt, guarAmt,
    pend, pendAmt, nopay, nopayAmt, back, backAmt, toBack, toBackAmt, over, nextOpen};
});

/* ==================== 台账筛选 + 排序（computed） ==================== */
export function rowDate(r){ return r[ST.basis] || r.openTime || r.deadline || ''; }
export const scopedRows = computed(() => {
  let arr = ST.rows.slice();
  // 日期区间过滤（按 ST.basis 取字段，YYYY-MM-DD 字符串字典序即时间序）
  const from = (ST.dateFrom || '').trim();
  const to   = (ST.dateTo   || '').trim();
  if(ST.onlyNoTime){
    /* 仅看「未填统计口径时间」：这类行本来就会被区间剔除，故区间在此模式下不生效 */
    arr = arr.filter(r => !recMonth(r));
  }else if(from || to){
    arr = arr.filter(r => {
      const d = rowDate(r);
      if(!d) return false;                       // 无时间字段的记录不进区间
      if(from && d < from) return false;
      if(to   && d > to)   return false;
      return true;
    });
  }
  /* 资金口径筛选（看板「资金状态」叶子跳转）：只留属于该口径的行（与饼图归桶同源，剔除「不投」） */
  if(ST.fund){
    const t0 = isoToday();
    arr = arr.filter(r => !isNoBid(r) && rowInFund(r, ST.fund, t0));
  }
  const f = ST.f, kw = f.kw.trim().toLowerCase();
  if(kw) arr = arr.filter(r => [r.projectName, r.bidNo, r.owner, r.winner, r.lead, r.remark, r.pkg]
    .some(v => String(v||'').toLowerCase().includes(kw)));
  if(f.lead) arr = arr.filter(r => (r.lead||'') === f.lead);
  if(f.result){
    /* 支持组合值（如 未中标|废标|流标），与标源总览的跳转口径一致 */
    const want = String(f.result).split('|');
    arr = arr.filter(r => want.indexOf(r.result || '待开标') >= 0);
  }
  if(f.method) arr = arr.filter(r => (r.method||'') === f.method);
  const {k, d} = ST.sort;
  const NUM_SORT = ['budget','ourPrice','winAmount','bondAmount','perfAmount','docFee','travelFee','sampleFee','bidEffort'];
  const DATE_SORT = ['openTime','deadline','bondPayDate','bondBackDate','bondDue','perfPayDate','perfBackDate','perfDue'];
  arr.sort((a,b) => {
    let x = a[k], y = b[k];
    if(k === 'amount'){ x = a.result === '中标' ? num(a.winAmount)||num(a.ourPrice) : num(a.ourPrice); y = b.result === '中标' ? num(b.winAmount)||num(b.ourPrice) : num(b.ourPrice); }
    if(k === 'bondStatus'){ x = bondState(a); y = bondState(b); }
    if(NUM_SORT.indexOf(k) >= 0) return (num(x) - num(y)) * d;
    if(DATE_SORT.indexOf(k) >= 0){ x = parseD(x); y = parseD(y); if(!x) return 1; if(!y) return -1; return (x - y) * d; }
    return String(x||'').localeCompare(String(y||''), 'zh') * d;
  });
  return arr;
});
/* 分页派生 */
export const filteredCnt = computed(() => scopedRows.value.length);
export const pageCnt = computed(() => Math.max(1, Math.ceil(filteredCnt.value / ST.pageSize)));
export const pagedRows = computed(() => {
  const i = Math.max(0, ST.page - 1) * ST.pageSize;
  return scopedRows.value.slice(i, i + ST.pageSize);
});
/* 员工名单（服务器上非 admin 用户，用于负责人筛选与标书负责人/协作人/审核人选择） */
export const staff = reactive({ list: [] });
export async function loadStaff(){
  try{ const r = await api('/api/staff'); staff.list = r.staff || []; }catch(e){ /* 静默，回退台账历史人名 */ }
}
/* 负责人筛选选项：服务器用户名单 ∪ 台账历史人名（兼容外部人员/离职员工） */
export const leadOpts = computed(() => {
  const set = new Set(staff.list.map(p => p.displayName));
  ST.rows.forEach(r => { const v = (r.lead || '').trim(); if(v) set.add(v); });
  return [...set].sort((a,b) => a.localeCompare(b,'zh'));
});
export const methodOpts = computed(() => { const m = [...new Set(ST.rows.map(r => r.method).filter(Boolean))]; return m.length ? m : METHOD_OPTS; });
export const tblCnt = computed(() => {
  let rangeTxt;
  if(ST.onlyNoTime) rangeTxt = '仅看未填' + (ST.basis === 'deadline' ? '交标' : '开标') + '时间';
  else if(ST.dateFrom && ST.dateTo) rangeTxt = ST.dateFrom + ' ~ ' + ST.dateTo;
  else if(ST.dateFrom) rangeTxt = ST.dateFrom + ' 起';
  else if(ST.dateTo)   rangeTxt = '至 ' + ST.dateTo;
  else rangeTxt = '全部';
  return '筛选后 ' + filteredCnt.value + ' 条 / 共 ' + ST.rows.length + ' 条（范围：' + rangeTxt + '）';
});

/* ==================== 本机存取 ==================== */
let cachedOwner = '';            // 本机缓存归属账号（防止换人登录后看到上一个人的可见字段）
function load(){
  let raw = null;
  try{ raw = localStorage.getItem(KEY); }catch(e){}
  if(raw){
    try{
      const o = JSON.parse(raw);
      cachedOwner = o._owner || '';
      ST.rows = (o.rows || []).map(r => Object.assign({id:uid()}, r));
      if(o.basis) ST.basis = o.basis;
      if(o.ymFrom && o.ymTo){ ST.ymFrom = o.ymFrom; ST.ymTo = o.ymTo; }
      else if(o.ym){ ST.ymFrom = o.ym; ST.ymTo = o.ym; }   // 兼容旧缓存（单月）
      if(o.dateFrom) ST.dateFrom = o.dateFrom;
      if(o.dateTo)   ST.dateTo   = o.dateTo;
      ST.tableCfg = normalizeTableCfg(o.tableCfg);
      setYmRange(ST.ymFrom, ST.ymTo);
    }catch(e){ ST.rows = SAMPLE.map(r => ({id:uid(), ...r})); }
  }else{
    ST.rows = SAMPLE.map(r => ({id:uid(), ...r}));
  }
}
function save(){
  try{ localStorage.setItem(KEY, JSON.stringify({rows:ST.rows, basis:ST.basis, ymFrom:ST.ymFrom, ymTo:ST.ymTo,
    dateFrom:ST.dateFrom, dateTo:ST.dateTo, tableCfg:ST.tableCfg,
    _owner: (auth.user && auth.user.username) || ''})); }
  catch(e){ toast('保存失败：浏览器存储不可用'); }
}
watch(() => [ST.rows, ST.basis, ST.ymFrom, ST.ymTo, ST.dateFrom, ST.dateTo], save, {deep:true});
/* 列宽/行高拖动是高频变更：单独防抖持久化，避免每次 pointermove 都序列化全部台账 */
let cfgSaveTm = null;
watch(() => ST.tableCfg, () => {
  clearTimeout(cfgSaveTm);
  cfgSaveTm = setTimeout(save, 250);
}, {deep:true});

/* ==================== 表格列配置 ==================== */
export function openCfg(){ cfgDlg.open = true; }
export function setColWidth(k, w){ ST.tableCfg.widths[k] = clampWidth(w); }
export function setRowHeight(h){ ST.tableCfg.rowH = clampRowH(h); }
/* 应用命名配置；id 为空表示恢复默认视图 */
export function applyTableCfg(id){
  const c = ST.tableCfg.list.find(x => x.id === id);
  if(c){
    ST.tableCfg.activeId = c.id;
    ST.tableCfg.cols = c.cols.slice();
    ST.tableCfg.widths = Object.assign({}, c.widths);
    ST.tableCfg.rowH = clampRowH(c.rowH);
  }else{
    const d = defaultTableCfg();
    ST.tableCfg.activeId = '';
    ST.tableCfg.cols = d.cols;
    ST.tableCfg.widths = d.widths;
    ST.tableCfg.rowH = d.rowH;
  }
}
/* 按名称保存当前列/列宽/行高：同名配置覆盖，否则新建；返回保存后的配置 */
export function saveTableCfg(name, snap){
  const n = String(name || '').trim().slice(0, 30);
  if(!n) return null;
  let c = ST.tableCfg.list.find(x => x.name === n);
  if(!c){ c = {id:uid(), name:n}; ST.tableCfg.list.push(c); }
  c.cols = snap.cols.slice();
  c.widths = Object.assign({}, snap.widths);
  c.rowH = clampRowH(snap.rowH);
  ST.tableCfg.activeId = c.id;
  return c;
}
export function deleteTableCfg(id){
  const i = ST.tableCfg.list.findIndex(x => x.id === id);
  if(i >= 0) ST.tableCfg.list.splice(i, 1);
  if(ST.tableCfg.activeId === id) ST.tableCfg.activeId = '';
}

let firstRun = false;
try{ firstRun = !localStorage.getItem(KEY); }catch(e){}
load();                          // 先读本机缓存，秒开界面

/* ==================== 未保存草稿（本机暂存，不进台账、不同步） ==================== */
const DKEY = 'workbuddy_bid_drafts_v1';
function readDrafts(){
  try{ return JSON.parse(localStorage.getItem(DKEY)) || {}; }catch(e){ return {}; }
}
function writeDrafts(d){
  try{ localStorage.setItem(DKEY, JSON.stringify(d)); }catch(e){ toast('草稿保存失败：浏览器存储不可用'); }
}
const DRAFT_STRIP = ['id', '_updatedBy', '_updatedAt'];
export function getDraft(id){
  const d = readDrafts();
  return id ? ((d.rows || {})[id] || null) : (d.__new || null);
}
export function setDraft(id, fields){
  const d = readDrafts();
  const o = {};
  Object.keys(fields).forEach(k => {
    if(DRAFT_STRIP.indexOf(k) < 0 && String(k).indexOf('__') !== 0) o[k] = fields[k];
  });
  if(id){ d.rows = d.rows || {}; d.rows[id] = o; }
  else{ d.__new = o; }
  writeDrafts(d);
}
export function clearDraft(id){
  const d = readDrafts();
  if(id){ if(d.rows) delete d.rows[id]; }
  else{ delete d.__new; }
  writeDrafts(d);
}

/* ==================== 服务器同步 ==================== */
let lastSync = 0;                       // 服务器时间戳（秒）
const pendingPush = new Map();          // 待上传的行 rid -> row
const pendingDel = new Set();           // 待删除的 rid

function applyRemoteRow(row){
  if(!row || !row.id) return;
  const i = ST.rows.findIndex(x => x.id === row.id);
  if(i < 0){ ST.rows.push(row); return; }
  if((ST.rows[i]._updatedAt || 0) <= (row._updatedAt || 0)) ST.rows[i] = row;
}

let syncTimer = null;
function startSync(){
  if(syncTimer) return;
  syncTimer = setInterval(syncTick, 15000);
  document.addEventListener('visibilitychange', () => { if(!document.hidden) syncTick(); });
}
async function syncTick(){
  if(document.hidden || auth.needLogin || !auth.token) return;
  /* 先推：把本机未上传的修改/删除补到服务器 */
  for(const [rid, row] of Array.from(pendingPush.entries())){
    try{
      const saved = await api('/api/rows/' + encodeURIComponent(rid), {method:'PUT', body: JSON.stringify(row)});
      pendingPush.delete(rid);
      applyRemoteRow(saved);
      if(saved._updatedAt) lastSync = Math.max(lastSync, saved._updatedAt);
    }catch(e){ if(auth.needLogin) return; }
  }
  for(const rid of Array.from(pendingDel)){
    try{ await api('/api/rows/' + encodeURIComponent(rid), {method:'DELETE'}); pendingDel.delete(rid); }
    catch(e){ if(auth.needLogin) return; }
  }
  /* 再拉：合并别人的变更 */
  try{
    const d = await api('/api/changes?since=' + lastSync);
    let n = 0;
    (d.changed || []).forEach(row => {
      if(dlg.open && dlg.id === row.id) return;   // 正在编辑的行不覆盖
      if(pendingPush.has(row.id)) return;
      const i = ST.rows.findIndex(x => x.id === row.id);
      if(i < 0){ ST.rows.push(row); n++; }
      else if((ST.rows[i]._updatedAt || 0) < (row._updatedAt || 0)){ ST.rows[i] = row; n++; }
    });
    (d.deleted || []).forEach(rid => {
      if(dlg.open && dlg.id === rid) return;
      const i = ST.rows.findIndex(x => x.id === rid);
      if(i >= 0){ ST.rows.splice(i, 1); n++; }
    });
    if(d.ts) lastSync = Math.max(lastSync, d.ts);
    if(n) toast('已同步同事提交的 ' + n + ' 条变更');
  }catch(e){ /* 静默，下轮再试 */ }
}

export async function bootRemote(){
  if(!auth.token){ auth.needLogin = true; return; }
  try{
    const me = await api('/api/me');
    auth.user = me.user;
    /* 本机缓存若归属其他账号（或旧缓存无归属且当前非管理员）→ 丢弃，避免越权看到字段 */
    if(cachedOwner ? (cachedOwner !== auth.user.username) : !auth.user.isAdmin){
      ST.rows = [];
    }
    let b = await api('/api/bootstrap');
    /* 服务器全新而本机有正式台账 → 一键迁移上服务器（仅限从未初始化过的库） */
    if(b.fresh && !firstRun && ST.rows.length){
      const payload = ST.rows.map(r => cleanRow(r));
      await api('/api/merge', {method:'POST', body: JSON.stringify({rows: payload, overwrite: true})});
      b = await api('/api/bootstrap');
      if(b.rows.length) toast('已把本机台账迁移到服务器（' + b.rows.length + ' 条）');
    }
    ST.rows = b.rows || [];
    lastSync = b.ts || (Date.now() / 1000);
    auth.needLogin = false;
  }catch(e){
    if(auth.needLogin) return;          // 凭证失效 → 弹登录框
    toast(e.message + '，暂用本机缓存，恢复后自动同步');
  }
  loadStaff();                          // 员工名单（不阻塞主流程）
  startSync();
}

/* 记忆化启动：路由守卫与 App 挂载共用同一次 bootRemote */
let _bootP = null;
export function boot(){
  if(!_bootP) _bootP = bootRemote();
  return _bootP;
}

/* ==================== 数据操作 ==================== */
export function openEdit(id){
  dlg.id = id || null;
  const r = id ? ST.rows.find(x => x.id === id) : null;
  dlg.base = r ? cleanRow(r) : null;     // 记录打开时的值，保存时只提交实际改动的字段
  dlg.open = true;
}
export function closeDlg(){ dlg.open = false; dlg.id = null; dlg.base = null; }
export async function upsertRow(id, o){
  const rid = id || uid();
  const base = id ? dlg.base : null;
  /* 标书状态变化时补写状态时间点（进行中/已结束的时间记录） */
  stampBidStatus(o, base ? base.bidStatus : '');
  if(id){
    const i = ST.rows.findIndex(x => x.id === id);
    if(i >= 0) ST.rows[i] = Object.assign({}, ST.rows[i], o);
    toast('已更新');
  }else{
    ST.rows.push(Object.assign({id:rid}, o));
    toast('已新增');
  }
  closeDlg();
  /* 上传服务器（失败则进待传队列，下次同步自动重试）。
     编辑已有行时只提交相对打开时快照有变化的字段：后端按字段合并，
     多人同时编辑同一行的不同字段就不会互相覆盖。 */
  const cur = ST.rows.find(x => x.id === rid) || Object.assign({id:rid}, o);
  const full = pushable(cur);
  let row = full;
  if(base){
    row = {};
    Object.keys(full).forEach(k => { if(!sameVal(full[k], base[k])) row[k] = full[k]; });
    if(!Object.keys(row).length) return;              // 无实际改动：不上传，避免全员白刷一次同步
  }
  pendingPush.set(rid, Object.assign(pendingPush.get(rid) || {}, row));
  try{
    const saved = await api('/api/rows/' + encodeURIComponent(rid), {method:'PUT', body: JSON.stringify(row)});
    pendingPush.delete(rid);
    applyRemoteRow(saved);
    if(saved._updatedAt) lastSync = Math.max(lastSync, saved._updatedAt);
  }catch(e){
    if(isPermErr(e)){ pendingPush.delete(rid); toast(e.message); return; }
    if(!auth.needLogin) toast('服务器保存失败，已存本机，稍后自动重试');
  }
}
export async function removeRow(id){
  const r = ST.rows.find(x => x.id === id);
  if(!r) return;
  if(!confirm('确认删除「' + (r.projectName || '') + '」？删除后不可恢复。')) return;
  ST.rows = ST.rows.filter(x => x.id !== id);
  closeDlg();
  toast('已删除');
  pendingPush.delete(id);
  pendingDel.add(id);
  try{
    await api('/api/rows/' + encodeURIComponent(id), {method:'DELETE'});
    pendingDel.delete(id);
  }catch(e){
    if(isPermErr(e)){ pendingDel.delete(id); toast(e.message); return; }
    if(!auth.needLogin) toast('服务器删除失败，稍后自动重试');
  }
}
/* 字段值等价判断：空值统一按空串，数字与字符串按文本比较（用于识别真正改动过的字段） */
function sameVal(a, b){
  const na = a === null || a === undefined ? '' : String(a).trim();
  const nb = b === null || b === undefined ? '' : String(b).trim();
  return na === nb;
}
/* 上传载荷：只提交当前账号「可写」的字段；只读/脱敏/隐藏字段由后端保留旧值。
   （后端仍会强制校验，前端过滤只是减少无谓的 403） */
function pushable(r){
  const o = cleanRow(r);
  if(auth.user && auth.user.isAdmin) return o;
  Object.keys(o).forEach(k => { if(!canWrite(k)) delete o[k]; });
  return o;
}
/* 权限类错误（后端 403）：不重试，直接提示 */
function isPermErr(e){ return /权限/.test((e && e.message) || ''); }
export function resetFilter(){
  ST.f = {kw:'', lead:'', result:'', method:''};
  ST.onlyNoTime = false;
  ST.fund = '';
  ST.dateFrom = _curStart;
  ST.dateTo   = _curEnd;
  ST.page = 1;
  ST.selIds = [];
}
/** 仅看「未填统计口径时间」的行：来自标源总览「另有 N 条未填…」的跳转 */
export function setOnlyNoTime(v){
  ST.onlyNoTime = !!v;
  if(ST.onlyNoTime) ST.fund = '';      // 两种跳转口径互斥
  ST.page = 1;
  ST.selIds = [];
}
/** 资金口径筛选：来自看板「资金状态」饼图叶子（投标/履约保证金、三项开销）的跳转 */
export function setFund(v){
  ST.fund = String(v || '');
  if(ST.fund) ST.onlyNoTime = false;   // 两种跳转口径互斥
  ST.page = 1;
  ST.selIds = [];
}
/** 快速时间范围：近 n 月（n=3/6/12）。from = TODAY.n 个月前当月 1 号；to = 本月末。n=0 表示全部时间。 */
export function setQuickMonths(n){
  ST.onlyNoTime = false;
  if(n === 0){ ST.dateFrom = ''; ST.dateTo = ''; }
  else{ ST.dateFrom = toISODate(monthAgo(n - 1)); ST.dateTo = toISODate(lastOfMonth(TODAY)); }
  ST.page = 1; ST.selIds = [];
}
export function setPage(n){ ST.page = Math.min(Math.max(1, n), pageCnt.value); }
export function setPageSize(n){ ST.pageSize = n; ST.page = 1; ST.selIds = []; }
/** 筛选条件变化 → 页码回到 1，选中清空（避免选中的行不在当前页） */
watch(() => [ST.dateFrom, ST.dateTo, ST.f.kw, ST.f.lead, ST.f.result, ST.f.method, ST.basis, ST.fund], () => {
  ST.page = 1;
  ST.selIds = [];
});
/* 手动改日期区间即退出「仅看未填时间」模式：该模式下区间不生效，避免造成误解 */
watch(() => [ST.dateFrom, ST.dateTo], () => { if(ST.onlyNoTime) ST.onlyNoTime = false; });
export function toggleSel(id){
  const i = ST.selIds.indexOf(id);
  if(i >= 0) ST.selIds.splice(i, 1); else ST.selIds.push(id);
}
export function selPageAll(){
  const curSet = new Set(pagedRows.value.map(r => r.id));
  const allSel = pagedRows.value.every(r => ST.selIds.indexOf(r.id) >= 0);
  // 要么把当前页全部加入（去重），要么全移除
  if(allSel){
    ST.selIds = ST.selIds.filter(id => !curSet.has(id));
  }else{
    pagedRows.value.forEach(r => { if(ST.selIds.indexOf(r.id) < 0) ST.selIds.push(r.id); });
  }
}
export function isSelected(id){ return ST.selIds.indexOf(id) >= 0; }

export async function batchDelete(ids){
  if(!ids.length) return;
  if(!confirm('确认删除选中的 ' + ids.length + ' 条台账？此操作不可恢复。')) return;
  const set = new Set(ids);
  ST.rows = ST.rows.filter(r => !set.has(r.id));
  ids.forEach(id => { pendingPush.delete(id); pendingDel.add(id); });
  toast('已删除 ' + ids.length + ' 条');
  ST.selIds = [];
  // 发 DELETE 到服务器（每条一次）
  await Promise.all(ids.map(id => (async () => {
    try{ await api('/api/rows/' + encodeURIComponent(id), {method:'DELETE'}); pendingDel.delete(id); }
    catch(e){ if(isPermErr(e)){ pendingDel.delete(id); toast(e.message); } }
  })()));
  // 修正页码（如果当前页因为数据减少而空了）
  if(ST.page > pageCnt.value) ST.page = pageCnt.value;
}
export function toggleSort(k){
  if(ST.sort.k === k) ST.sort.d *= -1; else ST.sort = {k, d:1};
}

/* ==================== 本机合并（服务器不可达时的导入兜底） ==================== */
function mergeRows(list, overwrite){
  overwrite = overwrite !== false;
  let add = 0, upd = 0, skip = 0;
  const idx = new Map();
  ST.rows.forEach((r,i) => idx.set(rowKey(r), i));
  list.forEach(raw => {
    const r = cleanRow(raw);
    const k = rowKey(r);
    if(idx.has(k)){
      if(!overwrite){ skip++; return; }
      const old = ST.rows[idx.get(k)];
      Object.keys(r).forEach(kk => { if(r[kk] !== '' && r[kk] != null) old[kk] = r[kk]; });
      upd++;
    }else{
      ST.rows.push(Object.assign({id:uid()}, r));
      idx.set(k, ST.rows.length - 1);
      add++;
    }
  });
  return {add, upd, skip};
}

/* ==================== 导入 ==================== */
export const impFiltered = computed(() => imp.list.filter(r => {
  if(imp.opt.needLead && !String(r.lead||'').trim()) return false;
  if(imp.opt.skipNo && r.__bid === false) return false;
  return true;
}));
export const impStat = computed(() => {
  const list = imp.list;
  const noLead = list.filter(r => !String(r.lead||'').trim()).length;
  const noBid = list.filter(r => r.__bid === false).length;
  const sel = impFiltered.value;
  const have = new Set(ST.rows.map(rowKey));
  let add = 0, upd = 0;
  sel.forEach(r => { have.has(rowKey(r)) ? upd++ : add++; });
  return {total:list.length, noLead, noBid, sel:sel.length, add, upd};
});
export function pickImportFile(){
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = '.xlsx,.xls,.csv';
  inp.onchange = () => { if(inp.files[0]) doImport(inp.files[0]); };
  inp.click();
}
function doImport(file){
  const name = file.name.toLowerCase();
  const finish = mtx => {
    const list = rowsFromMatrix(mtx);
    if(!list.length){ toast('没识别到有效数据行，请检查表头是否与模板一致'); return; }
    imp.name = file.name; imp.list = list;
    imp.opt = {needLead:true, skipNo:true, overwrite:true};
    imp.open = true;
  };
  const fr = new FileReader();
  if(/\.(xlsx|xls)$/.test(name)){
    fr.onload = e => {
      const wb = XLSX.read(new Uint8Array(e.target.result), {type:'array', cellDates:true});
      const ws = wb.Sheets[wb.SheetNames[0]];
      finish(XLSX.utils.sheet_to_json(ws, {header:1, raw:false, defval:''}));
    };
    fr.readAsArrayBuffer(file);
  }else{
    fr.onload = e => {
      const txt = String(e.target.result).replace(/^\uFEFF/,'');
      const wb = XLSX.read(txt, {type:'string', raw:false});
      const ws = wb.Sheets[wb.SheetNames[0]];
      finish(XLSX.utils.sheet_to_json(ws, {header:1, raw:false, defval:''}));
    };
    fr.readAsText(file, 'UTF-8');
  }
}
export async function confirmImport(){
  const sel = impFiltered.value;
  if(!sel.length){ toast('没有可导入的行，请调整上方勾选项'); return; }
  const payload = sel.map(r => cleanRow(r));
  try{
    /* 服务端按「招标编号+标段」去重合并，返回权威结果；未命中的人员字段会自动建用户账号 */
    const r = await api('/api/merge', {method:'POST', body: JSON.stringify({rows: payload, overwrite: imp.opt.overwrite})});
    (r.rows || []).forEach(applyRemoteRow);
    if(r.ts) lastSync = Math.max(lastSync, r.ts);
    imp.open = false; imp.list = [];
    let msg = '导入完成：新增 ' + r.add + ' 条，更新 ' + r.upd + ' 条' + (r.skip ? '，跳过 ' + r.skip + ' 条' : '');
    if(r.created_users && r.created_users.length){
      msg += '；已为 ' + r.created_users.length + ' 位未匹配人员自动创建账号：' + r.created_users.join('、') + '（密码 123456）';
    }
    toast(msg);
  }catch(e){
    if(auth.needLogin) return;
    /* 离线兜底：仅本机合并，恢复后不会自动上传，请联网后重新导入 */
    const r = mergeRows(sel, imp.opt.overwrite);
    imp.open = false; imp.list = [];
    toast('未能连接服务器，仅本机合并（新增 ' + r.add + ' / 更新 ' + r.upd + ' 条）');
  }
}

/* ==================== 导出 ==================== */
export function toMatrix(rows){
  return [F.map(f => f.l), ...rows.map(r => F.map(f => {
    if(f.der) return f.k === 'bidEffort' ? bidEffortDays(r) : '';
    const v = r[f.k];
    return v == null ? '' : v;
  }))];
}
export function exportXlsx(){
  const rows = scopedRows.value.length ? scopedRows.value : ST.rows;
  const tag = (ST.dateFrom && ST.dateTo) ? (ST.dateFrom + '_to_' + ST.dateTo)
             : (ST.dateFrom || ST.dateTo) ? (ST.dateFrom + ST.dateTo) : '全部';
  const fn = '投标台账_' + tag + '.xlsx';
  const aoa = toMatrix(rows);
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  ws['!cols'] = F.map(f => ({wch: f.k === 'projectName' ? 32 : (f.g === '开标结果' ? 16 : 15)}));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, '投标台账');
  XLSX.writeFile(wb, fn);
  toast('已导出 ' + rows.length + ' 条');
}
export function exportTemplate(){
  const sample = [{
    projectName:'（示例）××医院标识标牌采购', bidNo:'ZB-2026-0001', pkg:'一标段',
    owner:'××市人民医院', region:'市区', method:'公开招标', bizType:'标识标牌',
    projType:'货物', industry:'医疗健康', province:'河南省',
    deadline:'2026-11-10', openTime:'2026-11-12', openAddr:'××交易中心 3 号开标室',
    budget:190, ourPrice:168, bondAmount:3.8, bondStatus:'已缴', perfAmount:16.8, perfStatus:'未缴',
    bondPayDate:'2026-11-05', bondBackDate:'', bondDue:'2027-05-12',
    perfPayDate:'', perfBackDate:'', perfDue:'',
    lead:'张伟', copilot:'陈曦', reviewer:'李静',
    bidStatus:'未开始',
    docFee:0.08, travelFee:0.24, sampleFee:'',
    result:'待开标', winner:'', winAmount:'', rank:'', remark:'此行为示例，录入前请删除'
  }];
  const aoa = toMatrix(sample);
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  ws['!cols'] = F.map(() => ({wch:16}));
  const note = XLSX.utils.aoa_to_sheet([
    ['填写说明'],[''],
    ['1. 一行 = 一个标段。同一项目分多包投标时，每包单独一行，标段/包号填写区分。'],
    ['2. 金额单位统一为「万元」，只填数字，不要带「万」「元」「¥」；项目开销三项（购买标书费 / 差旅费 / 样品费）同样按万元填。'],
    ['3. 日期格式 2026-11-12（年-月-日）。'],
    ['4. 开标结果只能填：待开标 / 中标 / 未中标 / 废标 / 流标 / 不投 / 放弃。'],
    ['    「不投」= 报名了但决定不投标，不算废标、不进中标率分母、不计投标数。'],
    ['5. 投标/履约保证金状态只能填：无需缴纳 / 未缴 / 已缴 / 已退 / 不予退还。'],
    ['6. 中标单位请填最终中标方（即使不是我方）；我方中标填「本公司」。'],
    ['7. 标书负责人必填，用于「每人负责几个标」的统计。'],
    ['8. 标书状态只能填：未开始 / 进行中 / 已结束；三个状态时间点由系统自动记录，无需填写。'],
    ['9. 表头名称尽量不要改动，改动了导入时可能识别不到该列。']
  ]);
  note['!cols'] = [{wch:90}];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, '投标台账');
  XLSX.utils.book_append_sheet(wb, note, '填写说明');
  XLSX.writeFile(wb, '投标台账模板.xlsx');
  toast('模板已下载');
}

