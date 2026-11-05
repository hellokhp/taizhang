<script setup>
/* 存储操作日志：成功与被拒绝的动作都记录，可筛选、导出 CSV */
import { ref, watch } from 'vue'
import { api, apiBlob } from '../../api/client'
import { toast } from '../../stores/bid'
import { humanSize, tsText } from '../../lib/fsPerm'

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(['close']);
const items = ref([]);
const total = ref(0);
const kw = ref('');
const op = ref('');
const onlyReject = ref(false);
const OPS = [['mkdir', '新建目录'], ['upload', '上传'], ['overwrite', '覆盖'], ['modify', '修改'],
              ['rename', '重命名'], ['move', '移动'], ['delete', '删除'], ['restore', '恢复'],
              ['purge', '清理'], ['download', '下载'], ['grant', '授权'], ['revoke', '撤权'],
              ['rollback', '版本回滚']];

watch(() => props.open, v => { if(v) load(); });

async function load(){
  try{
    const q = ['limit=200', 'kw=' + encodeURIComponent(kw.value), 'op=' + encodeURIComponent(op.value),
               'result=' + (onlyReject.value ? '0' : '')].join('&');
    const r = await api('/api/fs/logs?' + q);
    items.value = r.items || [];
    total.value = r.total || 0;
  }catch(e){ toast(e.message); }
}

async function exportCsv(){
  try{
    const blob = await apiBlob('/api/fs/logs/export?limit=5000');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = '存储操作日志.csv'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  }catch(e){ toast(e.message); }
}
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:min(1100px, calc(100vw - 64px))">
      <div class="dlg-h"><h3>存储操作日志</h3><span class="act" @click="emit('close')">关闭 ✕</span></div>
      <div class="dlg-b">
        <div class="toolbar tbar" style="padding:0 0 10px;border:0;background:none">
          <input type="search" placeholder="搜索操作人 / 文件名 / 路径" style="width:240px" v-model="kw" @keyup.enter="load()">
          <select v-model="op" @change="load()">
            <option value="">全部动作</option>
            <option v-for="o in OPS" :key="o[0]" :value="o[0]">{{ o[1] }}</option>
          </select>
          <label class="imp-chk"><input type="checkbox" v-model="onlyReject" @change="load()"><span>只看被拒绝</span></label>
          <button class="btn ghost" @click="load()">刷新</button>
          <span class="cnt">显示 {{ items.length }} 条 · 共 {{ total }} 条</span>
          <button class="btn ghost" @click="exportCsv()">导出 CSV</button>
        </div>
        <div class="tbl-wrap" style="max-height:520px">
          <table class="users-tbl audit-tbl">
            <thead><tr><th style="width:150px">时间</th><th style="width:90px">操作人</th><th style="width:90px">动作</th>
              <th style="width:170px">对象</th><th>路径 / 备注</th><th style="width:90px">大小</th>
              <th style="width:80px">结果</th></tr></thead>
            <tbody>
              <tr v-for="o in items" :key="o.id">
                <td style="color:var(--sub)">{{ o.tsText }}</td>
                <td><b>{{ o.actor }}</b></td>
                <td>{{ o.opText }}</td>
                <td :title="o.nodeName">{{ o.nodeName }}</td>
                <td class="audit-v" :title="o.path + ' ' + o.target">
                  {{ o.path }}<em v-if="o.target"> · {{ o.target }}</em>
                  <i v-if="o.reason" class="rsn">（{{ o.reason }}）</i>
                </td>
                <td>{{ o.size ? humanSize(o.size) : '—' }}</td>
                <td>
                  <span v-if="o.result" style="color:var(--lose)">成功</span>
                  <span v-else style="color:var(--win);font-weight:600">被拒绝</span>
                </td>
              </tr>
              <tr v-if="!items.length"><td colspan="7" style="color:var(--sub)">暂无记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="dlg-f"><button class="btn ghost" @click="emit('close')">关闭</button></div>
    </div>
  </div>
</template>
