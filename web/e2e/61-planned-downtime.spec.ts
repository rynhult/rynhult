import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
test(`owner edits recurring downtime and sees honest backup age at ${width}`,  async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  const machine = { id: 'demo', name: 'demo-receiver', status: 'active', connected: false, role: 'always_on',
    offline_threshold_seconds: 600, info: { os: 'linux' }, accounts: [], last_seen: '2026-10-24T06:00:00Z',
    downtime_policy: { timezone: 'America/Chicago', windows: [{ days: [0,1,2,3,4,5,6], start: '01:30', end: '09:00' }] },
    planned_downtime: { active: true, until_reconnect: false, ends_at: '2026-10-24T14:00:00Z' } };
  await page.route('**/api/nodes', route => route.fulfill({ json: [machine] }));
  await page.route('**/api/nodes/demo-receiver/policy', async route => {
    const policy = route.request().postDataJSON();
    expect(policy.downtime_policy.timezone).toBe('America/Chicago');
    expect(policy.downtime_policy.windows[0].start).toBe('01:30');
    expect(policy.downtime_until_reconnect).toBe(true);
    await route.fulfill({ json: { ...machine, ...policy } });
  });
  let indefinite = false;
  await page.route('**/api/host/backups', route => route.fulfill({ json: { targets: [{
    id: 'a'.repeat(32), kind: 'node', enabled: true, repository: 'demo-receiver.example.lan',
    status: 'append-only receiver', stale: true, captured_at: 1792821600,
    last_successful_off_machine_at: 1792821600, planned_downtime: { active: true },
    next_capture_expected_at: indefinite ? undefined : 1792850400, last_job: { state: 'complete', note: 'caught up after 7 missed' },
  }], drill: {}, quarterly_due: false } }));
  await page.goto('/host');
  await expect(page.getByText(/^Planned downtime · last seen/)).toBeVisible();
  await expect(page.getByTestId('backup-status')).toContainText('next capture expected');
  await page.getByRole('button', { name: 'Machine settings for demo-receiver' }).click();
  await expect(page.getByLabel('Downtime timezone')).toHaveValue('America/Chicago');
  await page.getByRole('checkbox', { name: 'Until it reconnects', exact: true }).check();
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, `planned-downtime-policy-${width}.png`) });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('button', { name: 'Details', exact: true }).click();
  await expect(page.getByText(/Last successful off-machine backup/)).toBeVisible();
  await expect(page.getByText(/Next capture expected/)).toBeVisible();
  await expect(page.getByText(/caught up after 7 missed/)).toBeVisible();
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.locator('#host-backups').screenshot({ path: wikiShotPath(testInfo, `planned-downtime-backups-${width}.png`) });
  indefinite = true;
  await page.reload();
  await expect(page.getByTestId('backup-status')).toContainText('next capture expected on reconnect');
});

}

test('status board labels planned downtime without an outage border', async ({ page }, testInfo) => {
  await page.route('**/api/board', route => route.fulfill({ json: { generated_at: Date.now() / 1000, projects: [], machines: [
    { name: 'demo-receiver', label: 'demo-receiver', local: false, connected: false, alerts: [],
      planned_downtime: { active: true }, last_seen: '2026-10-24T06:00:00Z' },
  ] } }));
  await page.goto('/board');
  const machine = page.locator('[data-machine="demo-receiver"]');
  await expect(machine.getByText(/^Planned downtime ·/)).toBeVisible();
  await expect(machine).not.toHaveClass(/border-l-bad/);
  await expect(page.getByText('1 planned downtime', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('planned-downtime-board.png') });
});

for (const width of [1440, 390]) {
  test(`scheduled downtime can end on reconnect at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const machine = { id: 'demo', name: 'demo-receiver', status: 'active', connected: false, role: 'always_on',
      offline_threshold_seconds: 600, info: { os: 'linux' }, accounts: [], last_seen: '2026-10-24T06:00:00Z',
      policy_revision: 'a'.repeat(64), downtime_policy: { timezone: 'America/Chicago', windows: [
        { days: [0,1,2,3,4,5,6], start: '01:30', end: '09:00' }] } };
    let saved: Record<string, any> | undefined;
    await page.route('**/api/nodes', route => route.fulfill({ json: [{ ...machine, ...(saved ?? {}) }] }));
    await page.route('**/api/nodes/demo-receiver/policy', async route => {
      saved = route.request().postDataJSON();
      expect(saved!.expected_revision).toBe('a'.repeat(64));
      expect(saved!.downtime_policy.windows).toEqual([
        { days: [0,1,2,3,4,5,6], start: '01:30', until_reconnect: true }]);
      expect(saved!.downtime_until_reconnect).toBe(false);
      await route.fulfill({ json: { ...machine, ...saved } });
    });
    await page.goto('/host');
    await page.getByRole('button', { name: 'Machine settings for demo-receiver' }).click();
    await page.getByLabel('Window 1 end', { exact: true }).fill('08:15');
    const until = page.getByRole('checkbox', { name: 'Window 1 until it reconnects', exact: true });
    await expect(until).toBeVisible({ timeout: 5000 });
    await until.focus();
    await page.keyboard.press('Space');
    await expect(page.getByLabel('Window 1 end', { exact: true })).toHaveCount(0);
    await until.uncheck();
    await expect(page.getByLabel('Window 1 end', { exact: true })).toHaveValue('08:15');
    await until.check();
    await expect(page.getByText('Planned downtime from 01:30 until it reconnects', { exact: true })).toBeVisible();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await page.getByText('Planned downtime from 01:30 until it reconnects', { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: wikiShotPath(testInfo, `scheduled-reconnect-window-${width}.png`) });
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    expect(saved).toBeDefined();
    await expect(page.getByText(/Planned downtime from 01:30 until it reconnects/)).toBeVisible();
    await page.getByRole('button', { name: 'Machine settings for demo-receiver' }).click();
    await expect(page.getByRole('checkbox', { name: 'Window 1 until it reconnects', exact: true })).toBeChecked();
    await expect(page.getByLabel('Window 1 end', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  });
}
