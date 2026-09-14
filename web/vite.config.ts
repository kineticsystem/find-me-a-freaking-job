import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the API runs separately on :8099 (`python -m jobfinder serve`).
// In production FastAPI serves web/dist itself, so no proxy is needed.
const API = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8099'
const apiPaths = ['/jobs', '/health', '/runs', '/sources', '/digest', '/preferences', '/reload']

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // reachable from a phone on the same network during development
    proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: API, changeOrigin: true }])),
  },
})
