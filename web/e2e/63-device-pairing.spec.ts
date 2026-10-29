import { environmentValue } from "../../src/acc/env.mjs";
import { test, expect, type BrowserContext, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders, resetPairingThrottle, verifyAgain, wikiShotPath, expectAxeClean, freshStep, PASSWORD, loadSecret, totp } from './helpers';

test.use({ storageState: AUTH_FILE });

type PairingSettings = {
  pairing_enabled: boolean; machine_identities: boolean; public_hostname: string;
  private_origin: string; certificate_days: number;
};
const pairingSettings = (data: PairingSettings): PairingSettings => ({
  pairing_enabled: data.pairing_enabled, machine_identities: data.machine_identities,
  public_hostname: data.public_hostname, private_origin: data.private_origin || 'https://acc.example.lan',
  certificate_days: data.certificate_days,
});
let settingsBefore: PairingSettings | undefined;
let devicesBefore = new Set<string>();
let receiver: BrowserContext | undefined;

async function restoreOwnerSignIn(page: Page) {
  const state = await (await page.request.get('/api/auth/state')).json();
  if (state.authenticated) return false;
  page.once('dialog', dialog => dialog.accept());
  await page.goto('/login');
  await freshStep(page);
  await page.getByLabel('Username').fill('alex');
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Authenticator code').fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
  await page.context().storageState({ path: AUTH_FILE });
  return true;
}

