<script setup>
/* 标段附件上传：把文件存到该标段在文件存储中的项目目录
   目录规则：bidfile / 开标年 / 开标月 / 项目名（后端 GET /api/bid-dir 解析并补建） */
import { reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import { upDlg, closeBidUpload, toast } from '../stores/bid'
import { UploadQueue } from '../lib/fsUploader'
import UploadQueuePanel from './fs/UploadQueue.vue'

const info = reactive({ loading:false, nodeId:'', path:'', files:0, name:'', err:'' });
const queue = new UploadQueue();
const picker = ref(null);
const drag = ref(false);

async function load(){
  if(!upDlg.id) return;
  info.loading = true; info.err = '';
  try{
    const d = await api('/api/bid-dir?rid=' + encodeURIComponent(upDlg.id));
    info.nodeId = d.nodeId; info.path = d.path; info.files = d.files; info.name = d.name;
  }catch(e){ info.err = e.message || '项目目录准备失败'; }
  info.loading = false;
}

watch(() => upDlg.open, on => {
  if(!on){ queue.clearAll(); return; }
  queue.clearAll();
  load();
});

function push(files){
  if(!info.nodeId){ toast(info.err || '项目目录尚未就绪'); return; }
  const list = Array.prototype.map.call(files || [], f => ({ file: f, rel: f.name }))
    .filter(x => x.file && x.file.size > 0);
  if(!list.length) return;
  queue.add(list, info.nodeId, { conflict: 'rename' });   // 同名自动加序号，不覆盖
}
function onPick(e){ push(e.target.files); e.target.value = ''; }
function onDrop(e){
  drag.value = false;
  push((e.dataTransfer && e.dataTransfer.files) || []);
}
function openPicker(){ if(picker.value) picker.value.click(); }
async function onDone(){
  await load();
  toast('文件已存入项目目录');
}
</script>

<template>
  <div class="mask" :class="{on: upDlg.open}" @click.self="closeBidUpload">
    <div class="dlg up-dlg">
      <div class="dlg-h">
        <h3>上传项目文件</h3>
        <span class="act" style="color:var(--mut);cursor:pointer;" @click="closeBidUpload">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="up-proj">{{ info.name || '—' }}</div>
        <div class="up-path">
          <span class="k">存储目录</span>
          <code v-if="info.loading">准备目录…</code>
          <code v-else-if="info.path" :title="info.path">{{ info.path }}</code>
          <code v-else class="bad">{{ info.err || '—' }}</code>
          <span v-if="info.nodeId" class="cnt">已有 {{ info.files }} 个文件</span>
        </div>

        <div class="up-drop" :class="{on: drag, off: !info.nodeId}"
          @dragover.prevent="drag = true" @dragleave.prevent="drag = false" @drop.prevent="onDrop">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M4 15v3.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V15"/></svg>
          <div class="t">把文件拖到这里，或<button class="btn ghost up-pick" :disabled="!info.nodeId" @click="openPicker">选择文件</button></div>
          <div class="s">同名文件自动加序号，不覆盖已有文件；单文件上限 2GB</div>
          <input ref="picker" type="file" multiple style="display:none" @change="onPick" />
        </div>

        <UploadQueuePanel :queue="queue" @done="onDone" />
      </div>
      <div class="dlg-f">
        <span class="up-note">目录按「开标年 / 开标月 / 项目名」自动创建</span>
        <button class="btn ghost" @click="closeBidUpload">关闭</button>
      </div>
    </div>
  </div>
</template>
