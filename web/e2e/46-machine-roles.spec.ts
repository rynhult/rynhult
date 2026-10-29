import { wikiShotPath } from "./helpers";
import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`machine roles, neutral outage and manual checks at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const offlineAt = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    await page.route('**/api/nodes', route => route.fulfill({ json: [
      { id: 'w', name: 'demo-desk', role: 'workstation', status: 'active', connected: false,
        last_seen: offlineAt, offline_since: offlineAt, offline_threshold_seconds: 600,
        info: { os: 'linux' }, accounts: [] },
      { id: 'a', name: 'demo-cloud', role: 'always_on', status: 'active', connected: false, offline_alert: true,
        last_seen: offlineAt, offline_since: offlineAt, offline_threshold_seconds: 600,
        info: { os: 'darwin' }, accounts: [] },
    ] }));
    await page.route('**/api/nodes/check', route => route.fulfill({ json: { machines: route.request().method() === 'POST' ? [
      { name: 'demo-desk', state: 'offline', last_seen: offlineAt, offline_since: offlineAt },
      { name: 'local', state: 'responding', latency_ms: 12, info: { version: '1.0' } },
    ] : [] } }));
    await page.goto('/host');
    await expect(page.getByText(/Offline: workstation/)).toBeVisible();
    await expect(page.getByText('Always-on machine offline')).toBeVisible();
    await page.getByRole('button', { name: 'Check machines now' }).click();
    await expect(page.getByText(/Connected and responding · 12 ms/)).toBeVisible();
    await expect(page.getByText(/Rynhult cannot dial offline machines/)).toBeVisible();
    await page.getByRole('heading', { name: 'Machines', exact: true }).locator('xpath=ancestor::section[1]').screenshot({ animations: 'disabled', path: width === 1440 ? wikiShotPath(testInfo, "machine-roles-host.png") : testInfo.outputPath(`machine-host-${width}.png`) });
    await page.getByRole('button', { name: 'Machine settings for demo-desk' }).click();
    await expect(page.getByLabel('Machine role')).toHaveValue('workstation');
    await page.getByLabel('Machine role').selectOption('always_on');
    await expect(page.getByLabel('Offline alert after (minutes)')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: width === 1440 ? wikiShotPath(testInfo, "machine-roles-settings.png") : testInfo.outputPath(`machine-settings-${width}.png`) });
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: 'Add machine', exact: true }).click();
    await expect(page.getByLabel('Machine role')).toHaveValue('auto');
    await expect(page.getByText('Workstation: often off; no offline alerts or background retries.')).toBeVisible();
    await expectAxeClean(page);
    await page.screenshot({ animations: 'disabled', path: width === 1440 ? wikiShotPath(testInfo, "machine-roles-add.png") : testInfo.outputPath(`add-machine-${width}.png`) });
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.goto('/');
    // Overview keeps each machine's state (an offline always-on machine stands out); checks are on Host.
    await expect(page.getByText(/Offline: workstation/)).toBeVisible();
    await expect(page.getByText('Always-on machine offline')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Check machines now' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'All machines' })).toHaveAttribute('href', '/host');
    await page.getByRole('heading', { name: 'Machines', exact: true }).locator('xpath=ancestor::section[1]').screenshot({ animations: 'disabled', path: width === 390 ? wikiShotPath(testInfo, "machine-roles-board-phone.png") : testInfo.outputPath(`machine-board-${width}.png`) });
  });
}

test('Add machine and machine settings persist the chosen role', async ({ page }) => {
  const name = `demo-role-${randomUUID().slice(0, 8)}`;
  await page.goto('/host');
  const headers = await csrfHeaders(page);
  try {
    await page.getByRole('button', { name: 'Add machine', exact: true }).click();
    await page.getByLabel('Name', { exact: true }).fill(name);
    await page.getByLabel('Machine role').selectOption('workstation');
    await page.getByRole('button', { name: 'Create enrollment code' }).click();
    await page.getByRole('button', { name: 'Done', exact: true }).click();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await expect(page.getByLabel('Machine role')).toHaveValue('workstation');
    await page.getByLabel('Machine role').selectOption('always_on');
    await page.getByLabel('Offline alert after (minutes)').fill('15');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.reload();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await expect(page.getByLabel('Machine role')).toHaveValue('always_on');
    await expect(page.getByLabel('Offline alert after (minutes)')).toHaveValue('15');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: `Remove ${name}`, exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Remove', exact: true }).click();
    await expect(page.getByRole('button', { name: `Machine settings for ${name}`, exact: true })).toHaveCount(0);
  } finally {
    const removed = await page.request.post(`/api/nodes/${name}/remove`, { headers });
    expect([200, 404]).toContain(removed.status());
  }
});

test('offline session does not mount a terminal or poll remote files', async ({ page }) => {
  await page.route('**/api/sessions/offline-demo', route => route.fulfill({ json: {
    id: 'offline-demo', project_id: 1, project: 'demo', provider: 'claude', name: 'Offline work',
    observed_state: 'idle', desired_state: 'running', machine_offline: true, host: 'demo-desk',
    management: 'acc', created_at: new Date().toISOString(), last_error: 'old transport failure',
    launch_spec: { inputs: {}, files: {}, digest: 'demo' }, runs: [], worktree: null,
    changes: null, automation: {}, activity: null,
  } }));
  let sockets = 0;
  page.on('websocket', () => sockets++);
  await page.goto('/sessions/offline-demo');
  await expect(page.getByText('Machine offline. This view returns when the machine checks in.')).toBeVisible();
  await expect(page.getByText('old transport failure')).toHaveCount(0);
  await expect(page.locator('.xterm')).toHaveCount(0);
  expect(sockets).toBe(0);
});
