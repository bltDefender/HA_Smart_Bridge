import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// Relative Basis: die Assets liegen unter /ui und funktionieren auch im HA-iframe.
export default defineConfig({
  base: './',
  plugins: [svelte()],
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: { proxy: { '/ui/api': 'http://127.0.0.1:8080' } },
  test: { include: ['src/**/*.test.ts'] }
});
