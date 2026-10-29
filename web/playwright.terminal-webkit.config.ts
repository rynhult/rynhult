import { defineConfig, devices } from '@playwright/test';
import { environmentValue } from '../src/acc/env.mjs';
import base from './playwright.config';
const executablePath = environmentValue('ACC_E2E_WEBKIT_EXECUTABLE');
const firefoxPath = environmentValue('ACC_E2E_FIREFOX_EXECUTABLE');
export default defineConfig({ ...base, projects: [base.projects![0], {
  name: 'terminal-webkit', dependencies: ['setup'], testMatch: /terminal-(?:predictive|polling|update)\.spec\.ts/,
  use: { ...devices['Desktop Safari'], ...(executablePath ? { launchOptions: { executablePath } } : {}) },
}, { name: 'terminal-chromium', dependencies: ['setup'], testMatch: /terminal-polling\.spec\.ts/, use: { ...devices['Desktop Chrome'] } }, { name: 'terminal-firefox', dependencies: ['setup'], testMatch: /terminal-polling\.spec\.ts/,
use: { ...devices['Desktop Firefox'], ...(firefoxPath ? { launchOptions: { executablePath: firefoxPath } } : {}) } }] });
