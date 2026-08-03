import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// This app is Tier-2 (container) — there is no "integrated plugin bundle"
// build target the way Tier-1 apps have (see aw-app-template's dual
// plugin/standalone modes). aw-workspace reverse-proxies the whole
// container at /api/apps/kb (stripping that prefix before it reaches us —
// see aw-app.json's windows/main.json iframe + src/client.js's relative
// fetch paths), so a single ordinary app build is enough.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // Dev-only convenience: `npm run dev` proxies API calls to the
      // container's own backend (running separately, e.g. via `docker
      // compose` or `python -m kb_app.main` locally).
      '/api/kb': 'http://127.0.0.1:8000',
      '/mcp': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
