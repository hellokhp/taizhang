<script setup>
import { onMounted, computed } from 'vue'
import { roles, rm, loadRoles, openNewRole, openEditRole, saveRole, delRole } from '../stores/users'
import { FIELD_MODES } from '../stores/perm'

onMounted(loadRoles);

/* 权限目录按分组归并：页面 / 操作 */
const permGroups = computed(() => {
  const g = {};
  (roles.catalog.perms || []).forEach(p => { (g[p.group] = g[p.group] || []).push(p); });
  return Object.keys(g).map(k => ({name: k, items: g[k]}));
});
/* 字段目录按分组归并 */
const fieldGroups = computed(() => {
  const g = {};
  (roles.catalog.fields || []).forEach(f => { (g[f.group] = g[f.group] || []).push(f); });
  return Object.keys(g).map(k => ({name: k, items: g[k]}));
});
/* 脱敏规则 & 行级范围 */
const masks = computed(() => roles.catalog.masks || []);
const scopes = computed(() => roles.catalog.scopes || [{key:'ALL', label:'全部数据'}]);

function permCount(r){ return Object.keys(r.perms || {}).filter(k => r.perms[k]).length; }
function fieldCount(r){ return Object.keys(r.fieldPerms || {}).length; }
function scopeLabel(k){ const s = scopes.value.find(x => x.key === k); return s ? s.label : (k || '全部数据'); }

/* rm.fieldPerms[k] = {level, mask}；可写档不落库（删除该项） */
function ruleOf(k){
  const v = rm.fieldPerms[k];
  if(!v) return {level:'writable', mask:'full'};
  if(typeof v === 'string'){
    const lv = {'none':'hidden','read':'readonly','write':'writable'}[v] || v;
    return {level:lv, mask:'full'};
  }
  return {level: v.level || 'writable', mask: v.mask || 'full'};
}
function levelOf(k){ return ruleOf(k).level; }
function setLevel(k, lv){
  if(lv === 'writable'){ delete rm.fieldPerms[k]; return; }
  rm.fieldPerms[k] = {level: lv, mask: ruleOf(k).mask};
}
function setMask(k, mk){
  const cur = ruleOf(k);
  rm.fieldPerms[k] = {level: cur.level === 'writable' ? 'masked' : cur.level, mask: mk};
}
function setGroupMode(items, lv){ items.forEach(f => setLevel(f.key, lv)); }
</script>

<template>
  <div>
    <div class="card" style="margin-bottom:14px;">
      <div class="card-h" style="padding-bottom:12px;">
        <h2>角色与权限</h2>
        <span class="hint">字段四档：可编辑 / 只读 / 脱敏 / 不可见；内置角色可改权限但不可删除</span>
        <div class="spacer"></div>
        <button class="btn pri" @click="openNewRole()">＋ 新增角色</button>
      </div>
      <div class="card-b">
        <div class="tbl-wrap" style="max-height:520px;">
          <table class="users-tbl">
            <thead><tr><th>角色名称</th><th>标识</th><th>数据范围</th><th>模块/操作权限</th><th>受限字段</th><th style="text-align:right">操作</th></tr></thead>
            <tbody>
              <tr v-for="r in roles.list" :key="r.key">
                <td><b>{{ r.name }}</b><span v-if="r.isSystem" class="ro-tag" style="margin-left:6px">内置</span></td>
                <td style="color:var(--sub)">{{ r.key }}</td>
                <td><span :class="{ro: r.dataScope === 'SELF'}">{{ scopeLabel(r.dataScope) }}</span></td>
                <td><b>{{ permCount(r) }}</b> 项</td>
                <td>
                  <span v-if="fieldCount(r)" style="color:var(--warn)">{{ fieldCount(r) }} 个字段受限</span>
                  <span v-else style="color:var(--sub)">全部可编辑</span>
                </td>
                <td style="text-align:right">
                  <span class="act" @click="openEditRole(r)">配置权限</span>
                  <span v-if="!r.isSystem" class="act del" @click="delRole(r)">删除</span>
                </td>
              </tr>
              <tr v-if="!roles.list.length"><td colspan="6" style="color:var(--sub)">暂无角色</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="mask" :class="{on: rm.open}" @click.self="rm.open=false">
      <div class="dlg" style="width:880px;max-width:94vw">
        <div class="dlg-h">
          <h3>{{ rm.isEdit ? '配置权限 · ' + rm.name : '新增角色' }}</h3>
          <span class="act" style="color:var(--mut)" @click="rm.open=false">关闭 ✕</span>
        </div>
        <div class="dlg-b" style="max-height:66vh;overflow:auto;">
          <div class="form" style="grid-template-columns:1fr 1fr;margin-bottom:14px;">
            <div class="fld"><label>角色标识{{ rm.isEdit ? '（不可改）' : ' *' }}</label>
              <input type="text" v-model="rm.key" :disabled="rm.isEdit" placeholder="英文标识，如 bid_lead"></div>
            <div class="fld"><label>角色名称 *</label>
              <input type="text" v-model="rm.name" placeholder="如：投标负责人"></div>
            <div class="fld" style="grid-column:1 / -1">
              <label>数据范围（行级权限）</label>
              <select v-model="rm.dataScope">
                <option v-for="s in scopes" :key="s.key" :value="s.key">{{ s.label }}</option>
              </select>
            </div>
          </div>

          <div class="grp-title">模块 / 操作权限</div>
          <div class="perm-groups">
            <div class="perm-grp" v-for="g in permGroups" :key="g.name">
              <div class="perm-grp-h">{{ g.name }}</div>
              <label class="perm-chk" v-for="p in g.items" :key="p.key">
                <input type="checkbox" v-model="rm.perms[p.key]"><span>{{ p.label }}</span>
              </label>
            </div>
          </div>

          <div class="grp-title" style="margin-top:16px">
            台账字段权限
            <em class="hint" style="font-weight:400">默认「可编辑」；只读不可改、脱敏只返回打码值、不可见则接口不返回该字段</em>
          </div>
          <div class="field-perms">
            <div class="fp-grp" v-for="g in fieldGroups" :key="g.name">
              <div class="fp-grp-h">
                {{ g.name }}
                <span class="fp-quick">
                  <button class="chip" @click="setGroupMode(g.items, 'writable')">全可编辑</button>
                  <button class="chip" @click="setGroupMode(g.items, 'readonly')">全只读</button>
                </span>
              </div>
              <div class="fp-row" v-for="f in g.items" :key="f.key">
                <span class="fp-lbl">{{ f.label }}</span>
                <span class="fp-ctl">
                  <select :value="levelOf(f.key)" @change="setLevel(f.key, $event.target.value)"
                    :class="{'fp-read': levelOf(f.key) === 'readonly', 'fp-none': levelOf(f.key) === 'hidden', 'fp-mask': levelOf(f.key) === 'masked'}">
                    <option v-for="m in FIELD_MODES" :key="m.v" :value="m.v">{{ m.l }}</option>
                  </select>
                  <select v-if="levelOf(f.key) === 'masked'" :value="ruleOf(f.key).mask"
                    @change="setMask(f.key, $event.target.value)" class="fp-mask-sel" title="脱敏规则">
                    <option v-for="m in masks" :key="m.key" :value="m.key">{{ m.label }}</option>
                  </select>
                </span>
              </div>
            </div>
          </div>

          <div v-if="roles.err" style="color:var(--win);font-size:12px;margin-top:10px">{{ roles.err }}</div>
        </div>
        <div class="dlg-f">
          <button class="btn" @click="rm.open=false">取消</button>
          <button class="btn pri" @click="saveRole()">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
