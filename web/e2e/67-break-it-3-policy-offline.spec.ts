import { test, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders } from './helpers';

test.use({ storageState: AUTH_FILE });

test('an open machine-policy dialog blocks saving while Rynhult is offline', async ({ page }) => {
  await page.goto('/host');
  const name = `policy-offline-${randomUUID().slice(0, 8)}`;
  const headers = await csrfHeaders(page);
  expect((await page.request.post('/api/nodes', { headers, data: { name, role: 'always_on' } })).status()).toBe(201);
  try {
    let failed = false;
    let writes = 0;
    await page.route('**/api/health', route => failed ? route.abort('internetdisconnected') : route.continue());
    await page.route(`**/api/nodes/${name}/policy`, route => { writes++; return route.continue(); });
    await page.reload();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await page.getByLabel('Downtime timezone').fill('America/Chicago');
    failed = true;
    await expect(page.getByText('Offline, retrying', { exact: true })).toBeVisible({ timeout: 15000 });
    const save = page.getByRole('dialog').getByRole('button', { name: 'Save', exact: true });
    await expect(save).toBeDisabled();
    await expect(save).toHaveAttribute('title', /Offline/);
    expect(writes).toBe(0);
    failed = false;
    await expect(save).toBeEnabled({ timeout: 15000 });
    await expect(page.getByLabel('Downtime timezone')).toHaveValue('America/Chicago');
    await save.click();
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(writes).toBe(1);
  } finally {
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).ok()).toBe(true);
  }
});
