import { expect, test } from '@playwright/test';
import { expectAxeClean, wikiShotPath } from './helpers';

test.use({ viewport: { width: 390, height: 844 } });
test('standby status fits a phone', async ({ page }, testInfo) => {
  await page.route('**/api/health', route => route.fulfill({ json: {
    ok: true, ha: { enabled: true, role: 'standby', epoch: 7, database: 'ok', automatic_failover: false,
      automatic_evaluation: { evaluation_enabled: false, would_promote: false, reason: 'disabled', promotion_wired: false } },
  } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'High availability' })).toBeVisible();
  await expect(page.getByText('Standby · read-only', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, 'ha-standby-phone.png'), fullPage: true });
});
