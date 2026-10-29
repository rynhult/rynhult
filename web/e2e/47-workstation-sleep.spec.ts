import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`Host workstation sleep setting and resume at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    let minutes: number | null = null;
    let resumed = false;
    let stopPending = false;
    const node = () => ({
      id: 'desk', name: 'demo-desk', role: 'workstation', status: 'active', connected: true,
      info: { os: 'darwin', version: '1.0', hostname: 'demo-desk' }, accounts: ['alex-agent'],
      offline_threshold_seconds: 600, idle_sleep_minutes: minutes,
      sleep: { running_sessions: resumed ? 3 : 2,
        idle_sessions: [{ id: 'idle-demo', name: 'Review notes' }],
        paused_sessions: resumed ? [] : [{ id: 'paused-demo', name: 'Plan garden', stop_pending: stopPending }] },
    });
    await page.route('**/api/nodes', route => route.fulfill({ json: [node()] }));
    await page.route('**/api/nodes/demo-desk/policy', async route => {
      minutes = route.request().postDataJSON().idle_sleep_minutes;
      await route.fulfill({ json: node() });
    });
    await page.route('**/api/sessions/paused-demo/resume', async route => {
      resumed = true;
      await route.fulfill({ json: { ok: true } });
    });
    await page.goto('/host');
    const machines = page.getByRole('heading', { name: 'Machines', exact: true }).locator('xpath=ancestor::section[1]');
    await expect(machines.getByText('Keeping this machine awake: 2 sessions')).toBeVisible();
    await expect(machines.getByRole('link', { name: 'Review notes', exact: true })).toBeVisible();
    await expect(machines.getByText('paused so the machine can sleep')).toBeVisible();
    await page.getByRole('button', { name: 'Machine settings for demo-desk' }).click();
    const toggle = page.getByRole('checkbox', { name: 'Let this machine sleep when its sessions have been idle for N minutes' });
    await expect(toggle).not.toBeChecked();
    await toggle.check();
    await expect(page.getByLabel('Session idle time (minutes)')).toHaveValue('30');
    await page.getByLabel('Session idle time (minutes)').fill('45');
    await expectAxeClean(page);
    await page.screenshot({ path: wikiShotPath(testInfo, `workstation-sleep-settings-${width}.png`) });
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(machines.getByText('Sleep after 45 idle minutes')).toBeVisible();
    await machines.screenshot({ path: wikiShotPath(testInfo, `workstation-sleep-host-${width}.png`) });
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    stopPending = true;
    await page.reload();
    await expect(machines.getByText('Pause requested; stop not confirmed. Rynhult will retry.', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Resume Plan garden' })).toBeDisabled();
    stopPending = false;
    await page.reload();
    await page.getByRole('button', { name: 'Resume Plan garden' }).click();
    await expect(machines.getByText('Keeping this machine awake: 3 sessions')).toBeVisible();
    await expect(machines.getByRole('button', { name: 'Resume Plan garden' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Machine settings for demo-desk' }).click();
    await expect(toggle).toBeChecked();
    await expect(page.getByLabel('Session idle time (minutes)')).toHaveValue('45');
    await page.getByLabel('Machine role').selectOption('always_on');
    await expect(toggle).toHaveCount(0);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.route('**/api/host?machine=demo-desk', route => route.fulfill({ json: {
      connected: true, supported: false, version: '1.0', os: 'darwin',
    } }));
    await page.goto('/host?machine=demo-desk');
    await expect(page.getByText('Keeping this machine awake: 3 sessions')).toBeVisible();
    await page.getByRole('button', { name: 'Machine settings', exact: true }).click();
    await expect(page.getByLabel('Session idle time (minutes)')).toHaveValue('45');
  });
}
