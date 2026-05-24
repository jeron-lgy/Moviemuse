import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/subtitles/assets/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: 'index.html'
    }
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:18180',
      '/subtitles': 'http://127.0.0.1:18180'
    }
  }
})
