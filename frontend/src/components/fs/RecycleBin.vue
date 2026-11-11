<script setup>
/* 回收站：删除的节点保留 N 天；支持勾选多项批量恢复 / 批量彻底删除 */
import { ref, computed, watch, nextTick } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'
import { humanSize, tsText } from '../../lib/fsPerm'

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(['close', 'changed']);
const items = ref([]);
const days = ref(7);
const total = ref(0);
const topOnly = ref(true);        // 默认只看被删目录/文件的顶层项，避免整棵子树刷屏
const sel = ref([]);
const busy = ref(false);
const loading = ref(false);
const allCb = ref(null);

const shown = computed(() => topOnly.value
  ? items.value.filter(x => x.isTop !== false) : items.value);
const allChecked = computed(() => !!shown.value.length &&
  shown.value.every(x => sel.value.indexOf(x.id) >= 0));
const someChecked = computed(() => sel.value.length > 0 && !allChecked.value);
const selectedDirs = computed(() => shown.value.filter(x =>
  x.kind === 'dir' && sel.value.indexOf(x.id) >= 0).length);

watch(() => props.open, async v => { if(v) await load(); });
/* indeterminate 是 DOM property 而非 attribute：显式同步一次，兼容各 Vue 版本渲染策略 */
watch([someChecked, shown], () => nextTick(() => {
  if(allCb.value) allCb.value.indeterminate = someChecked.value;
}), { immediate: true });

async function load(){
  loading.value = true;
  try{
    const r = await api('/api/fs/recycle?limit=500');
    items.value = r.items || [];
    days.value = r.retentionDays;
    total.value = r.total == null ? items.value.length : r.total;
    const alive = {};
    items.value.forEach(x => { alive[x.id] = 1; });
    sel.value = sel.value.filter(id => alive[id]);   // 清掉已被处理掉的选中项
  }catch(e){ toast(e.message); }
  loading.value = false;
}

function isSel(id){ return sel.value.indexOf(id) >= 0; }
function toggleSel(id){
  if(busy.value) return;
  const i = sel.value.indexOf(id);
  if(i >= 0) sel.value.splice(i, 1);
  else sel.value.push(id);
}
function toggleAll(){
  if(busy.value) return;
  sel.value = allChecked.value ? [] : shown.value.map(x => x.id);
}
function clearSel(){ sel.value = []; }
function setTopOnly(v){
  topOnly.value = v;
  const ok = {};
  shown.value.forEach(x => { ok[x.id] = 1; });
  sel.value = sel.value.filter(id => ok[id]);       // 被折叠掉的项不再参与批量
}

/* 单条与批量都走同一组接口：后端会按父子顺序处理，并返回成功/跳过/失败明细 */
async function run(action, ids){
  if(!ids.length || busy.value) return;
  busy.value = true;
  try{
    const r = await api('/api/fs/recycle/batch-' + action,
      { method: 'POST', body: JSON.stringify({ ids: ids }) });
    let msg = action === 'restore'
      ? '已恢复 ' + r.succeeded + ' 个节点'
      : '已彻底删除 ' + r.succeeded + ' 项';
    if(r.skipped) msg += '（' + r.skipped + ' 项已随目录处理）';
    if(r.failedCount){
      const f = (r.failed && r.failed[0]) || {};
      msg += '，' + r.failedCount + ' 项失败：' + (f.name || '') + ' ' + (f.reason || '');
      toast(msg, 6000);
    }else toast(msg);
    clearSel();
    await load();
    emit('changed');
  }catch(e){ toast(e.message); }
  busy.value = false;
}

function batch(action){
  const ids = sel.value.slice();
  if(!ids.length) return;
  if(action === 'purge'){
    let tip = '确认彻底删除选中的 ' + ids.length + ' 项？\n此操作不可恢复。';
    if(selectedDirs.value) tip += '\n其中 ' + selectedDirs.value + ' 个目录会连同内部文件一起删除。';
    if(!window.confirm(tip)) return;
  }
  run(action, ids);
}

