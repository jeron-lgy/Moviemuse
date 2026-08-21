import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  root: 'worker',
  base: '/worker-assets/',
  build: {
    outDir: '../worker-dist',
    emptyOutDir: true
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:18181' }
  }
})
