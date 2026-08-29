import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // The browser talks to Vite, Vite forwards /api to nginx, and nginx load
    // balances across the two API replicas. Keeping the origin single avoids
    // CORS entirely in development.
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
