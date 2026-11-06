<script setup>
/* 回收站：删除的节点在此保留 N 天（与旧版本同一保留期），可恢复或彻底删除 */
import { ref, watch } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'
import { humanSize, tsText } from '../../lib/fsPerm'

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(['close', 'changed']);
const items = ref([]);
const days = ref(7);

watch(() => props.open, async v => { if(v) await load(); });

async function load(){
  try{
    const r = await api('/api/fs/recycle?limit=200');
    items.value = r.items || [];
    days.value = r.retentionDays;
  }catch(e){ toast(e.message); }
}

async function restore(n){
  try{ await api('/api/fs/recycle/' + n.id + '/restore', { method: 'POST' }); toast('已恢复：' + n.name); await load(); emit('changed'); }
  catch(e){ toast(e.message); }
}
async function purge(n){
  try{ await api('/api/fs/recycle/' + n.id, { method: 'DELETE' }); toast('已彻底删除'); await load(); emit('changed'); }
  catch(e){ toast(e.message); }
}
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:940px">
      <div class="dlg-h"><h3>回收站</h3><span class="act" @click="emit('close')">关闭 ✕</span></div>
      <div class="dlg-b">
        <div class="fs-perm-tip">删除的文件/目录在此保留 <b>{{ days }}</b> 天，到期由系统自动清理；期间可随时恢复。</div>
        <table class="users-tbl fs-bin-tbl">
          <thead><tr><th>名称 / 原路径</th><th style="width:64px">类型</th><th style="width:86px">大小</th>
            <th style="width:145px">删除时间</th><th style="width:90px">删除人</th>
            <th style="width:145px">自动清理时间</th><th style="width:160px">操作</th></tr></thead>
          <tbody>
            <tr v-for="n in items" :key="n.id">
              <td class="fs-rc-nm"><b :title="n.name">{{ n.name }}</b><div class="fs-path" :title="n.path">{{ n.path }}</div></td>
              <td>{{ n.kind === 'dir' ? '目录' : '文件' }}</td>
              <td>{{ n.kind === 'dir' ? '—' : humanSize(n.size) }}</td>
              <td>{{ tsText(n.deletedAt) }}</td>
              <td>{{ n.deletedBy }}</td>
              <td :class="{ exp: n.expired }">{{ tsText(n.expireAt) }}</td>
              <td>
                <button class="btnb btnb-txt" @click="restore(n)">恢复</button>
                <button class="btnb btnb-txt act-del" @click="purge(n)">彻底删除</button>
              </td>
            </tr>
            <tr v-if="!items.length"><td colspan="7" style="color:var(--sub)">回收站是空的</td></tr>
          </tbody>
        </table>
      </div>
      <div class="dlg-f"><button class="btn ghost" @click="emit('close')">关闭</button></div>
    </div>
  </div>
</template>
