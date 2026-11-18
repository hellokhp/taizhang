<script setup>
/* ================================================================
   标讯采集：配置增删改查 / 平台与配方 / 异步任务进度 / 采集结果
   执行在独立 worker；本页只做配置与任务提交，运行中按钮一律置灰。
   ================================================================ */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { toast } from '../stores/bid'
import { can } from '../stores/perm'
import { useTableLayout } from '../lib/useTableLayout'

const router = useRouter()

const TABS = [
  {k:'config', l:'采集配置'},
  {k:'platform', l:'平台与配方'},
  {k:'jobs', l:'采集任务'},
  {k:'items', l:'采集结果'},
  // 定时任务受独立权限控制：没配 view.schedule 的角色看不到这个 Tab
  {k:'schedule', l:'定时任务', perm:'view.schedule'},
];
const visibleTabs = computed(() => TABS.filter(t => !t.perm || can(t.perm)));
const tab = ref('config');
const canManage = computed(() => can('bidcrawl.manage'));
const canSchedAdd = computed(() => can('schedule.create'));
const canSchedEdit = computed(() => can('schedule.update'));
const canSchedDel = computed(() => can('schedule.delete'));

const platforms = ref([]);
const pid = ref('');

function platName(id){
  const p = platforms.value.find(x => x.id === id);
  return p ? p.name : (id || '—');
}
function scopeLabel(s){ return !s || s === 'global' ? '全局' : platName(s); }
function ts(v){
  if(!v) return '—';
  const d = new Date(v);
  return isNaN(d) ? String(v) : d.toLocaleString('zh-CN', {hour12:false});
}
function money(v){
  if(v == null || v === '') return '—';
  const n = Number(v);
  if(!isFinite(n)) return String(v);
  return '¥' + n.toLocaleString('zh-CN', {minimumFractionDigits:0, maximumFractionDigits:2});
}

async function loadPlatforms(){
  try{
    const r = await api('/api/bidcrawl/platforms');
    platforms.value = r.platforms || [];
    // 选中平台被删除后，回退到第一个合法平台，避免配置页残留失效 scope
    if(pid.value && !platforms.value.some(x => x.id === pid.value))
      pid.value = platforms.value.length ? platforms.value[0].id : '';
    if(!pid.value && platforms.value.length) pid.value = platforms.value[0].id;
  }catch(e){ toast(e.message); }
}

/* ==================== 采集配置 ==================== */
const KINDS = {
  keyword: {api:'keywords', label:'关键词', cols:[
    {k:'word', l:'关键词', w:220}, {k:'scope', l:'作用域', w:140},
    {k:'hits_last', l:'近轮命中', w:100, num:true}, {k:'sort_no', l:'排序', w:80, num:true},
    {k:'note', l:'备注', w:260, wrap:true}], search:'搜索关键词'},
  buyer: {api:'buyers', label:'采购人白名单', cols:[
    {k:'pattern', l:'匹配内容', w:260, wrap:true}, {k:'match_type', l:'匹配方式', w:110},
    {k:'scope', l:'作用域', w:140}, {k:'sort_no', l:'排序', w:80, num:true},
    {k:'note', l:'备注', w:260, wrap:true}], search:'搜索采购人'},
  rule: {api:'rules', label:'剔除规则', cols:[
    {k:'name', l:'规则名称', w:220}, {k:'rule_type', l:'类型', w:130},
    {k:'value', l:'规则值', w:340, wrap:true, mono:true}, {k:'scope', l:'作用域', w:140},
    {k:'sort_no', l:'排序', w:80, num:true}], search:'搜索规则名'},
  // 字段正则：卡片式界面（v-if 分支单独渲染），不进通用表格
  regex: {api:'', label:'字段正则', cols:[], search:''},
};

/* ==================== 表格布局：列宽 / 行高（本地记忆） ==================== */
const L = useTableLayout({
  storageKey: 'bidcrawl_table_v1', colDefault: 160, rowDefault: 42, colMin: 80, colMax: 600,
});
const cfgCols = computed(() => [...KINDS[kind.value].cols,
  {k:'__enabled', l:'启用', w:90, center:true},
  {k:'__op', l:'操作', w:120, center:true}]);
const PLAT_COLS = [
  {k:'pf.name', l:'平台', w:220, wrap:true}, {k:'pf.host', l:'域名', w:200},
  {k:'pf.status', l:'状态', w:100, center:true}, {k:'pf.ver', l:'配方版本', w:110, center:true},
  {k:'pf.ok', l:'最近成功', w:180}, {k:'pf.op', l:'操作', w:240, center:true},
];
const JOB_COLS = [
  {k:'jb.id', l:'任务', w:80}, {k:'jb.plat', l:'平台', w:150},
  {k:'jb.type', l:'类型', w:90}, {k:'jb.status', l:'状态', w:100, center:true},
  {k:'jb.prog', l:'进度', w:90, center:true}, {k:'jb.msg', l:'最新消息', w:460, wrap:true},
  {k:'jb.at', l:'提交时间', w:180}, {k:'jb.op', l:'操作', w:90, center:true},
];
const ITEM_COLS = [
  {k:'it.title', l:'标题', w:460, wrap:true}, {k:'it.buyer', l:'采购人', w:200},
  {k:'it.no', l:'招标编号', w:150}, {k:'it.open', l:'开标时间', w:150},
  {k:'it.pub', l:'发布时间', w:160}, {k:'it.end', l:'报名截止', w:160},
  {k:'it.budget', l:'预算', w:120, num:true}, {k:'it.status', l:'状态', w:90, center:true},
  {k:'it.plat', l:'平台', w:120}, {k:'it.op', l:'报名', w:96, center:true},
];
function colsW(cols){ return cols.reduce((s, c) => s + L.colWOf(c.k), 0); }
function rowStyle(prefix, id){
  const key = prefix + '-' + id;
  return {'--dt-row-h': L.rowHOf(key) + 'px', '--dt-row-pad': L.rowPadOf(key) + 'px'};
}
function cellCls(c){
  return [c.wrap ? 'dt-wrap-cell' : 'dt-ellipsis', c.num ? 'dt-num' : '',
    c.center ? 'dt-center' : '', c.mono ? 'mono' : ''].filter(Boolean).join(' ');
}
const cfgW = computed(() => colsW(cfgCols.value));
const platW = computed(() => colsW(PLAT_COLS));
const jobW = computed(() => colsW(JOB_COLS));
const itemW = computed(() => colsW(ITEM_COLS));

const RULE_TYPES = [
  {v:'title_regex', l:'标题正则', tip:'{"regex":"中标结果|成交公告"}'},
  {v:'url_regex', l:'链接正则', tip:'{"regex":"/result/"}'},
  {v:'buyer_regex', l:'采购人正则', tip:'{"regex":"某烟草"}'},
  {v:'notice_type', l:'公告类型', tip:'{"types":["结果公告","变更公告"]}'},
  {v:'age_days', l:'发布时间窗', tip:'{"days":35}'},
];

const kind = ref('keyword');
const rows = ref([]);
const kw = ref('');
const loading = ref(false);
const bulkText = ref('');
const bulkOpen = ref(false);

async function loadConfig(){
  loading.value = true;
  try{
    const qs = kw.value.trim() ? '?q=' + encodeURIComponent(kw.value.trim()) : '';
    const r = await api('/api/bidcrawl/' + KINDS[kind.value].api + qs);
    rows.value = r.items || [];
  }catch(e){ toast(e.message); }
  loading.value = false;
}
function switchKind(k){
  kind.value = k; kw.value = '';
  if(k === 'regex') loadRegex(); else loadConfig();
}
/* 配置表单元格显示值：空值统一展示为 —，避免行高塌陷 */
function cfgCell(r, c){
  if(c.k === 'scope') return scopeLabel(r.scope);
  if(c.k === 'value'){ try{ return JSON.stringify(r.value); }catch(e){ return '—'; } }
  if(c.k === 'match_type') return r.match_type === 'regex' ? '正则' : '包含';
  if(c.k === 'rule_type') return (RULE_TYPES.find(t => t.v === r.rule_type) || {}).l || r.rule_type;
  const v = r[c.k];
  return v == null || v === '' ? '—' : String(v);
}

/* 新增 / 编辑弹窗 */
const dlg = reactive({open:false, id:null, busy:false});
const form = reactive({word:'', pattern:'', match_type:'contains', name:'',
  rule_type:'title_regex', value:'', scope:'global', sort_no:0, note:'',
  is_sample:false, enabled:true});

function resetForm(){
  Object.assign(form, {word:'', pattern:'', match_type:'contains', name:'',
    rule_type:'title_regex', value:'', scope:'global', sort_no:0, note:'',
    is_sample:false, enabled:true});
}
function openNew(){
  resetForm();
  form.scope = pid.value || 'global';
  dlg.id = null; dlg.open = true;
}
function openEdit(r){
  resetForm();
  if(kind.value === 'keyword') form.word = r.word || '';
  if(kind.value === 'buyer'){ form.pattern = r.pattern || ''; form.match_type = r.match_type || 'contains'; }
  if(kind.value === 'rule'){
    form.name = r.name || ''; form.rule_type = r.rule_type || 'title_regex';
    form.value = typeof r.value === 'string' ? r.value : JSON.stringify(r.value || {}, null, 2);
  }
  form.scope = r.scope || 'global';
  form.sort_no = r.sort_no || 0;
  form.note = r.note || '';
  form.is_sample = !!r.is_sample;
  form.enabled = r.enabled !== false;
  dlg.id = r.id; dlg.open = true;
}
function payload(){
  const base = {scope:form.scope || 'global', sort_no:Number(form.sort_no) || 0,
    note:form.note || '', enabled:!!form.enabled};
  if(kind.value === 'keyword'){
    return Object.assign(base, {word:form.word.trim(), is_sample:!!form.is_sample});
  }
  if(kind.value === 'buyer'){
    return Object.assign(base, {pattern:form.pattern.trim(), match_type:form.match_type});
  }
  let value = form.value;
  try{ value = form.value.trim() ? JSON.parse(form.value) : {}; }
  catch(e){ throw new Error('规则值不是合法 JSON'); }
  return Object.assign(base, {name:form.name.trim(), rule_type:form.rule_type, value});
}
async function saveForm(){
  if(dlg.busy) return;
  let body;
  try{ body = payload(); }catch(e){ toast(e.message); return; }
  dlg.busy = true;
  try{
    const path = '/api/bidcrawl/' + KINDS[kind.value].api + (dlg.id ? '/' + dlg.id : '');
    await api(path, {method: dlg.id ? 'PUT' : 'POST', body: JSON.stringify(body)});
    dlg.open = false;
    toast('已保存，下一轮采集即时生效');
    await loadConfig();
  }catch(e){ toast(e.message); }
  dlg.busy = false;
}
async function toggleEnabled(r){
  if(!canManage.value) return;
  try{
    await api('/api/bidcrawl/' + KINDS[kind.value].api + '/' + r.id,
      {method:'PUT', body: JSON.stringify({enabled: !r.enabled})});
    await loadConfig();
  }catch(e){ toast(e.message); }
}
async function removeRow(r){
  if(!canManage.value) return;
  const name = r.word || r.pattern || r.name;
  if(!confirm('确定删除「' + name + '」？删除后下一轮采集不再使用。')) return;
  try{
    await api('/api/bidcrawl/' + KINDS[kind.value].api + '/' + r.id, {method:'DELETE'});
    toast('已删除');
    await loadConfig();
  }catch(e){ toast(e.message); }
}
async function saveBulk(){
  const text = bulkText.value.trim();
  if(!text) return;
  try{
    const r = await api('/api/bidcrawl/keywords/bulk',
      {method:'POST', body: JSON.stringify({words:text, scope:pid.value || 'global'})});
    bulkOpen.value = false; bulkText.value = '';
    toast('已导入 ' + (r.created || 0) + ' 个关键词');
    await loadConfig();
  }catch(e){ toast(e.message); }
}
async function previewRules(){
  if(!pid.value){ toast('请先在「平台与配方」选择平台'); return; }
  try{
    const r = await api('/api/bidcrawl/rules/preview',
      {method:'POST', body: JSON.stringify({platform_id: pid.value})});
    toast('最近 ' + (r.total || 0) + ' 条：保留 ' +
      (r.kept || 0) + '，剔除 ' + (r.dropped || 0), 5000);
  }catch(e){ toast(e.message); }
}

