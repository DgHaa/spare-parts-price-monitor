import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端开发时把 /api 代理到后端(默认 :8000)，避免跨域
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' }
  }
})
