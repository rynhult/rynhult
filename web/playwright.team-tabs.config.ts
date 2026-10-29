import { defineConfig, devices } from '@playwright/test';
import base from './playwright.delivery.config';
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0],
  { name: 'team-tabs', dependencies: ['setup'], testMatch: /(?:team-tabs|project-brief|21-team|72-worklist|74-team-tree|78-plan|79-orchestrate-walkthrough|80-orchestrator-breakit|86-orchestrate-guide-usability|88-orchestrator-assignments|88-orch-d-planning|89-orch-ux|89-break-it-13|90-orch-final-ux|92-break-it-14)\.spec\.ts/, use: { ...devices['Desktop Chrome'] } },
  { name: 'team-tabs-retained', dependencies: ['setup'], testMatch: /48-ux-final-states\.spec\.ts/, grep: /Team describes helper permissions/, use: { ...devices['Desktop Chrome'] } },
  { name: 'team-tabs-phone', dependencies: ['setup'], testMatch: /(?:team-tabs|72-worklist)\.spec\.ts/, use: { ...devices['Pixel 7'], viewport: { width: 390, height: 844 } } },
] });
