import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: proxy the Seam API (`seam serve`, :7860). Build: emit straight into the Python package so
// `seam serve` serves the UI from the same origin.
const api = 'http://127.0.0.1:7860'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': api, '/outputs': api, '/uploads': api, '/previews': api },
  },
  build: { outDir: '../src/seam/static/web', emptyOutDir: true },
})
