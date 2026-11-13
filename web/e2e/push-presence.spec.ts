import { expect, test } from './fixtures';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test.beforeEach(async ({ page }) => {
  // Model a person with a push device (possibly on another browser).
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), push_presence_enabled: true } });
  });
});

test('visible focused Inbox reports presence and visibility changes end it', async ({ page }) => {
  const leases: { active: boolean; scope: string; idle_ms: number }[] = [];
  await page.route('**/api/push/presence', async route => {
    leases.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/inbox');
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })
    .getByRole('link', { name: /^All items · / })).toBeVisible();
  await page.bringToFront();
  await page.locator('body').click({ position: { x: 5, y: 5 } });
  await expect.poll(() => leases.some(lease => lease.active && lease.scope === '/inbox')).toBe(true);
  // Chromium's headless page lifecycle does not expose OS tab hiding. Exercise
  // the real visibility event against its browser-reported property.
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
  });
  await expect.poll(() => leases.at(-1)?.active).toBe(false);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
  });
  await expect.poll(() => leases.at(-1)?.active).toBe(true);
});


test('late presence rejection preserves the current document', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/push/presence', route => {
    requests++;
    return route.fulfill({ status: requests === 1 ? 200 : 401, json: {} });
  });
  await page.goto('/inbox');
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeVisible();
  await expect.poll(() => requests).toBe(1);
  await page.evaluate(() => {
    document.documentElement.dataset.presenceProbe = 'same-document';
    document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
  });
  await expect.poll(() => requests).toBeGreaterThan(1);
  await page.waitForTimeout(500);
  await expect(page.locator('html')).toHaveAttribute('data-presence-probe', 'same-document');
});

test('a person without push devices does not send presence', async ({ page }) => {
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), push_presence_enabled: false } });
  });
  let requests = 0;
  await page.route('**/api/push/presence', route => {
    requests++;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/inbox');
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeVisible();
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange', { bubbles: true })));
  await page.waitForTimeout(500);
  expect(requests).toBe(0);
});
