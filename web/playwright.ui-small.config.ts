import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  ...[1440, 390].map(width => ({ name: `ui-small-${width}`, dependencies: ['setup'],
    testMatch: /(?:92-ui-small|93-settings-small)\.spec\.ts/, use: { ...base.projects![3].use, viewport: { width, height: 900 } } })),
  { name: 'related', dependencies: ['setup'], testMatch: /(?:26-ux-inbox|37-projects-start)\.spec\.ts/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'settings-related', dependencies: ['setup'], testMatch: /83-settings-focus\.spec\.ts/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'updates-related', dependencies: ['setup'], testMatch: /19-updates\.spec\.ts/, grep: /owners check, confirm and schedule updates/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  { name: 'audit-related', dependencies: ['setup'], testMatch: /60-sso-followups\.spec\.ts/, grep: /owner can read company failure diagnostics/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
] });
