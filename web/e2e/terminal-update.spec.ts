import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE, serviceWorkers: 'block' });
test.beforeEach(async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'terminal-webkit', 'Focused WebKit update checks.');
  await page.goto('/sessions');
});

test('a changed server build offers reload and preserves the page until confirmed', async ({ page }) => {
  const state = await (await page.request.get('/api/auth/state')).json();
  expect(state.instance.build).toBeTruthy();
  await page.route('**/api/auth/state', route => route.fulfill({ json: {
    ...state, instance: { ...state.instance, build: 'new-deployed-build' },
  } }));
  await expect(page.getByRole('region', { name: 'Update required' }).getByText('A new version is available — reload', { exact: true })).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: 'Reload', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('unsaved');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Update required' }).getByText('A new version is available — reload', { exact: true })).toBeVisible();
  await page.unroute('**/api/auth/state');
  await page.getByRole('button', { name: 'Reload', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Reload', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Update required' }).getByText('A new version is available — reload', { exact: true })).toHaveCount(0);
});

test.describe('worker-controlled reload', () => {
  test.use({ serviceWorkers: 'allow' });
  test('the notification worker never serves a cached application shell', async ({ page }) => {
    await page.evaluate(async () => {
      const registration = await navigator.serviceWorker.ready;
      await registration.update();
    });
    expect(await page.evaluate(async () => (await navigator.serviceWorker.ready).updateViaCache)).toBe('none');
    await page.reload();
    await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
    const response = await page.reload();
    expect(response?.fromServiceWorker()).toBe(false);
    expect(response?.headers()['cache-control']).toContain('no-cache');
    expect(await page.evaluate(() => caches.keys())).toEqual([]);
    const worker = await page.request.get('/sw.js');
    expect(worker.headers()['cache-control']).toContain('no-cache');
  });
});
