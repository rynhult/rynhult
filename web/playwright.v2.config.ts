import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// The orchestrator v2 working list and plan on the e2e server too (ACC_ORCHESTRATOR_V2=1, under e2e_server.py's
// ACC_ORCHESTRATOR=1), for specs that drive a real lead through them (80-orchestrator-breakit, 86-orchestrate-guide-usability).
export default defineConfig({
  ...base,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'v2', dependencies: ['setup'], testMatch: [/80-orchestrator-breakit\.spec\.ts/, /86-orchestrate-guide-usability\.spec\.ts/], use: base.projects![3].use },
  ],
  webServer: {
    ...base.webServer!,
    command: (base.webServer as { command: string }).command.replace('cd .. && ', 'cd .. && ACC_ORCHESTRATOR_V2=1 '),
  },
});
