<script setup>
/* ONLYOFFICE 在线编辑/预览：全屏覆盖层。
   - config 由后端签名生成（含 JWT），前端只负责加载 DS 的 api.js 并实例化编辑器；
   - 保存完全由 DS → 后端回调完成（旧版本自动备份、生成新版本），前端不参与；
   - 关闭时 emit changed 让父组件刷新列表（版本号/大小可能已变化）。 */
import { ref, watch, nextTick, onUnmounted } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'

const props = defineProps({
  open: { type: Boolean, default: false },
  nodeId: { type: String, default: '' },
  nodeName: { type: String, default: '' }
});
const emit = defineEmits(['close', 'changed']);

const loading = ref(false);
const error = ref('');
const mode = ref('');
const saved = ref(true);
const diagBusy = ref(false);
const diagRows = ref([]);
const diagTips = ref([]);
let editor = null;
let scriptPromise = null;

/* DS 的 onError 事件给的是 {errorCode, errorDescription}，没有 error 字段 ——
   以前只读 e.data.error，导致任何失败都显示「未知」。这里按官方错误码表翻译。 */
const ERR_TEXT = {
  '-1': '未知错误（DS 内部异常，看 docker logs taizhang-onlyoffice）',
  '-2': '打开文档超时（文件过大或回源太慢）',
  '-3': '文档转换失败（文件可能已损坏，或后缀与实际格式不符）',
  '-4': '与文档服务连接已断开',
  '-5': '保存失败',
  '-6': '文档已加密，无法编辑',
  '-7': '配置签名校验失败：后端 JWT 密钥与 DS 容器的 JWT_SECRET 不一致',
  '-8': '无法连接文档服务（DS 未就绪或地址不可达）',
  '-21': '未知错误（多为回源地址不可达）',
  '-22': '无法下载文件：DS 拿不到文件地址（OFFICE_BASE_URL 指向了容器自己）',
  '-23': '无法下载附件',
  '-24': '**无法下载文件** —— DS 回源访问不到后端，最常见原因：OFFICE_BASE_URL 写错（用了 127.0.0.1/localhost，或漏了端口）',
  '-25': '文件过大，超出服务限制',
  '-26': '文档内容已加密',
  '-27': '另存为失败',
  '-28': '另存为文件过大',
  '-29': '不支持的文档格式',
  '-30': '复制/移动文档失败'
};

function errText(d){
  const d0 = (d && typeof d === 'object') ? d : {};
  const code = d0.errorCode !== undefined ? String(d0.errorCode) : '';
  const desc = d0.errorDescription || '';
  let msg = ERR_TEXT[code] || (code ? '错误码 ' + code : '未知');
  if(desc && desc !== msg) msg += ' — ' + desc;
  return { code, msg };
}

/** 动态加载 Document Server 的 api.js（同一地址只加载一次） */
function loadApi(server){
  if(window.DocsAPI && window.DocsAPI.DocEditor) return Promise.resolve();
  if(scriptPromise) return scriptPromise;
  scriptPromise = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = server + '/web-apps/apps/api/documents/api.js';
    s.onload = () => res();
    s.onerror = () => { scriptPromise = null; s.remove(); rej(new Error('无法加载编辑器脚本，请确认服务地址可达：' + server)); };
    document.head.appendChild(s);
  });
  return scriptPromise;
}

function destroy(){
  if(editor){ try{ editor.destroyEditor(); }catch(e){} editor = null; }
}

watch(() => props.open, async v => {
  if(!v || !props.nodeId) return;
  destroy();
  loading.value = true;
  error.value = '';
  mode.value = '';
  diagRows.value = [];
  diagTips.value = [];
  try{
    const r = await api('/api/fs/office/config?node_id=' + encodeURIComponent(props.nodeId));
    mode.value = r.mode;
    await nextTick();                       // 等占位 div 渲染出来
    await loadApi(r.server);
    await nextTick();
    const cfg = r.config;
    cfg.events = {
      onError(e){
        const { code, msg } = errText(e && e.data);
        error.value = '编辑器异常（' + (code || '?') + '）：' + msg;
        console.error('[onlyoffice] onError', code, e && e.data);
      },
      onWarning(e){ console.warn('[onlyoffice] onWarning', e && e.data); },
      onDocumentStateChange(e){ saved.value = e.data === false; }
    };
    editor = new window.DocsAPI.DocEditor('fs-office-ph', cfg);
  }catch(e){ error.value = e.message || '打开失败'; }
  loading.value = false;
});

function close(){
  destroy();
  emit('changed');                          // 版本可能已保存，让列表刷新
  emit('close');
}

/** 出错时一键自检：把「DS 可达 / 回源地址 / 文件可取 / JWT 密钥」四项摊开 */
async function runDiag(){
  diagBusy.value = true;
  diagRows.value = [];
  diagTips.value = [];
  try{
    const d = await api('/api/fs/office/diag?node_id=' + encodeURIComponent(props.nodeId));
    diagRows.value = [
      ['DS 地址',   d.server || '（未配置）', d.server ? '' : 'bad'],
      ['DS 可达',   d.serverDetail || '—', d.serverOk ? 'ok' : 'bad'],
      ['DS 版本',   d.dsVersion || '—', ''],
      ['回源地址',  d.baseUrl || '—', ''],
      ['回源取文件', d.reverseDetail || '—', d.reverseOk ? 'ok' : 'bad'],
      ['JWT 密钥',  d.secretSet ? ('已设置（' + d.secretLen + ' 位）') : '未设置', d.secretSet ? 'ok' : 'bad']
    ];
    diagTips.value = d.hints || [];
  }catch(e){ toast(e.message || '诊断失败'); }
  diagBusy.value = false;
}

onUnmounted(destroy);
</script>

<template>
  <div class="fs-office" v-if="open">
    <div class="fs-office-bar">
      <b>{{ nodeName }}</b>
      <span class="fs-office-mode" v-if="mode">
        {{ mode === 'edit' ? '编辑模式 — 保存后生成新版本，旧版本自动备份（默认 7 天）' : '只读预览' }}
      </span>
      <span class="fs-office-mode" v-else></span>
      <button class="btn ghost" @click="close">关闭</button>
    </div>
    <div class="fs-office-body">
      <div class="fs-office-msg" v-if="loading">正在打开文档…</div>
      <div class="fs-office-msg err" v-else-if="error">
        <p class="fs-office-err">{{ error }}</p>
        <button class="btn" :disabled="diagBusy" @click="runDiag">
          {{ diagBusy ? '诊断中…' : '一键诊断' }}
        </button>
        <table class="fs-diag" v-if="diagRows.length">
          <tr v-for="r in diagRows" :key="r[0]">
            <th>{{ r[0] }}</th>
            <td :class="r[2]">{{ r[1] }}</td>
          </tr>
        </table>
        <ul class="fs-diag-tips" v-if="diagTips.length">
          <li v-for="(t, i) in diagTips" :key="i">{{ t }}</li>
        </ul>
      </div>
      <div id="fs-office-ph" v-show="!loading && !error"></div>
    </div>
  </div>
</template>
