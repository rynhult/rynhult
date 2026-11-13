import { expect } from '@playwright/test';
import { test } from './shell-state';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain, waitForSessionState } from './helpers';

// The member journey intentionally saves its own shell choice.
test.use({ storageState: AUTH_FILE, presentationChanges: ['layouts'] });
for (const role of ['owner', 'member']) test(`${role}: project navigation, compact header and edge resizing with keyboard in both themes`, async ({ page, context }) => {
  test.slow();
  await verifyAgain(page);
  const features = await (await page.request.get('/api/admin-features')).json();
  if (role === 'member') {
    const account = `polish-${Date.now()}-agent`;
    mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
    const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role, account } });
    const token = (await invite.json()).link.split('/').pop(); await context.clearCookies();
    const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-polish-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();
  }

  await page.request.put('/api/preferences/ui-header', { headers: await csrfHeaders(page), data: { order: ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'], hidden: [] } });
  const project = await gitProject(page, `polish-${role}`);
  const made = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Review cedar notes' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json(); await waitForSessionState(page, session.id, 'idle');
  const ownerReads: string[] = [];
  page.on('request', r => { if (['/api/providers', '/api/board', '/api/accounts'].includes(new URL(r.url()).pathname)) ownerReads.push(r.url()); });
  if (role === 'owner') await page.route('**/api/providers', route => route.fulfill({ json: { observations: { 'claude.rate_limits': { source: 'provider observation', observed_at: Date.now() / 1000 - 120, value: { five_hour: { used_percentage: 23 }, seven_day: { used_percentage: 41 } } } } } }));
  const writes: string[] = []; const sockets: string[] = [];
  page.on('request', r => { if (r.method() !== 'GET' && /\/api\/sessions/.test(r.url())) writes.push(r.url()); });
  page.on('websocket', ws => sockets.push(ws.url()));
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto('/projects');
    const sidebar = page.getByRole('complementary', { name: 'Context sidebar' });
    await sidebar.getByRole('textbox', { name: 'Search projects' }).fill(project.slug);
    const row = sidebar.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(project.slug) });
    await expect(row).toBeVisible(); await row.focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/projects/${project.id}$`));
    await expect(sidebar.getByRole('heading', { name: project.slug, exact: true })).toBeVisible();
    await expect(sidebar.getByRole('textbox', { name: 'Search projects' })).toHaveCount(0);
    for (const label of ['Work', 'Working locations']) await expect(sidebar.getByRole('heading', { name: label, exact: true })).toBeVisible();
    await expect.soft(sidebar.getByRole('heading', { name: 'Project', exact: true })).toBeVisible();
    await expect.soft(sidebar.getByRole('heading', { name: 'Sources', exact: true })).toHaveCount(0);
    await expect(sidebar.getByRole('link', { name: 'Sessions · 1', exact: true })).toBeVisible();
    await sidebar.getByRole('link', { name: 'Changes', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page.getByRole('main')).toContainText('Session changes');
    await page.getByRole('main').getByRole('link', { name: 'Review cedar notes', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}\\?tab=changes$`));
    await expect(page.getByRole('tab', { name: 'Changes', exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('.xterm')).toHaveCount(0);
    const projectMenu = sidebar.getByRole('region', { name: `${project.slug} context`, exact: true });
    const sessionsHeading = sidebar.getByRole('heading', { name: 'Sessions', exact: true });
    await expect(sessionsHeading).toBeVisible();
    await expect(sidebar.getByRole('link', { name: 'Review cedar notes · Idle', exact: true })).toHaveAttribute('href', `/sessions/${session.id}`);
    await expect(sidebar.getByRole('textbox', { name: 'Search projects' })).toHaveCount(0);
    await page.getByRole('tab', { name: 'Files', exact: true }).press('Enter');
    await expect(page.getByRole('tab', { name: 'Files', exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(sessionsHeading).toBeVisible();
    await page.reload();
    await expect(sessionsHeading).toBeVisible({ timeout: 15000 });
    // Sessions and projects have separate contexts, including after reload.
    await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).press('Enter');
    await expect(page).toHaveURL(/\/projects$/);
    await sidebar.getByRole('textbox', { name: 'Search projects' }).fill(project.slug);
    await row.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/projects/${project.id}$`));
    await expect(projectMenu.getByRole('link', { name: 'Overview', exact: true })).toHaveAttribute('href', `/projects/${project.id}`);
    await projectMenu.getByRole('link', { name: 'Files', exact: true }).press('Enter');
    await expect(page).toHaveURL(new RegExp(`/projects/${project.id}\\?tab=files$`));
    await expect(projectMenu).toBeVisible();
    await expect(page.locator('.xterm')).toHaveCount(0);
    await page.goto(`/projects/${project.id}?tab=changes`);
    await expect(page.getByRole('main').getByRole('heading', { name: project.slug, exact: true, level: 1 })).toBeVisible({ timeout: 15000 });
    await expect(sidebar.getByRole('button', { name: `Pin ${project.slug}`, exact: true })).toBeVisible();
    const separator = page.getByRole('separator', { name: 'Sidebar width' });
    expect(await separator.evaluate(node => getComputedStyle(node, '::after').backgroundColor)).not.toBe('rgba(0, 0, 0, 0)');
    await separator.focus(); await page.keyboard.press('End'); await expect(separator).toHaveAttribute('aria-valuenow', '400');
    await page.keyboard.press('Home'); await expect(separator).toHaveAttribute('aria-valuenow', '208');
    await page.keyboard.press('ArrowRight'); await expect(separator).toHaveAttribute('aria-valuenow', '216');
    await page.reload(); await expect(separator).toHaveAttribute('aria-valuenow', '216');
    await separator.dblclick(); await expect(separator).toHaveAttribute('aria-valuenow', '256');
    const box = (await separator.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2); await page.mouse.down(); await page.mouse.move(box.x + 64, box.y + box.height / 2); await page.mouse.up();
    expect(Number(await separator.getAttribute('aria-valuenow'))).toBeGreaterThan(300);
    await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(sidebar).toBeHidden(); await page.reload(); await expect(sidebar).toBeHidden();
    await page.locator('#shell-header').focus(); await page.keyboard.press('F6'); await expect(page.locator('#shell-rail')).toBeFocused();
    await page.keyboard.press('F6'); await expect(page.getByRole('main')).toBeFocused();
    await page.getByRole('button', { name: 'Expand sidebar', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(sidebar).toBeVisible();
    if (role === 'owner') {
      const usage = page.getByRole('button', { name: /Plan usage details$/ });
      await expect(usage).toHaveText('Claude 41%'); await usage.focus(); await page.keyboard.press('Enter');
      const details = page.getByRole('group', { name: /Plan usage details$/ });
      await expect(details).toContainText('Claude · 5-hour · 23%'); await expect(details).toContainText('Claude · 7-day · 41%');
      await expect(details).toContainText('provider observation'); await page.keyboard.press('Escape'); await expect(usage).toBeFocused();
    }
    await expectAxeClean(page);
    mkdirSync(`${process.env.ACC_E2E_BASE}/shots`, { recursive: true });
    await page.screenshot({ path: `${process.env.ACC_E2E_BASE}/shots/polish-${role}-${theme}.png`, animations: 'disabled' });
    const header = await page.locator('.shell-header-line').boundingBox(); expect(header!.height).toBeLessThanOrEqual(64);
    const required = page.locator('.shell-critical'); const rect = (await required.boundingBox())!;
    expect(rect.y).toBeGreaterThanOrEqual(header!.y); expect(rect.y + rect.height).toBeLessThanOrEqual(header!.y + header!.height);
    await sidebar.getByRole('link', { name: 'Back to all projects', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/projects$/);
  }
  if (role === 'member') expect(ownerReads).toEqual([]);
  expect(writes).toEqual([]); expect(sockets).toEqual([]);
  await page.request.post(`/api/sessions/${session.id}/stop`, { headers: await csrfHeaders(page) });
});

test('Project menu reads real P4 sources and drops the previous project on navigation or failed refresh', async ({ page }) => {
  test.slow(); // Real verification, two source registrations and three document loads.
  await verifyAgain(page);
  const features = await (await page.request.get('/api/admin-features')).json();

  const first = await gitProject(page, 'menu-cedar');
  const second = await gitProject(page, 'menu-birch');
  for (const [project, remote] of [[first, 'https://example.com/cedar/menu-source.git'], [second, 'https://example.com/birch/other-source.git']] as const) {
    const recorded = await page.request.post(`/api/projects/${project.id}/issue-sources`, { headers: await csrfHeaders(page), data: { remote_url: remote } });
    expect(recorded.ok(), await recorded.text()).toBeTruthy();
  }
  await page.goto(`/projects/${first.id}`);
  const sidebar = page.getByRole('complementary', { name: 'Context sidebar', exact: true });
  const firstMenu = sidebar.getByRole('region', { name: `${first.slug} context`, exact: true });
  await expect(firstMenu.getByRole('heading', { name: 'Project', exact: true })).toBeVisible();
  await expect(firstMenu.getByRole('heading', { name: 'Sources', exact: true })).toHaveCount(0);
  await expect(firstMenu).toContainText('https://example.com/cedar/menu-source.git');
  await expect(firstMenu.getByRole('link', { name: 'View source repositories', exact: true })).toHaveAttribute('href', `/projects/${first.id}#project-sources`);
  await page.goto(`/projects/${second.id}`);
  const secondMenu = sidebar.getByRole('region', { name: `${second.slug} context`, exact: true });
  await expect(secondMenu).toContainText('https://example.com/birch/other-source.git');
  await expect(sidebar).not.toContainText('https://example.com/cedar/menu-source.git');
  await page.route(`**/api/source-views/projects/${second.id}`, route => route.fulfill({ status: 503, json: { detail: 'Fixture source read unavailable' } }));
  await page.reload();
  await expect(secondMenu).toContainText("Couldn't load project sources.");
  await expect(secondMenu).not.toContainText('https://example.com/birch/other-source.git');
});
