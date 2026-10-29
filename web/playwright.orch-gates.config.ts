import { defineConfig, devices } from '@playwright/test';
import base from './playwright.delivery.config';
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0],
  { name: 'orch-gates', dependencies: ['setup'],
    testMatch: /(?:80-orchestrator-breakit|86-orchestrate-guide-usability|88-orch-d-planning|90-orch-final-ux|project-brief)\.spec\.ts/,
    use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
] });
