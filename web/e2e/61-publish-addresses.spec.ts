import { wikiShotPath } from "./helpers";
import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, csrfHeaders } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("owner manages publishing targets and reviews detection before saving", async ({ page }, testInfo) => {
  const label = `Example edge ${randomUUID().slice(0, 8)}`;
  await page.route('**/api/publish/addresses/detect', route => route.fulfill({ json: {
    suggestions: [{ address: '192.0.2.42', source: 'Interface demo', access: 'public' }],
    agreement: false, failures: ['https://api.ipify.org'],
  } }));
  await page.goto('/settings#publishing');
  const headers = await csrfHeaders(page);
  let addressId: string | undefined;
  try {
    const card = page.getByRole('region', { name: 'Publishing addresses' });
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: 'Detect', exact: true }).click();
    await expect(card.getByText(/Services disagree or could not/)).toBeVisible();
    await card.getByRole('button', { name: 'Use 192.0.2.42' }).click();
    await expect(card.getByLabel('Address or CIDR')).toHaveValue('192.0.2.42');
    await card.getByLabel('Address label').fill(label);
    await card.getByRole('button', { name: 'Add address', exact: true }).click();
    await expect(card.getByText(/globally routable/)).toBeVisible();
    // Internal inventory accepts documentation addresses for this artificial demo.
    await card.getByLabel('Address access').selectOption('internal');
    const added = page.waitForResponse(response => response.url().endsWith('/api/publish/addresses') && response.request().method() === 'POST');
    await card.getByRole('button', { name: 'Add address', exact: true }).click();
    const response = await added;
    expect(response.status()).toBe(201);
    addressId = (await response.json()).find((entry: { label: string }) => entry.label === label)?.id;
    expect(addressId).toBeTruthy();
    await expect(card.getByText(label, { exact: true })).toBeVisible();
    await card.screenshot({ path: wikiShotPath(testInfo, 'publishing-addresses.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await card.screenshot({ path: wikiShotPath(testInfo, 'publishing-addresses-mobile.png') });
    await card.getByRole('button', { name: `Remove ${label}` }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Remove address', exact: true }).click();
    await expect(card.getByText(label, { exact: true })).toHaveCount(0);
  } finally {
    if (addressId) {
      const removed = await page.request.post(`/api/publish/addresses/${addressId}/remove`, { headers });
      // The UI removal above may already have removed this test's address.
      if (!removed.ok()) {
        const rows = await (await page.request.get('/api/publish/addresses')).json();
        expect(rows.some((entry: { id: string }) => entry.id === addressId)).toBe(false);
      }
    }
  }
});

test("owner picks a host from a block and reviews the existing site's pinned address", async ({ page }, testInfo) => {
  await page.route('**/api/publish/zones', route => route.fulfill({ json: { configured: true,
    zones: [{ id: 'demo', name: 'example.com' }], public_ip: '192.0.2.10', internal_ip: '' } }));
  await page.route('**/api/publish/addresses', route => route.fulfill({ json: [
    { id: 'edge', label: 'Example edge block', address: '192.0.2.0/24', access: 'public', default: true, source: 'owner' },
  ] }));
  const live = { id: 'pub-demo', host: 'demo.example.com', port: 8100, access: 'public',
    action: 'publish', status: 'applied', target_ip: '192.0.2.10', requested_by: 'alex', decided_by: 'alex',
    created_at: new Date().toISOString(), detail: null, reason: null, project_id: null, session_id: null,
    completed_at: null, decided_at: null, zone_name: 'example.com', replaces: null, port_owner: null };
  let submitted: Record<string, unknown> | undefined;
  await page.route(/\/api\/publish(?:\?.*)?$/, async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON();
      await route.fulfill({ json: { ...live, id: 'pub-next', status: 'approved' }, status: 201 });
    } else await route.fulfill({ json: [live] });
  });
  await page.goto('/publishing');
  await expect(page.getByText('DNS target: 192.0.2.10', { exact: true })).toBeVisible();
  await page.getByLabel('Subdomain').fill('demo');
  await page.getByRole('combobox', { name: 'Access', exact: true }).selectOption('public');
  await page.getByText('Another service or port', { exact: true }).click();
  await page.getByLabel('Local port').fill('8100');
  await page.getByRole('combobox', { name: 'DNS address', exact: true }).selectOption('edge');
  await page.getByLabel('Host address from block').fill('192.0.2.42');
  await page.screenshot({ path: testInfo.outputPath('publishing-block-selection.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath('publishing-block-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('192.0.2.42');
  await page.getByRole('dialog').getByRole('button', { name: 'Replace the live site', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Publish publicly', exact: true }).click();
  await expect.poll(() => submitted?.target_ip).toBe('192.0.2.42');
  expect(submitted?.replace).toBe(true);
});
