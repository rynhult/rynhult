import { defineConfig } from '@playwright/test';
import base from './playwright.config';
import { environmentValue } from '../src/acc/env.mjs';

// Disposable, sequential UX walk; no state or port is shared with other lanes.
export default defineConfig({
  ...base, workers: 1,
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    ...[1440, 390].flatMap(width => (['dark', 'light'] as const).map(colorScheme => ({
      name: `orch-ux-${colorScheme === 'light' ? 'light-' : ''}${width}`, dependencies: ['setup'],
      testMatch: [/89-orch-ux\.spec\.ts/, /90-orch-final-ux\.spec\.ts/],
      ...(environmentValue('ORCH_UX_FOCUS') ? { grep: new RegExp(environmentValue('ORCH_UX_FOCUS')!) } : {}),
      use: { ...base.projects![3].use, viewport: { width, height: 900 }, colorScheme },
    }))),
    ...[1440, 390].flatMap(width => (['dark', 'light'] as const).map(colorScheme => ({
      name: `orch-first-run-${colorScheme}-${width}`, dependencies: ['setup'],
      testMatch: /29-first-run\.spec\.ts/, grep: /an empty install shows Get started/,
      use: { ...base.projects![3].use, viewport: { width, height: 900 }, colorScheme },
    }))),
    { name: 'orch-related', dependencies: ['setup'],
      testMatch: /(?:project-brief|88-orch-d-planning|88-orchestrator-assignments|86-orchestrate-guide-usability|75-gates|75-verification|29-first-run|22-pull-request|70-ux-remaining-session)\.spec\.ts/,
      use: { ...base.projects![3].use, viewport: { width: 1440, height: 900 } } },
  ],
});
