import { wikiShotPath } from "./helpers";
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, info } from './helpers';

test.use({ storageState: AUTH_FILE, timezoneId: 'UTC' });  // so the schedule's own timezone (America/Chicago) differs from the browser's

let scheduleId: string | undefined;
test.afterEach(async ({ page }) => {
  if (scheduleId) {
    const headers = await csrfHeaders(page);
    expect((await page.request.post(`/api/schedules/${scheduleId}/cancel`, { headers })).ok()).toBe(true);
    expect((await page.request.delete(`/api/schedules/${scheduleId}`, { headers })).ok()).toBe(true);
    scheduleId = undefined;
  }
});

test('a person creates a schedule, runs it now and cancels its ordinary session', async ({ page }, testInfo) => {
  const suffix = randomUUID().slice(0, 8);
  const slug = `schedule-demo-${suffix}`;
  const weekly = `Weekly review ${suffix}`;
  const monday = `Monday review ${suffix}`;
  const workspace = `${info().workspace}/${slug}`;
  mkdirSync(workspace, { recursive: true });
  execFileSync('git', ['init', '-q', '-b', 'main', workspace]);
  writeFileSync(`${workspace}/README.md`, 'Demo project\n');
  execFileSync('git', ['-C', workspace, 'add', '.']);
  execFileSync('git', ['-C', workspace, '-c', 'user.name=Alex Example', '-c', 'user.email=alex@example.com', 'commit', '-qm', 'Initial']);
  await page.goto('/sessions');
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const headers = { 'X-ACC-CSRF': csrf, Origin: info().origin };
  const project = await page.request.post('/api/projects', { headers,
    data: { slug, name: `Demo schedules ${suffix}`, workspace_path: workspace } });
  expect(project.status()).toBe(201);
  const pid = (await project.json()).id;
  await page.request.post(`/api/projects/${pid}/trust`, { headers });
  await page.goto('/schedules');
  await page.getByRole('button', { name: 'New schedule', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New schedule', exact: true });
  await dialog.getByRole('button', { name: 'Rename', exact: true }).click();
  await dialog.getByLabel('Name', { exact: true }).fill(weekly);
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(pid));
  await dialog.getByRole('combobox', { name: 'Timing', exact: true }).selectOption('weekly');
  await dialog.getByRole('combobox', { name: 'Weekday', exact: true }).selectOption('1');
  await dialog.getByLabel('Timezone', { exact: true }).fill('America/Chicago');
  await dialog.getByLabel('Task (optional)', { exact: true }).fill('Review the project and report findings.');
  await expect(dialog.getByRole('button', { name: 'Create paused schedule', exact: true })).toBeVisible();  // paused unless enabled
  // The next run is worked out live, by the scheduler's own rules (UX review R5).
  const next = dialog.getByTestId('next-run');
  await expect(next).toContainText(/Paused\. Once enabled, next run: Mon/);
  await expect(next).toContainText('in America/Chicago');
  await dialog.getByLabel('Enable schedule').check();
  await expect(next).toContainText(/^Next run: Mon/);
  await dialog.getByRole('combobox', { name: 'Timing', exact: true }).selectOption('cron');
  await dialog.getByLabel('Cron expression', { exact: true }).fill('61 * * * *');
  await expect(next).toContainText("Won't run:");
  await dialog.getByRole('combobox', { name: 'Timing', exact: true }).selectOption('weekly');
  await dialog.getByRole('combobox', { name: 'Weekday', exact: true }).selectOption('1');
  await expect(next).toContainText(/^Next run: Mon/);
  await dialog.getByRole('combobox', { name: 'Timing', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, "schedules-create.png"), fullPage: true });
  const creating = page.waitForResponse(response => new URL(response.url()).pathname === '/api/schedules' && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Create schedule', exact: true }).dblclick();
  const created = await creating;
  expect(created.status()).toBe(201);
  scheduleId = (await created.json()).id;
  await expect(dialog).not.toBeVisible();
  const row = page.getByRole('article', { name: weekly, exact: true });
  await expect(row.getByText('Enabled', { exact: true })).toBeVisible();
  const running = page.waitForResponse(response => new URL(response.url()).pathname === `/api/schedules/${scheduleId}/run` && response.request().method() === 'POST');
  await row.getByRole('button', { name: 'Run now', exact: true }).dblclick();
  const queued = await running;
  expect(queued.status()).toBe(201);
  const runId = (await queued.json()).id;
  const observed = await page.waitForResponse(async response => {
    if (new URL(response.url()).pathname !== '/api/schedules' || response.request().method() !== 'GET') return false;
    if (!response.ok()) return true;
    const schedules = await response.json();
    return schedules.some((schedule: { id: string; runs: { id: string; session_id: string | null; status: string }[] }) =>
      schedule.id === scheduleId && schedule.runs.some(run => run.id === runId && (run.session_id || run.status === 'failed')));
  }, { timeout: testInfo.timeout });
  expect(observed.ok()).toBe(true);
  const observedSchedule = (await observed.json()).find((schedule: { id: string }) => schedule.id === scheduleId);
  expect(observedSchedule.runs.find((run: { id: string }) => run.id === runId))
    .toMatchObject({ status: 'running', session_id: expect.any(String) });
  await expect(row.getByRole('link', { name: 'Open session' })).toBeVisible();
  expect((await new AxeBuilder({ page }).include('main').analyze()).violations).toEqual([]);
  await page.screenshot({ path: wikiShotPath(testInfo, "schedules.png"), fullPage: true });
  await row.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel run', exact: true }).click();
  await expect(row.getByText('cancelled', { exact: true })).toBeVisible();
  await row.getByRole('button', { name: 'Edit', exact: true }).click();
  const edit = page.getByRole('dialog', { name: 'Edit schedule' });
  await expect(edit.getByLabel('Name', { exact: true })).toHaveValue(weekly);
  await expect(edit.getByRole('combobox', { name: 'Timing', exact: true })).toHaveValue('weekly');
  await edit.getByLabel('Name', { exact: true }).fill(monday);
  await edit.getByRole('button', { name: 'Save schedule', exact: true }).dblclick();
  await expect(page.getByRole('article', { name: monday, exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: wikiShotPath(testInfo, "schedules-phone.png"), fullPage: true });
});
