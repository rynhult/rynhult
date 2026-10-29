import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  { name: 'providers', dependencies: ['setup'], repeatEach: 3,
    testMatch: /(?:15-availability|39-providers|68-ux-remaining-admin|40-host-machines|92-ui-small)\.spec\.ts/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
] });
