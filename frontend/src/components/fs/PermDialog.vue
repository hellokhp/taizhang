<script setup>
/* 原子化权限：按用户 / 按角色，对某个目录或文件授予 5 个独立动作（+ 是否向下继承） */
import { ref, watch } from 'vue'
import { api } from '../../api/client'
import { toast } from '../../stores/bid'
import { FS_OPS, fsPermText, tsText } from '../../lib/fsPerm'

const props = defineProps({ open: { type: Boolean, default: false }, nodeId: { type: String, default: '' },
                            nodeName: { type: String, default: '' }, path: { type: String, default: '' } });
const emit = defineEmits(['close']);

const items = ref([]);
const users = ref([]);
const roles = ref([]);
const canGrant = ref(false);
const subjType = ref('user');
const subjKey = ref('');
const ops = ref({});
const deny = ref({});
const inherit = ref(true);
const loading = ref(false);

watch(() => props.open, async (v) => {
  if(!v) return;
  loading.value = true;
  try{
    const a = await api('/api/fs/acl?node_id=' + props.nodeId);
    items.value = a.items || [];
    canGrant.value = a.canGrant;
    if(a.canGrant){
      const g = await api('/api/fs/grant-targets?node_id=' + props.nodeId);
      users.value = g.users || [];
      roles.value = g.roles || [];
      subjKey.value = (users.value[0] || {}).key || '';
    }
  }catch(e){ toast(e.message); }
  loading.value = false;
});

const pool = ref([]);
watch([subjType, users, roles], () => {
  pool.value = subjType.value === 'user' ? users.value : roles.value;
  subjKey.value = (pool.value[0] || {}).key || '';
});

function resetForm(){ ops.value = {}; deny.value = {}; inherit.value = true; }

function edit(row){
  subjType.value = row.subjectType;
  subjKey.value = row.subjectKey;
  ops.value = Object.assign({}, row.perm || {});
  deny.value = Object.assign({}, row.denyPerm || {});
  inherit.value = !!row.inherit;
}

async function save(){
  try{
    await api('/api/fs/acl', { method: 'PUT', body: JSON.stringify({
      nodeId: props.nodeId, subjectType: subjType.value, subjectKey: subjKey.value,
      ops: ops.value, deny: deny.value, inherit: inherit.value }) });
    toast('已保存授权');
    const a = await api('/api/fs/acl?node_id=' + props.nodeId);
    items.value = a.items || [];
    resetForm();
    emit('close');
  }catch(e){ toast(e.message); }
}

async function revoke(row){
  try{
    await api('/api/fs/acl/' + row.id, { method: 'DELETE' });
    const a = await api('/api/fs/acl?node_id=' + props.nodeId);
    items.value = a.items || [];
    toast('已撤销');
  }catch(e){ toast(e.message); }
}

function nameOf(row){
  const pool2 = row.subjectType === 'user' ? users.value : roles.value;
  const hit = pool2.filter(x => x.key === row.subjectKey)[0];
  return hit ? hit.name : row.subjectKey;
}
</script>

<template>
  <div class="mask" :class="{ on: open }" @click.self="emit('close')">
    <div class="dlg" style="width:680px">
      <div class="dlg-h">
        <h3>权限设置 · {{ nodeName || '存储根目录' }}</h3>
        <span class="act" @click="emit('close')">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="fs-perm-path">路径：<b>{{ path || '/' }}</b></div>
        <div class="fs-perm-tip">五个动作相互独立：能「上传」不代表能「覆盖」，能「查看」不代表能「删除」。
          勾选后点保存；同一节点上「拒绝」优先于「允许」，用户级优先于角色级。</div>

        <div class="fs-acl-list" v-if="items.length">
          <div class="fs-acl-row th">
            <span>对象</span><span>允许</span><span>拒绝</span><span>继承</span><span>操作人</span><span></span>
          </div>
          <div class="fs-acl-row" v-for="r in items" :key="r.id">
            <span><em>{{ r.subjectType === 'user' ? '用户' : '角色' }}</em> {{ nameOf(r) }}</span>
            <span class="ok">{{ fsPermText(r.perm) }}</span>
            <span class="no">{{ fsPermText(r.denyPerm) }}</span>
            <span>{{ r.inherit ? '向下继承' : '仅本级' }}</span>
            <span class="mut">{{ r.grantedBy }}<br><i>{{ tsText(r.grantedAt) }}</i></span>
            <span class="ops">
              <button class="btnb" v-if="canGrant" @click="edit(r)">编辑</button>
              <button class="btnb act-del" v-if="canGrant" @click="revoke(r)">撤销</button>
            </span>
          </div>
        </div>
        <div class="fs-empty" v-else>该节点暂无单独授权（权限继承自上级目录）</div>

        <template v-if="canGrant">
          <div class="fs-perm-form">
            <div class="line">
              <label>授权对象</label>
              <select v-model="subjType"><option value="user">用户</option><option value="role">角色</option></select>
              <select v-model="subjKey">
                <option v-for="p in pool" :key="p.key" :value="p.key">{{ p.name }}</option>
              </select>
              <label class="imp-chk"><input type="checkbox" v-model="inherit"><span>应用到子目录/子文件</span></label>
            </div>
            <div class="line ops-line">
              <label>允许</label>
              <span class="op" v-for="o in FS_OPS" :key="o.k">
                <label class="imp-chk"><input type="checkbox" v-model="ops[o.k]"><span>{{ o.l }}</span></label>
              </span>
            </div>
            <div class="line ops-line">
              <label>拒绝</label>
              <span class="op" v-for="o in FS_OPS" :key="o.k">
                <label class="imp-chk"><input type="checkbox" v-model="deny[o.k]"><span>{{ o.l }}</span></label>
              </span>
            </div>
          </div>
        </template>
        <div class="fs-empty" v-else>你对该节点没有授权权限（仅管理员、创建人或被授予「授权」的人可设置）</div>
      </div>
      <div class="dlg-f">
        <button class="btn ghost" @click="emit('close')">关闭</button>
        <button class="btn pri" v-if="canGrant" @click="save()">保存授权</button>
      </div>
    </div>
  </div>
</template>