test.beforeEach(async ({ page }) => {
  settingsBefore = undefined;
  receiver = undefined;
  const response = await page.request.get('/api/remote-access');
  expect(response.ok()).toBe(true);
  settingsBefore = pairingSettings(await response.json());
  devicesBefore = new Set((await (await page.request.get('/api/devices')).json()).devices.map((device: { id: string }) => device.id));
  if (environmentValue('ACC_E2E_REMOTE_EDGE') === '1') {
    expect((await page.request.post('/api/remote-access/off', { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
  await resetPairingThrottle(page);
});

test.afterEach(async ({ page }) => {
  await receiver?.close();
  if (!settingsBefore) return;
  let needsVerification = await restoreOwnerSignIn(page);
  const devices = (await (await page.request.get('/api/devices')).json()).devices as { id: string }[];
  for (const device of devices.filter(device => !devicesBefore.has(device.id))) {
    if (needsVerification) await verifyAgain(page);
    expect((await page.request.delete(`/api/devices/${device.id}`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    needsVerification = await restoreOwnerSignIn(page);
  }
  if (environmentValue('ACC_E2E_REMOTE_EDGE') === '1') {
    const off = await page.request.post('/api/remote-access/off', { headers: await csrfHeaders(page) });
    expect(off.ok()).toBe(true);
    expect(await off.json()).toMatchObject({ enabled: false, desired_enabled: false, pending: false });
  }
  const current = pairingSettings(await (await page.request.get('/api/remote-access')).json());
  if (JSON.stringify(current) !== JSON.stringify(settingsBefore)) {
    if (needsVerification) await verifyAgain(page);
    expect((await page.request.put('/api/remote-access', { headers: await csrfHeaders(page), data: settingsBefore })).ok()).toBe(true);
  }
  await page.context().storageState({ path: AUTH_FILE });
});

// A system-theme event and CSS color transitions finish after emulateMedia returns.
// Audit the settled theme rather than colors from two different animation frames.
async function setScheme(page: Page, colorScheme: 'light' | 'dark') {
  await page.emulateMedia({ colorScheme });
  if (colorScheme === 'dark') await expect(page.locator('html')).toHaveClass(/\bdark\b/);
  else await expect(page.locator('html')).not.toHaveClass(/\bdark\b/);
  await page.evaluate(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    await Promise.all(document.getAnimations()
      .filter(animation => animation.effect?.getTiming().iterations !== Infinity)
      .map(animation => animation.finished.catch(() => undefined)));
  });
}

test('remote access stays off while an owner prepares LAN pairing', async ({ page }, testInfo) => {
  test.skip(environmentValue("ACC_E2E_DEVICE_CA") === '1', 'This probe expects the production helper to be absent.');
  await page.goto('/settings');
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('remote-access');
  await expect(page).toHaveURL(/\/settings#remote-access$/);
  await page.locator('#remote-access').getByRole('link', { name: 'Remote access', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Public access status' })).toBeVisible();
  await expect(page.locator('p', { hasText: 'Public access' }).first()).toContainText(/\bOff\b/);
  await verifyAgain(page);
  await page.getByLabel('Public hostname').fill('remote.example.com');
  await page.getByLabel('Private recovery address').fill('https://acc.example.lan');
  await page.getByLabel('Allow LAN device pairing').check();
  // Machine identities are a separate, off-by-default choice that saving pairing never turns on.
  const machines = page.getByLabel('Give enrolled machines identities');
  await expect(machines).not.toBeChecked();
  await machines.check();
  await page.getByRole('button', { name: 'Save pairing settings' }).click();
  await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Give enrolled machines identities')).toBeChecked();
  await page.getByLabel('Give enrolled machines identities').uncheck();
  await page.getByRole('button', { name: 'Save pairing settings' }).click();
  await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
  await expect(page.locator('p', { hasText: 'Public access' }).first()).toContainText(/\bOff\b/);
  await page.getByLabel('Device name', { exact: true }).fill('Alex phone');
  await page.getByRole('button', { name: 'Pair device', exact: true }).click();
  await expect(page.getByRole('img', { name: 'One-use device pairing QR code' })).toBeVisible();
  const link = await page.getByLabel('One-use pairing link').inputValue();
  expect(link).toMatch(/^https:\/\/acc\.example\.lan\/pair-device#[A-Za-z0-9_-]{43}$/);
  receiver = await page.context().browser()!.newContext();
  const phone = await receiver.newPage();
  await phone.goto(new URL('/pair-device#' + link.split('#')[1], page.url()).href);
  await expect(phone.getByRole('button', { name: 'Request pairing' })).toBeVisible();
  expect(new URL(phone.url()).hash).toBe('');
  await phone.getByRole('button', { name: 'Request pairing' }).click();
  await expect(phone.getByRole('paragraph').filter({ hasText: 'Waiting for owner confirmation.' })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Confirm Alex phone' })).toBeVisible();
  const receiverCode = await phone.locator('code').textContent();
  expect(receiverCode).toMatch(/^[0-9]{6}$/);
  // The owner reads the code on the new device and types it; the trusted page never shows it.
  await expect(page.getByRole('dialog')).not.toContainText(receiverCode!);
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Confirm Alex phone' })).toBeDisabled();
  await page.getByRole('dialog').getByLabel('Matching code shown on Alex phone').fill(receiverCode!);
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm Alex phone' }).click();
  await page.getByRole('button', { name: 'Confirm device', exact: true }).click();
  // Uninstalled helpers fail visibly; no mock signer or machine changes.
  await expect(page.getByRole('alert')).toContainText("That service isn't available right now.");
  await page.getByRole('alert').locator('summary').click();
  await expect(page.getByText(/device signer is unavailable/)).toBeVisible();
  await receiver.close();
  // Capture a secret-free settings state only.
  await page.getByRole('button', { name: 'Close pairing' }).click();
  await expectAxeClean(page);
  await page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-remote-access.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectAxeClean(page);
  await page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-remote-access-phone.png'), fullPage: true });
});


test('browser pairing delivers an encrypted identity only once with temporary test CA', async ({ page }, testInfo) => {
  test.skip(environmentValue("ACC_E2E_DEVICE_CA") !== '1', 'Run with the test-only temporary CA fixture.');
  const suffix = randomUUID().slice(0, 8);
  const deviceName = `Alex phone ${suffix}`;
  const renamedDevice = `Travel phone ${suffix}`;
  await page.goto('/settings/remote-access');
  await verifyAgain(page);
  await page.getByLabel('Public hostname').fill('remote.example.com');
  await page.getByLabel('Private recovery address').fill('https://acc.example.lan');
  await page.getByLabel('Allow LAN device pairing').check();
  await page.getByRole('button', { name: 'Save pairing settings' }).click();
  await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
  await expect(page.locator('p', { hasText: 'Public access' }).first()).toContainText(/\bOff\b/);
  await page.getByLabel('Device name', { exact: true }).fill(deviceName);
  await page.getByRole('button', { name: 'Pair device', exact: true }).click();
  const link = await page.getByLabel('One-use pairing link').inputValue();
  receiver = await page.context().browser()!.newContext();
  const phone = await receiver.newPage();
  await phone.goto(new URL('/pair-device#' + link.split('#')[1], page.url()).href);
  await expect(phone.getByRole('status', { name: 'Announcements' })).toHaveCount(1);
  await phone.getByRole('button', { name: 'Request pairing' }).click();
  await expect(phone.getByRole('status', { name: 'Announcements' })).toContainText('Waiting for owner confirmation.');
  const receiverCode = await phone.locator('code').textContent();
  expect(receiverCode).toMatch(/^[0-9]{6}$/);
  // The owner reads the code on the new device and types it; the trusted page never shows it.
  await expect(page.getByRole('dialog')).not.toContainText(receiverCode!);
  await expect(page.getByRole('dialog').getByRole('button', { name: `Confirm ${deviceName}` })).toBeDisabled();
  await page.getByRole('dialog').getByLabel(`Matching code shown on ${deviceName}`).fill(receiverCode!);
  await page.getByRole('dialog').getByRole('button', { name: `Confirm ${deviceName}` }).click();
  await page.getByRole('button', { name: 'Confirm device', exact: true }).click();
  await page.getByRole('button', { name: 'Show import password' }).click();
  await expect(page.getByText('Import password:', { exact: false })).toBeVisible();
  await expect(phone.getByRole('button', { name: 'Download identity once' })).toBeEnabled();
  await phone.getByLabel('Identity format').selectOption('mobileconfig');
  await expectAxeClean(phone);
  await phone.screenshot({ path: wikiShotPath(testInfo, 'device-pairing-import.png'), fullPage: true });
  await setScheme(phone, 'light');
  await expectAxeClean(phone);
  await setScheme(phone, 'dark');
  const downloadPromise = phone.waitForEvent('download');
  await phone.getByRole('button', { name: 'Download identity once' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('acc-device.mobileconfig');
  // The page's own sentence, not the Announcements live region, which repeats it about 50 ms later.
  await expect(phone.getByRole('paragraph').filter({ hasText: /Certificate downloaded once/ })).toBeVisible();
  await expect(phone.getByRole('button', { name: 'Download identity once' })).toHaveCount(0);
  await receiver.close();
  await page.getByRole('button', { name: 'Close pairing' }).click();
  await expect(page.getByRole('button', { name: `Rename ${deviceName}` })).toBeVisible();
  await page.getByRole('button', { name: `Rename ${deviceName}` }).click();
  await page.getByLabel('New device name').fill(renamedDevice);
  await page.getByRole('button', { name: 'Save name', exact: true }).click();
  await expect(page.getByRole('button', { name: `Rename ${renamedDevice}` })).toBeVisible();
  await expect(page.getByRole('button', { name: `Renew ${renamedDevice}` })).toHaveCount(0);
  await page.getByRole('button', { name: 'Render Caddy fragment' }).click();
  await expect(page.getByRole('region', { name: 'Unapplied Caddy fragment' })).toContainText('TrustedLeafCerts');
  await expectAxeClean(page);
  await setScheme(page, 'light');
  await expectAxeClean(page);
  await setScheme(page, 'dark');
  // Only the registry/settings, after secrets have left the screen.
  await page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-remote-access.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-remote-access-phone.png'), fullPage: true });
  await setScheme(page, 'light');
  await expectAxeClean(page);
  await setScheme(page, 'dark');
  await page.getByRole('main').getByRole('link', { name: 'Guide', exact: true }).click();
  await expect(page).toHaveURL(/\/guide\/User-Guide\/Remote-Access$/);
  await expect(page.getByText(/Review and manage public remote access/)).toBeVisible();
  await page.goto('/settings#your-devices');
  const ownDevices = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Access devices', exact: true }) });
  await expect(ownDevices.getByRole('button', { name: `Rename ${renamedDevice}` })).toBeVisible();
  await expect(ownDevices.getByRole('button', { name: `Renew ${renamedDevice}` })).toBeVisible();
  await ownDevices.getByRole('button', { name: `Rename ${renamedDevice}` }).click();
  const longName = 'A'.repeat(92) + suffix;
  await page.getByLabel('New device name').fill(longName);
  await page.getByRole('button', { name: 'Save name', exact: true }).click();
  await expect(ownDevices.getByRole('button', { name: `Rename ${longName}` })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await ownDevices.getByRole('button', { name: `Rename ${longName}` }).click();
  await page.getByLabel('New device name').fill(renamedDevice);
  await page.getByRole('button', { name: 'Save name', exact: true }).click();
  await expect(ownDevices.getByRole('button', { name: `Rename ${renamedDevice}` })).toBeVisible();
  await ownDevices.screenshot({ path: wikiShotPath(testInfo, 'settings-your-devices.png') });
  // Restore preparation while this session is still verified; removing the device
  // deliberately revokes every owner session.
  expect((await page.request.put('/api/remote-access', { headers: await csrfHeaders(page), data: settingsBefore })).ok()).toBe(true);
  await page.getByRole('button', { name: `Remove ${renamedDevice}` }).click();
  await page.getByRole('button', { name: 'Remove device', exact: true }).click();
  // Revocation invalidates this sign-in and redirects immediately. Wait for that
  // navigation before probing a protected route to avoid two competing navigations.
  await expect(page).toHaveURL(/\/login$/);
  await page.goto('/settings');
  await expect(page).toHaveURL(/\/login$/);
  // Revocation deliberately invalidates the shared owner's sign-in. Restore the
  // fixture with a real login so later desktop and mobile tests get a live session.
  await freshStep(page);
  await page.getByLabel('Username').fill('alex');
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
  await page.getByLabel('Authenticator code').fill(totp(loadSecret()));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
  await page.context().storageState({ path: AUTH_FILE });
});

test('public status exposes review and an always available off action with helper absent', async ({ page }) => {
  test.skip(environmentValue("ACC_E2E_DEVICE_CA") === '1' || environmentValue("ACC_E2E_REMOTE_EDGE") === '1', 'Run separately with both test helpers disabled.');
  await page.goto('/settings/remote-access');
  await expect(page.getByRole('heading', { name: 'Public access status' })).toBeVisible();
  await expect(page.getByText('Last applied revision', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Turn off public access' })).toBeEnabled();
  await page.getByRole('button', { name: 'Turn off public access' }).click();
  await expect(page.getByRole('alert')).toContainText("That service isn't available right now.");
  await expect(page.getByText('Removal pending', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Turn off public access' })).toBeEnabled();
});
