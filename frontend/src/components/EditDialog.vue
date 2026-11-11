<script setup>
import { reactive, ref, computed, watch, onUnmounted } from 'vue'
import { ST, dlg, closeDlg, upsertRow, removeRow, toast, getDraft, setDraft, clearDraft, staff, openBidUpload, upDlg, filesState } from '../stores/bid'
import { S0, FORM_ITEMS, bidStatusLocked, stampBidStatus } from '../lib/fields'
import { can, canSee, canWrite, fieldMode } from '../stores/perm'
import { auth } from '../stores/auth'
import { api } from '../api/client'

const canEdit = computed(() => can('bid.edit'));
const canDel = computed(() => can('bid.delete'));
const isAdmin = computed(() => !!(auth.user && auth.user.isAdmin));
/* 标书状态的时间记录点由系统维护：表单里默认只读，管理员可修正 */
function lockedField(f){ return f.ro && !isAdmin.value; }
/* 非管理员只能向前推进（未开始 → 进行中 → 已结束），回退仅管理员 */
function statusLocked(o){ return bidStatusLocked(o, draft.bidStatus, isAdmin.value); }
/* 只读 / 脱敏 字段都会禁用编辑，标签提示对应档位 */
function lockTag(k){ const m = fieldMode(k); return m === 'masked' ? '脱敏' : (m === 'readonly' ? '只读' : ''); }

/* 按字段权限过滤表单：不可见字段不渲染，空分组不显示 */
const formItems = computed(() => {
  const out = [];
  let grp = null;
  FORM_ITEMS.forEach(it => {
    if(it.type === 'grp'){ grp = it; return; }
    if(!canSee(it.f.k)) return;
    if(grp){ out.push(grp); grp = null; }
    out.push(it);
  });
  return out;
});

const STAFF_KEYS = ['lead', 'copilot', 'reviewer'];   // 允许从员工名单选择的字段
/* datalist 候选：员工名单 ∪ 台账三个人员字段的历史人名 */
const staffOpts = computed(() => {
  const set = new Set(staff.list.map(p => p.displayName));
  ST.rows.forEach(r => STAFF_KEYS.forEach(k => { const v = String(r[k] || '').trim(); if(v) set.add(v); }));
  return [...set].sort((a,b) => a.localeCompare(b,'zh'));
});

const draft = reactive({});
const confirmLeave = ref(false);      // 「内容未保存」提示
let baseSnap = '';                    // 打开时的内容快照，用于脏检查
let statusPrev = '';                  // 上一次的标书状态（用于判断时间点是否需要写入）

const dirty = computed(() => JSON.stringify(draft) !== baseSnap);

/* ---------------- 标书文件（项目目录 / 标书） ----------------
   目录由后端 GET /api/bid-dir 按「bidfile/开标年/开标月/项目名/标书」解析并补建；
   状态改为「已结束」前必须有标书文件，保存时前置拦截 + 后端兜底。 */
const canUpFiles = computed(() => can('view.files'));
const fileInfo = reactive({ loading:false, path:'', files:0, err:'' });
const needFile = ref(false);          // 「标书文件尚未上传」提示层

async function refreshBidFiles(){
  if(!dlg.id){ fileInfo.path = ''; fileInfo.files = 0; fileInfo.err = ''; return 0; }
  fileInfo.loading = true;
  try{
    const d = await api('/api/bid-dir?rid=' + encodeURIComponent(dlg.id));
    fileInfo.path = d.bidPath || '';
    fileInfo.files = d.bidFiles || 0;
    fileInfo.err = '';
  }catch(e){
    fileInfo.err = e.message || '标书目录读取失败';
    fileInfo.loading = false;
    return -1;      // 读不到目录（无文件权限等）：不拦截保存，由后端给出明确提示
  }
  fileInfo.loading = false;
  return fileInfo.files;
}
function goUploadBid(){
  if(!dlg.id) return;
  needFile.value = false;
  openBidUpload(dlg.id, 'bid');
}
/* 上传窗口关闭后刷新标书文件数（可能刚传完） */
watch(() => upDlg.open, on => { if(!on && dlg.open) refreshBidFiles(); });
/* 上传在后台跑完时（窗口已关）也能把这里的文件数刷新过来 */
watch(() => filesState.rev, () => { if(dlg.open) refreshBidFiles(); });

watch(() => dlg.open, v => {
  if(v){
    const r = dlg.id ? ST.rows.find(x => x.id === dlg.id) : null;
    Object.keys(draft).forEach(k => delete draft[k]);
    Object.assign(draft, S0({}), {method:'公开招标', bondType:'银行转账', bondStatus:'未缴', result:'待开标'}, r || {});
    const st = getDraft(dlg.id);      // 恢复上次未保存的草稿
    if(st) Object.assign(draft, st);
    statusPrev = draft.bidStatus || '';
    baseSnap = JSON.stringify(draft);
    confirmLeave.value = false;
    needFile.value = false;
    document.addEventListener('keydown', onKey);
    refreshBidFiles();
  }else{
    needFile.value = false;
    document.removeEventListener('keydown', onKey);
  }
});
onUnmounted(() => document.removeEventListener('keydown', onKey));

