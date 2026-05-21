import { defineConfig } from 'vite'
import { resolve } from 'path'

export default defineConfig({
  root: '.',
  server: {
    port: 5173,
    proxy: {
      '/api': {
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
