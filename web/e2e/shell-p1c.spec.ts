import { test } from './shell-state';
import { expect, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['layouts', 'ui_header'] });
const order = ['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search'];
const defaults = { order, hidden: [] };

async function savePreference(page: Page, value = defaults) {
  expect((await page.request.put('/api/preferences/ui-header', { headers: await csrfHeaders(page), data: value })).ok()).toBeTruthy();
}
const editor = (page: Page) => page.getByRole('dialog', { name: 'Customize header', exact: true });
async function tools(page: Page) {
  if (page.viewportSize()!.width < 768 && !await page.getByRole('group', { name: 'Header tools', exact: true }).isVisible()) await page.getByRole('button', { name: 'Header tools', exact: true }).click();
}
async function customize(page: Page) {
  await tools(page); await expect(page.getByRole('button', { name: 'Customize header', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Customize header', exact: true }).click();
  await expect(editor(page)).toBeVisible();
}
async function shot(page: Page, name: string) {
  if (process.env.ACC_UPDATE_HEADER_SCREENSHOTS) await page.screenshot({ path: `../docs/wiki/.attachments/shell-header-${name}.png`, animations: 'disabled' });
}

for (const role of ['owner', 'member']) test(`${role} can reorder, hide and reset only optional modules at 1440/390 light/dark; server preference follows the person`, async ({ page, context }) => {
  test.slow();  await savePreference(page);
  if (role === 'member') {
    await page.request.put('/api/preferences/ui-header', { headers: await csrfHeaders(page), data: { order, hidden: order } });
    await page.goto('/projects');
    const account = `header-${Date.now()}-agent`;
    mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
    const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
    const token = (await invite.json()).link.split('/').pop(); await context.clearCookies();
    const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-header-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();

    expect(await (await page.request.get('/api/preferences/ui-header')).json()).toEqual(defaults);
  }
  const project = await gitProject(page, `header-${role}`);
  const requests: string[] = [];
  page.on('request', request => requests.push(new URL(request.url()).pathname));
  for (const width of [1440, 390]) for (const theme of ['light', 'dark'] as const) {
    await savePreference(page); await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme: theme });
    await page.goto(`/projects/${project.id}`); await customize(page);
    await expect(editor(page).getByRole('checkbox')).toHaveCount(5);
    for (const name of ['Machine load', 'Plan usage']) {
      if (role === 'member') await expect(editor(page).getByRole('checkbox', { name, exact: true })).toBeDisabled();
    }
    await editor(page).getByRole('checkbox', { name: 'Go to…', exact: true }).uncheck();
    const up = editor(page).getByRole('button', { name: 'Move Find project files up', exact: true });
    await up.focus();
    for (let n = 0; n < 4; n++) await page.keyboard.press('Enter');
    await expect(editor(page).getByRole('listitem').first()).toContainText('Find project files');
    await expect(up).toBeDisabled();
    await expectAxeClean(page); await shot(page, `${role}-${width}-${theme}-editor`);
    await editor(page).getByRole('button', { name: 'Save header', exact: true }).click();
    await expect(editor(page)).toBeHidden();
    expect(await (await page.request.get('/api/preferences/ui-header')).json()).toEqual({ order: ['file_search', ...order.slice(0, 4)], hidden: ['go_to'] });
    await tools(page);
    const modules = page.locator('[data-header-module]');
    expect(await modules.evaluateAll(nodes => nodes.map(node => node.getAttribute('data-header-module')))).toEqual(role === 'owner'
      ? ['file_search', 'machine_load', 'plan_usage', 'conversation_search'] : ['file_search', 'conversation_search']);
    await expect(page.getByRole('button', { name: 'Go to…', exact: true })).toHaveCount(0);
    await expect(page.locator('#shell-header').getByRole('link', { name: 'Rynhult', exact: true })).toBeVisible();
    await expect(page.locator('#shell-header').getByRole('button', { name: /^Account:/ })).toBeVisible();
    await expect(page.locator('#shell-header').getByRole('button', { name: /^New session/ })).toBeVisible();
    await expectAxeClean(page); await shot(page, `${role}-${width}-${theme}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // Opening a dialog from the phone overflow must leave it open and return focus to a live control.
    await page.getByRole('button', { name: 'Find project files', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Find project files', exact: true })).toContainText(project.folder);
    await page.keyboard.press('Escape');
    if (width === 390) await expect(page.getByRole('button', { name: 'Header tools', exact: true })).toBeFocused();
    await page.keyboard.press('Control+k'); await expect(page.getByRole('dialog', { name: 'Jump to…' })).toBeVisible(); await page.keyboard.press('Escape');
    await page.reload(); await tools(page);
    await expect(page.getByRole('button', { name: 'Go to…', exact: true })).toHaveCount(0);
    await customize(page); await editor(page).getByRole('button', { name: 'Reset to default', exact: true }).click();
    await editor(page).getByRole('button', { name: 'Save header', exact: true }).click(); await tools(page);
    await expect(page.getByRole('button', { name: 'Go to…', exact: true })).toBeVisible();
  }
  if (role === 'member') expect(requests.filter(path => ['/api/board', '/api/providers', '/api/accounts'].includes(path))).toEqual([]);
  else for (const path of ['/api/board', '/api/providers', '/api/accounts']) expect(requests).toContain(path);
  // A second page has no in-memory query cache, so persistence must come from the server.
  await page.request.put('/api/preferences/ui-header', { headers: await csrfHeaders(page), data: { order, hidden: ['go_to'] } });
  const second = await context.newPage(); await second.goto('/projects');
  await expect(second.getByRole('button', { name: 'Customize header', exact: true })).toBeVisible();
  await expect(second.getByRole('button', { name: 'Go to…', exact: true })).toHaveCount(0); await second.close();

  await page.reload(); await tools(page);
  await expect(page.getByRole('button', { name: 'Go to…', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Customize header', exact: true })).toBeVisible();
});

test('hiding file search and closing phone overflow preserve its explicitly selected project', async ({ page }) => {
   await savePreference(page);
  const working = await gitProject(page, 'cedar-working'); const target = await gitProject(page, 'birch-target');
  await page.goto(`/projects/${working.id}`); await page.getByRole('button', { name: 'Find project files', exact: true }).click();
  const search = page.getByRole('dialog', { name: 'Find project files', exact: true });
  await search.getByRole('combobox', { name: 'Search project' }).selectOption(String(target.id)); await page.keyboard.press('Escape');
  await customize(page); await editor(page).getByRole('checkbox', { name: 'Find project files', exact: true }).uncheck();
  await editor(page).getByRole('button', { name: 'Save header', exact: true }).click(); await expect(editor(page)).toBeHidden();
  await page.keyboard.press('Control+Shift+f'); await expect(search).toContainText(target.folder); await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 390, height: 900 }); await tools(page); await page.keyboard.press('Escape');
  await page.keyboard.press('Control+Shift+f'); await expect(search).toContainText(target.folder);
});

test('phone overflow stays inside the viewport without scrolling the shell sideways', async ({ page }) => {
   await savePreference(page);
  const project = await gitProject(page, 'cedar-header');
  await page.setViewportSize({ width: 390, height: 900 }); await page.goto(`/projects/${project.id}`);
  await tools(page);
  const panel = page.getByRole('group', { name: 'Header tools', exact: true });
  const bounds = await panel.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await panel.getByRole('button', { name: 'Customize header', exact: true }).focus();
  expect(await page.locator('.shell').evaluate(node => node.scrollLeft)).toBe(0);
  const brand = await page.locator('#shell-header').getByRole('link', { name: 'Rynhult', exact: true }).boundingBox();
  expect(brand!.x).toBeGreaterThanOrEqual(0);
});

test('failed preference reads and writes stay visible and never claim saved changes', async ({ page }) => {
   await savePreference(page);
  await page.route('**/api/preferences/ui-header', route => route.fulfill({ status: 503, json: { detail: 'Preference storage is unavailable.' } }));
  await page.goto('/projects');
  await expect(page.getByText('Header preferences unavailable. Using defaults.')).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: 'Customize header', exact: true })).toBeDisabled();
  await page.unroute('**/api/preferences/ui-header'); await page.getByRole('button', { name: 'Retry header preferences' }).click();
  await customize(page);
  await editor(page).getByRole('checkbox', { name: 'Go to…', exact: true }).uncheck();
  await page.route('**/api/preferences/ui-header', route => route.request().method() === 'PUT' ? route.fulfill({ status: 503, json: { detail: 'Preference storage is unavailable.' } }) : route.continue());
  await editor(page).getByRole('button', { name: 'Save header', exact: true }).click();
  await expect(editor(page)).toContainText('Header not saved');
  await expect(editor(page).getByRole('checkbox', { name: 'Go to…', exact: true })).not.toBeChecked();
  await expect(page.locator('#shell-header').getByRole('button', { name: 'Go to…', exact: true })).toBeVisible();
  await expectAxeClean(page);
  await page.unroute('**/api/preferences/ui-header'); await editor(page).getByRole('button', { name: 'Save header', exact: true }).click();
  await expect(editor(page)).toBeHidden(); await expect(page.getByRole('button', { name: 'Go to…', exact: true })).toHaveCount(0);
});

test('an offline browser does not queue a header save or trap the editor', async ({ page, context }) => {
   await savePreference(page); await page.goto('/projects'); await customize(page);
  await editor(page).getByRole('checkbox', { name: 'Go to…', exact: true }).uncheck();
  await context.setOffline(true); await editor(page).getByRole('button', { name: 'Save header', exact: true }).click();
  await expect(editor(page)).toContainText('Header not saved');
  await editor(page).getByRole('button', { name: 'Cancel', exact: true }).click(); await expect(editor(page)).toBeHidden();
  await context.setOffline(false);
  expect(await (await page.request.get('/api/preferences/ui-header')).json()).toEqual(defaults);
});

test('hiding all optional modules retains critical controls, owner authority and shortcuts', async ({ page }) => {

  await page.request.put('/api/preferences/ui-header', { headers: await csrfHeaders(page), data: { order, hidden: order } });
  await page.route('**/api/security/effective*', async route => {
    const response = await route.fetch(); const policy = await response.json();
    await route.fulfill({ json: { ...policy, mode: 'unrestricted', switches: Object.fromEntries(Object.keys(policy.switches).map(key => [key, false])) } });
  });
  await page.route('**/api/updates', async route => { const response = await route.fetch(); await route.fulfill({ json: { ...await response.json(), available: true } }); });
  await page.route('**/api/attention*', route => route.fulfill({ json: [{
    id: 901, kind: 'machine_offline', severity: 'warn', source: 'system', verified: true,
    title: 'Maple machine is offline', detail: null, persistent: 'machine_offline',
    session_id: null, session_name: null, project: null,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), acked_at: null, resolved_at: null,
  }] }));
  const requests: string[] = []; page.on('request', request => requests.push(new URL(request.url()).pathname));
  await page.goto('/projects');
  await expect(page.locator('[data-header-module]')).toHaveCount(0);
  await expect(page.locator('#shell-header').getByRole('link', { name: /^Security:.*Unrestricted/ })).toBeVisible();
  const inbox = page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Inbox, 1 open', exact: true });
  await expect(inbox).toBeVisible();
  await inbox.click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('region', { name: 'Inbox item details', exact: true }).getByRole('heading', { name: 'Maple machine is offline', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.locator('#shell-header').getByRole('link', { name: 'Update available' })).toBeVisible();
  await expect(page.getByRole('status', { name: 'Updating every few seconds' })).toBeVisible();
  for (const path of ['/api/board', '/api/providers', '/api/accounts']) await expect.poll(() => requests.includes(path)).toBe(true);
  await page.keyboard.press('Control+Shift+f'); await expect(page.getByRole('dialog', { name: 'Find project files' })).toBeVisible(); await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 390, height: 900 });
  await page.route('**/api/sessions', route => route.abort()); await page.reload();
  await expect(page.locator('#shell-header').getByText('Offline', { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.locator('#shell-header').getByRole('link', { name: /^Security:.*Unrestricted/ })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Phone rail', exact: true }).getByRole('button', { name: 'Inbox, 1 open', exact: true })).toBeVisible();
  await expect(page.locator('#shell-header').getByRole('link', { name: 'Update available' })).toBeVisible();
  await customize(page); await expect(editor(page).getByRole('checkbox')).toHaveCount(5);
});
