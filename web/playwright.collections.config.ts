import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  { name: 'collections', dependencies: ['setup'], testMatch: /(?:collections-table|92-ui-small|37-projects-start|69-ux-remaining-workspaces|95-ws-delete)\.spec\.ts/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'collections-touch-phone', dependencies: ['setup'], testMatch: /collections-table\.spec\.ts/, grep: /390/,
    use: { ...base.projects![4].use, viewport: { width: 390, height: 900 } } },
] });
