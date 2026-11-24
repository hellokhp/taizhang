<script setup>
import { onMounted, computed, ref } from 'vue'
import { roles, rm, loadRoles, openNewRole, openEditRole, saveRole, delRole } from '@/stores/common/users'
import { FIELD_MODES } from '@/stores/common/perm'
import { sys } from '@/stores/common/system'
import { SYSTEMS, SYSTEM_LIST } from '@/config/systems'
import { useCollapsibleGroups } from '@/lib/useCollapsibleGroups'

onMounted(loadRoles);

const isCenter = computed(() => sys.current === 'admin');
const SYS_LABEL = Object.fromEntries(Object.values(SYSTEMS).map(s => [s.key, s.name]));

/* 权限目录分组：
   - 普通系统按 group（页面 / 操作）
   - 管理中心按「归属系统」分类（配合折叠），跨系统的通用权限单列「通用」组 */
const permGroups = computed(() => {
  const g = {};
  if(isCenter.value){
    (roles.catalog.perms || []).forEach(p => {
      const ss = p.systems || [];
      const sysKey = ss.length > 1 ? 'common' : (ss[0] || 'taizhang');
      const name = sysKey === 'common' ? '通用（跨系统管理）' : (SYS_LABEL[sysKey] || sysKey);
      (g[name] = g[name] || []).push(p);
    });
    // 固定 ordering：台账 → 财务 → 设计 → 管理中心 → 通用
    const order = ['投标台账','财务管理','设计管理','管理中心','通用（跨系统管理）'];
    return Object.keys(g).sort((a,b) => (order.indexOf(a)+99||99) - (order.indexOf(b)+99||99))
                         .map(k => ({name:k, items:g[k]}));
  }
  (roles.catalog.perms || []).forEach(p => { (g[p.group] = g[p.group] || []).push(p); });
  return Object.keys(g).map(k => ({name: k, items: g[k]}));
});

/* 权限项分组折叠：小分组默认展开，大分组（如台账 20+ 项）默认收起 */
const pg = useCollapsibleGroups(permGroups, { keyOf: g => g.name, autoCollapseAt: 12 });
const permCollapsed = name => pg.isCollapsed(name);
const toggleGroup = name => pg.toggle(name);
const allPermsCollapsed = computed(() => pg.allCollapsed.value);
const expandAllPerms = () => pg.expandAll();
const collapseAllPerms = () => pg.collapseAll();
/* 字段目录按分组归并 */
const fieldGroups = computed(() => {
  const g = {};
  (roles.catalog.fields || []).forEach(f => { (g[f.group] = g[f.group] || []).push(f); });
  return Object.keys(g).map(k => ({name: k, items: g[k]}));
});
/* 脱敏规则 & 行级范围（台账专属；其它系统后端返回空 → 整块隐藏） */
const masks = computed(() => roles.catalog.masks || []);
const scopes = computed(() => roles.catalog.scopes || []);
const hasFields = computed(() => (roles.catalog.fields || []).length > 0);
const hasScopes = computed(() => (roles.catalog.scopes || []).length > 0);

function permCount(r){ return Object.keys(r.perms || {}).filter(k => r.perms[k]).length; }
function fieldCount(r){ return Object.keys(r.fieldPerms || {}).length; }
function scopeLabel(k){ const s = scopes.value.find(x => x.key === k); return s ? s.label : (k || '全部数据'); }

