import { expect, test } from "@playwright/test";
import { AUTH_FILE, PASSWORD, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("owner creates isolated credentials and confirms a DNS binding", async ({ page }, testInfo) => {
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  await page.getByLabel('Credential name').fill('Cloudflare certificates');
  await page.getByLabel('Cloudflare API token').fill('dns-example-canary-12345678');
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Save credential', exact: true }).click();
  await expect(page.getByText('Cloudflare certificates', { exact: true }).first()).toBeVisible();
  await page.route('**/api/dns/credentials/*/validate', route => route.fulfill({ json: { status: 'readable', revision: 1, issuance: 'unverified' } }));
  await page.getByRole('button', { name: 'Validate Cloudflare certificates', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Validate credential', exact: true }).click();
  await expect(page.locator('#infrastructure-keys').getByText('Credential readable. Certificate issuance remains unverified.', { exact: true })).toBeVisible();
  await page.unroute('**/api/dns/credentials/*/validate');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  await page.getByLabel('Credential name').fill('Cloudflare publishing');
  await page.getByLabel('Credential purpose').selectOption('publishing');
  await page.getByLabel('Cloudflare API token').fill('dns-example-publishing-87654321');
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Save credential', exact: true }).click();
  await expect(page.getByText('Cloudflare publishing', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Configure DNS' }).click();
  await expect(page.getByLabel('DNS provider')).toHaveValue('cloudflare');
  await page.getByLabel('Zone name').fill('example.com');
  await page.getByLabel('Certificate credential').selectOption({ label: 'Cloudflare certificates' });
  await page.getByLabel('Publishing credential').selectOption({ label: 'Cloudflare publishing' });
  await page.getByLabel('Confirm provider and zone').check();
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Save DNS binding' }).click();
  await expect(page.getByText('Saved, not applied', { exact: true })).toBeVisible();
  await expect(page.getByText('dns-example-canary-12345678')).toHaveCount(0);
  await page.locator('#dns-certificates').scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-dns.png') });
  // Unavailable privileged service is actionable and cannot produce a false Applied state.
  await page.getByRole('button', { name: 'Apply DNS binding' }).click();
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Confirm apply' }).click();
  await expect(page.getByText('The DNS helper on the main machine is unavailable, so the saved settings were not applied')).toBeVisible();
});

test('owner sees zone-scoped Azure and Route 53 setup instructions', async ({ page }, testInfo) => {
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  await page.getByLabel('Credential provider').selectOption('azure');
  await page.getByRole('dialog').locator('details summary').click();
  await expect(page.getByText('DNS Zone Contributor', { exact: false }).last()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Create an Azure service principal' })).toHaveAttribute('href', /learn.microsoft.com/);
  await page.getByLabel('Credential name').fill('Azure trial certificates');
  await page.getByLabel('Tenant ID').fill('11111111-1111-1111-1111-111111111111');
  await page.getByLabel('Subscription ID').fill('22222222-2222-2222-2222-222222222222');
  await page.getByLabel('Client ID', { exact: true }).fill('33333333-3333-3333-3333-333333333333');
  await page.getByLabel('Client secret', { exact: true }).fill('example-trial-secret');
  await page.getByLabel('Resource group').fill('dns-trial');
  await page.getByLabel('Credential zone').fill('example.com');
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Save credential', exact: true }).click();
  await expect(page.getByText('Azure trial certificates', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  await page.getByLabel('Credential provider').selectOption('route53');
  await page.getByRole('dialog').locator('details summary').click();
  await expect(page.getByRole('link', { name: 'Restrict Route 53 permissions' })).toHaveAttribute('href', /docs.aws.amazon.com/);
  await expect(page.getByLabel('Hosted zone ID')).toBeVisible();
  await expect(page.getByText('Pending live qualification', { exact: false }).last()).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-dns-route53.png') });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
});

test('owner can find a publication ID and submit a reviewed adoption request', async ({ page }, testInfo) => {
  await page.route('**/api/publish/zones', route => route.fulfill({ json: { zones: [], configured: false } }));
  await page.route(/\/api\/publish(?:\?.*)?$/, route => route.fulfill({ json: [{ id: 'pub-review-example', action: 'publish',
    host: 'demo.example.com', port: 8100, access: 'public', status: 'applied', target_ip: '192.0.2.10',
    requested_by: 'alex', decided_by: 'alex', created_at: new Date().toISOString(), detail: null,
    reason: null, project_id: null, session_id: null, completed_at: null, decided_at: null,
    zone_name: 'example.com', replaces: null, port_owner: null }] }));
  await page.goto('/publishing');
  await expect(page.getByText('Publication ID: pub-review-example', { exact: true })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, 'publishing-adoption-id.png') });
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Adopt reviewed legacy record' }).click();
  await page.getByLabel('Reviewed publication ID').fill('pub-review-example');
  await page.getByRole('dialog').getByLabel('Current password', { exact: true }).fill(PASSWORD);
  await page.getByRole('button', { name: 'Adopt record', exact: true }).click();
  // Read-only display fixture is deliberately absent from backend history: refusal precedes all DNS I/O.
  await expect(page.getByText('No applied publication has that ID', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
});

test('a retired legacy Publishing token cannot be restored', async ({ page }) => {
  const binding = { provider: 'cloudflare', zone: 'example.com', certificate_id: 'c1', publishing_id: 'p1', revision: 2,
    applied_revision: null, status: 'saved', generation: null, legacy_publishing: 'retired' };
  await page.route('**/api/dns/binding', route => route.fulfill({ json: { ...binding, publishing_source: 'vault' } }));
  await page.goto('/settings#dns');
  await expect(page.getByText('The legacy Publishing token was retired when its imported copy was removed', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore legacy Publishing token' })).toHaveCount(0);
  // Retired while selected: jobs are refused (amber note), and switching back to vault stays available.
  await page.unroute('**/api/dns/binding');
  await page.route('**/api/dns/binding', route => route.fulfill({ json: { ...binding, publishing_source: 'legacy' } }));
  await page.reload();
  await expect(page.getByText('Publishing jobs are refused until you select Use vault for Publishing.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Use vault for Publishing' })).toBeVisible();
});

for (const [provider, label, field, help] of [
  ['googleclouddns', 'Google Cloud DNS', 'Service account JSON', 'Google managed-zone permissions'],
  ['digitalocean', 'DigitalOcean', 'DigitalOcean API token', 'DigitalOcean token scopes'],
  ['hetzner', 'Hetzner DNS', 'Hetzner DNS API token', 'Hetzner Cloud DNS API'],
]) {
  test(`owner sees ${label} candidate setup and credential fields`, async ({ page }, testInfo) => {
    await page.goto('/settings#dns');
    await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
    await page.getByLabel('Credential provider').selectOption(provider);
    await expect(page.getByLabel(field, { exact: true })).toBeVisible();
    await page.getByRole('dialog').locator('details summary').click();
    await expect(page.getByRole('link', { name: help, exact: true })).toBeVisible();
    await expect(page.getByText('Pending live qualification', { exact: false }).last()).toBeVisible();
    await page.getByLabel(field, { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: wikiShotPath(testInfo, `settings-dns-${provider}.png`) });
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  });
}

test('release qualification comes from the API in pickers and saved bindings', async ({ page }, testInfo) => {
  await page.goto('/settings#dns');
  await page.getByRole('button', { name: 'Add infrastructure credential' }).click();
  const choices = page.getByLabel('Credential provider');
  await expect(choices.locator('option')).toHaveCount(6);
  await expect(choices).toHaveValue('cloudflare');
  await expect(choices.locator('option[value="cloudflare"]')).toHaveText('Cloudflare');
  for (const provider of ['azure', 'route53', 'googleclouddns', 'digitalocean', 'hetzner']) {
    await expect(choices.locator(`option[value="${provider}"]`)).toContainText('Preview: not yet qualified for this release');
    await choices.selectOption(provider);
    await expect(page.getByRole('dialog').getByText('Preview: not yet qualified for this release', { exact: true })).toBeVisible();
    await expect(page.getByRole('dialog').getByText('Cloudflare is the qualified provider.', { exact: true })).toBeVisible();
  }
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  const binding = { provider: 'route53', qualification: 'preview', zone: 'example.com', certificate_id: 'example-cert',
    publishing_id: 'example-pub', revision: 7, applied_revision: 7, status: 'applied' };
  await page.route('**/api/dns/binding', route => route.fulfill({ json: binding }));
  await page.reload();
  await expect(page.locator('#dns-certificates').getByText('Preview: not yet qualified for this release', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Apply DNS binding', exact: true })).toBeVisible();
  await page.locator('#dns-certificates').scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, 'settings-dns-qualification.png') });
  await page.getByRole('button', { name: 'Configure DNS' }).click();
  await expect(page.getByLabel('DNS provider')).toHaveValue('route53');
  await expect(page.getByLabel('DNS provider').locator('option[value="route53"]')).toContainText('Preview: not yet qualified for this release');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  // Simulate the server qualifying a provider: no client allow-list change is needed.
  await page.route('**/api/dns/providers', route => route.fulfill({ json: [
    { id: 'cloudflare', qualification: 'qualified' }, { id: 'route53', qualification: 'qualified' },
  ] }));
  await page.route('**/api/dns/binding', route => route.fulfill({ json: { ...binding, qualification: 'qualified' } }));
  await page.reload();
  await expect(page.locator('#dns-certificates').getByText('Preview: not yet qualified for this release', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Configure DNS' }).click();
  await expect(page.getByLabel('DNS provider').locator('option[value="route53"]')).toHaveText('Route 53');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.unroute('**/api/dns/providers');
  await page.unroute('**/api/dns/binding');
  await page.reload();
});
