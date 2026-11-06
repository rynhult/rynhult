import { expect, test } from './fixtures';
import { existsSync, writeFileSync, readFileSync } from 'node:fs';
import { AUTH_FILE, gitProject } from './helpers';

test.use({ storageState: AUTH_FILE });

test('bulk archive confirms in page and preserves stopped worktree and history', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'archive-stopped');
  const response = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', name: 'Archive preservation', worktree: true,
  } });
  expect(response.ok()).toBeTruthy();
  const session = await response.json();
  expect(session.worktree).toBeTruthy();
  const note = `${session.worktree}/preserved.txt`;
  writeFileSync(note, 'Keep this worktree and branch.\n');
  expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers })).ok()).toBeTruthy();
  await page.goto('/sessions');
  await page.getByRole('radio', { name: /^Stopped \d+/ }).click();
  await page.getByRole('button', { name: 'Archive all stopped', exact: true }).click();
  await expect(page.getByText('This applies across all filters.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect((await (await page.request.get(`/api/sessions/${session.id}`)).json()).desired_state).toBe('stopped');
  await page.getByRole('button', { name: 'Archive all stopped', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm archive all stopped', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: /Archived \d+ stopped sessions/ })).toBeVisible();
  const archived = await (await page.request.get(`/api/sessions/${session.id}`)).json();
  expect(archived.desired_state).toBe('archived');
  expect(archived.activity).toBeNull();
  expect(archived.worktree).toBe(session.worktree);
  expect(archived.worktree_branch).toBe(session.worktree_branch);
  expect(existsSync(note)).toBeTruthy();
  expect(readFileSync(note, 'utf8')).toBe('Keep this worktree and branch.\n');
  expect((await page.request.get(`/api/sessions/${session.id}/events`)).ok()).toBeTruthy();
});
