<script setup>
/* 标段附件上传：把文件存到该标段文件存储里项目目录下的子目录
   kind = tender（招标文件，默认）/ bid（标书文件）
   目录规则：bidfile / 开标年 / 开标月 / 项目名 / 招标文件｜标书（后端 GET /api/bid-dir 解析并补建） */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import { upDlg, closeBidUpload, loadBidFiles, toast } from '../stores/bid'
import { UploadQueue } from '../lib/fsUploader'
import UploadQueuePanel from './fs/UploadQueue.vue'

const META = {
  tender: {
    title: '上传招标文件',
    sub: '项目目录 / 招标文件',
    empty: '招标文件目录尚未就绪',
    done: '招标文件已存入项目目录'
  },
  bid: {
    title: '上传标书文件',
    sub: '项目目录 / 标书',
    empty: '标书目录尚未就绪',
    done: '标书文件已存入项目目录'
  }
};

const info = reactive({ loading:false, nodeId:'', path:'', files:0, name:'', err:'' });
const queue = new UploadQueue('上传文件');
const picker = ref(null);
const drag = ref(false);
const meta = computed(() => META[upDlg.kind] || META.tender);
let batchFrom = 0;                 // 本次打开窗口后新增的文件在队列里的起点（用于统计本批结果）

async function load(){
  if(!upDlg.id) return;
  info.loading = true; info.err = '';
  const isBid = upDlg.kind === 'bid';
  try{
    const d = await api('/api/bid-dir?rid=' + encodeURIComponent(upDlg.id));
    info.nodeId = isBid ? d.bidNodeId : d.tenderNodeId;
    info.path   = isBid ? d.bidPath   : d.tenderPath;
    info.files  = isBid ? d.bidFiles  : d.tenderFiles;
    info.name   = d.name;
    queue.sub   = d.name || '';        // 悬浮条上的项目名
  }catch(e){ info.err = e.message || '项目目录准备失败'; }
  info.loading = false;
}

watch(() => upDlg.open, on => {
  if(!on){
    /* 关窗口不再清空队列：上传转入后台继续，右下角悬浮条实时显示进度与失败 */
    if(queue.stats().busy) toast('上传在后台继续，进度见右下角「正在上传」');
    loadBidFiles(true);
    return;
  }
  if(!queue.stats().busy) queue.clearDone();    // 只清上一批已完成/已取消的，失败项留着可重试
  batchFrom = queue.items.length;
  queue.title = upDlg.kind === 'bid' ? '标书文件上传' : '招标文件上传';
  info.nodeId = ''; info.path = ''; info.files = 0;
  load();
});

function push(files){
  if(!info.nodeId){ toast(info.err || meta.value.empty); return; }
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

/* 一批传完：分清「全部成功 / 部分失败 / 已取消」，不再一律报成功 */
async function onDone(){
  const cur = queue.items.slice(batchFrom);
  const ok = cur.filter(it => it.status === 'done').length;
  const fail = cur.filter(it => it.status === 'fail');
  const cancel = cur.filter(it => it.status === 'cancel').length;
  await load();
  loadBidFiles(true);          // 文件数一变，「需要处理」的「标书未上传」立即跟上
  if(fail.length){
    toast(`已上传 ${ok} 个文件，${fail.length} 个失败（${fail[0].error || '原因见上传队列'}），可点重试`, 5000);
  }else if(ok){
    toast(meta.value.done);
  }else if(cancel){
    toast('上传已取消');
  }
}

/* Esc 关闭：capture 阶段拦截，避免同层的编辑弹窗把它当成「关闭编辑」 */
function onKey(e){
  if(e.key === 'Escape' && upDlg.open){
    e.stopPropagation();
    closeBidUpload();
  }
}
onMounted(() => document.addEventListener('keydown', onKey, true));
onUnmounted(() => document.removeEventListener('keydown', onKey, true));
</script>

<template>
  <div class="mask" :class="{on: upDlg.open}" style="z-index:130;" @click.self="closeBidUpload">
    <div class="dlg up-dlg">
      <div class="dlg-h">
        <h3>{{ meta.title }}</h3>
        <span class="act" style="color:var(--mut);cursor:pointer;" @click="closeBidUpload">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="up-proj">{{ info.name || '—' }}</div>
        <div class="up-path">
          <span class="k">{{ meta.sub }}</span>
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
        <span class="up-note">目录按「开标年 / 开标月 / 项目名 / {{ upDlg.kind === 'bid' ? '标书' : '招标文件' }}」自动创建；关闭窗口不会中断上传，进度见右下角</span>
        <button class="btn ghost" @click="closeBidUpload">关闭</button>
      </div>
    </div>
  </div>
</template>
