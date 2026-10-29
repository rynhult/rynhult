import { test, expect, type Locator } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders, expectAxeClean, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE });

async function tabTo(page: import('@playwright/test').Page, target: Locator) {
  for (let n = 0; n < 100; n++) {
    if (await target.evaluate(element => element === document.activeElement)) return;
    await page.keyboard.press('Tab');
  }
  await expect(target).toBeFocused();
}

test('remote settings reject hostile hostnames, retain failed drafts and persist a save across refresh', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings/remote-access');
  await verifyAgain(page);
  await page.getByLabel('Private recovery address').fill('https://acc.example.lan');
  await page.getByLabel('Allow LAN device pairing').uncheck();
  for (const hostname of ['🧪.example.com', '\u202eremote.example.com', 'a'.repeat(10000)]) {
    await page.getByLabel('Public hostname').fill(hostname);
    const response = page.waitForResponse(r => r.url().endsWith('/api/remote-access') && r.request().method() === 'PUT');
    await page.getByRole('button', { name: 'Save pairing settings' }).click();
    expect((await response).status()).toBe(422);
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByLabel('Public hostname')).toHaveValue(hostname);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.getByLabel('Public hostname').fill('remote.example.com');
  let fail = true;
  let savedOnServer!: () => void;
  const saved = new Promise<void>(resolve => { savedOnServer = resolve; });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  try {
    await page.route('**/api/remote-access', async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      if (fail) return route.abort('internetdisconnected');
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      savedOnServer();
      await gate;
      await route.fulfill({ response }).catch(() => undefined); // Reload may cancel the original response.
    });
    await page.getByRole('button', { name: 'Save pairing settings' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByLabel('Public hostname')).toHaveValue('remote.example.com');
    fail = false;
    await page.getByLabel('Certificate lifetime (days)').fill('30');
    await page.getByRole('button', { name: 'Save pairing settings' }).click();
    await saved;
    await expect(page.getByRole('button', { name: 'Saving…', exact: true })).toBeDisabled();
    page.once('dialog', dialog => dialog.accept());
    await page.reload();
    release();
    await expect(page.getByLabel('Certificate lifetime (days)')).toHaveValue('30');
    await expect(page.getByLabel('Public hostname')).toHaveValue('remote.example.com');
    await expect(page.getByLabel('Allow LAN device pairing')).not.toBeChecked();
    // Public access itself is shared with 64-remote-access-apply, which turns it on and off in parallel; only check it renders.
    await expect(page.locator('p', { hasText: 'Public access' }).first()).toContainText(/\b(On|Off|Unknown)\b/);
    await expectAxeClean(page);
  } finally {
    release();
  }
});

test('slow pairing double-click creates only one one-use link', async ({ page }) => {
  let attempts = 0;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  try {
    await page.route('**/api/devices?mine=true', route => route.fulfill({ json: { devices: [], pairing_enabled: true, policy_pending: false } }));
    await page.route('**/api/devices/pairings', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      attempts++;
      await gate;
      await route.fulfill({ json: { id: 'example-pairing', label: 'Demo phone', link: 'https://acc.example.lan/pair-device#example', qr_svg: '<svg/>', expires_at: new Date(Date.now() + 300000).toISOString() } });
    });
    await page.goto('/settings#your-devices');
    await page.getByLabel('Device name', { exact: true }).fill('Demo phone');
    await page.getByRole('button', { name: 'Pair device', exact: true }).dblclick();
    await expect(page.getByRole('button', { name: 'Pair device', exact: true })).toBeDisabled();
    await expect.poll(() => attempts).toBe(1);
    release();
    await expect(page.getByRole('dialog', { name: 'Pair device', exact: true })).toBeVisible();
  } finally {
    release();
  }
});

test('receiving-page Back, Forward and refresh never recover a used capability', async ({ page }) => {
  let claims = 0;
  await page.route('**/api/device-pairing/claim', route => {
    claims++;
    return route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } });
  });
  await page.goto('/pair-device#' + 't'.repeat(43));
  await expect(page).toHaveURL(/\/pair-device$/);
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  // The page's own sentence, not the Announcements live region, which repeats it about 50 ms later.
  await expect(page.getByRole('paragraph').filter({ hasText: 'Waiting for owner confirmation.' })).toBeVisible();
  await expect(page.getByRole('status', { name: 'Announcements' })).toContainText('Waiting for owner confirmation.');
  await page.goto('/login');
  await page.goBack();
  await expect(page.getByText(/A refreshed page cannot recover/)).toBeVisible();
  await page.goForward();
  await page.goBack();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Request pairing', exact: true })).toHaveCount(0);
  expect(claims).toBe(1);
});

test('keyboard-only phone policy editing repairs a 10k timezone without losing the form', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/host');
  const name = `a-chaos-${randomUUID().slice(0, 8)}`;
  const headers = await csrfHeaders(page);
  expect((await page.request.post('/api/nodes', { headers, data: { name, role: 'always_on' } })).status()).toBe(201);
  try {
    await page.reload();
    await tabTo(page, page.getByRole('button', { name: `Machine settings for ${name}` }));
    await page.keyboard.press('Enter');
    const zone = page.getByLabel('Downtime timezone');
    await tabTo(page, zone);
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText('x'.repeat(10000));
    const save = page.getByRole('dialog').getByRole('button', { name: 'Save', exact: true });
    await tabTo(page, save);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('100 characters');
    await expect(zone).toHaveValue('x'.repeat(10000));
    await tabTo(page, zone);
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText('UTC');
    await tabTo(page, save);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('dialog')).toBeHidden();
    await page.reload();
    await tabTo(page, page.getByRole('button', { name: `Machine settings for ${name}` }));
    await page.keyboard.press('Enter');
    await expect(page.getByLabel('Downtime timezone')).toHaveValue('UTC');
  } finally {
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).ok()).toBe(true);
  }
});

test('phone downtime window limit and Cancel never save an oversized draft', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/host');
  const name = `a-windows-${randomUUID().slice(0, 8)}`;
  const headers = await csrfHeaders(page);
  expect((await page.request.post('/api/nodes', { headers, data: { name, role: 'always_on' } })).status()).toBe(201);
  try {
    let writes = 0;
    await page.route(`**/api/nodes/${name}/policy`, route => { writes++; return route.continue(); });
    await page.reload();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    const add = page.getByRole('button', { name: 'Add Downtime Window' });
    const existing = await page.getByRole('button', { name: /Remove downtime window/ }).count();
    for (let n = existing; n < 28; n++) await add.click();
    await expect(add).toBeDisabled();
    await expect(page.getByRole('button', { name: /Remove downtime window/ })).toHaveCount(28);
    await expect(page.getByRole('dialog').getByRole('button', { name: 'Save', exact: true })).toBeInViewport();
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    expect(writes).toBe(0);
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await expect(page.getByRole('button', { name: /Remove downtime window/ })).toHaveCount(existing);
  } finally {
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).ok()).toBe(true);
  }
});
