import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// Lane E only. Before 0074-0076 integrate, tests use temporary predecessor stubs;
// they are never committed in migrations/versions. The wrapper is test-only.
const server = base.webServer as { command: string };
export default defineConfig({
  ...base, workers: 1, globalTeardown: './e2e/brief-teardown.ts',
  projects: [
    { name: 'setup', testMatch: /00-setup\.spec\.ts/, use: base.projects![0].use },
    ...([1440, 390] as const).flatMap(width => (['dark', 'light'] as const).map(colorScheme => ({
      name: `brief-${width}-${colorScheme}`, dependencies: ['setup'], testMatch: /project-brief\.spec\.ts/,
      use: { ...base.projects![3].use, viewport: { width, height: 900 }, colorScheme },
    }))),
  ],
  webServer: { ...base.webServer!, command: server.command.replace('cd .. && ', 'cd .. && ACC_ORCHESTRATOR_V2=1 ')
    .replace('tests/e2e_server.py', 'tests/project_brief_e2e_server.py') },
});
