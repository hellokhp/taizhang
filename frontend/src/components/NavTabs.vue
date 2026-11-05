<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ST } from '../stores/bid'
import { buildAlerts } from '../lib/alerts'
import { can, canAgg } from '../stores/perm'

const route = useRoute();
const router = useRouter();

/* 待处理事项数（待开标 / 标书未准备 / 待缴费 / 待退费），用作侧栏角标 */
const pendingCnt = computed(() => buildAlerts(ST.rows, {
  bid: canAgg('bidStatus'), bond: canAgg('bondStatus'), perf: canAgg('perfStatus'), minor: false
}).length);
</script>

<template>
  <nav class="nav">
    <button v-if="can('view.dashboard')" :class="{on: route.path === '/'}" @click="router.push('/')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/><rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/></svg>
      首页看板
    </button>
    <button v-if="can('view.alerts')" :class="{on: route.path === '/alerts'}" @click="router.push('/alerts')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M8 2.1c-2.5 0-4.1 1.9-4.1 4.3 0 2.2-.6 3.4-1.3 4.3h10.8c-.7-.9-1.3-2.1-1.3-4.3 0-2.4-1.6-4.3-4.1-4.3Z"/><path d="M6.4 13.1c.3.7.9 1.1 1.6 1.1s1.3-.4 1.6-1.1"/></svg>
      需要处理
      <span v-if="pendingCnt" class="badge">{{ pendingCnt }}</span>
    </button>
    <button v-if="can('view.manage')" :class="{on: route.path === '/manage'}" @click="router.push('/manage')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2.6" width="12" height="10.8" rx="1.6"/><path d="M2 6h12M6 6v7.4"/></svg>
      投标管理
      <span class="badge">{{ ST.rows.length }}</span>
    </button>
    <button v-if="can('view.timeline')" :class="{on: route.path === '/timeline'}" @click="router.push('/timeline')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="3" width="12" height="11" rx="1.6"/><path d="M2 6.4h12M5.4 1.6v2.8M10.6 1.6v2.8"/></svg>
      标源总览
    </button>
    <button v-if="can('view.assign')" :class="{on: route.path === '/assign'}" @click="router.push('/assign')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="5.4" cy="5" r="2.3"/><path d="M1.6 13.7c.5-2.4 2-3.7 3.8-3.7s3.3 1.3 3.8 3.7"/><circle cx="11.6" cy="5.8" r="1.8"/><path d="M9.8 13.7c.2-1.9 1.3-3 2.9-3 1.1 0 2 .6 2.6 1.7"/></svg>
      项目分配表
    </button>
    <button v-if="can('view.reports')" :class="{on: route.path === '/reports'}" @click="router.push('/reports')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="12" height="11" rx="1.6"/><path d="M2 6.4h12M5.4 1.6v2.8M10.6 1.6v2.8"/><path d="M9.8 10.9l2.4-2.4 1.2 1.2-2.4 2.4-1.5.3.3-1.5Z"/></svg>
      工作日报
    </button>
    <button v-if="can('view.files')" :class="{on: route.path === '/files'}" @click="router.push('/files')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M1.8 4.2c0-.9.7-1.6 1.6-1.6h2.5l1.4 1.7h5c.9 0 1.6.7 1.6 1.6v6.3c0 .9-.7 1.6-1.6 1.6H3.4c-.9 0-1.6-.7-1.6-1.6V4.2Z"/><path d="M1.8 7.4h12.4"/></svg>
      文件存储
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
    <button :class="{on: route.path === '/profile'}" @click="router.push('/profile')">
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="5.6" r="2.6"/><path d="M2.8 13.8c.7-2.6 2.8-4 5.2-4s4.5 1.4 5.2 4"/></svg>
      个人中心
    </button>
  </nav>
</template>
