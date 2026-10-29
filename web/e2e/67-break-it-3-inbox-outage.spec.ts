import { test, expect, type Locator, type Page } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

async function recoverAfterRetry(page: Page, status: Locator) {
  let requested = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.exposeFunction('inboxRetryDispatched', () => { requested = true; });
  const retry = status.getByRole('button', { name: 'Retry Inbox', exact: true });
  await retry.evaluate(button => button.addEventListener('click', () => {
    void (window as Window & { inboxRetryDispatched: () => Promise<void> }).inboxRetryDispatched();
  }, { once: true }));
  await page.route(/\/api\/attention(?:\?.*)?$/, async route => {
    // An automatic poll before the click must still fail, not block Retry behind the gate.
    if (!requested) return route.fallback();
    await gate;
    await route.fulfill({ json: [] });
  });
  try {
    await retry.click();
    await expect(retry).toBeDisabled();
    await expect(status).toContainText('Inbox unavailable');
    release();
    await expect(status).toBeHidden();
  } finally {
    release();
  }
}

for (const theme of ['dark', 'light'] as const) for (const width of [1440, 390]) test(`Inbox-only outage is visible without blocking content at ${width} in ${theme}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  await page.emulateMedia({ colorScheme: theme });
  await page.route(/\/api\/attention(?:\?.*)?$/, route =>
    route.fulfill({ status: 503, json: { detail: 'Test Inbox temporarily unavailable' } }));
  await page.goto('/');
  const status = page.getByRole('region', { name: 'Inbox status' });
  await expect(status).toContainText('Inbox unavailable', { timeout: 10000 });
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeInViewport();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await status.boundingBox())!.height).toBeLessThanOrEqual(96);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
  if (theme === 'dark') await page.screenshot({ path: wikiShotPath(testInfo, `inbox-unavailable-${width === 390 ? 'phone' : 'desktop'}.png`) });
  await recoverAfterRetry(page, status);
});

test('collapsing ordinary notices never hides a later Inbox refresh failure', async ({ page }) => {
  let failed = false;
  const at = new Date().toISOString();
  await page.route(/\/api\/attention(?:\?.*)?$/, route => failed
    ? route.abort('internetdisconnected')
    : route.fulfill({ json: [{
      id: 6701, kind: 'host', severity: 'info', title: 'Demo notice', detail: null, source: 'test', verified: true,
      session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
      acked_at: null, resolved_at: null, snoozed_until: null, publish_id: null, persistent: null,
    }] }));
  await page.goto('/');
  const status = page.getByRole('region', { name: 'Inbox status' });
  await expect(status).toContainText('Inbox (1)');
  await status.getByRole('button', { name: 'Collapse other notices' }).click();
  await expect(status).toBeHidden();
  failed = true;
  await expect(status).toContainText('Inbox unavailable', { timeout: 10000 });
  await expect(status.getByRole('link')).toHaveText('Inbox (1) · stale');
  await recoverAfterRetry(page, status);
});
