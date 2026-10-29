import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
test('an idle company passkey page stops promising an expired enrollment window', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-25T18:00:00Z') });
  await page.route('**/api/auth/passkeys', route => route.fulfill({ json: {
    passkeys: [], recovery_code: false, company_only: true,
    enrollment_until: '2026-10-25T18:05:00Z', policy: { owners: 'allow', members: 'allow' },
  } }));
  await page.goto('/settings#passkeys');
  const card = page.locator('#passkeys');
  await expect(card.getByText(/so you can add one passkey now without verifying again/)).toBeVisible();
  await page.clock.fastForward(301_000);
  await expect(card.getByText(/To add a passkey, sign out and sign in again/)).toBeVisible();
  await expect(card.getByText(/so you can add one passkey now without verifying again/)).toHaveCount(0);
  await expectAxeClean(page, '#passkeys');
  await card.screenshot({ path: '../.acc/artifacts/break-it-7/expired-passkey-window.png' });
});

test('server withdrawal replaces the cached company enrollment promise', async ({ page }) => {
  await page.route('**/api/auth/passkeys', route => route.fulfill({ json: {
    passkeys: [], recovery_code: false, company_only: true,
    enrollment_until: new Date(Date.now() + 300_000).toISOString(), policy: { owners: 'allow', members: 'allow' },
  } }));
  await page.route('**/api/auth/passkeys/register/begin', route => route.fulfill({ status: 403,
    json: { detail: 'Verify your sign-in again' } }));
  await page.goto('/settings#passkeys');
  const card = page.locator('#passkeys');
  await expect(card.getByText(/so you can add one passkey now without verifying again/)).toBeVisible();
  await card.getByRole('textbox', { name: 'Passkey name' }).fill('Travel key');
  await card.getByRole('button', { name: 'Add passkey', exact: true }).click();
  await expect(card.getByText(/To add a passkey, sign out and sign in again/)).toBeVisible();
});
