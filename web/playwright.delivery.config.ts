import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  workers: 1,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'delivery', dependencies: ['setup'], testMatch: /76-orchestrator-delivery\.spec\.ts/,
      use: base.projects![3].use },
  ],
  webServer: {
    ...base.webServer!,
    // The default server with the delivery slice's own switch on too (the preview's is on in e2e_server.py).
    command: (base.webServer as { command: string }).command.replace('cd .. && ', 'cd .. && ACC_ORCHESTRATOR_V2_DELIVERY=1 '),
  },
});