/* ==================== 平台与配方 ==================== */
const recipeDlg = reactive({open:false, pid:'', busy:false, version:'', verify:''});
const recipeText = ref('');

async function openRecipe(p){
  recipeDlg.pid = p.id; recipeDlg.open = true; recipeDlg.busy = true;
  recipeText.value = '';
  try{
    const r = await api('/api/bidcrawl/platforms/' + p.id + '/recipe');
    recipeText.value = r.recipe ? JSON.stringify(r.recipe, null, 2) : '';
    recipeDlg.version = p.active_ver || '—';
    recipeDlg.verify = r.verify ? JSON.stringify(r.verify, null, 2) : '';
  }catch(e){ toast(e.message); }
  recipeDlg.busy = false;
}
async function saveRecipe(){
  if(recipeDlg.busy) return;
  let raw;
  try{ raw = JSON.parse(recipeText.value); }
  catch(e){ toast('配方不是合法 JSON'); return; }
  recipeDlg.busy = true;
  try{
    const r = await api('/api/bidcrawl/platforms/' + recipeDlg.pid + '/recipe',
      {method:'PUT', body: JSON.stringify({recipe:raw, activate:true})});
    toast('配方已保存为 v' + r.version + ' 并启用');
    recipeDlg.open = false;
    await loadPlatforms();
  }catch(e){ toast(e.message); }
  recipeDlg.busy = false;
}
async function deleteRecipe(pidArg){
  if(!canManage.value || recipeDlg.busy) return;
  const target = pidArg || recipeDlg.pid;
  if(!target) return;
  const name = platName(target);
  if(!window.confirm('确定删除「' + name + '」的全部配方版本吗？\n\n平台会保留并置为停用，历史采集数据与任务记录不会删除；删除后可重新粘贴或生成配方。')) return;
  recipeDlg.busy = true;
  try{
    const r = await api('/api/bidcrawl/platforms/' + target + '/recipe', {method:'DELETE'});
    toast('已删除 ' + (r.versions_deleted || 0) + ' 个配方版本，平台已停用');
    if(recipeDlg.pid === target){
      recipeText.value = ''; recipeDlg.version = '—'; recipeDlg.verify = '';
      recipeDlg.open = false;
    }
    await loadPlatforms();
  }catch(e){ toast(e.message); }
  recipeDlg.busy = false;
}
async function deletePlatform(p){
  if(!canManage.value || recipeDlg.busy) return;
  const name = platName(p.id);
  const ok = window.confirm(
    '⚠ 确定彻底删除平台「' + name + '」吗？\n\n' +
    '将一并删除：配方全部版本、采集结果标讯、运行/任务记录、\n' +
    '详情 AI 缓存、凭据，以及该平台的专属关键词/采购人/剔除规则。\n' +
    '审计日志与全局配置保留。此操作不可恢复！\n\n' +
    '有排队/运行中的任务时会被拒绝，需先取消或稍后重试。');
  if(!ok) return;
  recipeDlg.busy = true;
  try{
    const r = await api('/api/bidcrawl/platforms/' + p.id, {method:'DELETE'});
    const d = r.deleted || {};
    toast('已删除平台「' + name + '」：' + (d.items||0) + ' 条标讯、' +
      (d.recipes||0) + ' 个配方版本、' + (d.jobs||0) + ' 个任务');
    if(recipeDlg.pid === p.id){ recipeDlg.open = false; recipeText.value = ''; }
    if(runForm.pid === p.id) runForm.pid = '';
    await loadPlatforms();
  }catch(e){ toast(e.message, 6000); }
  recipeDlg.busy = false;
}
async function togglePlatform(p){
  if(!canManage.value) return;
  const next = p.status === 'active' ? 'paused' : 'active';
  try{
    await api('/api/bidcrawl/platforms/' + p.id + '/status',
      {method:'POST', body: JSON.stringify({status: next})});
    toast(next === 'active' ? '平台已启用' : '平台已停用');
    await loadPlatforms();
  }catch(e){ toast(e.message); }
}
const STATUS_TXT = {active:'启用', paused:'停用', disabled:'停用', degraded:'降级', error:'异常', draft:'草稿'};
const STATUS_CLS = {active:'p-pay', paused:'p-na', disabled:'p-na', degraded:'p-fail', error:'p-win', draft:'p-na'};

function collect(p){
  tab.value = 'jobs';
  runForm.pid = p.id;
  submitRun();
}

/* ==================== AI 添加平台 ==================== */
const MODE_TIPS = {
  api_list: '接口型站点：从浏览器 F12 → Network 里找到列表接口，右键 Copy as cURL；返回样本是它的 Response（JSON）。详情同理；若详情是网页，把详情页的 cURL 与页面源码贴进来，列表里没有详情页链接时再贴一条公告的真实地址做示例。',
  html_list: 'CMS 型站点：粘贴列表页（或搜索后的列表页）请求的 cURL 与返回的 HTML 源码；详情贴一条详情页的 cURL 与源码，正文在 PDF 里也能自动识别。',
  auto: '只给入口 URL，由探测器抓取页面引用的 JS、自动试探列表/详情接口。适合结构简单的站点；SPA 或强网关站点建议改用上面两种模式。'
};
const analyzeDlg = reactive({
  open:false, mode:'api_list', busy:false, result:null, manual:'', recipeText:'',
  url:'', keyword:'门头', withLlm:true,
  siteUrl:'', name:'', detailPageTpl:'',
  listCurl:'', listSample:'', detailCurl:'', detailSample:''
});

function openAnalyze(){
  analyzeDlg.result = null; analyzeDlg.manual = '';
  analyzeDlg.recipeText = '';
  analyzeDlg.open = true;
}
function setAnalyzeMode(m){
  if(analyzeDlg.busy) return;
  analyzeDlg.mode = m;
  analyzeDlg.result = null; analyzeDlg.manual = ''; analyzeDlg.recipeText = '';
}
const canAnalyze = computed(() => {
  if(analyzeDlg.busy) return false;
  if(analyzeDlg.mode === 'auto') return !!analyzeDlg.url.trim();
  return !!analyzeDlg.listCurl.trim() && !!analyzeDlg.listSample.trim();
});
async function startAnalyze(){
  if(!canAnalyze.value) return;
  analyzeDlg.busy = true; analyzeDlg.result = null;
  analyzeDlg.manual = ''; analyzeDlg.recipeText = '';
  const body = {mode:analyzeDlg.mode, keyword:analyzeDlg.keyword.trim(),
    with_llm:analyzeDlg.withLlm, verify:true};
  if(analyzeDlg.mode === 'auto'){
    body.url = analyzeDlg.url.trim();
  }else{
    Object.assign(body, {
      list_curl:analyzeDlg.listCurl, list_sample:analyzeDlg.listSample,
      detail_curl:analyzeDlg.detailCurl, detail_sample:analyzeDlg.detailSample,
      site_url:analyzeDlg.siteUrl.trim(), name:analyzeDlg.name.trim(),
      detail_page_tpl:analyzeDlg.detailPageTpl.trim()
    });
  }
  try{
    const r = await api('/api/bidcrawl/analyze', {method:'POST', body: JSON.stringify(body)});
    analyzeDlg.result = r;
    analyzeDlg.recipeText = r.recipe ? JSON.stringify(r.recipe, null, 2) : '';
    if(!r.ok){
      analyzeDlg.manual = r.manual_steps || '';
      toast(r.error || '生成失败', 6000);
    }else{
      toast('配方已生成并试跑通过，可在下方编辑后保存', 4000);
    }
  }catch(e){ toast(e.message, 6000); }
  analyzeDlg.busy = false;
}
function editAnalyzedRecipe(){
  // 把（可能已在上方编辑过的）配方送到现有的配方编辑器
  const r = analyzeDlg.result;
  if(!r || !r.recipe || !analyzeDlg.recipeText.trim()) return;
  let raw;
  try{ raw = JSON.parse(analyzeDlg.recipeText); }
  catch(e){ toast('配方不是合法 JSON，请先修正'); return; }
  recipeDlg.pid = (raw.meta && raw.meta.id) || (r.recipe.meta && r.recipe.meta.id) || '';
  recipeText.value = JSON.stringify(raw, null, 2);
  recipeDlg.version = 'ai 生成';
  recipeDlg.verify = r.verify ? JSON.stringify(r.verify, null, 2) : '';
  analyzeDlg.open = false;
  recipeDlg.open = true;
}
const STAGE_CLS = {ok:'p-pay', warn:'p-win', error:'p-fail', skip:'p-na'};
function stageLabel(s){ return STAGE_CLS[s] || 'p-na'; }
function pct(v){ return (Number(v || 0) * 100).toFixed(0) + '%'; }
const URL_SOURCE_TXT = {field:'列表自带详情链接', example:'你提供的详情页示例（已套成 {字段} 模板）',
  detail:'详情 curl 的页面地址（已套成 {字段} 模板）',
  referer:'详情 curl 的 Referer 反推', guess:'兜底推断，可能打不开'};
function urlSourceTxt(s){ return URL_SOURCE_TXT[s] || s || '—'; }

/* ==================== 采集任务 ==================== */
const jobs = ref([]);
const busy = ref(false);
const runForm = reactive({pid:'', words:'', dry:false});
// 「全部启用平台」实际会跑哪些：与后端 _targets 一致（active / degraded）
const runnableCount = computed(() => platforms.value.filter(
  p => p.status === 'active' || p.status === 'degraded').length);
let timer = null;

const activeJob = computed(() => jobs.value.find(j => j.status === 'queued' || j.status === 'running'));
const jobLocked = computed(() => busy.value || !!activeJob.value);
const jobStatusTxt = {queued:'排队中', running:'运行中', ok:'完成', failed:'失败', cancelled:'已取消'};
const jobStatusCls = {queued:'p-wait', running:'p-wait', ok:'p-pay', failed:'p-win', cancelled:'p-na'};

async function loadJobs(){
  try{
    const r = await api('/api/bidcrawl/jobs?limit=30');
    jobs.value = r.jobs || [];
  }catch(e){ /* 轮询失败不打扰用户 */ }
}
function genToken(){
  try{ return crypto.randomUUID(); }
  catch(e){ return 'ui-' + Date.now() + '-' + Math.random().toString(16).slice(2); }
}
async function submitRun(){
  if(jobLocked.value) return;
  busy.value = true;
  try{
    const params = {dry_run: !!runForm.dry};
    const words = runForm.words.split(/[\s,，]+/).map(s => s.trim()).filter(Boolean);
    if(words.length) params.keywords = words;
    // 只认「采集任务」页自己的平台选择：曾经误把「采集结果」页的筛选值当目标平台，
    // 选「全部启用平台」时筛选器还停在上次看的站点上，于是只采了那一个平台。
    const target = runForm.pid || '';
    const body = {job_type:'run', platform_id: target || null, params,
      client_token: genToken()};
    const scope = target ? platName(target) : ('全部启用平台（' + runnableCount.value + ' 个）');
    const r = await api('/api/bidcrawl/jobs', {method:'POST', body: JSON.stringify(body)});
    toast(r.dedupe ? '已有相同任务在排队/运行，已合并为 #' + r.job_id
      : '任务 #' + r.job_id + ' 已提交：' + scope + '，可在下方查看进度', 4000);
    await loadJobs();
  }catch(e){ toast(e.message); }
  busy.value = false;
}
async function cancelJob(j){
  try{
    await api('/api/bidcrawl/jobs/' + j.id + '/cancel', {method:'POST'});
    toast('已请求取消，等待 worker 响应');
    await loadJobs();
  }catch(e){ toast(e.message); }
}

/* ==================== 采集结果 ==================== */
const items = ref([]);
const itemTotal = ref(0);
const PAGE_SIZES = [20, 50, 100, 200];
const PAGE_SIZE_KEY = 'bidcrawl.page.size';
// 每页条数记住上次选择（后端接口上限 500，这里最多 200 够用）
const pageSize = ref(PAGE_SIZES.includes(Number(localStorage.getItem(PAGE_SIZE_KEY)))
  ? Number(localStorage.getItem(PAGE_SIZE_KEY)) : 50);
const pageNo = ref(1);
const itemsWrap = ref(null);
const f = reactive({platform:'', q:'', status:''});
const itemsLoading = ref(false);

