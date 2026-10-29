import { defineConfig } from '@playwright/test';
import base from './playwright.config';

const legacy = process.env.ACC_ORCH_INTEGRATION_LEGACY === '1';
const server = base.webServer as { command: string };
export default defineConfig({
  ...base, workers: 1, globalTeardown: './e2e/brief-teardown.ts',
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    { name: 'orch-integration', dependencies: ['setup'],
      testMatch: legacy ? /(?:19-updates|21-team|74-team-tree|75-gates|75-verification)\.spec\.ts/
        : /(?:76-orchestrator-delivery|80-orchestrator-breakit|86-orchestrate-guide-usability|79-orchestrate-walkthrough|91-phone-pass-mobile|21-team|74-team-tree|89-image-access|72-worklist|77-orchestrator-switch|78-plan|80-admin-features|88-orch-d-planning|88-orchestrator-assignments|89-orch-ux|90-orch-scope-notice|project-brief)\.spec\.ts/,
      use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  ],
  webServer: { ...base.webServer!, command: server.command
    .replace('cd .. && ', `cd .. && ACC_ORCHESTRATOR_V2=${legacy ? '0' : '1'} ACC_ORCHESTRATOR_V2_DELIVERY=1 `) },
});
