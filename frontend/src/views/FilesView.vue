<script setup>
/* ================================================================
   文件存储：目录树 + 文件列表 + 分片上传队列 + 原子化权限 + 版本备份 + 操作日志
   ================================================================ */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, apiBlob } from '../api/client'
import { toast } from '../stores/bid'
import { can } from '../stores/perm'
import { FS_OPS, canFs, humanSize, kindOf, tsText, isOfficeExt, isOfficeEditable } from '../lib/fsPerm'
import { filesQueue, fromInput, fromDataTransfer } from '../lib/fsUploader'
import FileTree from '../components/fs/FileTree.vue'
import PermDialog from '../components/fs/PermDialog.vue'
import VersionDrawer from '../components/fs/VersionDrawer.vue'
import RecycleBin from '../components/fs/RecycleBin.vue'
import OpLogPanel from '../components/fs/OpLogPanel.vue'
import SettingsDialog from '../components/fs/SettingsDialog.vue'
import OfficeEditor from '../components/fs/OfficeEditor.vue'

const cur = ref('');
const trail = ref([]);
const items = ref([]);
const perm = ref({});
const name = ref('存储根目录');
const kw = ref('');
const sel = ref([]);
const loading = ref(false);
const dragging = ref(false);

const newName = ref('');
const showNew = ref(false);
const moving = ref(null);

const permOpen = ref(false);
const verOpen = ref(false);
const binOpen = ref(false);
const logOpen = ref(false);
const setOpen = ref(false);
const permTarget = ref({ id: '', name: '', path: '/' });
const verTarget = ref({ id: '', name: '' });
const verCanOverwrite = ref(false);

/* ONLYOFFICE 在线预览 / 编辑：officeOn 由 /office/enabled 探测，未配置时整块入口隐藏 */
const officeOn = ref(false);
const officeOpen = ref(false);
const officeId = ref('');
const officeName = ref('');

const fileInput = ref(null);
const dirInput = ref(null);
const treeRef = ref(null);
/* 共享队列：切到别的页面再回来，任务与失败记录仍在；后台上传不因路由卸载而中断。
   所有入口（上传文件 / 上传整个文件夹 / 拖拽）都进这一条队列，并汇总到右下角全局任务面板。 */
const queue = filesQueue;

/* 上传任务统一交给全局悬浮栏（常驻 / 可拖动 / 可折叠），页面里不再有表格底部的队列。
   这里只留计数与「展开 / 定位」入口；队列是普通对象，用轻量定时器刷计数即可。 */
const taskTick = ref(0);
const taskTimer = setInterval(() => { taskTick.value++; }, 600);
const taskCount = computed(() => { taskTick.value; return queue.items.length; });
const taskRunning = computed(() => { taskTick.value; return queue.stats().busy; });
function toggleDock(){ window.dispatchEvent(new Event('wb-open-upload-dock')); }

/* 页面里已不挂队列面板，上传完成后仍需刷新文件列表（含空文件夹建好的目录） */
const onQueueFinish = () => { refreshAll(); };
queue.onFinish = onQueueFinish;
onUnmounted(() => { if(queue.onFinish === onQueueFinish) queue.onFinish = null; });

const isAdmin = computed(() => can('file.admin'));
const shown = computed(() => {
  const k = kw.value.trim().toLowerCase();
  return k ? items.value.filter(x => String(x.name).toLowerCase().indexOf(k) >= 0) : items.value;
});
const curNode = computed(() => items.value.filter(x => sel.value.length === 1 && x.id === sel.value[0])[0] || null);

async function load(){
  loading.value = true;
  try{
    const r = await api('/api/fs/list?parent_id=' + cur.value + '&limit=500');
    items.value = r.items || [];
    perm.value = r.perm || {};
    name.value = r.name || '存储根目录';
    trail.value = r.trail || [];
    sel.value = [];
  }catch(e){ toast(e.message); }
  loading.value = false;
}

