import { test, expect } from '@playwright/test';
import { AUTH_FILE, PASSWORD, loadSecret, totp, freshStep, wikiShotPath, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

test.afterEach(async ({ page, context }) => {
  const state = await (await page.request.get('/api/auth/state')).json();
  // Recovery revokes the old fixture even if a later UI assertion fails.
  if (state.authenticated) await context.storageState({ path: AUTH_FILE });
});

test('passkey enrollment, discoverable login, rename and removal on desktop and phone', async ({ page, context }, testInfo) => {
  test.setTimeout(120_000);
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true,
  }});
  await page.goto('/settings');
  const card = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Passkeys', exact: true }) });
  await expect(page.getByRole('heading', { name: 'Passkeys', exact: true })).toBeVisible();
  await freshStep(page);
  await page.getByLabel('Verification password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Verify with password and authenticator' }).click();
  await expect(page.getByText('Verified for five minutes.')).toBeVisible();
  await page.getByLabel('Passkey name', { exact: true }).fill('Alex laptop');
  await page.getByRole('button', { name: 'Add passkey', exact: true }).click();
  await expect(page.getByText('Alex laptop', { exact: true })).toBeVisible();
  const credentials = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
  expect(credentials.credentials).toHaveLength(1);
  expect(credentials.credentials[0].isResidentCredential).toBe(true);
  await page.getByRole('button', { name: 'Rename Alex laptop' }).click();
  await page.getByLabel('New passkey name').fill('Alex security key');
  await page.getByRole('button', { name: 'Save name' }).click();
  await expect(page.getByText('Alex security key', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await card.evaluate(element => element.scrollIntoView({ block: 'start' }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-passkeys-phone.png') });
  await page.setViewportSize({ width: 1440, height: 900 });
  await card.screenshot({ path: wikiShotPath(testInfo, 'settings-passkeys.png') });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('button', { name: 'Sign in with a passkey' })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, 'passkey-sign-in.png') });
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Verify with a passkey' }).click();
  await expect(page.getByText('Verified for five minutes.')).toBeVisible();
  await page.getByRole('button', { name: 'Remove Alex security key' }).click();
  await page.getByRole('button', { name: 'Remove passkey', exact: true }).click();
  await expect(page.getByText('No passkeys yet.')).toBeVisible();
  await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
});


test('offline recovery works after the virtual authenticator is lost', async ({ page, context }, testInfo) => {
  test.setTimeout(120_000);
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true,
  }});
  await page.goto('/settings');
  await freshStep(page);
  await page.getByLabel('Verification password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Verify with password and authenticator' }).click();
  await expect(page.getByText('Verified for five minutes.')).toBeVisible();
  await page.getByLabel('Passkey name', { exact: true }).fill('Lost device');
  await page.getByRole('button', { name: 'Add passkey', exact: true }).click();
  await expect(page.getByText('Lost device', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Create recovery code', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Create recovery code', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Save your recovery code' });
  const code = (await dialog.locator('code').textContent())!;
  expect(code.length).toBeGreaterThan(40);
  await dialog.getByRole('button', { name: 'I saved the code' }).click();
  await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole('button', { name: 'Lost your passkeys?' }).click();
  await page.screenshot({ path: wikiShotPath(testInfo, 'passkey-recovery.png') });
  await page.getByLabel('Username', { exact: true }).fill('alex');
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Recovery code', { exact: true }).fill(code);
  await page.getByRole('button', { name: 'Recover sign-in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await page.goto('/settings');
  await expect(page.getByText('No passkeys yet.')).toBeVisible();
  await expect(page.getByText('Recovery code: not created.', { exact: false })).toBeVisible();
  // Recovery is a critical Inbox item for the person and every owner (security review PK-05).
  await page.goto('/inbox');
  await expect(page.getByText('alex recovered sign-in with an offline recovery code', { exact: true })).toBeVisible();
  await expect(page.getByText('Their passkeys and sign-ins were removed.', { exact: false })).toBeVisible();
});
