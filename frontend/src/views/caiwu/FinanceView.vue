<template>
  <div class="finance-page">
    <h2>财务管理</h2>
    <p class="hint">模块骨架 · 按业务填充。权限点：view.finance（需先在后端 PERM_CATALOG 登记）</p>

    <button @click="loadItems" :disabled="finance.loading">刷新列表</button>

    <table v-if="finance.items.length">
      <thead><tr><th>ID</th><th>标题</th><th>金额</th></tr></thead>
      <tbody>
        <tr v-for="it in finance.items" :key="it.id">
          <td>{{ it.id }}</td><td>{{ it.title }}</td><td>{{ it.amount }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else>暂无数据</p>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { finance, loadItems } from '@/stores/caiwu/finance'

onMounted(loadItems)
</script>

<style scoped>
.finance-page { padding: 16px; }
.hint { color: #888; font-size: 13px; }
table { border-collapse: collapse; margin-top: 12px; width: 100%; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
</style>
