import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`DNS fields precede optional help at ${width}px in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    await page.goto('/settings#dns');
    await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Values are write-only');
    for (const [provider, field, link] of [
      ['cloudflare', 'Cloudflare API token', 'Create a Cloudflare token'],
      ['azure', 'Client secret', 'Create an Azure service principal'],
      ['route53', 'Secret access key', 'Restrict Route 53 permissions'],
      ['googleclouddns', 'Service account JSON', 'Google managed-zone permissions'],
      ['digitalocean', 'DigitalOcean API token', 'DigitalOcean token scopes'],
      ['hetzner', 'Hetzner DNS API token', 'Hetzner Cloud DNS API'],
    ]) {
      await page.getByLabel('Credential provider').selectOption(provider);
      const help = dialog.locator('details');
      await expect(help).not.toHaveAttribute('open', '');
      await expect(dialog.getByRole('link', { name: link, exact: true })).toBeHidden();
      await expect(dialog.getByText('Enter secrets here; never paste them into agent chat.', { exact: true })).toBeVisible();
      if (provider !== 'cloudflare') {
        await expect(dialog.getByText('Cloudflare is the qualified provider.', { exact: true })).toBeVisible();
        await expect(dialog.getByText(/Pending live qualification/)).toBeVisible();
      }
      const input = page.getByLabel(field, { exact: true });
      await expect(input).toHaveAttribute('type', 'password');
      await expect(input).toHaveValue('');
      const scopeWarning = {
        cloudflare: 'not just selected names or types',
        azure: 'never the resource group or subscription',
        route53: 'replacement of all fields before expiry',
        googleclouddns: 'live file-custody and rollback tests remain required',
        digitalocean: 'Authority spans the account',
        hetzner: 'Tokens also permit other project resources',
      }[provider];
      await expect(dialog.getByText(scopeWarning!, { exact: false }).first()).toBeVisible();
      await input.fill('example-draft-secret');
      const summary = help.locator('summary');
      expect(await input.evaluate((node) => !!(node.compareDocumentPosition(document.querySelector('dialog details, [role="dialog"] details')!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
      await summary.focus();
      await page.keyboard.press('Enter');
      await expect(dialog.getByRole('link', { name: link, exact: true })).toBeVisible();
      await expectAxeClean(page);
      await summary.focus();
      await page.keyboard.press('Enter');
      await expect(summary).toBeFocused();
      await expect(input).toHaveValue('example-draft-secret');
      await expect(dialog.getByRole('button', { name: 'Save credential', exact: true })).toBeDisabled();
      expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    }
    await expectAxeClean(page);
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
    await expect(page.getByLabel('Cloudflare API token')).toHaveValue('');
    await expect(page.getByRole('dialog').getByLabel('Current password', { exact: true })).toHaveValue('');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  });
}

test('replacement keeps rotation warnings, password gating and the exact payload', async ({ page }) => {
  await page.route('**/api/dns/credentials', route => route.fulfill({ json: [
    { id: 'example-replace', name: 'Example publishing', provider: 'route53', purpose: 'publishing', revision: 1, disabled: false, qualification: 'preview' },
  ] }));
  let submitted: unknown;
  await page.route('**/api/dns/credentials/example-replace/replace', route => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: {} });
  });
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Replace Example publishing', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/previous encrypted value remains protected for 24 hours/)).toBeVisible();
  await expect(dialog.getByText(/Apply and verify renewal before revoking/)).toBeVisible();
  await expect(dialog.getByText(/Temporary credentials require a session token/)).toBeVisible();
  for (const [label, value] of [
    ['Access key ID', 'example-id'], ['Secret access key', 'example-secret'],
    ['Hosted zone ID', 'EXAMPLE'], ['Credential zone', 'example.com'],
  ]) await dialog.getByLabel(label, { exact: true }).fill(value);
  await expect(dialog.getByRole('button', { name: 'Save credential', exact: true })).toBeDisabled();
  await dialog.getByLabel('Current password', { exact: true }).fill('example-password');
  await dialog.locator('details summary').click();
  await expect(dialog.getByRole('link', { name: /DNS provider setup/ })).toHaveAttribute('target', '_blank');
  await dialog.locator('details summary').click();
  await dialog.getByRole('button', { name: 'Save credential', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(submitted).toEqual({ password: 'example-password', fields: {
    access_key_id: 'example-id', secret_access_key: 'example-secret', hosted_zone_id: 'EXAMPLE', zone_name: 'example.com',
  } });
});
