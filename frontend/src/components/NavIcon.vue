<script setup>
/* 侧栏图标集中管理：布局菜单按 name 取对应 SVG path */
defineProps({ name: { type: String, default: 'doc' } });
</script>

<template>
  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"
       stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <template v-if="name === 'grid'">
      <rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/>
      <rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/>
    </template>
    <template v-else-if="name === 'bell'">
      <path d="M8 2.1c-2.5 0-4.1 1.9-4.1 4.3 0 2.2-.6 3.4-1.3 4.3h10.8c-.7-.9-1.3-2.1-1.3-4.3 0-2.4-1.6-4.3-4.1-4.3Z"/>
      <path d="M6.4 13.1c.3.7.9 1.1 1.6 1.1s1.3-.4 1.6-1.1"/>
    </template>
    <template v-else-if="name === 'table'">
      <rect x="2" y="2.6" width="12" height="10.8" rx="1.6"/><path d="M2 6h12M6 6v7.4"/>
    </template>
    <template v-else-if="name === 'calendar'">
      <rect x="2" y="3" width="12" height="11" rx="1.6"/><path d="M2 6.4h12M5.4 1.6v2.8M10.6 1.6v2.8"/>
    </template>
    <template v-else-if="name === 'users'">
      <circle cx="5.4" cy="5" r="2.3"/><path d="M1.6 13.7c.5-2.4 2-3.7 3.8-3.7s3.3 1.3 3.8 3.7"/>
      <circle cx="11.6" cy="5.8" r="1.8"/><path d="M9.8 13.7c.2-1.9 1.3-3 2.9-3 1.1 0 2 .6 2.6 1.7"/>
    </template>
    <template v-else-if="name === 'report'">
      <rect x="2" y="3" width="12" height="11" rx="1.6"/><path d="M2 6.4h12M5.4 1.6v2.8M10.6 1.6v2.8"/>
      <path d="M9.8 10.9l2.4-2.4 1.2 1.2-2.4 2.4-1.5.3.3-1.5Z"/>
    </template>
    <template v-else-if="name === 'folder'">
      <path d="M1.8 4.2c0-.9.7-1.6 1.6-1.6h2.5l1.4 1.7h5c.9 0 1.6.7 1.6 1.6v6.3c0 .9-.7 1.6-1.6 1.6H3.4c-.9 0-1.6-.7-1.6-1.6V4.2Z"/>
      <path d="M1.8 7.4h12.4"/>
    </template>
    <template v-else-if="name === 'doc'">
      <rect x="2" y="2.4" width="12" height="11.2" rx="1.6"/><path d="M2 6h12M5.6 6v7.6"/>
      <path d="M8 9.2h3.4M8 11.4h2.2"/>
    </template>
    <template v-else-if="name === 'globe'">
      <circle cx="8" cy="8" r="5.8"/><path d="M2.4 8h11.2"/>
      <path d="M8 2.2c1.6 1.7 2.4 3.7 2.4 5.8S9.6 12.1 8 13.8c-1.6-1.7-2.4-3.7-2.4-5.8S6.4 3.9 8 2.2Z"/>
    </template>
    <template v-else-if="name === 'list'">
      <path d="M3.2 2.6h9.6M3.2 8h9.6M3.2 13.4h6.2"/><circle cx="12.8" cy="12.9" r="1.5"/>
    </template>
    <template v-else-if="name === 'user'">
      <circle cx="8" cy="5.2" r="2.7"/><path d="M2.6 13.8c.7-2.7 2.9-4.2 5.4-4.2s4.7 1.5 5.4 4.2"/>
    </template>
    <template v-else-if="name === 'shield'">
      <path d="M8 1.8 12.6 4v4.2c0 3-2 5.2-4.6 6-2.6-.8-4.6-3-4.6-6V4L8 1.8Z"/>
      <path d="M6.2 8.1l1.3 1.4 2.3-2.6"/>
    </template>
    <template v-else>
      <circle cx="8" cy="8" r="5.5"/>
    </template>
  </svg>
</template>
