import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('Back and a second S3 target do not reuse the first target’s check result', async ({ page }) => {
  let targets = 0;
  await page.route('**/api/host/backups/s3-targets', route => route.fulfill({ status: 201, json: { id: String(++targets).repeat(32) } }));
  await page.route('**/api/host/backups/*/setup', route => route.fulfill({ json: {
    kind: 's3', endpoint: 'https://s3.example.com', bucket: 'demo-backups', region: 'auto', prefix: 'acc/demo',
  } }));
  await page.route('**/api/host/backups/*/check', route => route.fulfill({ json: { kind: 's3', immutability: null } }));
  await page.goto('/host/backups/setup');
  for (let i = 0; i < 2; i++) {
    await page.getByRole('combobox', { name: 'Target type', exact: true }).selectOption('s3');
    await page.getByLabel('Endpoint', { exact: true }).fill('https://s3.example.com');
    await page.getByLabel('Bucket', { exact: true }).fill('demo-backups');
    await page.getByLabel('Access key ID', { exact: true }).fill('EXAMPLE');
    await page.getByLabel('Secret access key', { exact: true }).fill('example-secret');
    await page.getByRole('button', { name: 'Prepare S3 target', exact: true }).click();
    await expect(page.getByText('Separate maintenance credentials', { exact: true })).toBeVisible();
    await expect(page.getByText('Setup verified;', { exact: false })).toBeHidden();
    if (i === 0) {
      await page.getByRole('button', { name: 'Check setup', exact: true }).click();
      await expect(page.getByText('Setup verified;', { exact: false })).toBeVisible();
      await page.goBack();
      await page.goForward();
      await expect(page.getByRole('heading', { name: 'Check S3 storage', exact: true })).toBeVisible();
      await expect(page.getByText('Setup verified;', { exact: false })).toBeHidden();
      await page.goBack();
    }
  }
  expect(targets).toBe(2);
});
