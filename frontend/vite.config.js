import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const frontendDir = fileURLToPath(new URL('./', import.meta.url))

export default defineConfig({
  plugins: [react()],
  root: frontendDir,
  build: {
    // Vercel + FastAPI sert les assets statiques depuis public/ (doc officielle).
    outDir: '../public',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
