import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('S3 form retains credentials after a network failure and serializes slow double clicks', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let attempts = 0;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  try {
    const id = 'a'.repeat(32);
    await page.route('**/api/host/backups/s3-targets', async route => {
      attempts++;
      if (attempts === 1) return route.abort('internetdisconnected');
      await gate;
      await route.fulfill({ status: 201, json: { id } });
    });
    await page.route(`**/api/host/backups/${id}/setup`, route => route.fulfill({ json: {
      kind: 's3', endpoint: 'https://s3.example.com', bucket: 'demo-backups', region: 'auto', prefix: 'acc/demo',
    } }));
    await page.goto('/host/backups/setup');
    await page.getByRole('combobox', { name: 'Target type', exact: true }).selectOption('s3');
    await page.getByLabel('Endpoint', { exact: true }).fill('https://s3.example.com');
    await page.getByLabel('Bucket', { exact: true }).fill('demo-backups');
    await page.getByLabel('Access key ID', { exact: true }).fill('EXAMPLE');
    await page.getByLabel('Secret access key', { exact: true }).fill('example-secret');
    await page.getByRole('button', { name: 'Prepare S3 target', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Prepare S3 target', exact: true })).toBeEnabled();
    await expect(page.getByLabel('Secret access key', { exact: true })).toHaveValue('example-secret');
    expect(attempts).toBe(1);
    await page.getByRole('button', { name: 'Prepare S3 target', exact: true }).dblclick();
    await expect(page.getByRole('button', { name: 'Preparing…', exact: true })).toBeDisabled();
    await expect.poll(() => attempts).toBe(2);
    release();
    await expect(page.getByText('Separate maintenance credentials', { exact: true })).toBeVisible();
    await expect(page.locator('main')).not.toContainText('example-secret');
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Check S3 storage', exact: true })).toBeVisible();
    await page.getByText('Replace writer credentials', { exact: true }).click();
    await expect(page.getByLabel('New secret access key', { exact: true })).toHaveValue('');
  } finally {
    release();
  }
});