/* 管理中心：角色按系统分组折叠 */
const roleGroups = computed(() => {
  const g = {};
  roles.list.forEach(r => {
    const s = r.system || 'taizhang';
    const name = SYS_LABEL[s] || s;
    (g[name] = g[name] || {key:s, items:[]}).items.push(r);
  });
  const order = ['投标台账','财务管理','设计管理','管理中心'];
  return Object.keys(g).sort((a,b) => (order.indexOf(a)+99||99) - (order.indexOf(b)+99||99))
                       .map(k => ({name:k, system:g[k].key, items:g[k].items}));
});
const rg = useCollapsibleGroups(roleGroups, { keyOf: g => g.name, autoCollapseAt: 8 });
const roleCollapsed = name => rg.isCollapsed(name);
const toggleRoleGroup = name => rg.toggle(name);
const allRolesCollapsed = computed(() => rg.allCollapsed.value);
const expandAllRoles = () => rg.expandAll();
const collapseAllRoles = () => rg.collapseAll();

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
        <span class="hint">{{ isCenter ? '跨系统统一管理 · 角色按系统分类' : '本系统角色独立管理；内置角色可改权限但不可删除' }}</span>
        <div class="spacer"></div>
        <button v-if="isCenter" class="chip" @click="allRolesCollapsed ? expandAllRoles() : collapseAllRoles()">
          {{ allRolesCollapsed ? '全部展开' : '全部收起' }}
        </button>
        <button class="btn pri" @click="openNewRole()">＋ 新增角色</button>
      </div>
      <div class="card-b">
        <!-- 管理中心：按系统分组折叠 -->
        <div v-if="isCenter" class="role-groups">
          <div class="rg" v-for="g in roleGroups" :key="g.name" :class="{folded: roleCollapsed(g.name)}">
            <div class="rg-h" @click="toggleRoleGroup(g.name)">
              <span class="rg-caret">{{ roleCollapsed(g.name) ? '▸' : '▾' }}</span>
              <b>{{ g.name }}</b>
              <span class="rg-count">{{ g.items.length }} 个角色</span>
            </div>
            <div class="rg-body" v-show="!roleCollapsed(g.name)">
              <table class="users-tbl">
                <thead><tr><th>角色名称</th><th>标识</th><th>数据范围</th><th>模块/操作权限</th><th>受限字段</th><th style="text-align:right">操作</th></tr></thead>
                <tbody>
                  <tr v-for="r in g.items" :key="r.key">
                    <td><b>{{ r.name }}</b><span v-if="r.isSystem" class="ro-tag" style="margin-left:6px">内置</span></td>
                    <td style="color:var(--sub)">{{ r.key }}</td>
                    <td><span :class="{ro: r.dataScope === 'SELF'}">{{ scopeLabel(r.dataScope) }}</span></td>
                    <td><b>{{ permCount(r) }}</b> 项</td>
                    <td><span v-if="fieldCount(r)" style="color:var(--warn)">{{ fieldCount(r) }} 个字段受限</span><span v-else style="color:var(--sub)">全部可编辑</span></td>
                    <td style="text-align:right">
                      <span class="act" @click="openEditRole(r)">配置权限</span>
                      <span v-if="!r.isSystem" class="act del" @click="delRole(r)">删除</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-if="!roleGroups.length" style="color:var(--sub)">暂无角色，点右上角新增</div>
        </div>
        <!-- 其它系统：平铺 -->
        <div v-else class="tbl-wrap" style="max-height:520px;">
          <table class="users-tbl">
            <thead><tr><th>角色名称</th><th>标识</th><th>数据范围</th><th>模块/操作权限</th><th>受限字段</th><th style="text-align:right">操作</th></tr></thead>
            <tbody>
              <tr v-for="r in roles.list" :key="r.key">
                <td><b>{{ r.name }}</b><span v-if="r.isSystem" class="ro-tag" style="margin-left:6px">内置</span></td>
                <td style="color:var(--sub)">{{ r.key }}</td>
                <td><span :class="{ro: r.dataScope === 'SELF'}">{{ scopeLabel(r.dataScope) }}</span></td>
                <td><b>{{ permCount(r) }}</b> 项</td>
                <td>
                  <span v-if="!hasFields" style="color:var(--sub)">—</span>
                  <span v-else-if="fieldCount(r)" style="color:var(--warn)">{{ fieldCount(r) }} 个字段受限</span>
                  <span v-else style="color:var(--sub)">全部可编辑</span>
                </td>
                <td style="text-align:right">
                  <span class="act" @click="openEditRole(r)">配置权限</span>
                  <span v-if="!r.isSystem" class="act del" @click="delRole(r)">删除</span>
                </td>
              </tr>
              <tr v-if="!roles.list.length"><td colspan="6" style="color:var(--sub)">本系统暂无角色，点右上角新增</td></tr>
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
            <div class="fld" style="grid-column:1 / -1" v-if="isCenter || hasScopes">
              <label>归属系统{{ isCenter ? '（决定该角色在哪个系统生效）' : '' }}</label>
              <select v-model="rm.system" :disabled="rm.isEdit && !isCenter">
                <option v-for="s in SYSTEM_LIST" :key="s.key" :value="s.key">{{ s.name }}</option>
              </select>
            </div>
            <div class="fld" style="grid-column:1 / -1" v-if="hasScopes">
              <label>数据范围（行级权限）</label>
              <select v-model="rm.dataScope">
                <option v-for="s in scopes" :key="s.key" :value="s.key">{{ s.label }}</option>
              </select>
            </div>
          </div>

          <div class="grp-title">
            模块 / 操作权限
            <span class="perm-bulk">
              <button class="chip" @click="allPermsCollapsed ? expandAllPerms() : collapseAllPerms()">
                {{ allPermsCollapsed ? '全部展开' : '全部收起' }}
              </button>
            </span>
          </div>
          <div class="perm-groups">
            <div class="perm-grp" v-for="g in permGroups" :key="g.name" :class="{folded: permCollapsed(g.name)}">
              <div class="perm-grp-h perm-grp-toggle" @click="toggleGroup(g.name)">
                <span class="rg-caret">{{ permCollapsed(g.name) ? '▸' : '▾' }}</span>{{ g.name }}
                <span class="perm-grp-n">{{ g.items.length }}</span>
              </div>
              <template v-if="!permCollapsed(g.name)">
                <label class="perm-chk" v-for="p in g.items" :key="p.key">
                  <input type="checkbox" v-model="rm.perms[p.key]"><span>{{ p.label }}</span>
                </label>
              </template>
            </div>
          </div>

          <div v-if="hasFields">
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

