import { expect, test } from './fixtures';
import { AUTH_FILE, csrfHeaders, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['setup_level', 'layouts'] });
// Each journey starts in the new layout with a saved setup choice.
test.beforeEach(async ({ page }) => {
  const headers = await csrfHeaders(page);
  expect((await page.request.put('/api/preferences/setup-level', { headers, data: { level: 1, dismissed: true } })).ok()).toBeTruthy();
});

test('the new layout loads the saved level, hides the dismissed chooser and keeps its editor reachable', async ({ page }) => {
  const requests: string[] = [];
  page.on('request', request => { if (request.url().includes('/api/preferences/setup-level')) requests.push(request.url()); });
  await page.goto('/');
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Choose what you see' })).toHaveCount(0);
  await expect.poll(() => requests.length).toBeGreaterThan(0);
  await page.goto('/settings#all');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true, level: 1 })).toBeVisible();
  await page.goto('/settings#setup-level');
  await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toHaveValue('1');
});

test('first-run choice is skippable, server saved and changeable without Security changes', async ({ page }) => {
  expect((await page.request.put('/api/preferences/setup-level', { headers: await csrfHeaders(page), data: { level: null, dismissed: false } })).ok()).toBeTruthy();
  await page.goto('/');
  await expect(page.getByRole('complementary', { name: 'Choose what you see' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Settings → Setup level' })).toHaveAttribute('href', '/settings#setup-level');
  expect((await page.getByRole('complementary', { name: 'Choose what you see' }).boundingBox())!.height).toBeLessThan(56);
  for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    await expectAxeClean(page);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: 'Dismiss setup prompt', exact: true }).press('Enter');
  await expect(page.getByRole('complementary', { name: 'Choose what you see' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('complementary', { name: 'Choose what you see' })).toHaveCount(0);
  const before = await (await page.request.get('/api/security/registry')).json();
  await page.goto('/settings#setup-level');
  await page.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption('1');
  await page.getByRole('button', { name: 'Save what I see', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api/preferences/setup-level')).json()).level).toBe(1);
  const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
  await expect(rail.getByRole('button', { name: 'Workspaces', exact: true })).toHaveCount(0);
  await expect(rail.getByRole('button', { name: 'Sessions', exact: true })).toBeVisible();
  await rail.getByRole('button', { name: 'Tools', exact: true }).click();
  await expect(page).toHaveURL(/\/catalog$/);
  await expect(page.getByRole('heading', { name: 'Catalog', exact: true, level: 1 })).toBeVisible();
  const sidebar = page.getByRole('complementary', { name: 'Context sidebar' });
  await expect(sidebar.getByRole('link', { name: 'Catalog', exact: true })).toBeHidden();
  await sidebar.getByText('More tools', { exact: true }).click();
  await expect(sidebar.getByRole('link', { name: 'Catalog', exact: true })).toHaveCount(0);
  await expect(page.getByText(/^Not usually shown in your layout\./)).toBeVisible();
  expect(await (await page.request.get('/api/security/registry')).json()).toEqual(before);
  await page.goto('/settings#setup-level');
  await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toHaveValue('1');
});

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`setup choice and recommendations at ${width} in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    await page.goto('/settings#setup-level');
    await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toBeVisible();
    await page.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption('0');
    await expect(page.getByText('Run Build tasks without provider permission prompts', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Review matching security defaults', exact: true })).toBeVisible();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `../.acc/artifacts/setup-levels-${width}-${theme}.png`, fullPage: true });
  });
}

test('member levels never reveal owner tools, including More tools, in either theme and width', async ({ page, browser }) => {
  test.setTimeout(90_000);
  const { info, PASSWORD, totp } = await import('./helpers');
  const { mkdirSync } = await import('node:fs');
  const account = `level-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invited.ok()).toBeTruthy();
  const token = (await invited.json()).link.split('/').pop();
  const context = await browser.newContext({ baseURL: info().origin });
  try {
    const begin = await context.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    const joined = await context.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: {
      token, username: `robin-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret),
    } });
    expect(joined.ok()).toBeTruthy();
    const member = await context.newPage();
    const headers = await csrfHeaders(member);

    await member.goto('/settings#setup-level');
    for (const level of [0, 1, 2, 3, 4, 5, 6]) {
      // Tools is a real destination now; return to the editor before each change.
      await member.goto('/settings#setup-level');
      await member.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption(String(level));
      await member.getByRole('button', { name: 'Save what I see', exact: true }).click();
      await expect.poll(async () => (await (await member.request.get('/api/preferences/setup-level')).json()).level).toBe(level);
      await expect(member.getByRole('combobox', { name: 'Setup level', exact: true })).toHaveValue(String(level));
      const rail = member.getByRole('navigation', { name: 'Rail', exact: true });
      await expect(rail.getByRole('button', { name: 'Grid', exact: true })).toHaveCount(level === 1 ? 0 : 1);
      await rail.getByRole('button', { name: 'Tools', exact: true }).click();
      const sidebar = member.getByRole('complementary', { name: 'Context sidebar' });
      await sidebar.getByText('More tools', { exact: true }).click();
      await expect(sidebar.locator('a[href="/board"], a[href="/audit"], a[href="/groups"]')).toHaveCount(0);
    }
    for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
      await member.setViewportSize({ width, height: 900 }); await member.emulateMedia({ colorScheme });
      await member.goto('/settings#setup-level');
      await expect(member.getByRole('link', { name: 'View effective protections', exact: true })).toBeVisible();
      await expect(member.getByRole('link', { name: 'Review matching security defaults', exact: true })).toHaveCount(0);
      await expectAxeClean(member);
      expect(await member.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    expect((await page.request.get('/api/preferences/setup-level').then(r => r.json())).level).toBe(1);
  } finally { await context.close(); }
});

test('simplifying around an open terminal keeps its process, socket and header protections', async ({ page, context }) => {
  const { gitProject } = await import('./helpers');
  // Own the layout: this test must also run alone, without the journeys before it.
  const { id } = await gitProject(page, 'level-running');
  const headers = await csrfHeaders(page);
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Maple draft' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json();
  await page.goto(`/sessions/${session.id}`);
  await expect(page.locator('.xterm')).toBeVisible();
  const critical = await page.locator('.shell-critical').elementHandle();
  const terminal = await page.locator('.xterm').elementHandle();
  const sockets: string[] = []; const mutations: string[] = [];
  page.on('websocket', ws => sockets.push(ws.url()));
  page.on('request', r => { if (r.method() !== 'GET' && r.url().includes('/api/sessions/')) mutations.push(r.url()); });
  const settings = await context.newPage();
  try {
    await settings.goto('/settings#setup-level');
    await settings.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption('0');
    await settings.getByRole('button', { name: 'Save what I see', exact: true }).click();
    await expect.poll(async () => (await (await settings.request.get('/api/preferences/setup-level')).json()).level).toBe(0);
    await page.bringToFront();
    await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')));
    // Refocus after staleTime to refresh the server-side choice without reloading the terminal.
    await page.waitForTimeout(10_100);
    await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')));
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Grid', exact: true })).toBeVisible();
    await settings.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption('1');
    await settings.getByRole('button', { name: 'Save what I see', exact: true }).click();
    await expect.poll(async () => (await (await settings.request.get('/api/preferences/setup-level')).json()).level).toBe(1);
    await page.waitForTimeout(10_100);
    await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')));
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Grid', exact: true })).toHaveCount(0);
    await settings.goto('/setup/wizard');
    await expect(settings.getByRole('heading', { name: 'Set up Rynhult', exact: true })).toBeVisible();
    expect(await terminal!.evaluate(node => node.isConnected && node === document.querySelector('.xterm'))).toBe(true);
    expect(sockets).toEqual([]); expect(mutations).toEqual([]);
    expect(await critical!.evaluate(node => node.isConnected && node === document.querySelector('.shell-critical'))).toBe(true);
    await expect(page.locator('.shell-critical')).toContainText('Live');
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  } finally {
    await settings.close();
    await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  }
});
