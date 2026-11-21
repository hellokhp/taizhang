/* 财务模块状态 + API 调用（方案 B 框架 · 参照 stores/aiSummary.js） */
import { reactive } from 'vue'
import { api } from '@/api/client'

export const finance = reactive({
  items: [],
  loading: false,
});

export async function loadItems() {
  finance.loading = true;
  try {
    // 路径走 api() 封装：client.js 的 rewritePath 会自动处理域映射；
    // 后端 finance_router 挂在 /api/finance，同时兼容 /api 旧前缀。
    const r = await api('/api/finance/items');
    finance.items = r.items || [];
  } finally {
    finance.loading = false;
  }
}
