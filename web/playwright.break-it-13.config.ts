import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base, workers: 1,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'break-it-13', dependencies: ['setup'], testMatch: /89-break-it-13\.spec\.ts/,
      use: { ...base.projects![3].use, viewport: { width: 390, height: 900 } } },
    { name: 'assignments', dependencies: ['setup'], testMatch: /88-orchestrator-assignments\.spec\.ts/,
      use: base.projects![3].use },
  ],
  webServer: { ...base.webServer!, command: (base.webServer as { command: string }).command
    .replace('cd .. && ', 'cd .. && ACC_ORCHESTRATOR_V2=1 ACC_ORCHESTRATOR_V2_DELIVERY=1 ') },
});
