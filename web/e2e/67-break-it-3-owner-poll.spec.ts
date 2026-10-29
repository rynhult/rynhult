import { test, expect } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) test(`trusted pairing dialog clears recovered polling errors but retains a code refusal at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  let polls = 0;
  let cancelled = false;
  await page.route('**/api/devices?mine=true', route => route.fulfill({ json: { devices: [], pairing_enabled: true, policy_pending: false } }));
  await page.route('**/api/devices/pairings', route => route.request().method() === 'POST'
    ? route.fulfill({ json: {
      id: 'demo-poll', label: 'Demo phone', link: 'https://acc.example.lan/pair-device#example',
      qr_svg: '<svg/>', expires_at: new Date(Date.now() + 300000).toISOString(),
    } }) : route.continue());
  await page.route('**/api/devices/pairings/demo-poll', route => {
    polls++;
    return polls === 1 ? route.abort('internetdisconnected')
      : route.fulfill({ json: { status: cancelled ? 'cancelled' : 'claimed', label: 'Demo phone', person: 'alex', password: null } });
  });
  await page.route('**/api/devices/pairings/demo-poll/confirm', route => {
    cancelled = true;
    return route.fulfill({ status: 409, json: { detail: 'The matching code differs. Start a fresh pairing.' } });
  });
  await page.goto('/settings#your-devices');
  await page.getByLabel('Device name', { exact: true }).fill('Demo phone');
  await page.getByRole('button', { name: 'Pair device', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Pair device', exact: true });
  await expect(dialog.getByRole('alert')).toBeVisible({ timeout: 10000 });
  await expect(dialog.getByLabel('Matching code shown on Demo phone')).toBeVisible({ timeout: 10000 });
  await expect(dialog.getByRole('alert')).toBeHidden();
  await dialog.getByLabel('Matching code shown on Demo phone').fill('000000');
  await dialog.getByRole('button', { name: 'Confirm Demo phone', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm device', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('matching code differs');
  await expect(dialog.getByLabel('Matching code shown on Demo phone')).toBeHidden({ timeout: 10000 });
  await expect(dialog.getByRole('alert')).toContainText('matching code differs');
});
