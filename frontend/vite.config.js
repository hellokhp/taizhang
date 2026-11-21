import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发时 npm run dev（自带 /api 代理到本机 8000 后端）；
// 生产时 npm run build 产出 dist/，由 Nginx 或 server/main.py 托管。
export default defineConfig({
  plugins: [vue()],
  // @ = src 绝对路径。新代码统一用 '@/views/...' '@/stores/...' '@/api/...'：
  // 解析基准固定在 src，文件在域子目录之间移动时 import 不用改。
  // 旧相对路径不受影响，可继续共存、渐进迁移。编辑器补全见 jsconfig.json。
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    host: true, // 监听 0.0.0.0，允许局域网内其他电脑通过内网 IP 访问
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
