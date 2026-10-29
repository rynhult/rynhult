import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

test('phone owner can read scoped credential instructions and reach the form', async ({ page }, testInfo) => {
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  await page.getByLabel('Credential provider').selectOption('azure');
  await expect(page.getByRole('dialog').getByText('Preview: not yet qualified for this release', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByText('Cloudflare is the qualified provider.', { exact: true })).toBeVisible();
  await page.getByRole('dialog').locator('details summary').click();
  await expect(page.getByRole('link', { name: 'Create an Azure service principal' })).toBeVisible();
  await page.getByLabel('Client secret', { exact: true }).fill('example-phone-secret');
  await page.getByLabel('Credential zone').fill('example.com');
  await expectAxeClean(page);
  await page.getByLabel('Credential provider').selectOption('route53');
  await expect(page.getByLabel('Secret access key')).toHaveValue('');
  await page.getByRole('dialog').locator('details summary').click();
  await page.getByRole('link', { name: 'Restrict Route 53 permissions' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-dns-mobile.png') });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('phone owner can reach every wave 2 credential field and cancel safely', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  for (const [provider, field] of [
    ['googleclouddns', 'Service account JSON'], ['digitalocean', 'DigitalOcean API token'], ['hetzner', 'Hetzner DNS API token'],
  ]) {
    await page.getByLabel('Credential provider').selectOption(provider);
    await page.getByLabel(field, { exact: true }).scrollIntoViewIfNeeded();
    await expect(page.getByLabel(field, { exact: true })).toBeInViewport();
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeInViewport();
    await expectAxeClean(page);
    if (provider === 'googleclouddns') {
      await page.screenshot({ path: wikiShotPath(testInfo, 'settings-dns-google-mobile.png') });
    }
  }
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