function onKey(e){
  if(e.key !== 'Escape') return;
  if(confirmLeave.value){ confirmLeave.value = false; return; }
  if(needFile.value){ needFile.value = false; return; }
  if(upDlg.open) return;              // 上传窗在最上层，Esc 由它自己处理
  attemptClose();
}
/* 标书状态变更：立刻补写对应状态时间点（保存时 bid.js / 后端会再兜底一次） */
function onFieldChange(k){
  if(k !== 'bidStatus') return;
  stampBidStatus(draft, statusPrev);
  statusPrev = draft.bidStatus || '';
}
/* 所有关闭入口（点外围 / 关闭✕ / 取消 / Esc）：有未保存内容先询问 */
function attemptClose(){
  if(dirty.value){ confirmLeave.value = true; return; }
  closeDlg();
}
async function saveDlg(){
  confirmLeave.value = false;
  const o = {};
  Object.keys(draft).forEach(k => { o[k] = typeof draft[k] === 'string' ? draft[k].trim() : draft[k]; });
  if(!o.projectName){ toast('请填写项目名称'); return; }
  if(!o.openTime){ toast('请填写开标时间'); return; }
  if(!o.lead){ toast('请填写标书负责人'); return; }
  if(['中标','未中标'].indexOf(o.result) >= 0 && !o.winner){
    if(!confirm('未填写中标单位，确认保存？')) return;
  }
  /* 标书状态改为「已结束」：项目「标书」目录必须先有文件（新增标段补录历史数据不拦；
     无文件查看权限的账号不做前端拦截，交给后端返回明确提示） */
  const wasEnd = String((dlg.base && dlg.base.bidStatus) || '').trim() === '已结束';
  if(dlg.id && o.bidStatus === '已结束' && !wasEnd && canUpFiles.value){
    const n = await refreshBidFiles();
    if(n === 0){ needFile.value = true; return; }
  }
  clearDraft(dlg.id);                 // 已正式保存，草稿不再需要
  upsertRow(dlg.id, o);
}
function keepDraft(){
  setDraft(dlg.id, draft);
  confirmLeave.value = false;
  closeDlg();
  toast(dlg.id ? '已存草稿，下次编辑该标段时自动恢复' : '已存草稿，下次新增标段时自动恢复');
}
const updatedInfo = computed(() => {
  const r = dlg.id ? ST.rows.find(x => x.id === dlg.id) : null;
  if(!r || !r._updatedBy) return '';
  const t = r._updatedAt ? new Date(r._updatedAt * 1000).toLocaleString('zh-CN', {hour12:false}) : '';
  return '最近由 ' + r._updatedBy + ' 更新于 ' + t;
});
</script>

