<script setup>
import { useRoute, useRouter } from 'vue-router'
import { ST } from '../stores/bid'
import { can } from '../stores/perm'

const route = useRoute();
const router = useRouter();
</script>

<template>
  <nav class="nav">
    <button v-if="can('view.dashboard')" :class="{on: route.path === '/'}" @click="router.push('/')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/><rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/></svg>
      首页看板
    </button>
    <button v-if="can('view.manage')" :class="{on: route.path === '/manage'}" @click="router.push('/manage')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2.6" width="12" height="10.8" rx="1.6"/><path d="M2 6h12M6 6v7.4"/></svg>
      投标管理
      <span class="badge">{{ ST.rows.length }}</span>
    </button>
    <button v-if="can('view.users')" :class="{on: route.path === '/users'}" @click="router.push('/users')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="5.2" r="2.7"/><path d="M2.6 13.8c.7-2.7 2.9-4.2 5.4-4.2s4.7 1.5 5.4 4.2"/></svg>
      用户管理
    </button>
    <button v-if="can('view.roles')" :class="{on: route.path === '/roles'}" @click="router.push('/roles')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M8 1.8 12.6 4v4.2c0 3-2 5.2-4.6 6-2.6-.8-4.6-3-4.6-6V4L8 1.8Z"/><path d="M6.2 8.1l1.3 1.4 2.3-2.6"/></svg>
      角色权限
    </button>
    <button v-if="can('view.audit')" :class="{on: route.path === '/audit'}" @click="router.push('/audit')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3.2 2.6h9.6M3.2 8h9.6M3.2 13.4h6.2"/><circle cx="12.8" cy="12.9" r="1.5" fill="none"/></svg>
      审计日志
    </button>
  </nav>
</template>
