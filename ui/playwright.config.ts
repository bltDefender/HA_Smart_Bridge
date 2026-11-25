import { defineConfig } from '@playwright/test';

// Lokal mit PW_CHANNEL=chrome (System-Chrome); in CI wird Chromium von Playwright installiert.
export default defineConfig({
  testDir: 'e2e',
  webServer: {
    command: 'npm run build && npx vite preview --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI
  },
  use: {
    baseURL: 'http://127.0.0.1:4173',
    channel: process.env.PW_CHANNEL || undefined
  }
});
