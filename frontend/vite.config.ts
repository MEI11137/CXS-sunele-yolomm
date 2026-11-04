import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发时前端统一走 /api 代理到本机后端；生产由后端同源托管 dist，不写死端口。
const BACKEND = process.env.VITE_BACKEND_ORIGIN || 'http://127.0.0.1:5080'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/api': { target: BACKEND, changeOrigin: false },
    },
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
  },
})
