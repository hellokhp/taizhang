<script setup>
/* 存储设置：备份保留天数等「口子」，改完立即生效（可选择同步应用到已有版本） */
import { ref, watch } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(['close']);
const values = ref({});
const labels = ref({});
const root = ref('');
const stats = ref(null);
const EDITABLE = ['retention_days', 'log_retention_days', 'chunk_size', 'max_file_size',
                  'quota_bytes', 'max_name_len', 'max_depth', 'allow_ext', 'deny_ext',
                  'purge_hour', 'enable_dedup', 'max_versions', 'owner_full_control',
                  'office_server', 'office_secret', 'office_base_url'];

watch(() => props.open, async v => {
  if(!v) return;
  try{
    const s = await api('/api/fs/settings');
    values.value = s.values || {};
    labels.value = s.labels || {};
    root.value = s.root || '';
    stats.value = await api('/api/fs/stats');
  }catch(e){ toast(e.message); }
});

async function save(k){
  try{
    const apply = k === 'retention_days' &&
      window.confirm('是否把新保留天数同步应用到「已存在的旧版本」？\n\n是：存量版本的到期时间一起改；\n否：只影响之后新产生的版本。');
    await api('/api/fs/settings', { method: 'PUT', body: JSON.stringify({ k: k, v: String(values.value[k]),
                                                                          applyExisting: !!apply }) });
    toast('已保存');
  }catch(e){ toast(e.message); }
}

async function runGc(){
  try{
    const r = await api('/api/fs/gc', { method: 'POST' });
    toast('清理完成：版本 ' + r.stats.versions + ' · 节点 ' + r.stats.purged_nodes +
          ' · 实体 ' + r.stats.blobs + ' · 释放 ' + (r.stats.freed / 1048576).toFixed(1) + ' MB');
    stats.value = await api('/api/fs/stats');
  }catch(e){ toast(e.message); }
}

function mb(n){ return (Number(n || 0) / 1048576).toFixed(0); }
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:640px">
      <div class="dlg-h"><h3>存储设置</h3><span class="act" @click="emit('close')">关闭 ✕</span></div>
      <div class="dlg-b">
        <div class="fs-perm-tip">存储根目录：<b>{{ root }}</b></div>
        <div class="fs-set-row" v-for="k in EDITABLE" :key="k">
          <label :title="k">{{ labels[k] || k }}</label>
          <input v-model="values[k]">
          <button class="btn ghost" @click="save(k)">保存</button>
        </div>
        <div class="fs-gc" v-if="stats">
          <div class="fs-gc-h">
            <b>用量</b>
            <span>文件 {{ stats.files }} · 目录 {{ stats.dirs }} · 占用 {{ mb(stats.used) }} MB ·
              版本 {{ stats.versions }}（{{ mb(stats.versionBytes) }} MB） · 回收站 {{ stats.recycle }} ·
              日志 {{ stats.logs }}</span>
            <button class="btn ghost" @click="runGc()">立即清理</button>
          </div>
          <div class="fs-perm-tip">清理会删除「超过保留期的旧版本 / 回收站文件 / 无引用的实体文件 / 过期日志」。
            每天 {{ values.purge_hour }} 点自动执行一次。</div>
        </div>
      </div>
      <div class="dlg-f"><button class="btn ghost" @click="emit('close')">关闭</button></div>
    </div>
  </div>
</template>
