import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';
import { environmentValue } from '../src/acc/env.mjs';
const webkit = environmentValue('ACC_E2E_WEBKIT_EXECUTABLE');
const port = new URL(base.use!.baseURL!).port;
export default defineConfig({
  ...base, workers: 1, testIgnore: [],
  use: { ...base.use, baseURL: `https://localhost:${port}`, ignoreHTTPSErrors: true },
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: { ...devices['Desktop Chrome'] } },
    { name: 'csrf-chromium', dependencies: ['setup'], testMatch: /csrf\.spec\.ts/, use: { ...devices['Desktop Chrome'] } },
    { name: 'csrf-webkit', dependencies: ['setup'], testMatch: /csrf\.spec\.ts/, use: { ...devices['Desktop Safari'], ...(webkit ? { launchOptions: { executablePath: webkit } } : {}) } },
  ],
  webServer: { ...base.webServer!,
    command: (base.webServer as { command: string }).command.replace('cd .. && ', 'cd .. && ACC_E2E_HTTPS=1 '),
    url: `https://127.0.0.1:${port}/api/health`, ignoreHTTPSErrors: true },
});