<template>
  <div class="mask" :class="{on: dlg.open}" @click.self="attemptClose()">
    <div class="dlg">
      <div class="dlg-h">
        <h3>{{ dlg.id ? '编辑标段信息' : '新增标段' }}</h3>
        <span class="act" style="color:var(--mut)" @click="attemptClose()">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="form">
          <template v-for="it in formItems" :key="it.key">
            <div v-if="it.type === 'grp'" class="grp-title">{{ it.label }}</div>
            <div v-else class="fld" :class="{span2: it.f.span === 2, span3: it.f.span === 3}">
              <label>{{ it.f.l }}<b v-if="it.f.req"> *</b>
                <em v-if="lockTag(it.f.k)" class="ro-tag" :class="{masked: fieldMode(it.f.k) === 'masked'}">{{ lockTag(it.f.k) }}</em>
              </label>
              <select v-if="it.f.t === 'select'" v-model="draft[it.f.k]"
                :disabled="!canWrite(it.f.k) || lockedField(it.f)" @change="onFieldChange(it.f.k)">
                <option value="">请选择</option>
                <option v-for="o in it.f.opt" :key="o" :value="o"
                  :disabled="it.f.k === 'bidStatus' && statusLocked(o)">{{ o }}</option>
              </select>
              <input v-else-if="it.f.t === 'date'" type="date" v-model="draft[it.f.k]" :disabled="!canWrite(it.f.k) || lockedField(it.f)">
              <input v-else-if="it.f.t === 'num'" type="number" step="0.01" placeholder="0.00" v-model="draft[it.f.k]" :disabled="!canWrite(it.f.k) || lockedField(it.f)">
              <textarea v-else-if="it.f.t2 === 'area'" placeholder="补充说明" v-model="draft[it.f.k]" :disabled="!canWrite(it.f.k) || lockedField(it.f)"></textarea>
              <input v-else type="text" :placeholder="it.f.l" v-model="draft[it.f.k]" :disabled="!canWrite(it.f.k) || lockedField(it.f)" :list="STAFF_KEYS.indexOf(it.f.k) >= 0 ? 'staffList' : undefined">
              <div v-if="it.f.k === 'bidStatus'" class="fld-tip">
                {{ isAdmin ? '管理员可任意调整；状态时间点自动记录，也可手动修正。' : '只能向前推进（未开始 → 进行中 → 已结束），回退状态需管理员操作。' }}
              </div>
            </div>
          </template>
        </div>

        <!-- 标书文件：项目目录 / 标书（状态改「已结束」前必须有文件） -->
        <div v-if="dlg.id" class="bid-files">
          <div class="bf-h">
            <b>标书文件</b>
            <span class="bf-sub">目录 / 标书</span>
            <span class="bf-cnt">{{ fileInfo.loading ? '读取中…' : fileInfo.files + ' 个文件' }}</span>
          </div>
          <div class="bf-path" :class="{bad: !!fileInfo.err}" :title="fileInfo.path || fileInfo.err">
            {{ fileInfo.err || fileInfo.path || '目录准备中…' }}
          </div>
          <div class="bf-act">
            <button v-if="canUpFiles" class="btn ghost" @click="goUploadBid()">上传标书文件</button>
            <span class="bf-tip" :class="{warn: !fileInfo.loading && fileInfo.files <= 0}">
              {{ (!fileInfo.loading && fileInfo.files <= 0)
                ? '状态改为「已结束」前，需先上传标书文件'
                : '标书状态改为「已结束」前须已有标书文件，当前已满足' }}
            </span>
          </div>
        </div>
        <div v-else class="bid-files empty">标书文件：保存标段后，重新打开本窗口即可上传。</div>
      </div>
      <div class="dlg-f">
        <button class="btn danger" style="margin-right:auto" v-if="dlg.id && canDel" @click="removeRow(dlg.id)">删除此条</button>
        <span v-if="updatedInfo" style="font-size:11px;color:var(--sub);align-self:center;margin:0 6px">{{ updatedInfo }}</span>
        <button class="btn" @click="attemptClose()">取消</button>
        <button v-if="canEdit" class="btn pri" @click="saveDlg()">保存</button>
      </div>
    </div>
  </div>
  <div class="mask" :class="{on: confirmLeave}" style="z-index:120;" @click.self="confirmLeave = false">
    <div class="dlg" style="width:460px">
      <div class="dlg-h"><h3>内容未保存</h3><span class="act" style="color:var(--mut)" @click="confirmLeave = false">关闭 ✕</span></div>
      <div class="dlg-b">
        <div style="font-size:12.5px;color:var(--txt);line-height:1.7">当前{{ dlg.id ? '标段的修改' : '新增的内容' }}尚未保存，直接离开将丢失。</div>
        <div style="font-size:11.5px;color:var(--sub);margin-top:6px">「存草稿」会把内容暂存在本机浏览器（不进台账、不同步给同事），下次打开时自动恢复。</div>
      </div>
      <div class="dlg-f">
        <button class="btn" @click="confirmLeave = false">取消，继续编辑</button>
        <button class="btn" @click="keepDraft()">存草稿</button>
        <button class="btn pri" @click="saveDlg()">保存</button>
      </div>
    </div>
  </div>
  <!-- 状态改为「已结束」但标书目录为空：阻止保存，给上传入口 -->
  <div class="mask" :class="{on: needFile}" style="z-index:125;" @click.self="needFile = false">
    <div class="dlg" style="width:480px">
      <div class="dlg-h"><h3>标书文件尚未上传</h3><span class="act" style="color:var(--mut)" @click="needFile = false">关闭 ✕</span></div>
      <div class="dlg-b">
        <div style="font-size:12.5px;color:var(--txt);line-height:1.7">标书状态要改为「已结束」，需要先把标书文件上传到项目的「标书」目录里。</div>
        <div class="bf-path" :title="fileInfo.path">{{ fileInfo.path || '（目录准备中）' }}</div>
        <div style="font-size:11.5px;color:var(--sub);margin-top:8px">上传完成后回到本窗口再点「保存」即可。</div>
      </div>
      <div class="dlg-f">
        <button class="btn" @click="needFile = false">稍后上传</button>
        <button v-if="canUpFiles" class="btn pri" @click="goUploadBid()">去上传标书文件</button>
      </div>
    </div>
  </div>
  <!-- 员工名单候选（datalist：可选择也可自由输入，兼容历史数据中的外部人员） -->
  <datalist id="staffList">
    <option v-for="p in staffOpts" :key="p" :value="p"></option>
  </datalist>
</template>