function openDir(id){
  if(moving.value){
    doMove(moving.value, id);
    moving.value = null;
    return;
  }
  cur.value = id;
  load();
}
async function checkOffice(){
  try{
    const r = await api('/api/fs/office/enabled');
    officeOn.value = !!r.enabled;
  }catch(e){ officeOn.value = false; }   // 旧后端没有该路由时静默降级，不影响文件列表
}
onMounted(() => { load(); checkOffice(); });

function openOffice(n){
  if(!officeOn.value || n.kind !== 'file' || !isOfficeExt(n.name)) return;
  officeId.value = n.id;
  officeName.value = n.name;
  officeOpen.value = true;
}

async function refreshAll(){
  await load();
  if(treeRef.value) treeRef.value.refresh();
}

/* ---------- 目录操作 ---------- */
async function mkdir(){
  const nm = (newName.value || '').trim();
  if(!nm) return;
  try{
    await api('/api/fs/mkdir', { method: 'POST', body: JSON.stringify({ parentId: cur.value, name: nm }) });
    newName.value = ''; showNew.value = false;
    await refreshAll();
    toast('已创建：' + nm);
  }catch(e){ toast(e.message); }
}

function startMove(n){ moving.value = n; toast('请点击左侧目标目录完成移动（再次点击「移动」取消）'); }

async function doMove(n, targetId){
  try{
    await api('/api/fs/move', { method: 'POST', body: JSON.stringify({ nodeId: n.id, targetId: targetId }) });
    toast('已移动：' + n.name);
    await refreshAll();
  }catch(e){ toast(e.message); }
}

async function rename(n){
  const nm = window.prompt('新名称', n.name);
  if(!nm || nm === n.name) return;
  try{
    await api('/api/fs/rename', { method: 'POST', body: JSON.stringify({ nodeId: n.id, name: nm }) });
    await load();
  }catch(e){ toast(e.message); }
}

async function remove(n){
  const list = sel.value.length ? sel.value.slice() : (n ? [n.id] : []);
  if(!list.length) return;
  if(!window.confirm('确认删除选中的 ' + list.length + ' 项？\n删除后进回收站，' +
                     '可在保留期内恢复（回收站按钮）。')) return;
  for(const id of list){
    try{ await api('/api/fs/node/' + id, { method: 'DELETE' }); }
    catch(e){ toast(e.message); }
  }
  toast('已删除 ' + list.length + ' 项（可在回收站恢复）');
  await refreshAll();
}