async function loadItems(reset){
  if(reset) pageNo.value = 1;
  itemsLoading.value = true;
  try{
    const qs = new URLSearchParams();
    if(f.platform) qs.set('platform_id', f.platform);
    if(f.q.trim()) qs.set('q', f.q.trim());
    if(f.status) qs.set('status', f.status);
    qs.set('limit', String(pageSize.value));
    qs.set('offset', String((pageNo.value - 1) * pageSize.value));
    const r = await api('/api/bidcrawl/items?' + qs.toString());
    items.value = r.items || [];
    itemTotal.value = r.total || 0;
    // 页码越界（数据变少 / 改了筛选）时回落到最后一页，避免停在空白页
    if(!items.value.length && itemTotal.value > 0 && pageNo.value > pageCnt.value){
      pageNo.value = pageCnt.value;
      return loadItems();
    }
  }catch(e){ toast(e.message); }
  itemsLoading.value = false;
}
const pageCnt = computed(() => Math.max(1, Math.ceil(itemTotal.value / pageSize.value)));
const rangeFrom = computed(() => itemTotal.value ? (pageNo.value - 1) * pageSize.value + 1 : 0);
const rangeTo = computed(() => Math.min(itemTotal.value, pageNo.value * pageSize.value));
// 当前页前2后2 + 两端省略（与项目台账一致）
const visiblePages = computed(() => {
  const pc = pageCnt.value, cur = pageNo.value;
  if(pc <= 7) return Array.from({length: pc}, (_, i) => i + 1);
  const arr = [1];
  const lo = Math.max(2, cur - 2), hi = Math.min(pc - 1, cur + 2);
  if(lo > 2) arr.push('...');
  for(let i = lo; i <= hi; i++) arr.push(i);
  if(hi < pc - 1) arr.push('...');
  arr.push(pc);
  return arr;
});
function gotoPage(n){
  const p = Math.max(1, Math.min(pageCnt.value, Number(n) || 1));
  if(p === pageNo.value && items.value.length) return;
  pageNo.value = p;
  if(itemsWrap.value) itemsWrap.value.scrollTop = 0;
  loadItems();
}
function changePageSize(n){
  pageSize.value = Math.max(1, Number(n) || 50);
  localStorage.setItem(PAGE_SIZE_KEY, String(pageSize.value));
  gotoPage(1);
}
const itemStatusTxt = {open:'报名中', soon:'即将截止', expired:'已截止', unknown:'未知'};
const itemStatusCls = {open:'p-pay', soon:'p-fail', expired:'p-na', unknown:'p-na'};

/* ==================== 报名（写进投标管理） ==================== */
const enrolling = ref(0);              // 正在报名的 item id，防连点
const lastEnrolled = ref(null);        // {rid, name} 报名成功后给个去处
const canEnroll = computed(() => can('bid.edit'));
async function enroll(it){
  if(enrolling.value) return;
  enrolling.value = it.id;
  try{
    const r = await api('/api/bidcrawl/items/' + it.id + '/enroll', {method:'POST', body: '{}'});
    it.enrolled = true;
    it.project = r.project || it.project;
    lastEnrolled.value = {rid: r.rid, name: (r.project || {}).projectName || it.title};
    toast(r.existing ? '该项目已在投标管理中（未重复添加）'
                     : '已加入投标管理：' + lastEnrolled.value.name);
  }catch(e){ toast(e.message); }
  enrolling.value = 0;
}
function gotoManage(){ lastEnrolled.value = null; router.push('/manage'); }

/* ==================== 字段正则配置 ==================== */
const regexFields = ref([]);
const regexPid = ref('');
const regexLoading = ref(false);
const aliasInput = reactive({});
const aliasScope = reactive({});
const regexTplDlg = reactive({open:false, field_key:'', label:'', pattern_tpl:'',
  value_hint:'', busy:false});
const regexTestDlg = reactive({open:false, text:'', pid:'', busy:false, result:null});

async function loadRegex(){
  regexLoading.value = true;
  try{
    const r = await api('/api/bidcrawl/regex?platform_id=' + encodeURIComponent(regexPid.value || ''));
    regexFields.value = r.fields || [];
    for(const f of regexFields.value){
      if(!(f.field_key in aliasInput)) aliasInput[f.field_key] = '';
      // 默认作用域跟随当前视图：看全局就加全局，看某平台就默认加该平台
      aliasScope[f.field_key] = regexPid.value || 'global';
    }
  }catch(e){ toast(e.message); }
  regexLoading.value = false;
}
async function addAlias(f){
  if(!canManage.value) return;
  const alias = (aliasInput[f.field_key] || '').trim();
  if(!alias){ toast('先输入原文里的写法，例如「项目规模」'); return; }
  try{
    await api('/api/bidcrawl/regex/aliases', {method:'POST', body: JSON.stringify({
      field_key: f.field_key, alias, scope: aliasScope[f.field_key] || 'global'})});
    aliasInput[f.field_key] = '';
    await loadRegex();
    toast('已添加「' + alias + '」，下一次抽取即生效（最多 1 分钟）');
  }catch(e){ toast(e.message, 5000); }
}
async function delAlias(a){
  if(!confirm('删除匹配词「' + a.alias + '」？\n删除后该写法不再被识别。')) return;
  try{
    await api('/api/bidcrawl/regex/aliases/' + a.id, {method:'DELETE'});
    await loadRegex();
    toast('已删除「' + a.alias + '」');
  }catch(e){ toast(e.message); }
}
async function toggleAlias(a){
  try{
    await api('/api/bidcrawl/regex/aliases/' + a.id,
      {method:'PUT', body: JSON.stringify({enabled: !a.enabled})});
    await loadRegex();
  }catch(e){ toast(e.message); }
}
function openTpl(f){
  regexTplDlg.open = true; regexTplDlg.busy = false;
  regexTplDlg.field_key = f.field_key; regexTplDlg.label = f.label;
  regexTplDlg.pattern_tpl = f.pattern_tpl; regexTplDlg.value_hint = f.value_hint;
}
async function saveTpl(){
  regexTplDlg.busy = true;
  try{
    await api('/api/bidcrawl/regex/fields/' + regexTplDlg.field_key,
      {method:'PUT', body: JSON.stringify({label: regexTplDlg.label,
        pattern_tpl: regexTplDlg.pattern_tpl})});
    regexTplDlg.open = false;
    await loadRegex();
    toast('模板已保存');
  }catch(e){ toast(e.message, 6000); }
  regexTplDlg.busy = false;
}
async function openRegexTest(){
  regexTestDlg.open = true; regexTestDlg.busy = false;
  regexTestDlg.pid = regexPid.value || '';
  if(!regexTestDlg.text) regexTestDlg.result = null;
  await runRegexTest();
}
async function runRegexTest(){
  if(!regexTestDlg.text.trim()){ toast('先粘贴一段公告正文'); return; }
  regexTestDlg.busy = true;
  try{
    regexTestDlg.result = await api('/api/bidcrawl/regex/test',
      {method:'POST', body: JSON.stringify({text: regexTestDlg.text,
        platform_id: regexTestDlg.pid || ''})});
  }catch(e){ toast(e.message, 5000); }
  regexTestDlg.busy = false;
}

/* ==================== 定时任务 ==================== */
const schedules = ref([]);
const schedKeywords = ref([]);
const schKw = ref('');
const schDlg = reactive({open:false, id:null, name:'', run_at:'09:00',
  platforms:[], keywords:[], enabled:true, busy:false});
const SCHED_STATUS = {queued:'已提交', merged:'已合并', error:'提交失败'};

async function loadSchedules(){
  if(!can('view.schedule')) return;
  try{
    const r = await api('/api/bidcrawl/schedules');
    schedules.value = r.schedules || [];
  }catch(e){ /* 无权限时后端 403，页面本来也不会显示这个 Tab */ }
}
async function loadSchedKeywords(){
  try{
    const r = await api('/api/bidcrawl/keywords?scope=global&enabled=1');
    schedKeywords.value = r.items || [];
  }catch(e){ schedKeywords.value = []; }
}
const schKwList = computed(() => {
  const q = schKw.value.trim().toLowerCase();
  return q ? schedKeywords.value.filter(k => (k.word || '').toLowerCase().includes(q))
           : schedKeywords.value;
});
function openSched(s){
  schDlg.open = true; schDlg.busy = false; schKw.value = '';
  if(s){
    schDlg.id = s.id; schDlg.name = s.name || ''; schDlg.run_at = s.run_at || '09:00';
    schDlg.platforms = [...(s.platforms || [])];
    schDlg.keywords = [...(s.keywords || [])];
    schDlg.enabled = s.enabled !== false;
  }else{
    schDlg.id = null; schDlg.name = ''; schDlg.run_at = '09:00';
    schDlg.platforms = []; schDlg.keywords = []; schDlg.enabled = true;
  }
  // 每次打开都刷新：刚在「采集配置」里加的关键词要能立刻选到
  loadSchedKeywords();
}
function inArr(arr, v){ return arr.indexOf(v) >= 0; }
function toggleArr(arr, v){
  const i = arr.indexOf(v);
  if(i >= 0) arr.splice(i, 1); else arr.push(v);
}
// 空数组 = 全部（runner 侧语义）。「全部」按钮清空选择即回到全部
function clearSel(arr){ arr.splice(0, arr.length); }
async function saveSched(){
  if(schDlg.busy) return;
  schDlg.busy = true;
  const body = {name: (schDlg.name || '').trim(), run_at: schDlg.run_at,
    platforms: schDlg.platforms, keywords: schDlg.keywords, enabled: schDlg.enabled};
  try{
    if(schDlg.id) await api('/api/bidcrawl/schedules/' + schDlg.id,
      {method:'PUT', body: JSON.stringify(body)});
    else await api('/api/bidcrawl/schedules',
      {method:'POST', body: JSON.stringify(body)});
    toast(schDlg.id ? '定时任务已保存' : '定时任务已创建，到点由 worker 自动提交');
    schDlg.open = false;
    await loadSchedules();
  }catch(e){ toast(e.message, 5000); }
  schDlg.busy = false;
}
async function delSched(s){
  if(!confirm('删除定时任务「' + s.name + '」？\n已提交的采集任务不受影响。')) return;
  try{
    await api('/api/bidcrawl/schedules/' + s.id, {method:'DELETE'});
    toast('已删除定时任务');
    await loadSchedules();
  }catch(e){ toast(e.message); }
}
async function runSched(s){
  try{
    const r = await api('/api/bidcrawl/schedules/' + s.id + '/run',
      {method:'POST', body: '{}'});
    toast(r.dedupe ? '已有同平台采集任务在排队，已合并到 #' + r.job_id
      : '已提交采集任务 #' + r.job_id, 4500);
    await loadSchedules();
    await loadJobs();
  }catch(e){ toast(e.message, 5000); }
}
async function toggleSched(s){
  const body = {name: s.name, run_at: s.run_at, platforms: s.platforms || [],
    keywords: s.keywords || [], enabled: !s.enabled};
  try{
    await api('/api/bidcrawl/schedules/' + s.id, {method:'PUT', body: JSON.stringify(body)});
    await loadSchedules();
  }catch(e){ toast(e.message); }
}
function schedPlatNames(s){
  const arr = s.platforms || [];
  return arr.length ? arr.map(platName).join('、') : '全部启用平台';
}
function schedWordNames(s){
  const arr = s.keywords || [];
  return arr.length ? arr.join('、') : '各平台全部关键词';
}
// 列表里用一句话概括范围，完整名单放 title
function schedScopeTxt(s){
  const p = (s.platforms || []).length, w = (s.keywords || []).length;
  return (p ? p + ' 个平台' : '全部平台') + ' · ' + (w ? w + ' 个关键词' : '全部关键词');
}

/* ==================== 生命周期 ==================== */
watch(tab, (v) => {
  if(v === 'items' && !items.value.length) loadItems(true);
  else if(v === 'platform') loadPlatforms();
  else if(v === 'jobs') loadJobs();
  else if(v === 'schedule') loadSchedules();
  else if(v === 'config' && !rows.value.length) loadConfig();
});
// 权限变化后退回可见的第一个 Tab（例如角色被收回 view.schedule）
watch(visibleTabs, (list) => {
  if(list.length && !list.some(t => t.k === tab.value)) tab.value = list[0].k;
});
onMounted(async () => {
  await loadPlatforms();
  await Promise.all([loadConfig(), loadJobs()]);
  if(can('view.schedule')) loadSchedules();
  timer = setInterval(() => { if(tab.value === 'jobs') loadJobs(); }, 3000);
});
onUnmounted(() => { clearInterval(timer); });
</script>

