import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders } from './helpers';

test.use({ storageState: AUTH_FILE });

test('refresh during a privacy save loads persisted choices and deletion needs confirmation', async ({ page }) => {
  await page.goto('/settings#telemetry');
  const original = await (await page.request.get('/api/telemetry')).json();
  const headers = await csrfHeaders(page);
  let saved!: () => void;
  const persisted = new Promise<void>(resolve => { saved = resolve; });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  try {
    await page.route(/\/api\/telemetry$/, async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      saved();
      await gate;
      await route.fulfill({ response }).catch(() => undefined); // Reload can cancel the old response.
    });
    await page.getByText('Collector and consent details', { exact: true }).click();
    await page.getByLabel('Collector URL').fill('https://refresh.example.com');
    await page.getByRole('button', { name: 'Save privacy choices', exact: true }).click();
    await persisted;
    await page.reload();
    release();
    await page.getByText('Collector and consent details', { exact: true }).click();
    await expect(page.getByLabel('Collector URL')).toHaveValue('https://refresh.example.com');
    let deletes = 0;
    await page.route('**/api/telemetry/delete', route => { deletes++; return route.fulfill({ json: { ok: true } }); });
    await page.getByRole('button', { name: 'Delete data already sent', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    expect(deletes).toBe(0);
    await page.getByRole('button', { name: 'Delete data already sent', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Request deletion', exact: true }).click();
    await expect(page.getByText('Deletion queued.', { exact: false })).toBeVisible();
    expect(deletes).toBe(1);
  } finally {
    release();
    expect((await page.request.put('/api/telemetry', { headers, data: {
      usage: original.consent.usage, reports: original.consent.reports, url: original.url, delete_previous: false,
    } })).ok()).toBe(true);
  }
});
