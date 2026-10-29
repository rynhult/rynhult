import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('a new send-log entry does not erase unsaved privacy choices', async ({ page }) => {
  const state = await (await page.request.get('/api/telemetry')).json();
  let reads = 0;
  await page.route(/\/api\/telemetry(?:\?.*)?$/, route => {
    reads++;
    return route.fulfill({ json: { ...state, consent: { ...state.consent, usage: false }, total: reads, items: [] } });
  });
  await page.clock.install();
  await page.goto('/settings#telemetry');
  await page.getByText('Collector and consent details', { exact: true }).click();
  const url = page.getByLabel('Collector URL');
  await expect(url).toHaveValue(state.url);
  await url.fill('https://draft.example.com');
  await page.getByLabel('Share usage statistics').check();
  await page.clock.fastForward(15_100);
  await expect.poll(() => reads).toBeGreaterThan(1);
  await expect(url).toHaveValue('https://draft.example.com');
  await expect(page.getByLabel('Share usage statistics')).toBeChecked();
});
