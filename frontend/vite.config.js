import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  cacheDir: 'D:/Ackrate/.tool-cache/tmp/vite-modus',
  server: {
    host: '127.0.0.1',
    port: 5180,
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:8010' },
  },
})
