import { defineConfig } from 'vite'
import { resolve } from 'path'

export default defineConfig({
  root: '.',
  server: {
    port: 5173,
    allowedHosts: ['powwow-uncharted-uniformed.ngrok-free.dev'],
    proxy: {
      '/api': {
        target: 'http://localhost:8500',
        changeOrigin: true,
      },
      '/uploads': {
        target: 'http://localhost:8500',
        changeOrigin: true,
      },
    },
  },
  build: {
    lib: {
      entry: resolve(__dirname, 'src/repair-agent.ts'),
      formats: ['iife'],
      name: 'RepairAgent',
      fileName: () => 'repair-agent.js',
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
    
    outDir: 'dist',
  },
})
