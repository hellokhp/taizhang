/* ================================================================
   用户管理 + 角色权限（仅管理员）—— 按当前系统隔离
   用户按 dept 归属系统、角色按 system 归属、权限目录按系统过滤。
   ================================================================ */
import { reactive } from 'vue'
import { api } from '@/api/client'
import { auth } from '@/stores/common/auth'
import { toast } from '@/stores/taizhang/bid'
import { sys } from '@/stores/common/system'
import { SYSTEMS } from '@/config/systems'

/* 系统 → 默认部门（新建用户时预选） */
const SYS_DEPT = { taizhang: 'bid', finance: 'finance', design: 'design', admin: 'management' };
function curSystem(){ return sys.current || 'taizhang'; }
/* 管理中心（admin）走 system=all 拿全量用户/角色/权限目录；其余系统按自身隔离 */
function isCenter(){ return curSystem() === 'admin'; }
function sysQuery(){ return '?system=' + encodeURIComponent(isCenter() ? 'all' : curSystem()); }
/* 新建实体的归属系统：管理中心需按用户所选具体系统，默认 taizhang；其余用当前系统 */
function targetSystem(){ return isCenter() ? (rm.system || 'taizhang') : curSystem(); }

export const um = reactive({open:false, list:[], departments:[], err:''});
export const uform = reactive({isEdit:false, name:'', display:'', password:'', admin:false, role:'staff', dept:'bid'});

/* 角色与权限 */
export const roles = reactive({list:[], catalog:{perms:[], fields:[], scopes:[]}, err:''});
export const rm = reactive({open:false, isEdit:false, key:'', name:'', isSystem:false, system:'taizhang', perms:{}, fieldPerms:{}, dataScope:'ALL'});

/* 当前系统可选的角色（用于新建用户时的角色下拉）。
   管理中心显示全量角色（标注系统）供选择。 */
export function roleOptions(){
  if(isCenter()) return roles.list.slice();
  const s = curSystem();
  return roles.list.filter(r => (r.system || 'taizhang') === s);
}

export async function loadUsers(){
  try{
    const [u, cat] = await Promise.all([api('/api/users' + sysQuery()), api('/api/perm/catalog' + sysQuery())]);
    um.list = u.users || [];
    um.departments = cat.departments || [];
  }
  catch(e){ toast(e.message); }
}
export function openNewUser(){
  uform.isEdit = false; uform.name = ''; uform.display = ''; uform.password = ''; uform.admin = false;
  const opts = roleOptions();
  uform.role = opts.length ? opts[0].key : '';
  uform.dept = isCenter() ? 'bid' : (SYS_DEPT[curSystem()] || 'bid');
  um.err = ''; um.open = true;
}
export function openEditUser(u){
  uform.isEdit = true; uform.name = u.username; uform.display = u.displayName;
  uform.password = ''; uform.admin = !!u.isAdmin; uform.role = u.roleKey || 'staff';
  uform.dept = u.dept || 'bid';
  um.err = ''; um.open = true;
}
export async function saveUser(){
  um.err = '';
  if(!uform.name.trim()){ um.err = '请填写账号'; return; }
  if(!uform.isEdit && !uform.password){ um.err = '请填写密码'; return; }
  try{
    if(uform.isEdit){
      await api('/api/users/' + encodeURIComponent(uform.name) + sysQuery(), {method:'PUT',
        body: JSON.stringify({password: uform.password, display_name: uform.display,
          is_admin: uform.admin, role_key: uform.role, dept: uform.dept})});
      if(auth.user && uform.name === auth.user.username) auth.user.displayName = uform.display || uform.name;
      toast('已保存');
    }else{
      await api('/api/users' + sysQuery(), {method:'POST',
        body: JSON.stringify({username: uform.name.trim(), password: uform.password,
          display_name: uform.display, is_admin: uform.admin, role_key: uform.role,
          dept: uform.dept})});
      toast('已创建');
    }
    um.open = false;
    loadUsers();
  }catch(e){ um.err = e.message; }
}
export async function delUser(u){
  if(!confirm('确认删除用户「' + (u.displayName || u.username) + '」？')) return;
  try{
    await api('/api/users/' + encodeURIComponent(u.username) + sysQuery(), {method:'DELETE'});
    toast('已删除');
    loadUsers();
  }catch(e){ toast(e.message); }
}

/* ---------------- 角色 ---------------- */
export async function loadRoles(){
  try{
    const [r1, r2] = await Promise.all([api('/api/roles' + sysQuery()), api('/api/perm/catalog' + sysQuery())]);
    roles.list = r1.roles || [];
    roles.catalog = r2;
    /* 已打开编辑框时，若当前系统切换，关闭避免跨系统误改 */
    if(rm.open && rm.isEdit && rm.system && rm.system !== curSystem()){ rm.open = false; }
  }catch(e){ toast(e.message); }
}

export function openNewRole(){
  rm.isEdit = false; rm.key = ''; rm.name = ''; rm.isSystem = false;
  rm.system = isCenter() ? 'taizhang' : curSystem();   // 角色归属具体系统，管理中心默认台账
  rm.perms = {}; rm.fieldPerms = {}; rm.dataScope = 'ALL';
  roles.err = ''; rm.open = true;
}
export function openEditRole(r){
  rm.isEdit = true; rm.key = r.key; rm.name = r.name; rm.isSystem = !!r.isSystem;
  rm.system = r.system || 'taizhang';
  rm.perms = Object.assign({}, r.perms); rm.fieldPerms = Object.assign({}, r.fieldPerms);
  rm.dataScope = r.dataScope || 'ALL';
  roles.err = ''; rm.open = true;
}
export async function saveRole(){
  roles.err = '';
  if(!rm.isEdit && !rm.key.trim()){ roles.err = '请填写角色标识（英文，如 bid_lead）'; return; }
  if(!rm.name.trim()){ roles.err = '请填写角色名称'; return; }
  try{
    if(rm.isEdit){
      await api('/api/roles/' + encodeURIComponent(rm.key) + sysQuery(), {method:'PUT',
        body: JSON.stringify({name: rm.name, perms: rm.perms, field_perms: rm.fieldPerms,
          data_scope: rm.dataScope, system: rm.system})});
    }else{
      await api('/api/roles' + sysQuery(), {method:'POST',
        body: JSON.stringify({key: rm.key.trim(), name: rm.name, system: rm.system,
          perms: rm.perms, field_perms: rm.fieldPerms, data_scope: rm.dataScope})});
    }
    rm.open = false;
    toast('已保存');
    loadRoles();
  }catch(e){ roles.err = e.message; }
}
export async function delRole(r){
  if(!confirm('确认删除角色「' + r.name + '」？')) return;
  try{
    await api('/api/roles/' + encodeURIComponent(r.key) + sysQuery(), {method:'DELETE'});
    toast('已删除');
    loadRoles();
  }catch(e){ toast(e.message); }
}
