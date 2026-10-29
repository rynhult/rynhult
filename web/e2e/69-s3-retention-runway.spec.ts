import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`S3 runway warns at seven days and offers independent maintenance and rotation at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.route('**/api/host/backups', route => route.fulfill({ json: { coverage: 'Encrypted control state',
      quarterly_due: false, drill: { at: Date.now() / 1000 }, targets: [{
        id: 'a'.repeat(32), kind: 's3', repository: 's3:https://s3.example.com/demo-backups/acc/demo/repository',
        enabled: true, setup_verified_at: 1, stale: false, status: 'protected · verified 30-day Object Lock',
        captured_at: Date.now() / 1000, verified_at: Date.now() / 1000, restore_tested_at: Date.now() / 1000, last_successful_off_machine_at: Date.now() / 1000,
        immutable_until: Date.now() / 1000 + 37 * 86400, immutability_checked_at: Date.now() / 1000,
        retention_runway_days: 7, retention_warning: true, retention_lapsed: false,
      }] } }));
    await page.goto('/host');
    await expect(page.getByTestId('backup-status')).toContainText('retention maintenance due');
    const details = page.getByRole('button', { name: 'Details', exact: true });
    await details.focus();
    await page.keyboard.press('Enter');
    const runway = page.getByTestId('retention-runway');
    await expect(runway).toContainText('Retention runway: 7 days');
    await expect(runway).toContainText('Maintenance due');
    await expect(page.getByRole('link', { name: 'Retention maintenance guide' })).toHaveAttribute('href', '/guide/User-Guide/Backups#independent-retention-maintenance');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await (width === 390 ? runway.locator('..') : page.locator('#host-backups')).screenshot({ path: wikiShotPath(testInfo, `s3-retention-runway-${width}.png`) });
    await page.getByRole('link', { name: 'Start fresh S3 repository' }).focus();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/host\/backups\/setup\?kind=s3$/);
    await expect(page.getByLabel('Target type')).toHaveValue('s3');
    await expect(page.getByLabel('Secret access key', { exact: true })).toHaveValue('');
    await expect(page.getByRole('button', { name: 'Prepare S3 target' })).toBeVisible();
  });
}
for (const [days, lapsed, text] of [[30, false, 'Retention runway: 30 days'], [0, true, '30-day protection window has lapsed'], [null, null, 'Retention runway: Unverified']] as const) {
  test(`S3 retention keeps ${text} honest`, async ({ page }) => {
    await page.route('**/api/host/backups', route => route.fulfill({ json: { coverage: 'Encrypted control state', quarterly_due: false, drill: {}, targets: [{
      id: 'b'.repeat(32), kind: 's3', repository: 's3:https://s3.example.com/demo-backups/acc/demo/repository', enabled: true,
      status: 'backed up, not protected', stale: false, retention_runway_days: days, retention_warning: days === 0, retention_lapsed: lapsed,
    }] } }));
    await page.goto('/host#host-backups');
    await expect(page.getByTestId('retention-runway')).toContainText(text);
    if (days === 30) await expect(page.getByTestId('backup-status')).not.toContainText('retention maintenance due');
  });
}

test('maintenance guidance opens the shipped runbook and a parseable restricted IAM policy', async ({ page }) => {
  await page.route('**/api/host/backups', route => route.fulfill({ json: { coverage: 'Encrypted control state',
    quarterly_due: false, drill: {}, targets: [{ id: 'c'.repeat(32), kind: 's3',
      repository: 's3:https://s3.example.com/demo-backups/acc/demo/repository', enabled: true,
      status: 'backed up, not protected', stale: false, retention_runway_days: 7, retention_warning: true,
    }] } }));
  await page.goto('/host#host-backups');
  await page.getByRole('link', { name: 'Retention maintenance guide' }).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/guide\/User-Guide\/Backups#independent-retention-maintenance$/);
  await expect(page.getByRole('heading', { name: 'Independent retention maintenance', exact: true })).toBeVisible();
  const policy = JSON.parse(await page.locator('article pre').filter({ hasText: 'ListOnlyThisRepositoryVersions' }).innerText());
  expect(policy.Statement.flatMap((statement: { Action: string | string[] }) => statement.Action).sort()).toEqual([
    's3:GetObjectRetention', 's3:GetObjectVersion', 's3:ListBucketVersions', 's3:PutObjectRetention',
  ]);
  await expect(page.locator('article')).toContainText('Stop all');
});
