import { test, expect } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectAxeClean, resetPairingThrottle, verifyAgain, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) test(`a wrong pairing code stays visibly explained after the cancelled request leaves the list at ${width}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  await page.goto('/settings/remote-access');
  await verifyAgain(page);
  await resetPairingThrottle(page);
  const headers = await csrfHeaders(page);
  const existingDevices = (await (await page.request.get('/api/devices')).json()).devices.map((device: { id: string }) => device.id).sort();
  const prepared = await page.request.put('/api/remote-access', { headers, data: {
    public_hostname: 'remote.example.com', private_origin: 'https://acc.example.lan', pairing_enabled: true, certificate_days: 30,
  } });
  expect(prepared.ok()).toBe(true);
  for (const label of ['a'.repeat(10000), '\u202ePhone', ' ', 'Phone\nAnother person']) {
    const response = await page.request.post('/api/devices/pairings', { headers, data: { label } });
    expect(response.status()).toBe(422);
  }
  const started = await page.request.post('/api/devices/pairings', { headers, data: { label: 'هاتف 🧪' } });
  expect(started.ok()).toBe(true);
  const pairing = await started.json();
  const claimed = await page.request.post('/api/device-pairing/claim', { headers, data: { token: pairing.link.split('#')[1] } });
  expect(claimed.ok()).toBe(true);
  const code = (await claimed.json()).matching_code;
  await page.reload();
  const field = page.getByLabel('Matching code shown on هاتف 🧪');
  await expect(field).toBeVisible();
  await field.fill('ab٣xyz');
  await expect(field).toHaveValue('');
  await field.fill('123456');
  await expect(field).toHaveValue('123456');
  await field.fill(code === '000000' ? '111111' : '000000');
  await page.getByRole('button', { name: 'Confirm هاتف 🧪', exact: true }).click();
  const rejected = page.waitForResponse(r => r.url().endsWith(`/api/devices/pairings/${pairing.id}/confirm`));
  await page.getByRole('button', { name: 'Confirm device', exact: true }).click();
  expect((await rejected).status()).toBe(409);
  await expect(page.getByRole('alert')).toContainText('matching code differs');
  await expect(field).toBeHidden();
  const replay = await page.request.post(`/api/devices/pairings/${pairing.id}/confirm`, { headers, data: { matching_code: code } });
  expect(replay.status()).toBe(410);
  expect((await (await page.request.get('/api/devices')).json()).devices.map((device: { id: string }) => device.id).sort()).toEqual(existingDevices);
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (width === 1440) await page.locator('section').filter({ has: page.getByRole('heading', { name: 'Confirm pairing', exact: true }) })
    .screenshot({ path: wikiShotPath(testInfo, 'remote-access-pairing-cancelled.png') });
});