<style scoped>
/* 不锁容器/组 body 高度：展开即完整显示，页面整体滚动，不留大片空白。
   大组默认折叠 + 组头 sticky 吸顶，兼顾防挤压与导航。 */
.role-groups{display:flex;flex-direction:column;gap:10px;}
.rg{border:1px solid var(--line);border-radius:10px;overflow:visible;background:#fff;}
.rg.folded{background:var(--bg-2,#F7F6F2);}
.rg-h{display:flex;align-items:center;gap:8px;padding:10px 14px;cursor:pointer;background:var(--bg-2,#F7F6F2);
      user-select:none;position:sticky;top:45px;z-index:2;border-bottom:1px solid transparent;border-radius:10px 10px 0 0;}
.rg:not(.folded) .rg-h{border-bottom-color:var(--line);}
.rg-h:hover{background:rgba(216,182,120,.10);}
.rg-h b{font-size:13px;color:var(--pri);}
.rg-count{font-size:11px;color:var(--sub);margin-left:auto;background:rgba(0,0,0,.04);border-radius:10px;padding:1px 8px;}
.rg-caret{display:inline-block;width:12px;color:var(--sub);font-size:11px;}
.rg-body{padding:2px 0;}
/* 权限项弹窗分组 */
.perm-bulk{float:right;font-weight:400;}
.perm-grp.folded{opacity:.72;}
.perm-grp-toggle{cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px;}
.perm-grp-toggle:hover{color:var(--gold);}
.perm-grp-n{font-size:10px;color:var(--sub);background:rgba(0,0,0,.05);border-radius:8px;padding:0 6px;margin-left:2px;}
</style>
