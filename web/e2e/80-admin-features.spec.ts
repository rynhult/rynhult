import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`owner feature panel at ${width}px in ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    await page.goto('/settings#admin-features');
    const panel = page.locator('#admin-features');
    await expect(panel.getByRole('heading', { name: 'Admin features' })).toBeVisible();
    await expect(panel.getByRole('region', { name: 'Features', exact: true })).toBeVisible();
    await expect(panel.getByRole('region', { name: 'Previews', exact: true })).toBeVisible();
    const features = panel.getByRole('region', { name: 'Features', exact: true });
    await expect(features.getByRole('switch', { name: 'Orchestrator v2', exact: true })).toBeChecked();
    await expect(panel.getByRole('region', { name: 'Previews', exact: true }).getByRole('switch', { name: 'Orchestrator v2', exact: true })).toHaveCount(0);
    const schedules = panel.getByRole('switch', { name: 'Scheduled sessions', exact: true });
    await expect(schedules).toBeChecked();
    try {
      await schedules.focus();
      await page.keyboard.press('Space');
      await expect(schedules).not.toBeChecked();
      await expect(schedules).toBeFocused(); // saving never disables the focused switch
      await expect(panel.getByRole('status')).toHaveText('Saved: Scheduled sessions is off.');
      await expect.poll(async () => (await page.request.get('/api/schedules')).status()).toBe(404);
      await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Schedules', exact: true })).toHaveCount(0);
      await expect(panel.getByText('Core sessions', { exact: true })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expectAxeClean(page, '#admin-features');
      // The app scrolls its main pane: an element taller than that pane produces
      // clipped/blank locator screenshots. Capture the actual viewport instead.
      await page.screenshot({ path: wikiShotPath(testInfo, `admin-features-${width}-${theme}.png`) });
      const containers = panel.getByRole('switch', { name: 'Containers per session', exact: true });
      await containers.scrollIntoViewIfNeeded();
      await expect(containers).toBeDisabled(); // the harness pins its qualified container flag
      await expect(panel.getByText("Set by the server's configuration").first()).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`admin-previews-${width}-${theme}.png`) });
    } finally {
      // Restore the exact availability choice for subsequent stateful specs.
      const state = await (await page.request.get('/api/admin-features')).json();
      const headers = await csrfHeaders(page);
      const response = await page.request.put('/api/admin-features/schedules', { headers, data: { enabled: true, revision: state.revision } });
      expect(response.ok()).toBe(true);
    }
    await page.reload();
    await expect(schedules).toBeChecked();
    await expect.poll(async () => (await page.request.get('/api/schedules')).status()).toBe(200);
  });
}

test('stale feature revision refuses a second tab without widening availability', async ({ page }) => {
  const headers = await csrfHeaders(page);
  const before = await (await page.request.get('/api/admin-features')).json();
  try {
    const off = await page.request.put('/api/admin-features/multirun', { headers, data: { enabled: false, revision: before.revision } });
    expect(off.ok()).toBe(true);
    const stale = await page.request.put('/api/admin-features/multirun', { headers, data: { enabled: true, revision: before.revision } });
    expect(stale.status()).toBe(409);
    expect((await page.request.get('/api/multiruns')).status()).toBe(404);
  } finally {
    const state = await (await page.request.get('/api/admin-features')).json();
    expect((await page.request.put('/api/admin-features/multirun', { headers, data: { enabled: true, revision: state.revision } })).ok()).toBe(true);
  }
});

test('telemetry off still offers deleting data already sent', async ({ page }) => {
  const headers = await csrfHeaders(page);
  const before = await (await page.request.get('/api/admin-features')).json();
  try {
    expect((await page.request.put('/api/admin-features/telemetry', { headers, data: { enabled: false, revision: before.revision } })).ok()).toBe(true);
    await page.goto('/settings#telemetry');
    const card = page.locator('#telemetry');
    await expect(card.getByText('Turned off in Admin features: nothing new is shared')).toBeVisible();
    await expect(card.getByRole('button', { name: 'Delete data already sent' })).toBeVisible();
    expect((await page.request.get('/api/telemetry')).status()).toBe(404);
  } finally {
    const state = await (await page.request.get('/api/admin-features')).json();
    expect((await page.request.put('/api/admin-features/telemetry', { headers, data: { enabled: true, revision: state.revision } })).ok()).toBe(true);
  }
});
