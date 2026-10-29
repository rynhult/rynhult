import { test, expect } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const failure of ['network', 'server'] as const) test(`receiving device resumes status polling after a temporary ${failure} failure`, async ({ page }) => {
  await page.setViewportSize({ width: failure === 'network' ? 1440 : 390, height: 844 });
  let polls = 0;
  await page.route('**/api/device-pairing/claim', route => route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } }));
  await page.route('**/api/device-pairing/status', route => {
    polls++;
    return polls === 1 ? failure === 'network' ? route.abort('internetdisconnected')
      : route.fulfill({ status: 503, json: { detail: 'Temporary test outage' } })
      : route.fulfill({ json: { status: 'ready', available: true } });
  });

  await page.goto('/pair-device#' + 't'.repeat(43));
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeEnabled({ timeout: 12000 });
  await expect(page.getByRole('alert')).toBeHidden();
});

test('expired pairing disables a previously available download and stops polling', async ({ page }) => {
  let polls = 0;
  await page.route('**/api/device-pairing/claim', route => route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } }));
  await page.route('**/api/device-pairing/status', route => {
    polls++;
    return polls === 1 ? route.fulfill({ json: { status: 'ready', available: true } })
      : route.fulfill({ status: 410, json: { detail: 'This pairing expired. Start again on the trusted device.' } });
  });
  await page.goto('/pair-device#' + 't'.repeat(43));
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeEnabled({ timeout: 10000 });
  await expect(page.getByRole('alert')).toContainText('expired', { timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeDisabled();
  await page.waitForTimeout(6500);
  expect(polls).toBe(2);
});
