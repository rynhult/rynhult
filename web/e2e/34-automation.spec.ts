import { openAdvanced, wikiShotPath } from "./helpers";
import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, gitProject, waitForSessionState } from './helpers';
test.use({ storageState: AUTH_FILE });
const owned: string[] = [];
test.beforeEach(() => { owned.length = 0; });
test.afterEach(async ({ page }) => {
  for (const id of owned) {
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
});
test('edit auto-resume and goal while running, then stop', async ({ page }, testInfo) => {
  const { id: pid, headers } = await gitProject(page, 'goal-demo');
  const r = await page.request.post('/api/sessions', { headers, data: { project_id: pid, provider: 'claude', name: 'Goal demo' } });
  const sid = (await r.json()).id;
  expect(r.ok()).toBe(true);
  owned.push(sid);
  const nextWrite = () => page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/api/sessions/${sid}/automation`);
  await page.goto(`/sessions/${sid}`);
  await waitForSessionState(page, sid, 'idle', 'claude_hook');
  await expect(page.getByRole('region', { name: 'Session automation' })).toHaveCount(0);  // always off the terminal
  await page.goto(`/sessions/${sid}?tab=spec#automation`);
  await page.getByRole('button', { name: 'Goal and auto-resume', exact: true }).click();
  const settings = page.getByRole('dialog', { name: 'Goal and auto-resume' });
  await settings.getByLabel('Resume when the limit resets').check();
  await settings.getByLabel('Goal text').fill('Finish the example.lan demo');
  await settings.screenshot({ path: wikiShotPath(testInfo, "sessions-automation-settings.png") });
  const configured = nextWrite();
  await settings.getByRole('button', { name: 'Save', exact: true }).click();
  expect((await configured).ok()).toBe(true);
  await expect(settings).toHaveCount(0);
  await expect(page.getByText(/\d \/ 5 continuations/)).toBeVisible();
  await page.reload();
  await page.getByRole('region', { name: 'Session automation' }).getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(settings.getByLabel('Resume when the limit resets')).toBeChecked();
  await expect(settings.getByLabel('Goal text')).toHaveValue('Finish the example.lan demo');
  // A slow save must not erase what the person typed meanwhile (break-it B5); a double-click saves once.
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let writes = 0;
  await page.route(`**/api/sessions/${sid}/automation`, async route => {
    if (route.request().method() === 'PUT') { writes++; await gate; }
    await route.continue();
  });
  try {
    await settings.getByLabel('Goal text').fill('First edit');
    const started = page.waitForRequest(request => request.method() === 'PUT'
      && new URL(request.url()).pathname === `/api/sessions/${sid}/automation`);
    await settings.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await started;
    await expect(settings.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
    await settings.getByLabel('Goal text').fill('Keep typing during a slow save');
    const saved = nextWrite();
    release();
    expect((await saved).ok()).toBe(true);
    await expect(settings.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
    await expect(settings.getByLabel('Goal text')).toHaveValue('Keep typing during a slow save');
    expect(writes).toBe(1);
  } finally {
    release();
    await page.unroute(`**/api/sessions/${sid}/automation`);
  }
  const saved = nextWrite();
  await settings.getByRole('button', { name: 'Save', exact: true }).dblclick();
  expect((await saved).ok()).toBe(true);
  await expect(settings).toHaveCount(0);
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${sid}`)).json()).automation.goal.text)
    .toBe('Keep typing during a slow save');
  const stopped = nextWrite();
  await page.getByRole('button', { name: 'Stop goal', exact: true }).click();
  expect((await stopped).ok()).toBe(true);
  await expect(page.getByText('Goal status: stopped')).toBeVisible();
  await page.locator('[aria-label="Session automation"]').screenshot({ path: wikiShotPath(testInfo, "sessions-automation.png") });
  await page.getByRole('region', { name: 'Session automation' }).getByRole('button', { name: 'Edit' }).click();
  await settings.getByLabel('Goal text').fill('Continue the demo');
  const restarted = nextWrite();
  await settings.getByRole('button', { name: 'Save', exact: true }).click();
  expect((await restarted).ok()).toBe(true);
  await expect(page.getByText('Goal status: running')).toBeVisible();
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Session automation' })).toHaveCount(0);
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await page.locator('.xterm-helper-textarea').press('x');
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${sid}`)).json()).automation.goal.status, {
    message: 'Waiting for the server to receive the terminal key before closing its socket',
    timeout: testInfo.timeout,
  }).toBe('person typed');
  await page.goto(`/sessions/${sid}?tab=spec#automation`);
  await expect(page.getByText('Goal status: person typed')).toBeVisible();
});

test('launch controls default off and persist a bounded goal', async ({ page }, testInfo) => {
  const { id: projectId } = await gitProject(page, 'goal-launch');
  await page.reload(); // Fetch the trust state changed through the test API before opening the dialog.
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(projectId));
  await openAdvanced(dialog);
  await expect(dialog.getByLabel('Resume when the limit resets')).not.toBeChecked();
  await dialog.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('cursor');
  await expect(dialog.getByLabel('Resume when the limit resets')).toBeDisabled();
  await expect(dialog.getByText('This provider has no reliable plan-limit signal in Rynhult.')).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('claude');
  await dialog.getByRole('button', { name: 'Rename', exact: true }).click();
  await dialog.getByLabel('Name', { exact: true }).fill('Launch goal demo');
  await dialog.getByLabel('Resume when the limit resets').check();
  await dialog.getByLabel('Goal text').fill('Finish the example.com demo');
  await dialog.getByLabel('Maximum continuations').fill('3');
  await dialog.getByLabel('Maximum hours').fill('1');
  await dialog.screenshot({ path: wikiShotPath(testInfo, "new-session-automation.png") });
  const launched = page.waitForResponse(response => new URL(response.url()).pathname === '/api/sessions' && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).dblclick();
  const response = await launched;
  expect(response.ok()).toBe(true);
  owned.push((await response.json()).id);
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]+$/);
  await page.getByRole('tab', { name: 'Setup', exact: true }).click();
  await expect(page.getByText('Resumes when the plan limit resets')).toBeVisible();
  await expect(page.getByText(/\d \/ 3 continuations/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Stop goal', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
