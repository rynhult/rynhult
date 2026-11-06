import { writeFileSync } from 'node:fs';
import { test as base, expect } from './fixtures';
import { AUTH_FILE, csrfHeaders, gitProject, info, waitForSessionState } from './helpers';

/** Disposable, test-owned subscription session; never borrows a live session. */
export const test = base.extend<{ subscriptionSession: string }>({
  storageState: AUTH_FILE,
  subscriptionSession: async ({ page }, use) => {
    const { id, headers } = await gitProject(page, 'subscription-guard');
    const response = await page.request.post('/api/sessions', { headers, data: {
      project_id: id, provider: 'claude', name: 'Subscription guard', initial_prompt: 'The person wrote this prompt',
    } });
    expect(response.status()).toBe(201);
    const sid = (await response.json()).id;
    try {
      writeFileSync(`${info().run_dir}/${sid}/fake-auth-method`, 'claude.ai');
      expect((await page.request.put(`/api/sessions/${sid}/automation`, { headers, data: { autoresume: false } })).ok()).toBe(true);
      await waitForSessionState(page, sid, 'idle', 'claude_hook');
      await use(sid);
    } finally {
      expect((await page.request.post(`/api/sessions/${sid}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    }
  },
});
export { expect };
