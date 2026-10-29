import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  { name: 'desktop', dependencies: ['setup'], testMatch: /(?:10-app|49-ux-final-shots|51-ux-final-guide-refresh|collections-table|92-publishing-tabs|team-tabs|project-brief|68-ux-remaining-admin|39-providers|26-ux-inbox|92-ui-small)\.spec\.ts/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'phone', dependencies: ['setup'], testMatch: /team-tabs\.spec\.ts/,
    use: { ...base.projects![4].use, viewport: { width: 390, height: 844 } } },
] });
