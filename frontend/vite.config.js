import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const frontendDir = fileURLToPath(new URL('./', import.meta.url))

export default defineConfig({
  plugins: [react()],
  root: frontendDir,
  build: {
    // Vercel construit depuis la racine -> on sort le dist dans /dist racine.
    outDir: '../dist',
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
