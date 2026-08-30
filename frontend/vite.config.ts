import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const runtimeEnv = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env || {}
const apiPort = runtimeEnv.QUANT_LAB_API_PORT || '8765'

export default defineConfig({
  plugins: [react()],
  // pnpm's isolated links can point at a store outside the project. Vite's
  // automatic esbuild dependency crawl follows those links before the server
  // is ready and can fail on locked-down Windows machines. The application
  // has only two small runtime dependencies, so let Vite transform them on
  // demand instead of making startup depend on a pre-bundle.
  optimizeDeps: {
    // noDiscovery is supported by current Vite releases and avoids an
    // automatic dependency crawl through protected pnpm links.
    noDiscovery: true,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
})
