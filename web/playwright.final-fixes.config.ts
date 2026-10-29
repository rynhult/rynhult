import { defineConfig, devices } from '@playwright/test';
import base from './playwright.delivery.config';
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0],
  { name: 'final-fixes', dependencies: ['setup'],
    testMatch: /(?:10-app|37-projects-start|80-orchestrator-breakit|91-phone-pass-mobile|88-orch-d-planning|90-orch-final-ux|92-ui-small|project-brief|93-launch-hit-target)\.spec\.ts/,
    use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
] });
