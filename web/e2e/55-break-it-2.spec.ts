import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });

test('report review actions remain reachable on a narrow phone', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const state = await (await page.request.get('/api/telemetry')).json();
  await page.route(/\/api\/telemetry(?:\?.*)?$/, route => route.fulfill({ json: {
    ...state, url: 'https://telemetry.example.com', items: [{ id: 'phone-report', kind: 'reports', state: 'queued',
      body: JSON.stringify({ schema: 'report/1', message: 'مرحبا 🧪 ' + 'x'.repeat(10_000) }),
      created: 1792800000, result: '', receipt: '', digest: 'a'.repeat(64), destination: 'https://telemetry.example.com' }], total: 1,
  } }));
  await page.goto('/settings#telemetry');
  await page.getByRole('group', { name: 'Reports awaiting review' }).locator('summary').click();
  for (const name of ['Send this report', 'Don’t send', 'Remove message']) {
    const box = await page.getByRole('button', { name, exact: true }).boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(320);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await expectAxeClean(page, '#telemetry');
  await page.getByRole('group', { name: 'Reports awaiting review' }).locator('summary').evaluate(el => el.scrollIntoView({ block: 'start' }));
  const summary = (await page.getByRole('group', { name: 'Reports awaiting review' }).locator('summary').boundingBox())!;
  await expect(page.getByRole('button', { name: 'Older log entries', exact: true })).toHaveCount(0);
  const navigation = (await page.getByRole('button', { name: 'Remove message', exact: true }).boundingBox())!;
  const top = Math.max(0, summary.y - 4);
  await page.screenshot({ path: testInfo.outputPath('phone-report.png'), clip: {
    x: 16, y: top, width: 288, height: navigation.y + navigation.height + 16 - top,
  } });
});
