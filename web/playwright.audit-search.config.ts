import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  ...[1440, 390].map(width => ({ name: `audit-${width}`, dependencies: ['setup'],
    testMatch: /92-ui-small\.spec\.ts/, grep: /Audit/, use: { ...base.projects![3].use, viewport: { width, height: 900 } } })),
  { name: 'audit-related', dependencies: ['setup'], testMatch: /60-sso-followups\.spec\.ts/, grep: /owner can read company failure diagnostics/,
    use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
] });
