import { test, expect } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE });

test('stale remote-access tab cannot re-enable pairing or overwrite a newer hostname', async ({ page, context }) => {
  await page.goto('/settings/remote-access');
  await verifyAgain(page);
  const headers = await csrfHeaders(page);
  await page.request.put('/api/remote-access', { headers, data: {
    pairing_enabled: true, public_hostname: 'remote.example.com', private_origin: 'https://acc.example.lan', certificate_days: 90,
  } });
  await page.reload();
  await expect(page.getByLabel('Allow LAN device pairing')).toBeChecked();
  const other = await context.newPage();
  await other.goto('/settings/remote-access');
  await expect(other.getByLabel('Public hostname')).toHaveValue('remote.example.com');
  await page.getByLabel('Public hostname').fill('other.example.com');
  await page.getByLabel('Allow LAN device pairing').uncheck();
  await page.getByRole('button', { name: 'Save pairing settings' }).click();
  await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
  await other.getByLabel('Certificate lifetime (days)').fill('30');
  const response = other.waitForResponse(r => r.url().endsWith('/api/remote-access') && r.request().method() === 'PUT');
  await other.getByRole('button', { name: 'Save pairing settings' }).click();
  expect((await response).status()).toBe(409);
  await expect(other.getByRole('alert')).toContainText(/changed/i);
  const saved = await (await page.request.get('/api/remote-access')).json();
  expect(saved.public_hostname).toBe('other.example.com');
  expect(saved.pairing_enabled).toBe(false);
});

for (const enabled of [true, false]) {
  test(`stale remote-access tab cannot ${enabled ? 'disable' : 're-enable'} machine identities`, async ({ page, context }) => {
    await page.goto('/settings/remote-access');
    await verifyAgain(page);
    const headers = await csrfHeaders(page);
    const prepared = await page.request.put('/api/remote-access', { headers, data: {
      pairing_enabled: true, public_hostname: 'remote.example.com', private_origin: 'https://acc.example.lan',
      certificate_days: 90, machine_identities: !enabled,
    } });
    expect(prepared.ok()).toBe(true);
    await page.reload();
    const machineIdentities = page.getByRole('checkbox', { name: /Give enrolled machines identities/ });
    await expect(machineIdentities).toBeChecked({ checked: !enabled });
    const other = await context.newPage();
    await other.goto('/settings/remote-access');
    await expect(other.getByRole('checkbox', { name: /Give enrolled machines identities/ })).toBeChecked({ checked: !enabled });
    await machineIdentities.setChecked(enabled);
    await page.getByRole('button', { name: 'Save pairing settings' }).click();
    await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
    await other.getByLabel('Certificate lifetime (days)').fill('30');
    const response = other.waitForResponse(r => r.url().endsWith('/api/remote-access') && r.request().method() === 'PUT');
    await other.getByRole('button', { name: 'Save pairing settings' }).click();
    expect((await response).status()).toBe(409);
    await expect(other.getByRole('alert')).toContainText(/changed in another tab/i);
    const saved = await (await page.request.get('/api/remote-access')).json();
    expect(saved.machine_identities).toBe(enabled);
    expect(saved.certificate_days).toBe(90);
    await other.close();
  });
}
