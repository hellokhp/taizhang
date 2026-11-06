/* ================================================================
   用户管理 + 角色权限（仅管理员）
   ================================================================ */
import { reactive } from 'vue'
import { api } from '../api/client'
import { auth } from './auth'
import { toast } from './bid'

export const um = reactive({open:false, list:[], err:''});
export const uform = reactive({isEdit:false, name:'', display:'', password:'', admin:false, role:'staff'});

/* 角色与权限 */
export const roles = reactive({list:[], catalog:{perms:[], fields:[]}, err:''});
export const rm = reactive({open:false, isEdit:false, key:'', name:'', isSystem:false, perms:{}, fieldPerms:{}, dataScope:'ALL'});

export async function loadUsers(){
  try{ um.list = (await api('/api/users')).users || []; }
  catch(e){ toast(e.message); }
}
export function openNewUser(){
  uform.isEdit = false; uform.name = ''; uform.display = ''; uform.password = ''; uform.admin = false; uform.role = 'staff';
  um.err = ''; um.open = true;
}
export function openEditUser(u){
  uform.isEdit = true; uform.name = u.username; uform.display = u.displayName;
  uform.password = ''; uform.admin = !!u.isAdmin; uform.role = u.roleKey || 'staff';
  um.err = ''; um.open = true;
}
export async function saveUser(){
  um.err = '';
  if(!uform.name.trim()){ um.err = '请填写账号'; return; }
  if(!uform.isEdit && !uform.password){ um.err = '请填写密码'; return; }
  try{
    if(uform.isEdit){
      await api('/api/users/' + encodeURIComponent(uform.name), {method:'PUT',
        body: JSON.stringify({password: uform.password, display_name: uform.display,
          is_admin: uform.admin, role_key: uform.role})});
      if(auth.user && uform.name === auth.user.username) auth.user.displayName = uform.display || uform.name;
      toast('已保存');
    }else{
      await api('/api/users', {method:'POST',
        body: JSON.stringify({username: uform.name.trim(), password: uform.password,
          display_name: uform.display, is_admin: uform.admin, role_key: uform.role})});
      toast('已创建');
    }
    um.open = false;
    loadUsers();
  }catch(e){ um.err = e.message; }
}
export async function delUser(u){
  if(!confirm('确认删除用户「' + (u.displayName || u.username) + '」？')) return;
  try{
    await api('/api/users/' + encodeURIComponent(u.username), {method:'DELETE'});
    toast('已删除');
    loadUsers();
  }catch(e){ toast(e.message); }
}

/* ---------------- 角色 ---------------- */
export async function loadRoles(){
  try{
    const [r1, r2] = await Promise.all([api('/api/roles'), api('/api/perm/catalog')]);
    roles.list = r1.roles || [];
    roles.catalog = r2;
  }catch(e){ toast(e.message); }
}

export function openNewRole(){
  rm.isEdit = false; rm.key = ''; rm.name = ''; rm.isSystem = false; rm.perms = {}; rm.fieldPerms = {};
  rm.dataScope = 'ALL';
  roles.err = ''; rm.open = true;
}
export function openEditRole(r){
  rm.isEdit = true; rm.key = r.key; rm.name = r.name; rm.isSystem = !!r.isSystem;
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
      await api('/api/roles/' + encodeURIComponent(rm.key), {method:'PUT',
        body: JSON.stringify({name: rm.name, perms: rm.perms, field_perms: rm.fieldPerms,
          data_scope: rm.dataScope})});
    }else{
      await api('/api/roles', {method:'POST',
        body: JSON.stringify({key: rm.key.trim(), name: rm.name,
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
    await api('/api/roles/' + encodeURIComponent(r.key), {method:'DELETE'});
    toast('已删除');
    loadRoles();
  }catch(e){ toast(e.message); }
}