<template>
  <div class="card" style="margin-bottom:0;">
    <div class="card-h">
      <h2>标讯采集</h2>
      <span class="hint">平台配方一次解析、长期复用；关键词 / 白名单 / 剔除规则改动下一轮即时生效</span>
      <div class="spacer"></div>
      <div class="dt-tools">
        <span class="lab">表格</span>
        <button class="btnb btnb-txt" title="按住左右拖动，整体调整列宽"
          @pointerdown="L.beginResize('allcol', '', $event)">⇔ 列宽</button>
        <button class="btnb btnb-txt" title="按住上下拖动，整体调整行高"
          @pointerdown="L.beginResize('allrow', '', $event)">⇕ 行高</button>
        <button class="btnb btnb-txt" title="恢复默认列宽与行高" @click="L.reset()">重置</button>
      </div>
      <div class="seg">
        <button v-for="t in visibleTabs" :key="t.k" :class="{on: tab === t.k}" @click="tab = t.k">{{ t.l }}</button>
      </div>
    </div>

    <!-- ==================== 采集配置 ==================== -->
    <template v-if="tab === 'config'">
      <!-- ---------- 字段正则：普通员工只维护「匹配词」，正则由系统拼 ---------- -->
      <template v-if="kind === 'regex'">
        <div class="toolbar tbar">
          <div class="seg">
            <button v-for="(v, k) in KINDS" :key="k" :class="{on: kind === k}"
              @click="switchKind(k)">{{ v.label }}</button>
          </div>
          <span class="tb-sep"></span>
          <span class="lbl">视图：</span>
          <select v-model="regexPid" @change="loadRegex()">
            <option value="">全局匹配词</option>
            <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}（全局 + 本平台）</option>
          </select>
          <button class="btn ghost" @click="openRegexTest()">匹配测试</button>
          <span class="cnt">共 {{ regexFields.length }} 个字段 · 改完最多 1 分钟全部平台生效</span>
        </div>
        <div class="rx-wrap">
          <div v-for="f in regexFields" :key="f.field_key" class="rx-card" :class="{dim: !f.enabled}">
            <div class="rx-head">
              <b>{{ f.label }}</b>
              <code class="rx-key">{{ f.field_key }}</code>
              <span class="rx-hint">{{ f.value_hint }}</span>
              <button v-if="canManage" class="btnb btnb-txt" title="调整整条正则（高级）"
                @click="openTpl(f)">模板</button>
            </div>
            <div class="rx-tags">
              <span v-for="a in f.aliases" :key="a.id" class="rx-tag" :class="{off: !a.enabled}"
                :title="(a.enabled ? '点击停用' : '点击启用') + '「' + a.alias + '」'"
                @click="canManage && toggleAlias(a)">
                {{ a.alias }}
                <i v-if="a.scope !== 'global'" class="rx-sc">{{ platName(a.scope) }}</i>
                <button v-if="canManage" class="rx-x" title="删除" @click.stop="delAlias(a)">×</button>
              </span>
              <span v-if="!f.aliases.length" class="rx-empty">还没配置匹配词 —— 该字段不会走正则</span>
            </div>
            <div v-if="canManage" class="rx-add">
              <input v-model="aliasInput[f.field_key]" maxlength="40"
                placeholder="输入原文里的写法（如：项目规模），回车添加"
                @keyup.enter="addAlias(f)">
              <select v-if="regexPid" v-model="aliasScope[f.field_key]">
                <option :value="regexPid">仅 {{ platName(regexPid) }}</option>
                <option value="global">全局</option>
              </select>
              <button class="btn ghost" @click="addAlias(f)">添加</button>
            </div>
            <div v-if="f.error" class="rx-err">正则有误：{{ f.error }}</div>
            <details class="rx-regex">
              <summary>拼装后的正则</summary>
              <code>{{ f.regex || '（没有启用中的匹配词，未生成正则）' }}</code>
            </details>
          </div>
          <div v-if="!regexFields.length" class="dt-empty" style="padding:24px;">
            {{ regexLoading ? '加载中…' : '没有可配置的字段' }}
          </div>
        </div>
      </template>
      <template v-else>
      <div class="toolbar tbar">
        <div class="seg">
          <button v-for="(v, k) in KINDS" :key="k" :class="{on: kind === k}" @click="switchKind(k)">{{ v.label }}</button>
        </div>
        <span class="tb-sep"></span>
        <input type="search" :placeholder="KINDS[kind].search" style="width:220px;" v-model="kw" @keyup.enter="loadConfig()">
        <button class="btn ghost" @click="loadConfig()">搜索</button>
        <span class="tb-sep"></span>
        <select v-model="pid" title="新增配置时默认作用于此平台">
          <option value="">全局</option>
          <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
        <span class="cnt">{{ rows.length }} 条</span>
        <template v-if="canManage">
          <button v-if="kind === 'keyword'" class="btn ghost" @click="bulkOpen = true">批量粘贴</button>
          <button v-if="kind === 'rule'" class="btn ghost" @click="previewRules()">试跑预览</button>
          <button class="btn pri" @click="openNew()">＋ 新增</button>
        </template>
      </div>
      <div class="dt-wrap" style="max-height:calc(100vh - 330px);">
        <table class="dt-table" :style="{'--dt-w': cfgW + 'px'}">
          <colgroup>
            <col v-for="c in cfgCols" :key="c.k" :style="{width: L.colWOf(c.k) + 'px'}">
          </colgroup>
          <thead>
            <tr>
              <th v-for="c in cfgCols" :key="c.k" :class="{'dt-center': c.center}">
                {{ c.l }}
                <i class="dt-col-handle" :title="'拖动调整「' + c.l + '」列宽'"
                  @pointerdown="L.beginResize('col', c.k, $event)"></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.id" :class="{dim: !r.enabled}" :style="rowStyle('cfg', r.id)">
              <td v-for="(c, ci) in KINDS[kind].cols" :key="c.k" :class="cellCls(c)"
                  :title="c.wrap ? '' : cfgCell(r, c)">
                <i v-if="ci === 0" class="dt-row-handle" title="拖动调整本行高度"
                  @pointerdown="L.beginResize('row', 'cfg-' + r.id, $event)"></i>
                {{ cfgCell(r, c) }}
              </td>
              <td class="dt-center">
                <span class="pill" :class="r.enabled ? 'p-pay' : 'p-na'" style="cursor:pointer;"
                  :title="canManage ? '点击切换启用状态' : ''" @click="toggleEnabled(r)">
                  {{ r.enabled ? '启用' : '停用' }}
                </span>
              </td>
              <td class="dt-center">
                <button v-if="canManage" class="btnb btnb-txt" @click="openEdit(r)">编辑</button>
                <button v-if="canManage" class="btnb btnb-txt act-del" @click="removeRow(r)">删除</button>
                <span v-if="!canManage" class="dt-empty">—</span>
              </td>
            </tr>
            <tr v-if="!rows.length">
              <td :colspan="cfgCols.length" class="dt-empty">
                {{ loading ? '加载中…' : '暂无配置' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      </template>
    </template>

    <!-- ==================== 平台与配方 ==================== -->
    <template v-else-if="tab === 'platform'">
      <div class="toolbar tbar">
        <span class="lbl">采集平台：</span>
        <span class="cnt" style="margin-left:0">{{ platforms.length }} 个平台</span>
        <span class="tb-sep"></span>
        <button v-if="canManage" class="btn pri" @click="openAnalyze()">＋ AI 添加平台</button>
        <button class="btn ghost" @click="loadPlatforms()">刷新</button>
      </div>
      <div class="dt-wrap" style="max-height:calc(100vh - 330px);">
        <table class="dt-table" :style="{'--dt-w': platW + 'px'}">
          <colgroup>
            <col v-for="c in PLAT_COLS" :key="c.k" :style="{width: L.colWOf(c.k) + 'px'}">
          </colgroup>
          <thead>
            <tr>
              <th v-for="c in PLAT_COLS" :key="c.k" :class="{'dt-center': c.center}">
                {{ c.l }}
                <i class="dt-col-handle" :title="'拖动调整「' + c.l + '」列宽'"
                  @pointerdown="L.beginResize('col', c.k, $event)"></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in platforms" :key="p.id" :style="rowStyle('pf', p.id)">
              <td class="dt-wrap-cell" :title="p.name + '（' + p.id + '）'">
                <i class="dt-row-handle" title="拖动调整本行高度"
                  @pointerdown="L.beginResize('row', 'pf-' + p.id, $event)"></i>
                <b>{{ p.name }}</b>
                <div style="color:var(--sub);font-size:11px;">{{ p.id }}</div>
              </td>
              <td class="dt-ellipsis" :title="p.host || '—'">{{ p.host || '—' }}</td>
              <td class="dt-center">
                <span class="pill" :class="STATUS_CLS[p.status] || 'p-na'">{{ STATUS_TXT[p.status] || p.status }}</span>
              </td>
              <td class="dt-center">v{{ p.active_ver || '—' }}</td>
              <td class="dt-ellipsis" :title="ts(p.last_ok_at)">{{ ts(p.last_ok_at) }}</td>
              <td class="dt-center">
                <button class="btnb btnb-txt" @click="openRecipe(p)">配方</button>
                <button v-if="canManage" class="btnb btnb-txt" @click="togglePlatform(p)">
                  {{ p.status === 'active' ? '停用' : '启用' }}
                </button>
                <button v-if="canManage" class="btnb btnb-txt"
                  :disabled="jobLocked || p.status !== 'active'"
                  :title="jobLocked ? '已有任务在排队/运行' : ''"
                  @click="collect(p)">立即采集</button>
                <button v-if="canManage && p.active_ver" class="btnb btnb-txt act-del"
                  :disabled="recipeDlg.busy"
                  title="删除该平台全部配方版本，平台将停用；历史数据保留"
                  @click="deleteRecipe(p.id)">删除配方</button>
                <button v-if="canManage" class="btnb btnb-txt act-del"
                  :disabled="recipeDlg.busy || jobLocked"
                  title="彻底删除平台及其全部数据（不可恢复）"
                  @click="deletePlatform(p)">删除平台</button>
              </td>
            </tr>
            <tr v-if="!platforms.length">
              <td :colspan="PLAT_COLS.length" class="dt-empty">暂无平台，请先导入配方</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ==================== 采集任务 ==================== -->
    <template v-else-if="tab === 'jobs'">
      <div class="toolbar tbar">
        <span class="lbl">平台：</span>
        <select v-model="runForm.pid" :disabled="jobLocked">
          <option value="">全部启用平台（{{ runnableCount }} 个）</option>
          <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}{{ (p.status === 'active' || p.status === 'degraded') ? '' : '（' + (STATUS_TXT[p.status] || p.status) + '）' }}</option>
        </select>
        <input type="text" v-model="runForm.words" :disabled="jobLocked"
          placeholder="关键词（留空=全部启用关键词，可用逗号分隔）" style="width:280px;">
        <label class="imp-chk"><input type="checkbox" v-model="runForm.dry" :disabled="jobLocked"><span>空跑（不入库）</span></label>
        <span class="tb-sep"></span>
        <button class="btn pri" :disabled="jobLocked" :title="jobLocked ? '已有任务在排队/运行' : ''" @click="submitRun()">
          {{ busy ? '提交中…' : (activeJob ? '运行中，请稍候' : '开始采集') }}
        </button>
        <button class="btn ghost" @click="loadJobs()">刷新</button>
        <span class="cnt">{{ activeJob ? '任务 #' + activeJob.id + ' 运行中' : '当前空闲' }}（需 worker 进程在线）</span>
      </div>

      <!-- 当前任务进度 -->
      <div v-if="activeJob" class="bc-prog">
        <div class="bc-prog-line">
          <span>{{ activeJob.stage === 'claimed' ? '已领取，准备执行' : '采集进行中' }}</span>
          <span>{{ activeJob.progress || 0 }}%</span>
        </div>
        <div class="ai-bar"><i :style="{width: Math.max(0, Math.min(100, activeJob.progress || 0)) + '%'}"></i></div>
        <div class="bc-prog-meta">
          <span>任务 #{{ activeJob.id }}</span>
          <span>{{ activeJob.status === 'queued' ? '排队中' : '运行中' }}</span>
          <span v-if="activeJob.message">{{ activeJob.message }}</span>
          <span>提交人 {{ activeJob.created_by || '—' }}</span>
          <span>{{ ts(activeJob.created_at) }}</span>
          <button class="btn ghost" style="margin-left:auto;" @click="cancelJob(activeJob)">取消任务</button>
        </div>
      </div>

      <div class="dt-wrap" style="max-height:calc(100vh - 380px);">
        <table class="dt-table" :style="{'--dt-w': jobW + 'px'}">
          <colgroup>
            <col v-for="c in JOB_COLS" :key="c.k" :style="{width: L.colWOf(c.k) + 'px'}">
          </colgroup>
          <thead>
            <tr>
              <th v-for="c in JOB_COLS" :key="c.k" :class="{'dt-center': c.center}">
                {{ c.l }}
                <i class="dt-col-handle" :title="'拖动调整「' + c.l + '」列宽'"
                  @pointerdown="L.beginResize('col', c.k, $event)"></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="j in jobs" :key="j.id" :style="rowStyle('jb', j.id)">
              <td class="dt-ellipsis" :title="'任务 #' + j.id">
                <i class="dt-row-handle" title="拖动调整本行高度"
                  @pointerdown="L.beginResize('row', 'jb-' + j.id, $event)"></i>
                #{{ j.id }}
              </td>
              <td class="dt-ellipsis" :title="j.platform_id ? platName(j.platform_id) : '全部平台'">
                {{ j.platform_id ? platName(j.platform_id) : '全部平台' }}
              </td>
              <td class="dt-ellipsis">{{ j.job_type === 'run' ? '采集' : '规则预览' }}</td>
              <td class="dt-center">
                <span class="pill" :class="jobStatusCls[j.status] || 'p-na'">{{ jobStatusTxt[j.status] || j.status }}</span>
              </td>
              <td class="dt-center">{{ j.progress || 0 }}%</td>
              <td class="dt-wrap-cell" :title="j.error || j.message || ''">{{ j.error || j.message || '—' }}</td>
              <td class="dt-ellipsis" :title="ts(j.created_at)">{{ ts(j.created_at) }}</td>
              <td class="dt-center">
                <button v-if="canManage && (j.status === 'queued' || j.status === 'running')"
                  class="btnb btnb-txt" @click="cancelJob(j)">取消</button>
                <span v-else class="dt-empty">—</span>
              </td>
            </tr>
            <tr v-if="!jobs.length">
              <td :colspan="JOB_COLS.length" class="dt-empty">暂无任务</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ==================== 采集结果 ==================== -->
    <template v-else-if="tab === 'items'">
      <div class="toolbar tbar">
        <select v-model="f.platform" @change="loadItems(true)">
          <option value="">全部平台</option>
          <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
        <select v-model="f.status" @change="loadItems(true)">
          <option value="">全部状态</option>
          <option value="open">报名中</option>
          <option value="soon">即将截止</option>
          <option value="expired">已截止</option>
          <option value="unknown">未知</option>
        </select>
        <input type="search" v-model="f.q" placeholder="搜索标题 / 采购人" style="width:240px;" @keyup.enter="loadItems(true)">
        <button class="btn ghost" @click="loadItems(true)">查询</button>
        <span class="cnt">共 {{ itemTotal }} 条</span>
      </div>
      <div v-if="lastEnrolled" style="display:flex;align-items:center;gap:10px;margin:8px 0;
        padding:7px 12px;background:#f0f9eb;border:1px solid #e1f3d8;border-radius:4px;
        font-size:12px;color:#3f8f2f;">
        <span>已加入投标管理：<b>{{ lastEnrolled.name }}</b></span>
        <button class="btnb btnb-txt" style="color:var(--pri);" @click="gotoManage">去查看 →</button>
        <button class="btnb" style="margin-left:auto;" title="关闭" @click="lastEnrolled = null">✕</button>
      </div>
      <div class="dt-wrap" ref="itemsWrap" style="max-height:calc(100vh - 380px);">
        <table class="dt-table" :style="{'--dt-w': itemW + 'px'}">
          <colgroup>
            <col v-for="c in ITEM_COLS" :key="c.k" :style="{width: L.colWOf(c.k) + 'px'}">
          </colgroup>
          <thead>
            <tr>
              <th v-for="c in ITEM_COLS" :key="c.k" :class="{'dt-center': c.center}">
                {{ c.l }}
                <i class="dt-col-handle" :title="'拖动调整「' + c.l + '」列宽'"
                  @pointerdown="L.beginResize('col', c.k, $event)"></i>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="it in items" :key="it.id" :style="rowStyle('it', it.id)">
              <td class="dt-wrap-cell">
                <i class="dt-row-handle" title="拖动调整本行高度"
                  @pointerdown="L.beginResize('row', 'it-' + it.id, $event)"></i>
                <a v-if="it.url" :href="it.url" target="_blank" rel="noopener" :title="it.title"
                  style="color:var(--pri);text-decoration:none;">{{ it.title }}</a>
                <span v-else>{{ it.title }}</span>
              </td>
              <td class="dt-ellipsis" :title="it.buyer || '—'">{{ it.buyer || '—' }}</td>
              <td class="dt-ellipsis" :title="(it.project && it.project.bidNo) || '—'">{{ (it.project && it.project.bidNo) || '—' }}</td>
              <td class="dt-ellipsis" :title="(it.project && it.project.openTime) || '—'">{{ (it.project && it.project.openTime) || '—' }}</td>
              <td class="dt-ellipsis" :title="ts(it.publish_at)">{{ ts(it.publish_at) }}</td>
              <td class="dt-ellipsis" :title="ts(it.doc_get_end)">{{ ts(it.doc_get_end) }}</td>
              <td class="dt-num" :title="money(it.budget_yuan)">{{ money(it.budget_yuan) }}</td>
              <td class="dt-center">
                <span class="pill" :class="itemStatusCls[it.status] || 'p-na'">{{ itemStatusTxt[it.status] || it.status }}</span>
              </td>
              <td class="dt-ellipsis" :title="platName(it.platform_id)">{{ platName(it.platform_id) }}</td>
              <td class="dt-center">
                <button v-if="it.enrolled" class="btnb btnb-txt" title="已在投标管理中"
                  style="color:var(--ok,#3f8f2f);" @click="gotoManage">已报名</button>
                <button v-else class="btn" style="padding:2px 10px;font-size:11.5px;"
                  :disabled="!canEnroll || enrolling === it.id"
                  :title="canEnroll ? '把该项目加入投标管理' : '无台账新增权限'"
                  @click="enroll(it)">{{ enrolling === it.id ? '提交中…' : '报名' }}</button>
              </td>
            </tr>
            <tr v-if="!items.length">
              <td :colspan="ITEM_COLS.length" class="dt-empty">{{ itemsLoading ? '加载中…' : '暂无采集结果' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <select v-model.number="pageSize" :disabled="itemsLoading" @change="changePageSize(pageSize)">
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }} 条/页</option>
        </select>
        <span class="pg-info">
          第 <b>{{ pageNo }}</b> / {{ pageCnt }} 页 · 共 <b>{{ itemTotal }}</b> 条
          <template v-if="itemTotal">（当前 {{ rangeFrom }}–{{ rangeTo }}）</template>
        </span>
        <div class="pg-btns">
          <button class="pg-btn" :disabled="itemsLoading || pageNo <= 1" title="首页" @click="gotoPage(1)">«</button>
          <button class="pg-btn" :disabled="itemsLoading || pageNo <= 1" title="上一页" @click="gotoPage(pageNo - 1)">‹</button>
          <template v-for="(p, i) in visiblePages" :key="i">
            <button v-if="p !== '...'" class="pg-btn" :class="{on: p === pageNo}"
              :disabled="itemsLoading" @click="gotoPage(p)">{{ p }}</button>
            <span v-else class="pg-dots">…</span>
          </template>
          <button class="pg-btn" :disabled="itemsLoading || pageNo >= pageCnt" title="下一页" @click="gotoPage(pageNo + 1)">›</button>
          <button class="pg-btn" :disabled="itemsLoading || pageNo >= pageCnt" title="末页" @click="gotoPage(pageCnt)">»</button>
        </div>
        <span class="jump">跳转至
          <input type="number" min="1" :max="pageCnt" :value="pageNo" :disabled="itemsLoading"
            @keyup.enter="gotoPage($event.target.value)"
            @change="gotoPage($event.target.value)">
          页
        </span>
      </div>
    </template>

    <!-- ==================== 定时任务 ==================== -->
    <template v-else-if="tab === 'schedule'">
      <div class="toolbar tbar">
        <button v-if="canSchedAdd" class="btn pri" @click="openSched(null)">+ 新建定时任务</button>
        <button class="btn ghost" @click="loadSchedules()">刷新</button>
        <span class="cnt">共 {{ schedules.length }} 条 · 采集 worker 每 30 秒检查一次，到点自动提交</span>
      </div>
      <div class="dt-wrap" style="max-height:calc(100vh - 340px);">
        <table class="dt-table" style="--dt-w:840px;">
          <thead>
            <tr>
              <th style="width:210px;">任务</th>
              <th style="width:88px;" class="dt-center">执行时间</th>
              <th style="width:72px;" class="dt-center">状态</th>
              <th style="width:170px;">上次执行</th>
              <th style="width:135px;">下次执行</th>
              <th style="width:165px;" class="dt-center">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in schedules" :key="s.id">
              <td>
                <div class="sch-name" :title="s.name">{{ s.name }}</div>
                <div class="sch-sub" :title="schedPlatNames(s) + ' ｜ ' + schedWordNames(s)">
                  {{ schedScopeTxt(s) }}
                </div>
              </td>
              <td class="dt-center"><b>{{ s.run_at }}</b></td>
              <td class="dt-center">
                <span class="pill" :class="s.enabled ? 'p-pay' : 'p-na'">{{ s.enabled ? '启用' : '停用' }}</span>
              </td>
              <td class="dt-ellipsis">
                <template v-if="s.last_run_at">
                  {{ ts(s.last_run_at) }}
                  <a v-if="s.last_job_id" href="javascript:void 0" style="color:var(--pri);"
                    :title="'查看任务 #' + s.last_job_id" @click="tab = 'jobs'">#{{ s.last_job_id }}</a>
                  <span v-if="s.last_status === 'error'" style="color:var(--warn);"
                    :title="s.last_error || ''">提交失败</span>
                  <span v-else-if="s.last_status === 'merged'" style="color:var(--mut);">合并到活动任务</span>
                </template>
                <span v-else style="color:var(--mut);">未执行</span>
              </td>
              <td class="dt-ellipsis">{{ s.enabled ? ts(s.next_run_at) : '—' }}</td>
              <td class="dt-center">
                <button v-if="canSchedEdit" class="btnb btnb-txt" title="立即提交一次采集"
                  @click="runSched(s)">立即执行</button>
                <button v-if="canSchedEdit" class="btnb btnb-txt" @click="openSched(s)">编辑</button>
                <button v-if="canSchedEdit" class="btnb btnb-txt"
                  @click="toggleSched(s)">{{ s.enabled ? '停用' : '启用' }}</button>
                <button v-if="canSchedDel" class="btnb btnb-txt" style="color:var(--warn);"
                  @click="delSched(s)">删除</button>
              </td>
            </tr>
            <tr v-if="!schedules.length">
              <td colspan="6" class="dt-empty">
                还没有定时任务{{ canSchedAdd ? '，点左上角「新建定时任务」' : '' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>

  <!-- 配置新增 / 编辑 -->
  <div class="mask" :class="{on: dlg.open}" @click.self="dlg.open = false">
    <div class="dlg" style="width:min(620px, calc(100vw - 40px));">
      <div class="dlg-h">
        <h3>{{ dlg.id ? '编辑' : '新增' }}{{ KINDS[kind].label }}</h3>
        <button class="btnb" title="关闭" @click="dlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <div class="form" style="grid-template-columns:repeat(2,1fr);">
          <div v-if="kind === 'keyword'" class="fld span2">
            <label>关键词 <b>*</b></label>
            <input v-model="form.word" maxlength="32" placeholder="如：门头标识">
          </div>
          <div v-if="kind === 'buyer'" class="fld span2">
            <label>采购人匹配内容 <b>*</b></label>
            <input v-model="form.pattern" placeholder="如：烟草 / 银行">
          </div>
          <div v-if="kind === 'buyer'" class="fld">
            <label>匹配方式</label>
            <select v-model="form.match_type">
              <option value="contains">包含</option>
              <option value="regex">正则</option>
            </select>
          </div>
          <div v-if="kind === 'rule'" class="fld">
            <label>规则名称 <b>*</b></label>
            <input v-model="form.name" placeholder="如：结果类公告">
          </div>
          <div v-if="kind === 'rule'" class="fld">
            <label>规则类型</label>
            <select v-model="form.rule_type">
              <option v-for="t in RULE_TYPES" :key="t.v" :value="t.v">{{ t.l }}</option>
            </select>
          </div>
          <div v-if="kind === 'rule'" class="fld span2">
            <label>规则值（JSON） <b>*</b></label>
            <textarea v-model="form.value" rows="3" style="font-family:ui-monospace,monospace;"
              :placeholder="(RULE_TYPES.find(t => t.v === form.rule_type) || {}).tip"></textarea>
          </div>
          <div class="fld">
            <label>作用域</label>
            <select v-model="form.scope">
              <option value="global">全局</option>
              <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}（仅此平台）</option>
            </select>
          </div>
          <div class="fld">
            <label>排序</label>
            <input type="number" v-model.number="form.sort_no">
          </div>
          <div class="fld span2">
            <label>备注</label>
            <input v-model="form.note">
          </div>
          <div class="fld" style="flex-direction:row;align-items:center;gap:16px;">
            <label class="imp-chk"><input type="checkbox" v-model="form.enabled"><span>启用</span></label>
            <label v-if="kind === 'keyword'" class="imp-chk"><input type="checkbox" v-model="form.is_sample"><span>示例词</span></label>
          </div>
        </div>
        <p class="fld-tip">保存后立即写入数据库，下一轮采集直接生效，无需重新解析平台。</p>
      </div>
      <div class="dlg-f">
        <button class="btn ghost" @click="dlg.open = false">取消</button>
        <button class="btn pri" :disabled="dlg.busy" @click="saveForm()">{{ dlg.busy ? '保存中…' : '保存' }}</button>
      </div>
    </div>
  </div>

  <!-- 关键词批量粘贴 -->
  <div class="mask" :class="{on: bulkOpen}" @click.self="bulkOpen = false">
    <div class="dlg" style="width:min(560px, calc(100vw - 40px));">
      <div class="dlg-h"><h3>批量导入关键词</h3>
        <button class="btnb" title="关闭" @click="bulkOpen = false">✕</button></div>
      <div class="dlg-b">
        <div class="fld">
          <label>每行一个词，也支持逗号分隔</label>
          <textarea v-model="bulkText" rows="8" placeholder="门头标识&#10;灯箱&#10;发光字"></textarea>
        </div>
        <p class="fld-tip">重复词会自动合并；作用域默认为当前所选平台，选「全局」则对所有平台生效。</p>
      </div>
      <div class="dlg-f">
        <button class="btn ghost" @click="bulkOpen = false">取消</button>
        <button class="btn pri" @click="saveBulk()">导入</button>
      </div>
    </div>
  </div>

  <!-- 配方查看 / 保存新版本 -->
  <div class="mask" :class="{on: recipeDlg.open}" @click.self="recipeDlg.open = false">
    <div class="dlg">
      <div class="dlg-h">
        <h3>采集配方 · {{ platName(recipeDlg.pid) }}（当前 v{{ recipeDlg.version }}）</h3>
        <button class="btnb" title="关闭" @click="recipeDlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <textarea v-model="recipeText" rows="22" :readonly="!canManage"
          style="width:100%;font-family:ui-monospace,monospace;font-size:11.5px;line-height:1.55;"
          placeholder="该平台还没有配方，可粘贴 JSON 后保存"></textarea>
        <p class="fld-tip">保存会在 bid_recipe_version 生成新版本并立即启用；旧版本保留可回滚。</p>
      </div>
      <div class="dlg-f">
        <button v-if="canManage && recipeDlg.version !== '—'" class="btn danger"
          style="margin-right:auto" :disabled="recipeDlg.busy" @click="deleteRecipe()">删除配方</button>
        <button class="btn ghost" @click="recipeDlg.open = false">关闭</button>
        <button v-if="canManage" class="btn pri" :disabled="recipeDlg.busy" @click="saveRecipe()">
          {{ recipeDlg.busy ? '保存中…' : '保存为新版本并启用' }}
        </button>
      </div>
    </div>
  </div>

  <!-- AI 添加平台 -->
  <div class="mask" :class="{on: analyzeDlg.open}" @click.self="analyzeDlg.open = false">
    <div class="dlg" style="width:min(860px, calc(100vw - 40px));">
      <div class="dlg-h">
        <h3>AI 自动生成采集配方 <span style="font-size:12px;font-weight:400;color:var(--sub);margin-left:8px;">探测 → 生成 → 试跑</span></h3>
        <button class="btnb" title="关闭" @click="analyzeDlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <!-- 模式切换 -->
        <div class="bc-modes">
          <button v-for="m in [['api_list','接口 JSON'],['html_list','HTML 列表'],['auto','自动探测']]"
            :key="m[0]" class="bc-mode" :class="{on: analyzeDlg.mode === m[0]}"
            :disabled="analyzeDlg.busy" @click="setAnalyzeMode(m[0])">{{ m[1] }}</button>
          <span class="fld-tip" style="margin:0 0 0 auto;">{{ analyzeDlg.mode === 'auto' ? '旧模式' : '推荐：用真实 curl + 返回样本反推结构' }}</span>
        </div>

        <!-- 自动探测（旧） -->
        <template v-if="analyzeDlg.mode === 'auto'">
          <div class="form" style="grid-template-columns:minmax(280px,1fr) 150px auto;">
            <div class="fld">
              <label>招标公告列表页 URL <b>*</b></label>
              <input v-model="analyzeDlg.url" placeholder="https://www.cfcpn.com/... 或具体列表页"
                :disabled="analyzeDlg.busy" @keyup.enter="startAnalyze()">
            </div>
            <div class="fld">
              <label>样本关键词</label>
              <input v-model="analyzeDlg.keyword" placeholder="用于验证筛选是否生效"
                :disabled="analyzeDlg.busy">
            </div>
            <div class="fld" style="align-self:end;">
              <button class="btn pri" :disabled="!canAnalyze" @click="startAnalyze()">
                {{ analyzeDlg.busy ? '探测中…' : '生成配方' }}
              </button>
            </div>
          </div>
        </template>

        <!-- 证据模式 -->
        <template v-else>
          <div class="form" style="grid-template-columns:1fr 1fr 1fr;">
            <div class="fld">
              <label>站点名称</label>
              <input v-model="analyzeDlg.name" placeholder="如：农银e采平台（不填自动取标题）"
                :disabled="analyzeDlg.busy">
            </div>
            <div class="fld">
              <label>样本关键词</label>
              <input v-model="analyzeDlg.keyword" placeholder="curl 里出现的搜索词，如 门楣"
                :disabled="analyzeDlg.busy">
            </div>
            <div class="fld">
              <label>列表页 URL（可选）</label>
              <input v-model="analyzeDlg.siteUrl" placeholder="给平台记录入口地址"
                :disabled="analyzeDlg.busy">
            </div>
          </div>

          <div class="fld" style="margin-top:10px;">
            <label>① 列表请求 curl <b>*</b>
              <span class="fld-tip">F12 → Network → 右键 Copy as cURL（bash）</span></label>
            <textarea v-model="analyzeDlg.listCurl" rows="4" spellcheck="false" class="bc-ta"
              :disabled="analyzeDlg.busy"
              placeholder="curl 'https://xxx/api/list' -H 'Content-Type: application/json' --data-raw '{...}'"></textarea>
          </div>
          <div class="fld" style="margin-top:8px;">
            <label>② 列表返回{{ analyzeDlg.mode === 'api_list' ? '样本（JSON）' : '样本（HTML 源码）' }} <b>*</b>
              <span class="fld-tip">{{ analyzeDlg.mode === 'api_list' ? 'Network → Response 全文' : '列表搜索结果那一段页面源码，Ctrl+U 复制整页也可以' }}</span></label>
            <textarea v-model="analyzeDlg.listSample" rows="6" spellcheck="false" class="bc-ta"
              :disabled="analyzeDlg.busy"
              :placeholder="analyzeDlg.mode === 'api_list' ? '{&quot;code&quot;:200,&quot;data&quot;:{&quot;rows&quot;:[...]}}' : '&lt;ul class=&quot;newslist&quot;&gt;&lt;li&gt;...&lt;/li&gt;&lt;/ul&gt;'"></textarea>
          </div>

          <div class="form" style="grid-template-columns:1fr 1fr;margin-top:10px;">
            <div class="fld">
              <label>③ 详情请求 curl（可选）</label>
              <textarea v-model="analyzeDlg.detailCurl" rows="3" spellcheck="false" class="bc-ta"
                :disabled="analyzeDlg.busy"
                placeholder="详情接口/详情页的 cURL；填了才能自动还原 id 模板"></textarea>
            </div>
            <div class="fld">
              <label>④ 详情返回样本（可选）</label>
              <textarea v-model="analyzeDlg.detailSample" rows="3" spellcheck="false" class="bc-ta"
                :disabled="analyzeDlg.busy"
                placeholder="详情 JSON 或详情页源码；正文在 PDF 里也能识别"></textarea>
            </div>
          </div>
          <div class="form" style="grid-template-columns:1fr auto;margin-top:8px;align-items:end;">
            <div class="fld">
              <label>详情页 URL 示例（可选）
                <span class="fld-tip">{{ analyzeDlg.mode === 'html_list'
                  ? '可贴一条公告的真实地址，也可直接写 https://站点域名{href}（用行内链接拼）'
                  : '粘贴任意一条公告的真实地址即可，系统会用列表里的 id 字段套成模板' }}</span></label>
              <input v-model="analyzeDlg.detailPageTpl" :disabled="analyzeDlg.busy"
                :placeholder="analyzeDlg.mode === 'html_list'
                  ? 'https://www.365trade.com.cn{href}'
                  : 'https://x/#/noticeDetail?messageId=M10001（也可直接写 {messageId}）'">
            </div>
            <div class="fld">
              <button class="btn pri" :disabled="!canAnalyze" @click="startAnalyze()">
                {{ analyzeDlg.busy ? '生成中…' : '生成并校验' }}
              </button>
            </div>
          </div>
        </template>

        <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:10px;">
          <label style="font-size:12px;display:flex;align-items:center;gap:5px;cursor:pointer;">
            <input type="checkbox" v-model="analyzeDlg.withLlm" :disabled="analyzeDlg.busy">
            使用 AI 做字段语义映射（关闭则纯启发式）
          </label>
        </div>
        <p class="fld-tip">{{ MODE_TIPS[analyzeDlg.mode] }}</p>
        <p class="fld-tip">
          结构（jsonpath / CSS 选择器 / 参数模板 / 翻页参数）全部由你提供的真实请求与样本反推，
          先在样本上回测，再用同一份配方真实请求一次；AI 只负责把站点原始字段名映射到标准字段。<br>
          生成的配方默认 status=paused，且关闭全局采购人白名单，请在「配方」里审核后再启用。
        </p>

        <div v-if="analyzeDlg.busy" style="margin-top:12px;padding:10px 12px;border-radius:4px;
          background:var(--card,#f7f8fa);border:1px solid var(--line2);font-size:12px;color:var(--sub);">
          {{ analyzeDlg.mode === 'auto' ? '正在抓取页面与 JS、试探接口、生成配方并试跑校验…' : '正在解析 curl 与样本、反推结构、回测并试跑…' }}
          通常需要 3～20 秒，请勿关闭窗口。
        </div>

        <div v-if="analyzeDlg.result" style="margin-top:14px;">
          <!-- 生成时间线 -->
          <div style="font-weight:600;font-size:13px;margin-bottom:8px;">生成时间线
            <span style="font-weight:400;color:var(--sub);font-size:11.5px;margin-left:8px;">
              {{ analyzeDlg.result.evidence?.requests ?? '' }}
              <template v-if="analyzeDlg.result.evidence?.requests">次请求 · </template>
              {{ analyzeDlg.result.evidence?.elapsed_ms ?? 0 }} ms
            </span>
          </div>
          <div style="max-height:190px;overflow:auto;border:1px solid var(--line2);border-radius:4px;">
            <div v-for="(s,i) in (analyzeDlg.result.stages || [])" :key="i"
              style="display:flex;align-items:flex-start;gap:9px;padding:7px 10px;
                border-bottom:1px solid var(--line2);font-size:11.5px;">
              <span class="pill" :class="stageLabel(s.status)" style="flex:none;min-width:34px;text-align:center;">{{ s.status }}</span>
              <span style="flex:none;width:110px;font-weight:600;">{{ s.name }}</span>
              <span style="flex:1;color:var(--sub);word-break:break-all;line-height:1.5;">{{ s.detail }}</span>
              <span style="flex:none;color:var(--sub);font-family:ui-monospace,monospace;">{{ s.ms }}ms</span>
            </div>
          </div>

          <!-- 证据摘要 -->
          <div v-if="analyzeDlg.result.evidence" style="margin-top:12px;">
            <div style="font-weight:600;font-size:13px;margin-bottom:8px;">证据摘要</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:8px;">
              <!-- 证据模式（curl + 样本） -->
              <div v-if="analyzeDlg.result.evidence.mode" class="bc-ev">
                <b>列表请求</b>
                <span>{{ analyzeDlg.result.evidence.list_request?.method }}
                  {{ analyzeDlg.result.evidence.list_request?.url }}</span>
                <span>样本 {{ analyzeDlg.result.evidence.items }} 条 · 请求头
                  {{ (analyzeDlg.result.evidence.list_headers || []).length }} 个</span>
                <span v-if="analyzeDlg.result.evidence.list_request?.params">
                  query {{ JSON.stringify(analyzeDlg.result.evidence.list_request.params) }}</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.mode" class="bc-ev">
                <b>条目规则</b>
                <span v-if="analyzeDlg.result.evidence.items_path">
                  items_path <code>{{ analyzeDlg.result.evidence.items_path }}</code></span>
                <span v-if="analyzeDlg.result.evidence.row_selector">
                  row_selector <code>{{ analyzeDlg.result.evidence.row_selector }}</code></span>
                <span>关键词参数 <code>{{ analyzeDlg.result.evidence.keyword_param || '未识别' }}</code>
                  <template v-if="analyzeDlg.result.evidence.keyword_by === 'value'">（按样本值命中）</template>
                  <template v-else-if="analyzeDlg.result.evidence.keyword_by === 'name'">（按参数名推断）</template>
                </span>
                <span>翻页 <code>{{ analyzeDlg.result.evidence.page_param || '单页' }}</code>
                  <template v-if="analyzeDlg.result.evidence.size_param"> · 页宽 <code>{{ analyzeDlg.result.evidence.size_param }}</code></template>
                </span>
              </div>
              <div v-if="analyzeDlg.result.evidence.mode" class="bc-ev">
                <b>详情</b>
                <span v-if="analyzeDlg.result.evidence.detail_url" style="word-break:break-all;">
                  {{ analyzeDlg.result.evidence.detail_url }}</span>
                <span v-if="analyzeDlg.result.evidence.content_path">
                  正文 <code>{{ analyzeDlg.result.evidence.content_path }}</code></span>
                <span v-if="analyzeDlg.result.evidence.content_css">
                  正文容器 <code>{{ analyzeDlg.result.evidence.content_css }}</code></span>
                <span v-if="analyzeDlg.result.evidence.pdf_url" style="word-break:break-all;">
                  PDF <a :href="analyzeDlg.result.evidence.pdf_url" target="_blank" rel="noopener"
                    style="color:var(--pri);">直链</a></span>
                <span v-if="!analyzeDlg.result.evidence.detail_url && !analyzeDlg.result.evidence.content_path"
                  style="color:var(--warn);">未提供详情证据，配方只采列表</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.mode" class="bc-ev">
                <b>详情页链接（点击标讯跳转的地址）</b>
                <span style="word-break:break-all;">
                  <code>{{ analyzeDlg.result.evidence.url_template ||
                    (analyzeDlg.result.evidence.url_field?.path
                      ? '列表字段 ' + analyzeDlg.result.evidence.url_field.path : '—') }}</code>
                </span>
                <span :style="{color: analyzeDlg.result.evidence.url_source === 'guess' ? 'var(--warn)' : 'var(--sub)'}">
                  来源：{{ urlSourceTxt(analyzeDlg.result.evidence.url_source) }}
                  <template v-if="analyzeDlg.result.evidence.url_source === 'guess'">
                    · 建议在上方填「详情页 URL 示例」</template>
                </span>
              </div>
              <!-- 自动探测模式 -->
              <div v-if="analyzeDlg.result.evidence.browser" class="bc-ev">
                <b>浏览器嗅探（SPA）</b>
                <span>{{ analyzeDlg.result.evidence.browser.browser }}</span>
                <span>捕获 {{ analyzeDlg.result.evidence.browser.requests }} 个取数请求 ·
                  {{ analyzeDlg.result.evidence.browser.ok ? '已定位接口' : '未定位' }}</span>
                <span v-if="analyzeDlg.result.evidence.browser.reason"
                  style="color:var(--warn);">{{ analyzeDlg.result.evidence.browser.reason }}</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.list" class="bc-ev">
                <b>列表接口</b>
                <span>{{ analyzeDlg.result.evidence.list.method }} {{ analyzeDlg.result.evidence.list.url }}</span>
                <span>{{ analyzeDlg.result.evidence.list.items_path }} · total={{ analyzeDlg.result.evidence.list.total }}</span>
                <span>关键词参数 <code>{{ analyzeDlg.result.evidence.list.keyword_key }}</code> ·
                  命中率 {{ pct(analyzeDlg.result.evidence.list.keyword_hit) }} ·
                  翻页{{ analyzeDlg.result.evidence.list.page2_differs ? '有效' : '无效/单页' }}</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.detail" class="bc-ev">
                <b>详情二次请求</b>
                <span>body {{ JSON.stringify(analyzeDlg.result.evidence.detail.body) }}</span>
                <span>{{ analyzeDlg.result.evidence.detail.row_path }}.{{ analyzeDlg.result.evidence.detail.content_path }}</span>
                <span>{{ analyzeDlg.result.evidence.detail.content_len }} 字符 · {{ analyzeDlg.result.evidence.detail.content_encoding }}</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.html_list" class="bc-ev" style="grid-column:1/-1;">
                <b>HTML 列表探测</b>
                <span>{{ analyzeDlg.result.evidence.html_list.search.url }} ·
                  {{ analyzeDlg.result.evidence.html_list.row_selector }}（{{
                    analyzeDlg.result.evidence.html_list.rows }} 行 / 共 {{
                    analyzeDlg.result.evidence.html_list.total_pages }} 页）</span>
                <span>关键词参数 <code>{{ analyzeDlg.result.evidence.html_list.search.keyword_param }}</code>
                  · 分页 <code>{{ analyzeDlg.result.evidence.html_list.search.page_param }}</code>
                  · 命中率 {{ pct(analyzeDlg.result.evidence.html_list.keyword_hit) }}
                  · 翻页{{ analyzeDlg.result.evidence.html_list.page2_differs ? '有效' : '无效' }}</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.pdf" class="bc-ev" style="grid-column:1/-1;">
                <b>PDF 正文（下载链接，按二进制解析后接入 AI 抽取）</b>
                <span style="word-break:break-all;">
                  <a :href="analyzeDlg.result.evidence.pdf.pdf_url" target="_blank" rel="noopener"
                    style="color:var(--pri);">{{ analyzeDlg.result.evidence.pdf.pdf_url }}</a>
                </span>
                <span>按 PDF 地址缓存文本，同一公告重复命中不重复下载；无模型时降级为正则</span>
              </div>
              <div v-if="analyzeDlg.result.evidence.detail_url_template" class="bc-ev" style="grid-column:1/-1;">
                <b>详情页 URL 模板</b>
                <span style="word-break:break-all;">{{ analyzeDlg.result.evidence.detail_url_template }}</span>
              </div>
              <div v-if="(analyzeDlg.result.evidence.errors || []).length" class="bc-ev" style="grid-column:1/-1;">
                <b>探测告警</b>
                <span v-for="(e,i) in analyzeDlg.result.evidence.errors" :key="i">{{ e }}</span>
              </div>
            </div>
          </div>

          <!-- 试跑校验 -->
          <div v-if="analyzeDlg.result.verify && !analyzeDlg.result.verify.skipped" style="margin-top:12px;">
            <div style="font-weight:600;font-size:13px;margin-bottom:8px;">试跑校验
              <span class="pill" :class="analyzeDlg.result.verify.valid ? 'p-pay' : 'p-fail'"
                style="margin-left:8px;">{{ analyzeDlg.result.verify.valid ? '通过' : '未通过' }}</span>
            </div>
            <div style="font-size:11.5px;color:var(--sub);line-height:1.8;">
              样本 {{ analyzeDlg.result.verify.items }} 条（total {{ analyzeDlg.result.verify.total ?? '—' }}）·
              {{ analyzeDlg.result.verify.pdf_url ? 'PDF 正文' : '详情正文' }}
              {{ analyzeDlg.result.verify.detail_len }} 字符
              <span v-if="analyzeDlg.result.verify.field_coverage"> · 字段非空率：
                <span v-for="(v,k) in analyzeDlg.result.verify.field_coverage" :key="k"
                  style="display:inline-block;margin-right:8px;">{{ k }} <b>{{ pct(v) }}</b></span>
              </span>
              <span v-if="analyzeDlg.result.verify.pdf_meta" style="display:block;">
                PDF 下载 <b>HTTP {{ analyzeDlg.result.verify.pdf_meta.status }}</b> ·
                {{ analyzeDlg.result.verify.pdf_meta.bytes }} 字节
                <span v-if="analyzeDlg.result.verify.pdf_meta.filename">
                  · {{ analyzeDlg.result.verify.pdf_meta.filename }}</span>
                · 解析库 <b>{{ analyzeDlg.result.verify.pdf_meta.backend || '无' }}</b>
                <span v-if="analyzeDlg.result.verify.pdf_meta.tried">
                  （{{ analyzeDlg.result.verify.pdf_meta.tried.join(' / ')}}）</span>
                <span v-if="analyzeDlg.result.verify.pdf_url" style="word-break:break-all;">
                  · <a :href="analyzeDlg.result.verify.pdf_url" target="_blank" rel="noopener"
                    style="color:var(--pri);">打开/下载该 PDF</a></span>
              </span>
              <span v-if="(analyzeDlg.result.verify.errors || []).length"
                style="color:var(--warn);display:block;">{{ analyzeDlg.result.verify.errors.join('；') }}</span>
              <span v-for="w in (analyzeDlg.result.verify.warnings || [])" :key="w"
                style="color:var(--sub);display:block;">· {{ w }}</span>
            </div>
          </div>

          <!-- 成功提示 -->
          <div v-if="analyzeDlg.result.ok" style="background:#f0f9eb;border:1px solid #e1f3d8;color:#3f8f2f;
            padding:8px 12px;border-radius:4px;font-size:12px;margin:12px 0 8px;">
            ✅ {{ analyzeDlg.result.warning || '配方已生成并校验通过' }}
          </div>
          <!-- 失败提示 -->
          <div v-else style="background:#fef0f0;border:1px solid #fde2e2;color:#c0392b;
            padding:8px 12px;border-radius:4px;font-size:12px;margin:12px 0 8px;">
            ⚠ {{ analyzeDlg.result.error }}
          </div>

          <!-- 配方（可编辑） -->
          <div v-if="analyzeDlg.result.recipe" style="margin-top:8px;">
            <div style="font-weight:600;font-size:13px;margin-bottom:6px;">配方 JSON（可直接修改后保存）</div>
            <textarea v-model="analyzeDlg.recipeText" rows="14" spellcheck="false"
              style="width:100%;font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;
                background:var(--card,#fafafa);color:var(--txt);border:1px solid var(--line2);
                border-radius:4px;padding:8px;"></textarea>
          </div>

          <!-- 手动步骤 -->
          <div v-if="analyzeDlg.manual" style="margin-top:10px;">
            <div style="font-weight:600;font-size:13px;margin-bottom:6px;">手动生成配方步骤</div>
            <pre style="background:var(--card,#f5f7fa);padding:12px;border-radius:4px;font-size:11.5px;
              line-height:1.6;white-space:pre-wrap;max-height:260px;overflow:auto;">{{ analyzeDlg.manual }}</pre>
          </div>
        </div>
      </div>
      <div class="dlg-f">
        <button class="btn ghost" @click="analyzeDlg.open = false">关闭</button>
        <button v-if="analyzeDlg.result && analyzeDlg.result.recipe" class="btn pri" @click="editAnalyzedRecipe()">
          编辑并保存配方
        </button>
      </div>
    </div>
  </div>

  <!-- 定时任务 新建 / 编辑 -->
  <div class="mask" :class="{on: schDlg.open}" @click.self="schDlg.open = false">
    <div class="dlg" style="width:min(760px, calc(100vw - 40px));">
      <div class="dlg-h">
        <h3>{{ schDlg.id ? '编辑定时任务' : '新建定时任务' }}</h3>
        <button class="btnb" title="关闭" @click="schDlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <div class="form" style="grid-template-columns:repeat(2,1fr);">
          <div class="fld">
            <label>任务名称</label>
            <input v-model="schDlg.name" maxlength="60" placeholder="留空自动命名，如「每日 09:00」">
          </div>
          <div class="fld">
            <label>每天执行时间 <b>*</b></label>
            <input v-model="schDlg.run_at" type="time" step="60">
            <div style="font-size:11.5px;color:var(--mut);margin-top:4px;">
              服务器本地时间；保存后由采集 worker 到点提交
            </div>
          </div>

          <div class="fld span2">
            <label>采集平台 <span style="font-weight:400;color:var(--mut);">（不选=全部启用平台）</span></label>
            <div class="sch-pick">
              <button type="button" class="sch-chip sch-all"
                :class="{on: !schDlg.platforms.length}" @click="clearSel(schDlg.platforms)">
                全部启用平台
              </button>
              <button type="button" v-for="p in platforms" :key="p.id" class="sch-chip"
                :class="{on: inArr(schDlg.platforms, p.id)}"
                @click="toggleArr(schDlg.platforms, p.id)">
                {{ p.name }}<i v-if="p.status !== 'active' && p.status !== 'degraded'"
                  class="sch-off">{{ STATUS_TXT[p.status] || p.status }}</i>
              </button>
            </div>
          </div>

          <div class="fld span2">
            <label>采集关键词 <span style="font-weight:400;color:var(--mut);">（不选=各平台全部关键词，含平台专属词）</span></label>
            <div class="sch-pick">
              <input v-model="schKw" type="search" placeholder="搜索关键词" class="sch-search">
              <button type="button" class="sch-chip sch-all"
                :class="{on: !schDlg.keywords.length}" @click="clearSel(schDlg.keywords)">
                全部关键词
              </button>
              <button type="button" v-for="k in schKwList" :key="k.id" class="sch-chip"
                :class="{on: inArr(schDlg.keywords, k.word)}"
                @click="toggleArr(schDlg.keywords, k.word)">{{ k.word }}</button>
              <span v-if="!schKwList.length" class="sch-empty">
                {{ schedKeywords.length ? '没有匹配的关键词' : '暂无启用的全局关键词' }}
              </span>
            </div>
          </div>

          <div class="fld span2">
            <label class="imp-chk" style="margin:0;">
              <input type="checkbox" v-model="schDlg.enabled">
              <span>启用（取消后不再自动提交，可随时手动「立即执行」）</span>
            </label>
          </div>
        </div>
      </div>
      <div class="dlg-f">
        <span style="margin-right:auto;font-size:11.5px;color:var(--mut);">
          平台与关键词按「任一平台 × 任一关键词」组合采集
        </span>
        <button class="btn ghost" @click="schDlg.open = false">取消</button>
        <button class="btn pri" :disabled="schDlg.busy" @click="saveSched()">
          {{ schDlg.busy ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>

  <!-- 字段正则 模板编辑（高级） -->
  <div class="mask" :class="{on: regexTplDlg.open}" @click.self="regexTplDlg.open = false">
    <div class="dlg" style="width:min(700px, calc(100vw - 40px));">
      <div class="dlg-h">
        <h3>正则模板 · {{ regexTplDlg.label }}</h3>
        <button class="btnb" title="关闭" @click="regexTplDlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <div class="fld">
          <label>完整正则 <span style="font-weight:400;color:var(--mut);">（{alts} = 该字段下所有匹配词）</span></label>
          <textarea v-model="regexTplDlg.pattern_tpl" rows="3" class="bc-ta"></textarea>
          <div style="font-size:11.5px;color:var(--mut);margin-top:5px;line-height:1.6;">
            一般不需要动这里。{alts} 会被替换成 <code>(?:预算|项目规模|…)</code>；
            真正的取值部分（括号里的捕获组）必须保留，否则该字段取不到值。
          </div>
        </div>
        <div class="fld" style="margin-top:10px;">
          <label>这段正则在找什么</label>
          <input :value="regexTplDlg.value_hint" disabled>
        </div>
      </div>
      <div class="dlg-f">
        <button class="btn ghost" @click="regexTplDlg.open = false">取消</button>
        <button class="btn pri" :disabled="regexTplDlg.busy" @click="saveTpl()">
          {{ regexTplDlg.busy ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>

  <!-- 字段正则 匹配测试 -->
  <div class="mask" :class="{on: regexTestDlg.open}" @click.self="regexTestDlg.open = false">
    <div class="dlg" style="width:min(820px, calc(100vw - 40px));">
      <div class="dlg-h">
        <h3>正则匹配测试</h3>
        <button class="btnb" title="关闭" @click="regexTestDlg.open = false">✕</button>
      </div>
      <div class="dlg-b">
        <textarea v-model="regexTestDlg.text" rows="6" class="bc-ta"
          placeholder="把公告正文粘进来（纯文本即可，不用 HTML），用来看当前配置能匹配到哪些字段"></textarea>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
          <select v-model="regexTestDlg.pid">
            <option value="">全局配置</option>
            <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <button class="btn pri" :disabled="regexTestDlg.busy" @click="runRegexTest()">
            {{ regexTestDlg.busy ? '匹配中…' : '开始匹配' }}
          </button>
          <span class="cnt" v-if="regexTestDlg.result">
            命中 {{ regexTestDlg.result.matched }} / {{ regexTestDlg.result.total }} 个字段
          </span>
        </div>
        <div v-if="regexTestDlg.result" style="margin-top:10px;max-height:300px;overflow:auto;">
          <table class="dt-table">
            <thead>
              <tr><th style="width:150px;">字段</th><th>匹配到的值</th></tr>
            </thead>
            <tbody>
              <tr v-for="x in regexTestDlg.result.fields" :key="x.field_key">
                <td>{{ x.label }}</td>
                <td :style="{color: x.value ? 'var(--txt)' : 'var(--mut)'}">
                  {{ x.value || '（未命中）' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="dlg-f">
        <span style="margin-right:auto;font-size:11.5px;color:var(--mut);">
          只验证正则兜底这一层；正常情况下优先走 AI，这条链路只在模型不可用时生效
        </span>
        <button class="btn ghost" @click="regexTestDlg.open = false">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
tr.dim td{opacity:.55;}
.bc-prog{padding:12px 18px 14px;border-bottom:1px solid var(--line2);background:#FDFCF8;}
.bc-prog-line{display:flex;justify-content:space-between;font-size:12.5px;color:var(--txt);margin-bottom:7px;}
.bc-prog-meta{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-top:8px;font-size:11.5px;color:var(--sub);}
.dt-table td.mono{font-family:ui-monospace,monospace;font-size:11px;}
.dt-table td a:hover{text-decoration:underline !important;}
/* 表头文字与列宽手柄互不遮挡 */
.dt-table th{padding-right:18px;}
/* AI 配方生成：探测证据卡 */
.bc-modes{display:flex;align-items:center;gap:6px;margin-bottom:12px;border-bottom:1px solid var(--line2);padding-bottom:10px;}
.bc-mode{border:1px solid var(--line2);background:var(--card,#fff);color:var(--sub);
  font-size:12.5px;padding:5px 12px;border-radius:4px;cursor:pointer;}
.bc-mode:hover{border-color:var(--pri);color:var(--pri);}
.bc-mode.on{background:var(--pri);border-color:var(--pri);color:#fff;font-weight:600;}
.bc-mode:disabled{opacity:.6;cursor:not-allowed;}
.bc-ta{width:100%;font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;
  background:var(--card,#fafafa);color:var(--txt);border:1px solid var(--line2);
  border-radius:4px;padding:8px;resize:vertical;white-space:pre;}
.bc-ev{display:flex;flex-direction:column;gap:4px;padding:9px 11px;border:1px solid var(--line2);
  border-radius:5px;background:var(--card);font-size:11.5px;color:var(--sub);line-height:1.5;}
.bc-ev b{font-size:11.5px;color:var(--txt);font-weight:600;}
.bc-ev span{word-break:break-all;}
.bc-ev code{font-family:ui-monospace,monospace;background:var(--bg-2,#f5f7fa);
  padding:1px 4px;border-radius:3px;color:var(--txt);}

/* 定时任务：平台/关键词多选（胶囊按钮，避免 checkbox 在窄容器里把文字压成竖排） */
.sch-pick{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  border:1px solid var(--line2,#e5e7eb);border-radius:4px;padding:8px 10px;
  max-height:190px;overflow-y:auto;overflow-x:hidden;width:100%;box-sizing:border-box;
  background:var(--card,#fff);}
.sch-chip{flex:0 0 auto;white-space:nowrap;font-size:12.5px;line-height:1.4;
  padding:4px 10px;border:1px solid var(--line2,#e5e7eb);border-radius:14px;
  background:var(--bg-2,#f7f9fc);color:var(--txt);cursor:pointer;}
.sch-chip:hover{border-color:var(--pri);}
.sch-chip.on{background:var(--pri);border-color:var(--pri);color:#fff;font-weight:600;}
.sch-chip .sch-off{font-style:normal;font-size:10.5px;opacity:.7;margin-left:3px;}
.sch-chip.sch-all{margin-right:2px;}
.sch-search{flex:1 0 100%;order:-1;width:100%;margin:0 0 2px;box-sizing:border-box;}
.sch-empty{font-size:12px;color:var(--mut);}
/* 定时任务列表：任务名 + 采集范围副标题 */
.sch-name{font-size:12.5px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.sch-sub{font-size:11px;color:var(--mut);margin-top:2px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}

/* 字段正则卡片 */
.rx-wrap{display:flex;flex-direction:column;gap:10px;max-height:calc(100vh - 330px);
  overflow:auto;padding-right:4px;}
.rx-card{border:1px solid var(--line2,#e5e7eb);border-radius:6px;padding:10px 12px;
  background:var(--card,#fff);}
.rx-card.dim{opacity:.55;}
.rx-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.rx-head b{font-size:13px;}
.rx-key{font-family:ui-monospace,monospace;font-size:11px;background:var(--bg-2,#f5f7fa);
  padding:1px 5px;border-radius:3px;color:var(--sub);}
.rx-hint{font-size:11.5px;color:var(--mut);}
.rx-head .btnb{margin-left:auto;}
.rx-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}
.rx-tag{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;padding:3px 8px;
  border:1px solid var(--line2,#e5e7eb);border-radius:12px;cursor:pointer;
  background:var(--bg-2,#f7f9fc);color:var(--txt);}
.rx-tag.off{text-decoration:line-through;opacity:.45;}
.rx-sc{font-style:normal;font-size:10.5px;color:var(--mut);}
.rx-x{border:none;background:none;color:var(--mut);cursor:pointer;font-size:13px;
  line-height:1;padding:0 0 0 2px;}
.rx-x:hover{color:var(--warn);}
.rx-empty{font-size:12px;color:var(--mut);}
.rx-add{display:flex;gap:6px;align-items:center;}
.rx-add input{flex:1;min-width:170px;}
.rx-err{font-size:11.5px;color:var(--warn);margin-top:6px;}
.rx-regex{margin-top:8px;font-size:11.5px;color:var(--mut);}
.rx-regex summary{cursor:pointer;}
.rx-regex code{display:block;margin-top:4px;font-family:ui-monospace,monospace;font-size:11px;
  word-break:break-all;background:var(--bg-2,#f5f7fa);padding:6px 8px;border-radius:4px;
  color:var(--txt);}
</style>
