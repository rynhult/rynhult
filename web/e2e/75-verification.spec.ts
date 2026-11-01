import { test, expect } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, gitProject } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owner controls known flakes and the bounded wrap-up, and sees exact stale reasons at 390px', async ({ page }) => {
  const { id } = await gitProject(page, `verification-${Date.now()}`);
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/projects/${id}#project-gates`);
    const flakes = page.getByRole('form', { name: 'Known flakes' });
    await flakes.getByLabel('Known flaky test IDs').fill('tests/test_demo.py::test_one');
    await flakes.getByRole('button', { name: 'Save known flakes' }).click();
    await expect(flakes.getByLabel('Known flaky test IDs')).toHaveValue('tests/test_demo.py::test_one');
    const wrapup = page.getByRole('form', { name: 'Expiry wrap-up' });
    await expect(wrapup.getByLabel('Lead wrap-up (minutes)')).toHaveValue('30');
    await wrapup.getByLabel('Lead wrap-up (minutes)').fill('');
    await expect(wrapup.getByRole('button', { name: 'Save wrap-up' })).toBeDisabled();
    await wrapup.getByLabel('Lead wrap-up (minutes)').fill('20');
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === '/api/gates/wrapup');
    await wrapup.getByRole('button', { name: 'Save wrap-up' }).click();
    expect((await saved).ok()).toBe(true);
    expect((await (await page.request.get('/api/gates/wrapup')).json()).minutes).toBe(20);
    await expect(wrapup.getByLabel('Lead wrap-up (minutes)')).toHaveValue('20');
    await flakes.screenshot({ path: '../.acc/artifacts/orch-b-flakes-phone.png' });
    await wrapup.screenshot({ path: '../.acc/artifacts/orch-b-wrapup-phone.png' });
    await page.route('**/api/gates?*', route => route.fulfill({ json: {
      budget: { enabled: false }, running: 0, full_running: 0, jobs: [{
        id: 'a'.repeat(32), branch: 'acc/checked', base: 'main', state: 'passed', suite: 'related',
        priority: 'check', results: {}, head: 'b'.repeat(40), base_sha: 'c'.repeat(40),
        commands_digest: 'd'.repeat(64), runner_version: 'test/gate-1', stale: 'base moved',
        created_at: new Date().toISOString(), requested_by: 'owner'
      }]
    } }));
    await page.reload();
    await expect(page.getByText('stale: base moved', { exact: true })).toBeVisible();
    await page.screenshot({ path: '../.acc/artifacts/orch-b-verification-phone.png', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  } finally {
    expect((await page.request.put('/api/gates/wrapup', { headers: await csrfHeaders(page), data: { minutes: 30 } })).ok()).toBe(true);
  }
});

test('an owner-accepted retry is labelled accepted instead of still needing triage', async ({ page }) => {
  const { id } = await gitProject(page, `accepted-retry-${Date.now()}`);
  await page.route('**/api/gates?*', route => route.fulfill({ json: {
    budget: { enabled: false }, running: 0, full_running: 0, jobs: [{
      id: 'a'.repeat(32), branch: 'acc/retry', base: 'main', state: 'retry-passed', suite: 'related',
      priority: 'check', stale: false, results: { accepted_flake: { item: 7, by: 'alex' } },
      created_at: new Date().toISOString(), requested_by: 'owner'
    }]
  } }));
  await page.goto(`/projects/${id}#project-gates`);
  await expect(page.getByText('retry-passed · flake task accepted', { exact: true })).toBeVisible();
  await expect(page.getByRole('form', { name: 'Accept flake task' })).toHaveCount(0);
});
