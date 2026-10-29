import { test, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders } from './helpers';

test.use({ storageState: AUTH_FILE });

test('stale machine policy tab cannot erase another tabs downtime windows', async ({ page, context }) => {
  await page.goto('/host');
  const name = `policy-conflict-${randomUUID().slice(0, 8)}`;
  const headers = await csrfHeaders(page);
  const created = await page.request.post('/api/nodes', { headers, data: { name, role: 'always_on' } });
  expect(created.status()).toBe(201);
  const other = await context.newPage();
  try {
    await page.reload();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await other.goto('/host');
    await other.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await page.getByRole('button', { name: 'Add Downtime Window' }).click();
    await page.getByLabel('Downtime timezone').fill('UTC');
    await page.getByRole('checkbox', { name: 'Window 1 until it reconnects', exact: true }).check();
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await other.getByLabel('Offline alert after (minutes)').fill('20');
    const response = other.waitForResponse(r => r.url().endsWith(`/api/nodes/${name}/policy`));
    await other.getByRole('button', { name: 'Save', exact: true }).click();
    expect((await response).status()).toBe(409);
    await expect(other.getByRole('dialog').getByRole('alert')).toContainText(/changed/i);
    const nodes = await (await page.request.get('/api/nodes')).json();
    expect(nodes.find((n: { name: string }) => n.name === name).downtime_policy.windows).toEqual([
      { days: [0,1,2,3,4,5,6], start: '01:30', until_reconnect: true }]);
  } finally {
    await other.close();
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).ok()).toBe(true);
  }
});
