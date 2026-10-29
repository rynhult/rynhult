import { test, expect } from '@playwright/test';
import { AUTH_FILE, PASSWORD, csrfHeaders, freshStep, loadSecret, totp } from './helpers';

test.use({ storageState: AUTH_FILE });

test('inviting an owner asks for fresh verification first, whatever the passkey policy', async ({ page }) => {
  test.slow();
  // The shared fixture may already be verified by an earlier owner flow.
  // Sign in separately without ending that fixture's server-side session.
  await page.context().clearCookies();
  await page.goto('/login');
  await freshStep(page);
  await page.getByLabel('Username', { exact: true }).fill('alex');
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Authenticator code', { exact: true }).fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await page.goto('/settings#people');
  await expect(page.getByRole('heading', { name: 'People', exact: true })).toBeVisible();
  const role = page.getByRole('combobox', { name: 'Role', exact: true });
  await role.selectOption('owner');
  await page.getByRole('button', { name: 'Create invite link' }).click();
  await expect(page.getByText('Verify your sign-in again in Settings → Passkeys, then try again within five minutes.')).toBeVisible();
  await expect(page.locator('code').filter({ hasText: '/invite/' })).toHaveCount(0);
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('passkeys');
  await freshStep(page);
  await page.getByLabel('Verification password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Verify with password and authenticator' }).click();
  await expect(page.getByText('Verified for five minutes.')).toBeVisible();
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('people');
  await role.selectOption('owner');
  const created = page.waitForResponse(response => response.url().endsWith('/api/users/invites') && response.request().method() === 'POST');
  let inviteId: number | undefined;
  try {
    await page.getByRole('button', { name: 'Create invite link' }).click();
    const response = await created;
    expect(response.status()).toBe(201);
    inviteId = (await response.json()).id;
    await expect(page.locator('code').filter({ hasText: '/invite/' })).toBeVisible();
  } finally {
    if (inviteId !== undefined) {
      expect((await page.request.post(`/api/users/invites/${inviteId}/revoke`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    }
  }
});
