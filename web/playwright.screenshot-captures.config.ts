import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  { name: 'desktop', dependencies: ['setup'], testMatch: /\.spec\.ts$/, testIgnore: [/mobile/, /00-setup\.spec\.ts/],
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'mobile', dependencies: ['setup'], testMatch: /mobile.*\.spec\.ts$/,
    use: { ...base.projects![4].use, viewport: { width: 390, height: 844 } } },
] });
