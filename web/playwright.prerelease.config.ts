import { defineConfig, devices } from '@playwright/test';
import base from './playwright.delivery.config';
export default defineConfig({ ...base, workers: 1, projects: [base.projects![0],
  {name:'prerelease', dependencies:['setup'], testMatch:/19-updates\.spec\.ts/,
    use:{...devices['Desktop Chrome']}}] });
