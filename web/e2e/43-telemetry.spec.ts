import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeCleanPage, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owner reviews opt-in payloads and can leave both switches off', async ({ page }, testInfo) => {
  await page.goto('/settings#usage-and-bug-reports');
  const card = page.getByRole('heading', { name: 'Usage and bug reports', exact: true }).locator('..').locator('..');
  await expect(page.getByLabel('Share usage statistics')).not.toBeChecked();
  await expect(page.getByLabel('Offer bug reports for review')).not.toBeChecked();
  await page.getByRole('button', { name: 'Show exactly what would be sent' }).click();
  await expect(page.getByRole('region', { name: 'Usage payload' })).toContainText('usage/1');
  const state = await page.request.get('/api/telemetry');
  expect((await state.json()).consent.usage).toBe(false);
  await page.getByText('Collector and consent details', { exact: true }).click();
  await page.getByLabel('Collector URL').fill('http://collector.example.com');
  await page.getByRole('button', { name: 'Save privacy choices' }).click();
  await expect(page.getByText('Use an HTTPS collector URL without credentials, query or fragment')).toBeVisible();
  await page.getByLabel('Collector URL').fill('https://telemetry.example.com');
  await page.getByRole('button', { name: 'Save privacy choices' }).click();
  await expect(page.getByText('Privacy choices saved.')).toBeVisible();
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('telemetry-settings.png') });
  await page.locator('#telemetry').screenshot({ path: wikiShotPath(testInfo, 'usage-and-bug-reports-settings.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('telemetry-settings-phone.png') });
  await expectAxeCleanPage(page, '/settings');
});

test('report review shows the exact text and binds approval to it', async ({ page }, testInfo) => {
  const state = await (await page.request.get('/api/telemetry')).json();
  let approved = false;
  const report = { id: 'demo-report', kind: 'reports', state: 'queued', body: JSON.stringify({ acc: '0.15.0-rc.1', build: 'a'.repeat(64), component: 'supervisor', exception: 'TimeoutError', exit_code: null, fingerprint: 'a'.repeat(16), frames: [{ module: 'acc/server/supervisor.py', function: 'refresh_providers', line: 123 }], install: 'a'.repeat(22), install_kind: 'git-checkout', log: [], message: 'Rynhult failed safely', os: 'linux', provider: null, repeats: '1', report: '11111111-1111-4111-8111-111111111111', schema: 'report/1', trigger: 'supervisor_stage' }),
    created: 1792800000, result: '', attempts: 0, receipt: '', digest: 'a'.repeat(64), destination: 'https://telemetry.example.com' };
  await page.route(/\/api\/telemetry(?:\?.*)?$/, route => route.fulfill({ json: { ...state, consent: { ...state.consent, reports: true },
    items: [{ ...report, state: approved ? 'approved' : 'queued' }], total: 1 } }));
  await page.route('**/api/telemetry/reports/demo-report', async route => {
    expect(route.request().postDataJSON()).toEqual({ decision: 'send', expected_digest: report.digest, remove_message: false });
    approved = true;
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/settings#usage-and-bug-reports');
  await expect(page.getByRole('button', { name: 'Send this report' })).toBeHidden();
  await page.getByRole('group', { name: 'Reports awaiting review' }).locator('summary').click();
  await expect(page.getByRole('region', { name: 'Payload demo-report' })).toHaveText(report.body);
  await page.getByRole('region', { name: 'Payload demo-report' }).locator('..').screenshot({ path: wikiShotPath(testInfo, 'usage-and-bug-reports-review.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('region', { name: 'Payload demo-report' }).locator('..').screenshot({ path: wikiShotPath(testInfo, 'usage-and-bug-reports-review-phone.png') });
  await page.getByRole('button', { name: 'Send this report' }).click();
  await page.locator('#telemetry summary').filter({ hasText: /^History and delivery status/ }).click();
  await expect(page.locator('#telemetry summary').filter({ hasText: /^reports · approved/ })).toBeVisible();
  expect(approved).toBe(true);
});

test('collector changes refresh an already visible exact usage preview', async ({ page }) => {
  const choices = { usage: true, reports: false, delete_previous: false };
  await page.request.put('/api/telemetry', { data: { ...choices, url: 'https://first.example.com' } });
  try {
    await page.goto('/settings#usage-and-bug-reports');
    await page.getByRole('button', { name: 'Show exactly what would be sent' }).click();
    const preview = page.getByRole('region', { name: 'Usage payload' });
    await expect(preview).toContainText('usage/1');
    const previous = await preview.textContent();
    await page.getByText('Collector and consent details', { exact: true }).click();
    await page.getByLabel('Collector URL').fill('https://second.example.com');
    await page.getByRole('button', { name: 'Save privacy choices' }).click();
    await expect(page.getByText('Privacy choices saved.')).toBeVisible();
    const next = (await (await page.request.get('/api/telemetry/preview')).json()).usage;
    expect(next).not.toBe(previous);
    await expect(preview).toHaveText(next);
  } finally {
    await page.request.put('/api/telemetry', {
      data: { usage: false, reports: false, url: 'https://telemetry.example.com', delete_previous: false },
    });
  }
});
