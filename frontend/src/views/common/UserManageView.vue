<script setup>
import { onMounted, computed } from 'vue'
import { sys } from '@/stores/common/system'
import { um, uform, loadUsers, openNewUser, openEditUser, saveUser, delUser, roles, loadRoles, roleOptions } from '@/stores/common/users'
import { auth } from '@/stores/common/auth'
import { useCollapsibleGroups } from '@/lib/useCollapsibleGroups'

onMounted(() => { loadUsers(); loadRoles(); });
function roleName(u){ if(u.isAdmin) return '管理员（全部权限）'; const r = roles.list.find(x => x.key === u.roleKey); return r ? r.name : (u.roleKey || (u.roleKey===''?'未分配':u.roleKey)); }
const isPlatformAdmin = computed(() => !!(auth.user && (auth.user.isAdmin || auth.user.dept === 'management')));
const isCenter = computed(() => sys.current === 'admin');
const deptName = k => ({bid:'投标部',finance:'财务部',design:'设计部',management:'管理部'}[k] || k || '投标部');

/* 管理中心：用户按部门分组折叠 */
const deptGroups = computed(() => {
  const g = {};
  um.list.forEach(u => {
    const d = u.dept || 'bid';
    (g[d] = g[d] || []).push(u);
  });
  const order = ['management','bid','finance','design'];
  return Object.keys(g).sort((a,b) => (order.indexOf(a)+99||99) - (order.indexOf(b)+99||99))
                       .map(k => ({key:k, name:deptName(k), items:g[k]}));
});
/* 智能默认：人数 > 6 的部门（如投标部 13 人）默认折叠，小组展开；
   大组内部再限高滚动 + 组头吸顶，避免挤掉其它部门。 */
const dg = useCollapsibleGroups(deptGroups, { keyOf: g => g.key, autoCollapseAt: 6 });
const deptCollapsed = k => dg.isCollapsed(k);
const toggleDept = k => dg.toggle(k);
const allDeptsCollapsed = computed(() => dg.allCollapsed.value);
const expandAllDepts = () => dg.expandAll();
const collapseAllDepts = () => dg.collapseAll();
/* 部门下拉：管理部可选全部，普通系统管理员只能把用户建在自己系统归属的部门 */
const deptOptions = computed(() => {
  if(isPlatformAdmin.value) return um.departments || [];
  const mods = (auth.user && auth.user.modules) || [];
  return (um.departments || []).filter(d => (d.modules || []).includes(sys.current));
});
</script>

