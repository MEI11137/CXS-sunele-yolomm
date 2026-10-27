import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 后端默认 5020（见 web-flask/config.py 的 FLASK_SERVICE_PORT）。
// 前端统一走同源 /api 代理：既避开跨域，也不用把端口写死在业务代码里。
const BACKEND = process.env.VITE_BACKEND_ORIGIN || 'http://127.0.0.1:5020'

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
      '/api': {
        target: BACKEND,
        changeOrigin: false,
        // 文件会话 cookie 需要原样透传
        cookieDomainRewrite: '',
      },
    },
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
  },
})
