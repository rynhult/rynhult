import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

// Disposable pass-15 journeys; isolated from the installation-wide full gate.
export default defineConfig({
  ...base, workers: 1,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'break-it-15', dependencies: ['setup'],
      testMatch: [/project-brief\.spec\.ts/, /lead-kickoff\.spec\.ts/,
        /76-orchestrator-delivery\.spec\.ts/, /93-break-it-15\.spec\.ts/],
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
