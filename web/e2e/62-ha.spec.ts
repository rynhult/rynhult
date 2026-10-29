import { expect, test } from '@playwright/test';
import { expectAxeClean, wikiShotPath } from './helpers';

test('standby shows observation only and active shows current epoch', async ({ page }, testInfo) => {
  let role = 'standby';
  await page.route('**/api/health', route => route.fulfill({ json: {
    ok: true, ha: { enabled: true, role, epoch: 7, database: 'ok', automatic_failover: false,
      automatic_evaluation: { evaluation_enabled: false, would_promote: false, reason: 'disabled', promotion_wired: false } },
  } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'High availability' })).toBeVisible();
  await expect(page.getByText('Standby · read-only', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'New session' })).toHaveCount(0);
  await expect(page.getByText('7', { exact: true })).toBeVisible();
  await expect(page.getByText('Would promote now: No')).toBeVisible();
  await expect(page.getByText(/Disabled · automatic failover evaluation is off/)).toBeVisible();
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, 'ha-standby.png'), fullPage: true });
  role = 'active';
  await page.goto('/ha');
  await expect(page.getByText('Active main', { exact: true })).toBeVisible();
  await expect(page.getByText('Manual promotion only')).toBeVisible();
});

test('standby marks cached health as stale when refresh fails', async ({ page }) => {
  let unavailable = false;
  await page.route('**/api/health', route => route.fulfill(unavailable ? { status: 503, json: { detail: 'Unavailable' } } : {
    json: { ok: true, ha: { enabled: true, role: 'standby', epoch: 7, database: 'ok', automatic_failover: false,
      automatic_evaluation: { evaluation_enabled: false, would_promote: false, reason: 'disabled', promotion_wired: false } } },
  }));
  await page.goto('/');
  await expect(page.getByText('Standby · read-only', { exact: true })).toBeVisible();
  unavailable = true;
  await expect(page.getByText(/Showing the last data received; refresh failed/)).toBeVisible({ timeout: 12000 });
});

test('an uncertain command outcome is visible before failover', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ json: {
    ok: true, ha: { enabled: true, role: 'standby', epoch: 7, database: 'ok', uncertain: true, automatic_failover: false },
  } }));
  await page.goto('/');
  await expect(page.getByText(/A command outcome is uncertain/)).toBeVisible();
  await expect(page.getByText(/clean handover is blocked/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'New session' })).toHaveCount(0);
});

test('automatic evaluation is observation only when enabled', async ({ page }, testInfo) => {
  await page.route('**/api/health', route => route.fulfill({ json: {
    ok: true, ha: { enabled: true, role: 'standby', epoch: 7, database: 'ok', automatic_failover: false,
      automatic_evaluation: { evaluation_enabled: true, would_promote: false,
        reason: 'telemetry_unavailable', promotion_wired: false } },
  } }));
  await page.goto('/ha');
  await expect(page.getByText('Would promote now: No')).toBeVisible();
  await expect(page.getByText(/Telemetry unavailable/)).toBeVisible();
  await expect(page.getByText(/Automatic promotion is not connected/)).toBeVisible();
  await expect(page.getByRole('button', { name: /promote/i })).toHaveCount(0);
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, 'ha-evaluation.png'), fullPage: true });
});

test('missing evaluation data is unavailable rather than disabled', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ json: {
    ok: true, ha: { enabled: true, role: 'standby', epoch: 7, database: 'unavailable', automatic_failover: false },
  } }));
  await page.goto('/ha');
  await expect(page.getByText(/Evaluation unavailable/)).toBeVisible();
  await expect(page.getByText(/Disabled · automatic failover evaluation is off/)).toHaveCount(0);
});
