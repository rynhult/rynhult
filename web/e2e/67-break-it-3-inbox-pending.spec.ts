import { test, expect } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('an initial Inbox outage stays visible while a slow retry is pending', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let hold = false;
  let release!: () => void;
  let started!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(/\/api\/attention(?:\?.*)?$/, async route => {
    if (!hold) return route.fulfill({ status: 503, json: { detail: 'Inbox unavailable' } });
    started();
    await gate;
    return route.fulfill({ json: [] });
  });
  await page.goto('/');
  const status = page.getByRole('region', { name: 'Inbox status' });
  await expect(status).toContainText('Inbox unavailable', { timeout: 10000 });
  hold = true;
  try {
    await status.getByRole('button', { name: 'Retry Inbox', exact: true }).click();
    await requested;
    await expect(status).toContainText('Inbox unavailable');
    await expect(status.getByRole('button', { name: 'Retry Inbox', exact: true })).toBeDisabled();
  } finally {
    release();
  }
  await expect(status).toBeHidden();
});