<template>
  <div>
    <div class="card" style="margin-bottom:14px;">
      <div class="card-h" style="padding-bottom:12px;">
        <h2>用户管理</h2>
        <span v-if="isCenter" class="hint">按部门分类 · 人数较多的部门默认收起</span>
        <div class="spacer"></div>
        <button v-if="isCenter" class="chip" @click="allDeptsCollapsed ? expandAllDepts() : collapseAllDepts()">
          {{ allDeptsCollapsed ? '全部展开' : '全部收起' }}
        </button>
        <button class="btn pri" @click="openNewUser()">＋ 新增用户</button>
      </div>
      <div class="card-b">
        <!-- 管理中心：按部门分组折叠 -->
        <div v-if="isCenter" class="dept-groups">
          <div class="dg" v-for="g in deptGroups" :key="g.key" :class="{folded: deptCollapsed(g.key)}">
            <div class="dg-h" @click="toggleDept(g.key)">
              <span class="dg-caret">{{ deptCollapsed(g.key) ? '▸' : '▾' }}</span>
              <b>{{ g.name }}</b>
              <span class="dg-count">{{ g.items.length }} 人</span>
            </div>
            <div class="dg-body" v-show="!deptCollapsed(g.key)">
              <table class="users-tbl">
                <thead><tr><th>账号</th><th>显示名</th><th>角色</th><th>类型</th><th style="text-align:right">操作</th></tr></thead>
                <tbody>
                  <tr v-for="u in g.items" :key="u.username">
                    <td><b>{{ u.username }}</b><span v-if="auth.user && u.username === auth.user.username" style="color:var(--sub);margin-left:6px">（当前登录）</span></td>
                    <td>{{ u.displayName }}</td>
                    <td>{{ roleName(u) }}</td>
                    <td><span v-if="u.isAdmin" style="color:#A87F32;font-weight:600">管理员</span><span v-else style="color:var(--mut)">普通用户</span></td>
                    <td style="text-align:right"><span class="act" @click="openEditUser(u)">编辑</span><span class="act del" @click="delUser(u)">删除</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-if="!deptGroups.length" style="color:var(--sub)">暂无用户，点右上角新增</div>
        </div>
        <!-- 其它系统：平铺 -->
        <div v-else class="tbl-wrap" style="max-height:480px;">
          <table class="users-tbl">
            <thead><tr><th>账号</th><th>显示名</th><th>部门</th><th>角色</th><th>类型</th><th style="text-align:right">操作</th></tr></thead>
            <tbody>
              <tr v-for="u in um.list" :key="u.username">
                <td><b>{{ u.username }}</b><span v-if="auth.user && u.username === auth.user.username" style="color:var(--sub);margin-left:6px">（当前登录）</span></td>
                <td>{{ u.displayName }}</td>
                <td>{{ u.deptName || '投标部' }}</td>
                <td>{{ roleName(u) }}</td>
                <td><span v-if="u.isAdmin" style="color:#A87F32;font-weight:600">管理员</span><span v-else style="color:var(--mut)">普通用户</span></td>
                <td style="text-align:right"><span class="act" @click="openEditUser(u)">编辑</span><span class="act del" @click="delUser(u)">删除</span></td>
              </tr>
              <tr v-if="!um.list.length"><td colspan="6" style="color:var(--sub)">暂无用户</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="mask" :class="{on: um.open}" @click.self="um.open = false">
      <div class="dlg" style="width:420px">
        <div class="dlg-h"><h3>{{ uform.isEdit ? '编辑用户' : '新增用户' }}</h3><span class="act" style="color:var(--mut)" @click="um.open=false">关闭 ✕</span></div>
        <div class="dlg-b">
          <div class="form" style="grid-template-columns:1fr">
            <div class="fld"><label>账号<b v-if="!uform.isEdit"> *</b></label><input type="text" v-model="uform.name" :disabled="uform.isEdit" placeholder="登录账号（创建后不可改）"></div>
            <div class="fld"><label>显示名</label><input type="text" v-model="uform.display" placeholder="页面右上角显示的名字"></div>
            <div class="fld"><label>密码{{ uform.isEdit ? '（留空 = 不修改）' : ' *' }}</label><input type="password" v-model="uform.password" placeholder="请输入密码"></div>
            <div class="fld"><label>部门{{ uform.admin ? '（管理员恒为管理部 · 全模块）' : '（决定可进入的系统模块）' }}</label>
              <select v-model="uform.dept" :disabled="uform.admin">
                <option v-for="d in deptOptions" :key="d.key" :value="d.key">{{ d.label }}</option>
              </select>
            </div>
            <div class="fld"><label>角色{{ uform.admin ? '（管理员已拥有全部权限）' : '' }}</label>
              <select v-model="uform.role" :disabled="uform.admin">
                <option v-for="r in roleOptions()" :key="r.key" :value="r.key">{{ r.name }}</option>
              </select>
            </div>
            <label class="imp-chk"><input type="checkbox" v-model="uform.admin"><span>设为管理员 —— 绕过所有权限限制，可进入「用户管理」「角色权限」页</span></label>
          </div>
          <div v-if="um.err" style="color:var(--win);font-size:12px;margin-top:8px">{{ um.err }}</div>
        </div>
        <div class="dlg-f">
          <button class="btn" @click="um.open=false">取消</button>
          <button class="btn pri" @click="saveUser()">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 不锁死容器高度：内容自然撑开填满视口，页面整体滚动，下方不留大片空白。
   大组默认折叠（useCollapsibleGroups）作为防挤压第一道防线；组头 sticky 吸顶导航。 */
.dept-groups{display:flex;flex-direction:column;gap:10px;}
.dg{border:1px solid var(--line);border-radius:10px;overflow:visible;background:#fff;}
.dg.folded{background:var(--bg-2,#F7F6F2);}
.dg-h{display:flex;align-items:center;gap:8px;padding:10px 14px;cursor:pointer;background:var(--bg-2,#F7F6F2);
      position:sticky;top:45px;z-index:2;border-bottom:1px solid transparent;border-radius:10px 10px 0 0;}
.dg:not(.folded) .dg-h{border-bottom-color:var(--line);}
.dg-h:hover{background:rgba(216,182,120,.10);}
.dg-h b{font-size:13px;color:var(--pri);}
.dg-count{font-size:11px;color:var(--sub);margin-left:auto;background:rgba(0,0,0,.04);border-radius:10px;padding:1px 8px;}
.dg-caret{display:inline-block;width:12px;color:var(--sub);font-size:11px;}
/* 展开即完整显示，不设高度上限、不在组内滚动 */
.dg-body{padding:2px 0;}
</style>
