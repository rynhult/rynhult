import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, gitProject } from './helpers';

test.use({ storageState: AUTH_FILE });

test('a valid long gate branch fits a 390px phone', async ({ page }) => {
  const { id } = await gitProject(page, 'b11-gate');
  const branch = `acc/${'review-'.repeat(11)}fix`;
  await page.route('**/api/gates?project=*', async route => {
    const response = await route.fetch();
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, jobs: [{ id: 'b11-gate', branch, base: 'main',
      suite: 'related', priority: 'check', state: 'queued', waiting: 'load', slot: null, attempt: 0,
      reason: null, requested_by: 'alex', results: {}, created_at: new Date().toISOString(), finished_at: null }] } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${id}#project-gates`);
  await expect(page.getByText(branch, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
});

test('a failed Stop goal tells the person it did not stop', async ({ page }, testInfo) => {
  const { id, headers } = await gitProject(page, 'b11-goal');
  const response = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', name: 'Goal failure', automation: {
      goal: { text: 'Review the demo', max_continuations: 2, max_hours: 1 } } } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const session = (await response.json()).id;
  try {
    await page.goto(`/sessions/${session}`);
    await page.route(`**/api/sessions/${session}/automation`, route => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Controller unavailable; try again' }),
    }));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: 'Stop goal', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Session automation' }).getByRole('alert')).toBeVisible();
    await expect(page.getByRole('region', { name: 'Session automation' })).toContainText('Controller unavailable; try again');
    expect((await (await page.request.get(`/api/sessions/${session}`)).json()).automation.goal.text).toBe('Review the demo');
    await page.getByRole('region', { name: 'Session automation' }).screenshot({ path: testInfo.outputPath('stop-goal-error-phone.png') });
    await page.unroute(`**/api/sessions/${session}/automation`);
    await page.getByRole('button', { name: 'Stop goal', exact: true }).click();
    await expect(page.getByText('Goal status: stopped', { exact: true })).toBeVisible();
    await expect(page.getByText('Controller unavailable; try again', { exact: true })).toHaveCount(0);
  } finally {
    await page.request.post(`/api/sessions/${session}/stop`, { headers: await csrfHeaders(page) });
  }
});
