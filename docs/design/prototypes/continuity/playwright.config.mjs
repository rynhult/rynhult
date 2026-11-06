import { createRequire } from 'node:module';
const require = createRequire(new URL('../../../../web/package.json', import.meta.url));
const { defineConfig } = require('@playwright/test');
export default defineConfig({
  testDir: '.', testMatch: 'continuity.spec.mjs', workers: 1, retries: 0,
  timeout: 45_000, outputDir: '../../../../.acc/continuity-results',
  reporter: 'list', use: { browserName: 'chromium', viewport: { width: 1440, height: 900 } },
});
