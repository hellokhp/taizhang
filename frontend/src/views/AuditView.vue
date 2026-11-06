<script setup>
import { onMounted, ref, computed } from 'vue'
import { api } from '../api/client'
import { toast } from '../stores/bid'

const logs = ref([]);
const total = ref(0);
const kw = ref('');
const onlyReject = ref(false);
const loading = ref(false);

const ACTION_TXT = {create:'新增', update:'修改', delete:'删除', reject:'越权拦截'};
const ACTION_CLS = {create:'a-create', update:'a-update', delete:'a-del', reject:'a-reject'};

function ts(t){ return new Date((t || 0) * 1000).toLocaleString('zh-CN', {hour12:false}); }
function short(v){ const s = String(v == null ? '' : v); return s.length > 26 ? s.slice(0,26) + '…' : s; }

async function load(){
  loading.value = true;
  try{
    const r = await api('/api/audit?limit=200');
    logs.value = r.logs || [];
    total.value = r.total || 0;
  }catch(e){ toast(e.message); }
  loading.value = false;
}
onMounted(load);

const shown = computed(() => {
  const k = kw.value.trim().toLowerCase();
  return logs.value.filter(o => {
    if(onlyReject.value && o.allowed) return false;
    if(!k) return true;
    return [o.user, o.fieldLabel, o.field, o.rid, o.old, o.new]
      .some(v => String(v || '').toLowerCase().includes(k));
  });
});
</script>

<template>
  <div class="card">
    <div class="card-h" style="padding-bottom:0">
      <h2>审计日志</h2>
      <span class="hint">字段级变更留痕：谁 · 何时 · 哪条台账 · 哪个字段 · 旧值 → 新值（含被拒绝的越权写入）</span>
    </div>
    <div class="toolbar tbar">
      <input type="search" placeholder="搜索操作人 / 字段 / 台账ID / 值" style="width:260px;" v-model="kw">
      <label class="imp-chk"><input type="checkbox" v-model="onlyReject"><span>只看越权拦截</span></label>
      <span class="tb-sep"></span>
      <button class="btn ghost" @click="load()">刷新</button>
      <span class="cnt">显示 {{ shown.length }} 条 · 库内共 {{ total }} 条（最多保留 5000）</span>
    </div>
    <div class="tbl-wrap" style="max-height:640px;">
      <table class="users-tbl audit-tbl">
        <thead><tr><th style="width:150px">时间</th><th style="width:110px">操作人</th><th style="width:90px">动作</th><th style="width:130px">字段</th><th>原值</th><th>新值</th><th style="width:80px">结果</th></tr></thead>
        <tbody>
          <tr v-for="o in shown" :key="o.id">
            <td style="color:var(--sub);font-variant-numeric:tabular-nums">{{ ts(o.ts) }}</td>
            <td><b>{{ o.user }}</b></td>
            <td><span class="audit-act" :class="ACTION_CLS[o.action]">{{ ACTION_TXT[o.action] || o.action }}</span></td>
            <td>{{ o.fieldLabel }}</td>
            <td class="audit-v" :title="o.old">{{ short(o.old) || '—' }}</td>
            <td class="audit-v" :title="o.new">{{ short(o.new) || '—' }}</td>
            <td>
              <span v-if="o.allowed" style="color:var(--lose)">已写入</span>
              <span v-else style="color:var(--win);font-weight:600">已拦截</span>
            </td>
          </tr>
          <tr v-if="!shown.length">
            <td colspan="7" style="color:var(--sub)">{{ loading ? '加载中…' : '暂无记录' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
