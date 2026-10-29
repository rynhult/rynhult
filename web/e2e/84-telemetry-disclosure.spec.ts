import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

const report = { id: 'review-example', kind: 'reports', state: 'queued', body: '{"schema":"report/1","message":"Example failure"}',
  created: 1792800000, result: '', attempts: 0, receipt: '', digest: 'a'.repeat(64), destination: 'https://telemetry.example.com' };
const state = { consent: { usage: false, reports: false, by: 'alex', at: '2026-10-26T12:00:00Z', usage_schema: 'usage/1', report_schema: 'report/1' },
  url: 'https://telemetry.example.com', total: 2, url_locked: false, policy: {}, items: [
    report, { ...report, id: 'failed-example', state: 'retry', result: 'sending failed' },
  ] };

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`consent and pending review stay visible at ${width}px in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    let submitted: unknown;
    await page.route(/\/api\/telemetry(?:\?.*)?$/, route => route.request().method() === 'PUT'
      ? (submitted = route.request().postDataJSON(), route.fulfill({ json: {} }))
      : route.fulfill({ json: state }));
    await page.goto('/settings#telemetry');
    const card = page.locator('#telemetry');
    const operations = card.locator('details').filter({ has: page.locator('summary', { hasText: /^Collector and consent details$/ }) });
    const history = card.locator('details').filter({ has: page.locator('summary', { hasText: /^History and delivery status/ }) });
    await expect(operations).not.toHaveAttribute('open', '');
    await expect(history).not.toHaveAttribute('open', '');
    await expect(page.getByLabel('Collector URL')).toBeHidden();
    await expect(card.getByText(/Collector destination/)).toContainText(state.url);
    await expect(card.getByText(/unknown secrets or names can be missed/)).toBeVisible();
    await expect(card.getByText(/sending failed/).first()).toBeVisible();
    await expect(card.getByRole('button', { name: 'Delete data already sent', exact: true })).toBeVisible();
    await expect(page.getByLabel('Also delete previously sent data when turning a choice off')).toBeChecked();
    await expect(card.getByRole('button', { name: 'Older log entries' })).toHaveCount(0);
    const pending = card.getByRole('group', { name: 'Reports awaiting review' });
    await expect(pending).toContainText('queued');
    await pending.locator('summary').click();
    await expect(pending.getByRole('region', { name: 'Payload review-example' })).toHaveText(report.body);
    await expect(pending.getByRole('button', { name: 'Send this report' })).toBeVisible();
    await expectAxeClean(page, '#telemetry');
    await operations.locator('summary').focus();
    await page.keyboard.press('Enter');
    await page.getByLabel('Collector URL').fill('https://draft.example.com');
    await page.getByLabel('Share usage statistics').check();
    await operations.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(operations.locator('summary')).toBeFocused();
    await expect(page.getByLabel('Share usage statistics')).toBeChecked();
    await history.locator(':scope > summary').click();
    await expect(history).toContainText('sending failed');
    await expectAxeClean(page, '#telemetry');
    await page.getByRole('button', { name: 'Save privacy choices', exact: true }).click();
    expect(submitted).toEqual({ usage: true, reports: false, url: 'https://draft.example.com', delete_previous: true });
    expect(await card.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  });
}

test('policy and missing collector force off independently without hiding deletion', async ({ page }) => {
  let current = { ...state, url_locked: true, consent: { ...state.consent, usage: true, reports: true }, policy: { usage: 'off' } };
  await page.route(/\/api\/telemetry(?:\?.*)?$/, route => route.fulfill({ json: current }));
  await page.goto('/settings#telemetry');
  await expect(page.getByLabel('Share usage statistics')).toBeDisabled();
  await expect(page.getByLabel('Share usage statistics')).not.toBeChecked();
  await expect(page.getByLabel('Offer bug reports for review')).toBeEnabled();
  await expect(page.getByLabel('Offer bug reports for review')).toBeChecked();
  await expect(page.getByText('The collector address is managed by the installation configuration.', { exact: true })).toBeVisible();
  await page.getByText('Collector and consent details', { exact: true }).click();
  await expect(page.getByLabel('Collector URL')).toBeDisabled();
  current = { ...current, url: '', policy: { usage: 'off' } };
  await page.reload();
  await expect(page.getByLabel('Offer bug reports for review')).toBeDisabled();
  await expect(page.getByLabel('Offer bug reports for review')).not.toBeChecked();
  await expect(page.getByText('Collection is disabled because no collector address is configured.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Delete data already sent', exact: true })).toBeEnabled();
});

test('history retains receipts and paging exposes older pending reports', async ({ page }) => {
  const reads: string[] = [];
  await page.route(/\/api\/telemetry(?:\?.*)?$/, route => {
    reads.push(route.request().url());
    const older = new URL(route.request().url()).searchParams.get('offset') === '50';
    return route.fulfill({ json: { ...state, total: 51, items: older ? [report] : Array.from({ length: 50 }, (_, i) => ({
      ...report, id: 'sent-' + i, kind: 'deletion', state: 'sent', result: 'sent', receipt: 'example-receipt-' + i,
    })) } });
  });
  await page.goto('/settings#telemetry');
  const card = page.locator('#telemetry');
  await expect(card.getByText(/Older pages can also contain reports awaiting review/)).toBeVisible();
  await card.locator('summary').filter({ hasText: /^History and delivery status/ }).click();
  await card.locator('summary').filter({ hasText: /^deletion · sent/ }).first().click();
  await expect(card.getByText('Deletion receipt: example-receipt-0', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Older log entries', exact: true }).click();
  await expect(page.getByRole('group', { name: 'Reports awaiting review' })).toContainText('queued');
  await expect(page.getByRole('button', { name: 'Older log entries', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Previous log entries', exact: true }).click();
  await expect(card).toContainText('Showing entries 1–50 of 51');
  expect(reads.some(url => url.endsWith('offset=50'))).toBe(true);
});
