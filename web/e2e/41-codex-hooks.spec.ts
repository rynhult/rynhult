import { wikiShotPath } from "./helpers";
import { expect, test } from '@playwright/test';
import { AUTH_FILE, control, gitProject, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE });

test('Codex hook review appears in Needs you and Inbox with a plain explanation', async ({ page }, testInfo) => {
  const { id: projectId, headers } = await gitProject(page, 'hooks-demo');
  const started = await page.request.post('/api/sessions', { headers,
    data: { project_id: projectId, provider: 'codex', name: 'Codex hook review' } });
  expect(started.ok()).toBe(true);
  const session = await started.json();
  try {
    await waitForSessionState(page, session.id, 'idle', 'codex_hook');
    control(session.id, 'hooks-review');
    await waitForSessionState(page, session.id, 'waiting_input');
    const message = 'Codex is asking to trust hooks; open the session to review';
    await page.goto('/sessions?group=needs_you');
    const row = page.getByRole('row').filter({ has: page.locator(`a[href="/sessions/${session.id}"]`) });
    await expect(row).toContainText('Needs you');
    await expect(row).toContainText(message);
    await page.goto('/inbox');
    const inbox = page.getByRole('main').locator(`a[href="/sessions/${session.id}"]`).first()
      .locator('xpath=ancestor::div[contains(@class,"grid")][1]');
    await expect(inbox.getByText(message, { exact: true })).toBeVisible();
    await page.screenshot({ path: wikiShotPath(testInfo, 'guardrails-codex-hook-review.png'), fullPage: true });
    control(session.id, 'answer');
    await waitForSessionState(page, session.id, 'active');
    await expect(inbox.getByText(message, { exact: true })).toHaveCount(0);
  } finally {
    expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers })).ok()).toBe(true);
  }
});
