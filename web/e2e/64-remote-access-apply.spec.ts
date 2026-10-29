import { environmentValue } from "../../src/acc/env.mjs";
import { test, expect } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, verifyAgain, wikiShotPath, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owner reviews exact policy, applies it and removes public routing with temporary helper', async ({ page }, testInfo) => {
  test.skip(environmentValue("ACC_E2E_REMOTE_EDGE") !== '1', 'Requires the temporary, in-process root-contract fixture.');
  await page.goto('/settings/remote-access');
  const headers = await csrfHeaders(page);
  const original = await (await page.request.get('/api/remote-access')).json();
  const settings = {
    pairing_enabled: original.pairing_enabled, machine_identities: original.machine_identities,
    public_hostname: original.public_hostname, private_origin: original.private_origin || 'https://acc.example.lan',
    certificate_days: original.certificate_days,
  };
  expect((await page.request.post('/api/remote-access/off', { headers })).ok()).toBe(true);
  await page.reload();
  await verifyAgain(page);
  try {
    await page.getByLabel('Public hostname').fill('remote.example.com');
    await page.getByLabel('Private recovery address').fill('https://acc.example.lan');
    await page.getByRole('button', { name: 'Save pairing settings' }).click();
    await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
    await page.getByRole('button', { name: 'Review Caddy configuration' }).click();
    const policy = page.getByRole('region', { name: 'Caddy configuration to apply' });
    await expect(policy).toContainText('require_and_verify');
    await expect(policy).toContainText('"TrustedLeafCerts": []');
    await expect(policy).toContainText('unix//run/accd.sock');
    // Only the public change: the root base (other sites, DNS credentials) never leaves the helper.
    await expect(policy).toContainText('base_revision');
    await expect(policy).not.toContainText('acc.example.lan');
    await page.getByRole('button', { name: 'Apply reviewed configuration' }).click();
    await page.getByRole('button', { name: 'Apply public access', exact: true }).click();
    await expect(page.getByText('On', { exact: true })).toBeVisible();
    await expect(page.getByText('verified', { exact: true })).toBeVisible();
    await expect(page.getByText('remote.example.com', { exact: true })).toBeVisible();
    await expectAxeClean(page);
    await page.getByRole('heading', { name: 'Remote access', level: 1, exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: wikiShotPath(testInfo, 'remote-access-applied.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expectAxeClean(page);
    await page.getByRole('heading', { name: 'Remote access', level: 1, exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: wikiShotPath(testInfo, 'remote-access-applied-phone.png'), fullPage: true });
    await page.emulateMedia({ colorScheme: 'light' });
    await expectAxeClean(page);
    await page.getByRole('button', { name: 'Turn off public access' }).click();
    await expect(page.getByText('Off', { exact: true })).toBeVisible();
    await expect(page.getByText('None applied', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Turn off public access' })).toBeEnabled();
  } finally {
    const off = await page.request.post('/api/remote-access/off', { headers });
    expect(off.ok()).toBe(true);
    expect(await off.json()).toMatchObject({ enabled: false, desired_enabled: false, pending: false });
    expect((await page.request.put('/api/remote-access', { headers, data: settings })).ok()).toBe(true);
  }
});
