import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({ ...base, workers: 1, projects: [base.projects![0], {
  name: 'files', dependencies: ['setup'], testMatch: /12-files\.spec\.ts/,
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
}] });
