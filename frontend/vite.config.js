import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// amazon-chime-sdk-js references the Node `global` object; map it to the browser's globalThis.
// The dev server's default SPA fallback serves index.html for /call/... (patient video links).
export default defineConfig({
  plugins: [react()],
  define: {
    global: 'globalThis'
  },
  build: {
    // The Chime SDK chunk (~1.4 MB, lazy-loaded only for real calls) is expected to be large.
    chunkSizeWarningLimit: 1500
  },
  optimizeDeps: {
    esbuildOptions: {
      define: { global: 'globalThis' }
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true
      }
    }
  }
});