function restore(n){ run('restore', [n.id]); }
function purge(n){
  const tip = '确认彻底删除「' + n.name + '」？此操作不可恢复。' +
              (n.kind === 'dir' ? '\n目录内的文件会一并删除。' : '');
  if(!window.confirm(tip)) return;
  run('purge', [n.id]);
}
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:960px">
      <div class="dlg-h"><h3>回收站</h3><span class="act" @click="emit('close')">关闭 ✕</span></div>
      <div class="dlg-b">
        <div class="fs-perm-tip">删除的文件/目录在此保留 <b>{{ days }}</b> 天，到期由系统自动清理；期间可勾选多项批量恢复或彻底删除。</div>

        <div class="fs-bin-bar">
          <label class="fs-bin-chk">
            <input type="checkbox" :checked="topOnly" @change="setTopOnly($event.target.checked)">仅显示顶层项目
          </label>
          <span class="fs-bin-cnt">回收站共 {{ total }} 条 · 当前显示 {{ shown.length }} 条</span>
          <span class="sp"></span>
          <template v-if="sel.length">
            <span class="fs-bin-sel">已选 <b>{{ sel.length }}</b> 项</span>
            <button class="btn ghost" :disabled="busy" @click="clearSel">取消选择</button>
            <button class="btn pri" :disabled="busy" @click="batch('restore')">{{ busy ? '处理中…' : '批量恢复' }}</button>
            <button class="btn ghost danger" :disabled="busy" @click="batch('purge')">批量彻底删除</button>
          </template>
          <span v-else class="fs-bin-hint">勾选左侧复选框可批量操作</span>
        </div>

        <table class="users-tbl fs-bin-tbl">
          <thead><tr>
            <th style="width:36px">
              <input type="checkbox" class="th-cb" ref="allCb" :checked="allChecked"
                     :indeterminate="someChecked" :disabled="!shown.length || busy" @change="toggleAll">
            </th>
            <th>名称 / 原路径</th><th style="width:58px">类型</th><th style="width:82px">大小</th>
            <th style="width:140px">删除时间</th><th style="width:86px">删除人</th>
            <th style="width:140px">自动清理时间</th><th style="width:150px">操作</th>
          </tr></thead>
          <tbody>
            <tr v-for="n in shown" :key="n.id" :class="{ sel: isSel(n.id) }" @click="toggleSel(n.id)">
              <td><input type="checkbox" class="row-cb" :checked="isSel(n.id)" @click.stop="toggleSel(n.id)"></td>
              <td class="fs-rc-nm"><b :title="n.name">{{ n.name }}</b><div class="fs-path" :title="n.path">{{ n.path }}</div></td>
              <td>{{ n.kind === 'dir' ? '目录' : '文件' }}</td>
              <td>{{ n.kind === 'dir' ? '—' : humanSize(n.size) }}</td>
              <td>{{ tsText(n.deletedAt) }}</td>
              <td>{{ n.deletedBy }}</td>
              <td :class="{ exp: n.expired }">{{ tsText(n.expireAt) }}</td>
              <td>
                <button class="btnb btnb-txt" :disabled="busy" @click.stop="restore(n)">恢复</button>
                <button class="btnb btnb-txt act-del" :disabled="busy" @click.stop="purge(n)">彻底删除</button>
              </td>
            </tr>
            <tr v-if="!shown.length"><td colspan="8" style="color:var(--sub)">
              {{ loading ? '加载中…' : (items.length ? '当前筛选下没有项目' : '回收站是空的') }}
            </td></tr>
          </tbody>
        </table>
        <div class="fs-bin-foot" v-if="items.length > shown.length || total > items.length">
          <template v-if="items.length > shown.length">
            已折叠 {{ items.length - shown.length }} 条目录内子项；恢复目录时会连同内部文件一起恢复。
          </template>
          <template v-if="total > items.length">
            回收站共 {{ total }} 条，当前只加载最近 {{ items.length }} 条；请先处理这批再继续。
          </template>
        </div>
      </div>
      <div class="dlg-f"><button class="btn ghost" @click="emit('close')">关闭</button></div>
    </div>
  </div>
</template>
