<script setup>
import { onMounted } from 'vue'
import { um, uform, loadUsers, openNewUser, openEditUser, saveUser, delUser, roles, loadRoles } from '../stores/users'
import { auth } from '../stores/auth'

onMounted(() => { loadUsers(); loadRoles(); });
function roleName(u){ if(u.isAdmin) return '管理员（全部权限）'; const r = roles.list.find(x => x.key === u.roleKey); return r ? r.name : (u.roleKey || '投标专员'); }
</script>

<template>
  <div>
    <div class="card" style="margin-bottom:14px;">
      <div class="card-h" style="padding-bottom:12px;">
        <h2>用户管理</h2>
        <button class="btn pri" @click="openNewUser()">＋ 新增用户</button>
      </div>
      <div class="card-b">
        <div class="tbl-wrap" style="max-height:480px;">
          <table class="users-tbl">
            <thead><tr><th>账号</th><th>显示名</th><th>角色</th><th>类型</th><th style="text-align:right">操作</th></tr></thead>
            <tbody>
              <tr v-for="u in um.list" :key="u.username">
                <td><b>{{ u.username }}</b><span v-if="auth.user && u.username === auth.user.username" style="color:var(--sub);margin-left:6px">（当前登录）</span></td>
                <td>{{ u.displayName }}</td>
                <td>{{ roleName(u) }}</td>
                <td><span v-if="u.isAdmin" style="color:#A87F32;font-weight:600">管理员</span><span v-else style="color:var(--mut)">普通用户</span></td>
                <td style="text-align:right"><span class="act" @click="openEditUser(u)">编辑</span><span class="act del" @click="delUser(u)">删除</span></td>
              </tr>
              <tr v-if="!um.list.length"><td colspan="5" style="color:var(--sub)">暂无用户</td></tr>
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
            <div class="fld"><label>角色{{ uform.admin ? '（管理员已拥有全部权限）' : '' }}</label>
              <select v-model="uform.role" :disabled="uform.admin">
                <option v-for="r in roles.list" :key="r.key" :value="r.key">{{ r.name }}</option>
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
