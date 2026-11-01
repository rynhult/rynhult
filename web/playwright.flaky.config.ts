import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

// Focused repeats need only setup. One worker also keeps global-setting repeats from overlapping.
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0], {
  name: 'flaky', dependencies: ['setup'],
  testMatch: [ /86-orchestrate-guide-usability\.spec\.ts/, /88-orch-d-planning\.spec\.ts/,
    /93-break-it-15\.spec\.ts/, /92-ui-small\.spec\.ts/, /terminal-input\.spec\.ts/,
    /75-js-payload\.spec\.ts/, /project-brief\.spec\.ts/, /21-team\.spec\.ts/, /22-pull-request\.spec\.ts/,
    /99-project-delete\.spec\.ts/, /19-issue-sources\.spec\.ts/, /34-automation\.spec\.ts/,
    /75-verification\.spec\.ts/, /80-orchestrator-breakit\.spec\.ts/, /89-orch-ux\.spec\.ts/,
    /90-orch-scope-notice\.spec\.ts/, /team-tabs\.spec\.ts/ ],
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
}] });
