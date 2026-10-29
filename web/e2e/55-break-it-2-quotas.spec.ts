import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders } from './helpers';

test.use({ storageState: AUTH_FILE });

test('a stale quota form cannot overwrite another tab’s saved policy', async ({ page, context }) => {
  await page.goto('/groups');
  const users = await (await page.request.get('/api/users')).json();
  const alex = users.users.find((u: { username: string }) => u.username === 'alex');
  const headers = await csrfHeaders(page);
  const created = await page.request.post('/api/groups', { headers,
    data: { name: `Two tabs quotas ${randomUUID().slice(0, 8)}`, maintainer_id: alex.id } });
  expect(created.status(), await created.text()).toBe(201);
  const group = await created.json();
  const second = await context.newPage();
  try {
    for (const tab of [page, second]) {
      await tab.goto(`/groups?group=${group.id}&tab=board`);
      await expect(tab.getByText('No group sessions yet.', { exact: false })).toBeVisible();
      await tab.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('settings');
      await expect(tab.getByLabel('Daily launches', { exact: true })).toHaveValue('50');
    }
    await page.getByLabel('Daily launches', { exact: true }).fill('7');
    await page.getByRole('button', { name: 'Save launch quotas', exact: true }).click();
    await expect(page.getByText('Daily launch quota: 7', { exact: false })).toBeVisible();
    await second.getByLabel('Concurrent sessions', { exact: true }).fill('2');
    const saved = second.waitForResponse(r => r.url().endsWith(`/api/groups/${group.id}/policy`) && r.request().method() === 'POST');
    await second.getByRole('button', { name: 'Save launch quotas', exact: true }).click();
    expect((await saved).status()).toBe(409);
    await expect(second.getByText('The group changed while you were editing. Reload the page and save the launch quotas again.', { exact: true })).toBeVisible();
    const usage = await (await page.request.get(`/api/groups/${group.id}/usage`)).json();
    expect(usage.limits.daily_launches).toBe(7);
  } finally {
    await second.close();
    const current = await (await page.request.get(`/api/groups/${group.id}`)).json();
    expect((await page.request.post(`/api/groups/${group.id}/archive`, { headers, data: { revision: current.revision } })).ok()).toBe(true);
  }
});

test('opening quota settings before a slow board arrives uses the saved values', async ({ page }) => {
  await page.goto('/groups');
  const users = await (await page.request.get('/api/users')).json();
  const alex = users.users.find((u: { username: string }) => u.username === 'alex');
  const headers = await csrfHeaders(page);
  const created = await page.request.post('/api/groups', { headers,
    data: { name: `Slow quotas ${randomUUID().slice(0, 8)}`, maintainer_id: alex.id } });
  expect(created.status()).toBe(201);
  const group = await created.json();
  let release = () => {};
  try {
    expect((await page.request.post(`/api/groups/${group.id}/policy`, { headers, data: {
      revision: group.revision, concurrent: 2, daily_launches: 7,
    } })).status()).toBe(200);
    const gate = new Promise<void>(resolve => { release = resolve; });
    await page.route(`**/api/groups/${group.id}/board`, async route => { await gate; await route.continue(); });
    await page.goto(`/groups?group=${group.id}&tab=board`);
    await page.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('settings');
    release();
    await expect(page.getByLabel('Daily launches', { exact: true })).toHaveValue('7');
    await expect(page.getByLabel('Concurrent sessions', { exact: true })).toHaveValue('2');
  } finally {
    release();
    const current = await (await page.request.get(`/api/groups/${group.id}`)).json();
    expect((await page.request.post(`/api/groups/${group.id}/archive`, { headers, data: { revision: current.revision } })).ok()).toBe(true);
  }
});
