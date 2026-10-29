import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

// Focused terminal-input and lazy-loading neighbours, without installation-wide stateful projects.
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0], {
  name: 'terminal-misc', dependencies: ['setup'],
  testMatch: [/terminal-(?:input|polling)\.spec\.ts/,
    /01-units\.spec\.ts/, /80-chunk-recovery\.spec\.ts/],
  use: { ...devices['Desktop Chrome'] },
}] });
