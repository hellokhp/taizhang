import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时 npm run dev（自带 /api 代理到本机 8000 后端）；
// 生产时 npm run build 产出 dist/，由 Nginx 或 server/main.py 托管。
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
