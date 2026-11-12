import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址默认 8001。留一个环境变量口子，是因为本机常年有 Docker 容器占着 8001
// （agent-gatekeeper-web 映射 0.0.0.0:8001），本地直接跑 uvicorn 时只能换端口：
//   SSRM_API_TARGET=http://127.0.0.1:8010 npm run dev
const API_TARGET = process.env.SSRM_API_TARGET ?? 'http://127.0.0.1:8001'

// 开发态：前端 5173，后端 8001。
// 统一约定所有请求走 /api 前缀，由 Vite 代理转发到后端 —— 这样开发环境和
// 生产环境（nginx 反代）的请求路径完全一致，代码里不需要 if (dev) 分支。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
