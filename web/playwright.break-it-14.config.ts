import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

// Break-it 14: careless-new-user probes on a fresh fixture install, sequential, desktop and phone.
export default defineConfig({
  ...base, workers: 1,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'break-it-14', dependencies: ['setup'], testMatch: /92-break-it-14\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'break-it-14-phone', dependencies: ['setup'], testMatch: /92-break-it-14\.spec\.ts/, use: { ...devices['Pixel 7'] } },
    { name: 'related', dependencies: ['setup'],
      testMatch: [/project-brief\.spec\.ts/, /78-plan\.spec\.ts/, /88-orch-d-planning\.spec\.ts/, /89-orch-ux\.spec\.ts/, /88-orchestrator-assignments\.spec\.ts/],
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
