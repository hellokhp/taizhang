<script setup>
/* 版本/备份抽屉：覆盖、修改、删除都会留下旧版本，默认保留 7 天（可在设置里改） */
import { ref, watch } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'
import { humanSize, tsText } from '../../lib/fsPerm'

const props = defineProps({ open: { type: Boolean, default: false }, nodeId: { type: String, default: '' },
                            nodeName: { type: String, default: '' }, canOverwrite: { type: Boolean, default: false } });
const emit = defineEmits(['close', 'changed']);

const items = ref([]);
const days = ref(7);
const TYPE = { upload: '首次上传', overwrite: '覆盖写入', modify: '改名或移动前',
               delete: '删除前备份', restore: '回滚生成' };

watch(() => props.open, async v => {
  if(!v || !props.nodeId) return;
  try{
    const r = await api('/api/fs/versions?node_id=' + props.nodeId);
    items.value = r.items || [];
    days.value = r.retentionDays;
  }catch(e){ toast(e.message); }
});

async function restore(v){
  try{
    await api('/api/fs/versions/' + v.id + '/restore', { method: 'POST' });
    toast('已回滚到 v' + v.ver);
    emit('changed');
    emit('close');
  }catch(e){ toast(e.message); }
}

async function pin(v){
  try{
    await api('/api/fs/versions/' + v.id + '/pin?pinned=' + (v.pinned ? 0 : 1), { method: 'POST' });
    v.pinned = !v.pinned;
    toast(v.pinned ? '已锁定，不会被自动清理' : '已解除锁定');
  }catch(e){ toast(e.message); }
}
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:820px">
      <div class="dlg-h">
        <h3>历史版本 · {{ nodeName }}</h3>
        <span class="act" @click="emit('close')">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="fs-perm-tip">覆盖、改名/移动、删除前都会自动保留旧版本，保留 <b>{{ days }}</b> 天；
          锁定后的版本不会被自动清理。回滚会生成一个新版本，不会销毁历史。</div>
        <table class="users-tbl fs-ver-tbl">
          <thead><tr><th style="width:56px">版本</th><th style="width:120px">产生原因</th>
            <th style="width:86px">大小</th><th style="width:150px">时间</th><th style="width:80px">操作人</th>
            <th style="width:150px">到期</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="v in items" :key="v.id">
              <td><b>v{{ v.ver }}</b></td>
              <td>{{ TYPE[v.type] || v.type }}</td>
              <td>{{ humanSize(v.size) }}</td>
              <td>{{ tsText(v.ts) }}</td>
              <td>{{ v.actor }}</td>
              <td>
                <span v-if="v.pinned" class="fs-pin">已锁定</span>
                <span v-else :class="{ exp: v.expired }">{{ tsText(v.keepUntil) }}</span>
              </td>
              <td class="fs-ver-op">
                <button class="btnb btnb-txt" :disabled="!canOverwrite" @click="restore(v)">回滚到此</button>
                <button class="btnb btnb-txt" @click="pin(v)">{{ v.pinned ? '解锁' : '锁定' }}</button>
              </td>
            </tr>
            <tr v-if="!items.length"><td colspan="7" style="color:var(--sub)">暂无历史版本</td></tr>
          </tbody>
        </table>
      </div>
      <div class="dlg-f"><button class="btn ghost" @click="emit('close')">关闭</button></div>
    </div>
  </div>
</template>
