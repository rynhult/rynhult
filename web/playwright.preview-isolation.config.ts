import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// Focused concurrency qualification, without unrelated stateful dependency suites.
export default defineConfig({
  ...base,
  workers: 1,
  projects: base.projects!.filter(p => p.name === 'setup' || p.name === 'desktop').map(p => p.name === 'desktop' ? {
    ...p,
    dependencies: ['setup'],
    testIgnore: [/mobile|00-setup\.spec\.ts/],
    testMatch: [/21-preview\.spec\.ts/, /70-ux-remaining-session\.spec\.ts/],
  } : p),
});
