import { defineConfig } from '@playwright/test';
import base from './playwright.config';
export default defineConfig({ ...base, workers: 1, projects: [
  { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
  ...[1440, 390].map(width => ({ name: `project-delete-${width}`, dependencies: ['setup'],
    testMatch: /99-project-delete\.spec\.ts/, use: { ...base.projects![3].use, viewport: { width, height: 900 } } })),
] });
