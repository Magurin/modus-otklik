import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  cacheDir: 'D:/Ackrate/.tool-cache/tmp/vite-modus',
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VITE_PORT || 5180),
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API || 'http://127.0.0.1:8010',
        changeOrigin: true,
        // бэкенд строит ссылки по адресу, на который пришел запрос: передаем ему адрес фронтенда, а не свой порт
        configure: (proxy) => proxy.on('proxyReq', (req, src) => req.setHeader('X-Forwarded-Host', src.headers.host)),
      },
    },
  },
})