/* ---------- 下载 ---------- */
async function download(n){
  try{
    const blob = await apiBlob('/api/fs/download?node_id=' + n.id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = n.name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }catch(e){ toast(e.message); }
}

/* ---------- 上传 ---------- */
function existingNames(){
  const s = {};
  items.value.forEach(x => { s[String(x.name).toLowerCase()] = 1; });
  return s;
}

function conflictFor(list){
  const has = existingNames();
  return list.some(f => has[String(f.rel.split('/').pop() || f.file.name).toLowerCase()]);
}

const conflictOpen = ref(false);
const conflictCount = ref(0);
let conflictResolve = null;

function askConflict(n){
  conflictCount.value = n;
  conflictOpen.value = true;
  return new Promise(r => { conflictResolve = r; });
}
function answerConflict(v){
  conflictOpen.value = false;
  if(conflictResolve) conflictResolve(v);
  conflictResolve = null;
}

/* 给本批任务打上来源标签，右下角全局任务面板据此区分「上传文件 / 上传文件夹 / 拖拽」 */
function tagQueue(label){
  queue.title = label;
  queue.sub = '目标目录：' + (name.value || '存储根目录');
}

async function push(list, parentId, label){
  if(!list.length) return;
  let conflict = 'reject';
  if(conflictFor(list)){
    conflict = await askConflict(list.length);
    if(!conflict) return;
  }
  tagQueue(label || ('上传文件 · ' + list.length + ' 个'));
  queue.add(list, parentId, { conflict: conflict });
}

function onPickFiles(e){
  const list = fromInput(e.target.files);
  e.target.value = '';
  push(list, cur.value, '上传文件 · ' + list.length + ' 个');
}

async function onPickFolder(e){
  const list = fromInput(e.target.files);
  e.target.value = '';
  const root = (list[0] && list[0].rel.split('/')[0]) || '文件夹';
  await uploadTree(list, '上传文件夹 · ' + root + '（' + list.length + ' 个文件）');
}

async function uploadTree(list, label){
  if(!list.length) return;
  // 先按相对路径一次性建好目录树，再把整批文件一次性加入队列（空目录也会保留）
  const dirs = [];
  list.forEach(f => {
    const d = f.rel.indexOf('/') > 0 ? f.rel.split('/').slice(0, -1).join('/') : '';
    if(d && dirs.indexOf(d) < 0) dirs.push(d);
  });
  let map = {};
  if(dirs.length){
    try{
      const r = await api('/api/fs/mkdir/batch', { method: 'POST',
        body: JSON.stringify({ parentId: cur.value, paths: dirs }) });
      map = r.map || {};
    }catch(e){ toast(e.message); return; }
  }
  /* 同名确认整批只弹一次：以前按子目录逐组 await push，一个多层文件夹会连弹多次，
     且用户中途取消会让后面的分组整组不进队列。 */
  let conflict = 'reject';
  if(conflictFor(list)){
    conflict = await askConflict(list.length);
    if(!conflict) return;
  }
  const groups = {};
  list.forEach(f => {
    const d = f.rel.indexOf('/') > 0 ? f.rel.split('/').slice(0, -1).join('/') : '';
    const pid = d ? (map[d] || cur.value) : cur.value;
    (groups[pid] = groups[pid] || []).push(f);
  });
  tagQueue(label || ('上传文件夹 · ' + list.length + ' 个文件'));
  Object.keys(groups).forEach(pid => queue.add(groups[pid], pid, { conflict: conflict }));
  if(dirs.length) await refreshAll();
}

async function onDrop(e){
  dragging.value = false;
  const dt = e.dataTransfer;
  if(!dt) return;
  if(dt.items && dt.items.length && dt.items[0].webkitGetAsEntry){
    const r = await fromDataTransfer(dt);
    if(r.files.length) await uploadTree(r.files, '拖拽上传 · ' + r.files.length + ' 个文件');
    else if(r.dirs.length){
      try{
        await api('/api/fs/mkdir/batch', { method: 'POST',
          body: JSON.stringify({ parentId: cur.value, paths: r.dirs }) });
        await refreshAll();
        toast('已创建 ' + r.dirs.length + ' 个目录（空文件夹）');
      }catch(err){ toast(err.message); }
    }
  }else{
    const list = fromInput(dt.files);
    await push(list, cur.value, '拖拽上传 · ' + list.length + ' 个文件');
  }
}

/* ---------- 弹窗 ---------- */
function openPerm(n){
  const t = n || { id: cur.value, name: name.value };
  permTarget.value = { id: t.id, name: t.name, path: trail.value.map(x => x.name).join('/') || '/' };
  permOpen.value = true;
}
function openVersions(n){
  verTarget.value = { id: n.id, name: n.name };
  verCanOverwrite.value = canFs(n && n.perm, 'overwrite');
  verOpen.value = true;
}

function toggleSel(id){
  const i = sel.value.indexOf(id);
  if(i >= 0) sel.value.splice(i, 1);
  else sel.value.push(id);
}
function allSel(){
  sel.value = shown.value.length && sel.value.length === shown.value.length ? [] : shown.value.map(x => x.id);
}

function onKey(e){
  if(e.key === 'Escape'){
    if(conflictOpen.value) answerConflict('');
    else if(moving.value) moving.value = null;
  }
}
onMounted(() => document.addEventListener('keydown', onKey));
onUnmounted(() => document.removeEventListener('keydown', onKey));
onUnmounted(() => clearInterval(taskTimer));
</script>

<template>
  <div class="card fs-card">
    <div class="card-h" style="padding-bottom:0">
      <h2>文件存储</h2>
      <span class="hint">目录 / 文件的上传、查看、删除、修改、覆盖五个动作分别授权；
        覆盖、改名、删除前自动保留旧版本（默认 7 天）</span>
    </div>

    <div class="toolbar tbar fs-tools">
      <template v-if="!showNew">
        <button class="btn pri" :disabled="!canFs(perm,'upload')" @click="showNew = true">＋ 新建目录</button>
        <button class="btn" :disabled="!canFs(perm,'upload')" @click="fileInput.click()">上传文件</button>
        <button class="btn" :disabled="!canFs(perm,'upload')" @click="dirInput.click()">上传整个文件夹</button>
      </template>
      <template v-else>
        <input placeholder="目录名称" style="width:200px" v-model="newName" @keyup.enter="mkdir()">
        <button class="btn pri" @click="mkdir()">创建</button>
        <button class="btn ghost" @click="showNew = false; newName = ''">取消</button>
      </template>
      <span class="tb-sep"></span>
      <button class="btn ghost" :disabled="!curNode" @click="download(curNode)"
              v-if="curNode && curNode.kind === 'file'">下载</button>
      <button class="btn ghost" :disabled="!curNode || !canFs(curNode.perm,'modify')" @click="rename(curNode)">重命名</button>
      <button class="btn ghost" :disabled="!curNode || !canFs(curNode.perm,'modify')" @click="startMove(curNode)">
        {{ moving ? '选择目标目录…' : '移动' }}
      </button>
      <button class="btn ghost" :disabled="!curNode || curNode.kind !== 'file'" @click="openVersions(curNode)">版本</button>
      <button class="btn ghost" @click="openPerm(curNode)">权限</button>
      <button class="btn ghost danger" :disabled="!sel.length" @click="remove(curNode)">删除</button>
      <span class="tb-sep"></span>
      <button class="btn ghost" v-if="isAdmin" @click="binOpen = true">回收站</button>
      <button class="btn ghost" v-if="isAdmin" @click="logOpen = true">操作日志</button>
      <button class="btn ghost" v-if="isAdmin" @click="setOpen = true">设置</button>
      <button class="btn ghost" @click="refreshAll()">刷新</button>
      <button class="btn ghost" v-if="taskCount" title="展开 / 定位上传任务栏" @click="toggleDock">
        上传任务 {{ taskCount }}<template v-if="taskRunning"> · 进行中</template>
      </button>

      <span class="fs-myperm">
        我在本目录：
        <em v-for="o in FS_OPS" :key="o.k" :class="{ on: canFs(perm, o.k) }">{{ o.l }}</em>
      </span>
    </div>

    <input type="file" multiple hidden ref="fileInput" @change="onPickFiles">
    <input type="file" multiple webkitdirectory hidden ref="dirInput" @change="onPickFolder">

    <div class="fs-body">
      <aside class="fs-side">
        <FileTree ref="treeRef" :current="cur" @open="openDir" />
      </aside>

      <section class="fs-main" :class="{ drag: dragging }"
               @dragover.prevent="dragging = true"
               @dragleave.prevent="dragging = false"
               @drop.prevent="onDrop">
        <div class="fs-crumb">
          <span v-for="(t, i) in trail" :key="t.id" @click="openDir(t.id)">
            <i v-if="i">/</i>{{ t.name }}
          </span>
          <input type="search" placeholder="搜索当前目录" v-model="kw">
          <span class="cnt">{{ shown.length }} 项</span>
        </div>

        <div class="tbl-wrap fs-tbl-wrap">
          <table class="users-tbl fs-tbl">
            <thead>
              <tr>
                <th style="width:34px"><input type="checkbox" class="th-cb" :checked="shown.length && sel.length === shown.length" @change="allSel()"></th>
                <th>名称</th>
                <th style="width:60px">类型</th>
                <th style="width:80px">大小</th>
                <th style="width:52px">版本</th>
                <th style="width:92px">修改人</th>
                <th style="width:145px">修改时间</th>
                <th style="width:320px">可操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="n in shown" :key="n.id" :class="{ sel: sel.indexOf(n.id) >= 0 }" @click="toggleSel(n.id)">
                <td><input type="checkbox" class="row-cb" :checked="sel.indexOf(n.id) >= 0" @click.stop="toggleSel(n.id)"></td>
                <td class="fs-nm">
                  <span class="fs-ico" :class="{ dir: n.kind === 'dir' }">{{ n.kind === 'dir' ? '▤' : kindOf(n.name) }}</span>
                  <span class="txt" v-if="n.kind === 'dir'" :title="n.name" @click.stop="openDir(n.id)">{{ n.name }}</span>
                  <span class="txt lnk" v-else-if="officeOn && isOfficeExt(n.name)"
                        :title="n.name + '（点击在线预览 / 编辑）'" @click.stop="openOffice(n)">{{ n.name }}</span>
                  <span class="txt" v-else :title="n.name">{{ n.name }}</span>
                </td>
                <td>{{ n.kind === 'dir' ? '目录' : (n.ext || '文件') }}</td>
                <td>{{ n.kind === 'dir' ? '—' : humanSize(n.size) }}</td>
                <td>{{ n.kind === 'dir' ? '—' : 'v' + n.ver }}</td>
                <td>{{ n.updatedBy || n.owner }}</td>
                <td>{{ tsText(n.updatedAt) }}</td>
                <td class="fs-ops">
                  <em v-for="o in FS_OPS" :key="o.k" :class="{ on: canFs(n.perm, o.k) }">{{ o.l }}</em>
                  <span class="sp"></span>
                  <button class="btnb btnb-txt" v-if="officeOn && n.kind === 'file' && isOfficeExt(n.name)"
                          @click.stop="openOffice(n)">
                    {{ isOfficeEditable(n.name) && canFs(n.perm, 'overwrite') ? '编辑' : '预览' }}
                  </button>
                  <button class="btnb btnb-txt" v-if="n.kind === 'file'" @click.stop="download(n)">下载</button>
                  <button class="btnb btnb-txt" v-if="n.kind === 'file'" @click.stop="openVersions(n)">版本</button>
                  <button class="btnb btnb-txt" @click.stop="openPerm(n)">权限</button>
                  <button class="btnb btnb-txt act-del" @click.stop="remove(n)">删除</button>
                </td>
              </tr>
              <tr v-if="!shown.length">
                <td colspan="8" style="color:var(--sub)">
                  {{ loading ? '加载中…' : '空目录 — 可直接把文件或整个文件夹拖到这里上传' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="fs-drop" v-if="dragging">松开即可上传（支持整个文件夹）</div>
      </section>
    </div>

    <PermDialog :open="permOpen" :node-id="permTarget.id" :node-name="permTarget.name"
                :path="permTarget.path" @close="permOpen = false" />
    <VersionDrawer :open="verOpen" :node-id="verTarget.id" :node-name="verTarget.name"
                   :can-overwrite="verCanOverwrite"
                   @close="verOpen = false" @changed="load()" />
    <RecycleBin :open="binOpen" @close="binOpen = false" @changed="refreshAll()" />
    <OpLogPanel :open="logOpen" @close="logOpen = false" />
    <SettingsDialog :open="setOpen" @close="setOpen = false" />
    <OfficeEditor :open="officeOpen" :node-id="officeId" :node-name="officeName"
                  @close="officeOpen = false" @changed="load()" />

    <div class="mask" :class="{ on: conflictOpen }" style="z-index:120">
      <div class="dlg" style="width:460px">
        <div class="dlg-h"><h3>存在同名文件</h3><span class="act" @click="answerConflict('')">关闭 ✕</span></div>
        <div class="dlg-b">
          当前目录已存在 {{ conflictCount }} 个同名文件，如何处理？
          <div class="fs-perm-tip">「覆盖」会保留旧版本（可在「版本」里回滚）；「自动重命名」保留两份。</div>
        </div>
        <div class="dlg-f">
          <button class="btn ghost" @click="answerConflict('')">取消</button>
          <button class="btn" @click="answerConflict('rename')">自动重命名</button>
          <button class="btn pri" @click="answerConflict('overwrite')">覆盖</button>
        </div>
      </div>
    </div>
  </div>
</template>
