import { expect, type Page } from '@playwright/test';
import { test } from './shell-state';
import { mkdirSync, existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, verifyAgain, sessionWithChanges, expectAxeClean, info, PASSWORD, totp, freshStep, loadSecret } from './helpers';
test.use({ presentationChanges: ['layouts', 'admin_features'], storageState: AUTH_FILE });

test('Files keeps the same editor, draft and selection across keyboard moves, Fill and phone fallback', async ({ page }) => {

  const project = await gitProject(page, 'cedar-columns');
  const created = await page.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(page), data: { action: 'create', path: 'notes.txt', content: 'First line\nSecond line\n' } });
  expect(created.ok()).toBeTruthy();
  await page.goto(`/projects/${project.id}?tab=files&file=notes.txt`);
  const editor = page.getByRole('textbox', { name: 'File content' });
  await expect(editor).toBeVisible(); await editor.fill('Retained draft');
  await editor.press('Control+Home'); await editor.press('Shift+ArrowRight');
  expect(await page.evaluate(() => getSelection()?.toString())).toBe('R');
  const original = await editor.elementHandle();
  const tab = page.getByRole('tab', { name: /notes.txt/ });
  await expect(tab).toBeVisible();
  await tab.focus(); await page.keyboard.press('Control+Shift+ArrowRight');
  await expect(page.getByRole('tablist', { name: 'Column 2 views' })).toContainText('notes.txt');
  await page.getByRole('button', { name: 'Fill working panel' }).click();
  await expect(editor).toHaveText('Retained draft');
  await page.getByRole('button', { name: 'Restore columns' }).click();
  await page.setViewportSize({ width: 390, height: 900 });
  await expect(editor).toBeVisible();
  expect(await editor.evaluate((node, prior) => node === prior, original)).toBe(true);
  await editor.focus(); expect(await page.evaluate(() => getSelection()?.toString())).toBe('R');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole('tablist', { name: 'Column 2 views' })).toBeVisible();
  await page.getByRole('button', { name: 'Close view' }).click();
  await expect(page.getByRole('dialog', { name: 'Discard unsaved edits?' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(editor).toHaveText('Retained draft');
});

test('session terminal claims free control and moves/fills without reconnecting; closing releases clean control but never stops', async ({ page, context }) => {
  test.slow();
  const id = await sessionWithChanges(page, 'column-terminal');
  const sockets: string[] = []; const input: string[] = []; const mutations: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET') mutations.push(new URL(request.url()).pathname); });
  page.on('websocket', socket => { if (!socket.url().includes('/tty?')) return; sockets.push(socket.url()); socket.on('framesent', event => { const message = JSON.parse(String(event.payload)); if (message.t === 'i') input.push(message.d); }); });
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  expect(sockets).toHaveLength(1);
  expect(new URL(sockets[0]).searchParams.get('mode')).toBe('read');
  expect(new URL(sockets[0]).searchParams.get('run_id')).toMatch(/^\d+$/);
  const editor = page.locator('.xterm-helper-textarea'); const original = await editor.elementHandle();
  await editor.focus(); await page.keyboard.type('retained-terminal');
  const terminalHost = page.locator("[data-terminal-host-layer] > div");
  const before = await terminalHost.boundingBox();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: '日本', selectionStart: 2, selectionEnd: 2 });
  // Trigger a placement command without blurring native preedit; it must wait for commit.
  await page.getByRole('combobox', { name: 'Move view to column' }).evaluate((node: HTMLSelectElement) => { node.value = '1'; node.dispatchEvent(new Event('change', { bubbles: true })); });
  expect(await terminalHost.boundingBox()).toEqual(before);
  expect(input.join('')).toBe('retained-terminal');
  await cdp.send('Input.insertText', { text: '日本' });
  await expect(page.getByRole('tablist', { name: 'Column 2 views' })).toBeVisible();
  await editor.focus(); await page.keyboard.press('Enter');
  await expect.poll(() => input.join('')).toBe('retained-terminal日本\r');
  await expect.poll(() => existsSync(`${info().run_dir}/${id}/fake.received`) ? readFileSync(`${info().run_dir}/${id}/fake.received`, 'utf8') : '').toBe('retained-terminal日本\n');
  const terminalTab = page.locator('[data-view-tab]').filter({ hasText: /Terminal/ });
  await terminalTab.focus(); await page.keyboard.press('Control+Shift+ArrowRight');
  await page.getByRole('button', { name: 'Fill working panel' }).click();
  await page.getByRole('button', { name: 'Restore columns' }).click();
  expect(await editor.evaluate((node, prior) => node === prior, original)).toBe(true);
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  const observer = await context.newPage();
  try {
    await observer.goto(`/sessions/${id}`);
    await expect(observer.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Close view', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Release hidden control', exact: true })).toHaveCount(0);
    await page.evaluate(id => { history.pushState({ key: crypto.randomUUID() }, '', `/sessions/${id}`); window.dispatchEvent(new PopStateEvent('popstate')); }, id);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    await expect(editor).toBeVisible();
    expect(await editor.evaluate((node, prior) => node === prior, original)).toBe(true);
    expect(sockets).toHaveLength(1); expect(input.join('')).toBe('retained-terminal日本\r');
    expect(mutations.filter(path => /\/(stop|resume|interrupt)$/.test(path))).toEqual([]);
    await page.getByRole('button', { name: 'Release control', exact: true }).click();
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await observer.getByRole('button', { name: 'Control terminal', exact: true }).click();
    await expect(observer.getByText('You control this terminal', { exact: true })).toBeVisible();
  } finally { await observer.close(); }
  await expect(page.getByRole('button', { name: 'Take control…', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Control terminal', exact: true }).click();
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  await editor.focus(); await page.keyboard.type('unfinished');
  await page.getByRole('button', { name: 'Terminal options', exact: true }).click();
  await page.getByRole('button', { name: 'Detach terminal', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Detach with unresolved input?' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(sockets).toHaveLength(1);
  await page.getByRole('button', { name: 'Terminal options', exact: true }).click();
  await page.getByRole('button', { name: 'Detach terminal', exact: true }).click();
  await page.getByRole('button', { name: 'Detach without replay', exact: true }).click();
  await expect(editor).toHaveCount(0);
  const sent = input.join('');
  await page.getByRole('button', { name: 'Recheck terminal' }).click();
  await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
  expect(sockets).toHaveLength(2); expect(input.join('')).toBe(sent);
});

test('phone brand and host never overlap at 320–390px in either theme', async ({ page }) => {

  await page.goto('/projects');
  for (const width of [320, 360, 390]) for (const colorScheme of ['light', 'dark'] as const) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    const brand = page.locator('.shell-brand'); const host = page.locator('.shell-host');
    await expect(brand).toBeVisible(); await expect(host).toBeVisible();
    const a = (await brand.boundingBox())!; const b = (await host.boundingBox())!;
    expect(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y).toBe(true);
    expect(await brand.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    expect(a.x + a.width).toBeLessThanOrEqual(width);
    expect(b.x + b.width).toBeLessThanOrEqual(width);
  }
});

for (const role of ['owner', 'member']) test(`${role}: three columns, pointer/keyboard resize, phone fallback and local restoration pass axe in both themes`, async ({ page, context }) => {
  test.setTimeout(180000);
  if (role === 'member') {
    const account = `columns-${Date.now()}-agent`; mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
    const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
    expect(invite.ok()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop(); await context.clearCookies();
    const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-columns-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();

  }
  const project = await gitProject(page, `cedar-${role}`);
  for (const path of ['one.txt', 'two.txt', 'three.txt']) expect((await page.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(page), data: { action: 'create', path, content: path } })).ok()).toBeTruthy();
  await page.goto(`/projects/${project.id}?tab=files&file=one.txt`);
  await expect(page.getByRole('tab', { name: /one.txt/ })).toHaveAttribute('aria-selected', 'true');
  const history = await page.evaluate(() => window.history.length);
  await page.getByRole('button', { name: /^Open two.txt/ }).click();
  await expect(page.getByRole('tab', { name: /two.txt/ })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('combobox', { name: 'Move view to column' }).selectOption('1');
  await page.locator('[data-working-view]').filter({ has: page.getByRole('textbox', { name: 'File content' }).filter({ hasText: 'two.txt' }) }).getByRole('button', { name: /^Open three.txt/ }).click();
  await expect(page.getByRole('tab', { name: /three.txt/ })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('combobox', { name: 'Move view to column' }).selectOption('2');
  await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', '3');
  expect(await page.evaluate(() => window.history.length)).toBe(history + 2);
  const divider = page.getByRole('separator', { name: 'Resize columns 1 and 2' });
  await divider.focus(); await page.keyboard.press('ArrowRight');
  const bounds = (await divider.boundingBox())!; await page.mouse.move(bounds.x + 4, bounds.y + 30); await page.mouse.down(); await page.mouse.move(bounds.x + 28, bounds.y + 30); await page.mouse.up();
  for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', width === 390 ? '1' : '3');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const view of await page.locator('[data-working-view]:visible').all()) expect(await view.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  }
  // The arrangement is saved per device class, so reload in the class that made it.
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect.poll(async () => (await (await page.request.get(`/api/projects/${project.id}/layouts/desktop/wide`)).json()).layout?.columns.length).toBe(3);
  // Column count alone can match an earlier save while the final resize/focus is pending.
  await expect.poll(() => page.evaluate(id => Object.keys(localStorage).filter(key => {
    if (!key.startsWith('ryn-layout-pending:v2:')) return false;
    return JSON.parse(key.slice('ryn-layout-pending:v2:'.length))[2] === id;
  }), project.id)).toEqual([]);
  await page.reload();
  await expect(page.getByRole('tab', { name: /three.txt/ })).toBeVisible();
  await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', '3');
  const record = await (await page.request.get(`/api/projects/${project.id}/layouts/desktop/wide`)).json();
  for (const hidden of ['draft', 'three.txt']) expect(JSON.stringify(record.layout)).not.toContain(hidden);
  await page.getByRole('tab', { name: /three.txt/ }).dragTo(page.getByRole('tab', { name: /one.txt/ }));
  await expect(page.getByRole('tablist', { name: 'Column 1 views' })).toContainText('three.txt');
  await page.getByRole('button', { name: 'Reset columns' }).click();
  await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', '1');
  await expect(page.getByRole('tablist', { name: 'Column 1 views' }).getByRole('tab')).toHaveCount(3);
  const effects: string[] = []; page.on('request', request => { if (request.method() !== 'GET') effects.push(new URL(request.url()).pathname); });
  await page.getByRole('link', { name: 'Preview', exact: true }).click();
  // Sidebar navigation commits before the WorkingArea route effect opens its view.
  await expect(page.locator('[data-view-tab]').filter({ hasText: / · Preview$/ })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('combobox', { name: 'Move view to column' }).selectOption('1');
  for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', width === 390 ? '1' : '2');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(effects.filter(path => /\/(start|launch|resume|stop|publish)$/.test(path))).toEqual([]);
});

test('Files retains the base and draft through navigation, storage failure and a concurrent disk edit', async ({ page }) => {
   const project = await gitProject(page, 'cedar-conflict');
  for (const path of ['draft.txt', 'other.txt']) expect((await page.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(page), data: { action: 'create', path, content: 'Original' } })).ok()).toBeTruthy();
  await page.addInitScript(() => { const original = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) { if (key.startsWith('ryn-layout-pending:')) throw new DOMException('quota', 'QuotaExceededError'); return original.call(this, key, value); }; });
  await page.goto(`/projects/${project.id}?tab=files&file=draft.txt`);
  const editor = page.getByRole('textbox', { name: 'File content' }); await editor.fill('Local retained text');
  await page.getByRole('button', { name: /^Open other.txt/ }).click(); await expect(editor).toHaveText('Original');
  await page.goBack(); await expect(editor).toHaveText('Local retained text');
  const original = await (await page.request.get(`/api/files/project/${project.id}/read?path=draft.txt`)).json();
  expect((await page.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(page), data: { action: 'write', path: 'draft.txt', content: 'Concurrent disk text', revision: original.revision } })).ok()).toBeTruthy();
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Keep draft and use current revision' })).toBeDisabled();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Current version on disk' })).toHaveText('Concurrent disk text');
  await expect(editor).toHaveText('Local retained text');
  await page.getByRole('button', { name: 'Keep draft and use current revision' }).click();
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/files/project/${project.id}/read?path=draft.txt`)).json()).content).toBe('Local retained text');
});

test('restored unauthorized references are neutral until explicit recheck', async ({ page }) => {
   const project = await gitProject(page, 'cedar-unavailable');
  expect((await page.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(page), data: { action: 'create', path: 'private-notes.txt', content: 'Sensitive fixture text' } })).ok()).toBeTruthy();
  await page.goto(`/projects/${project.id}?tab=files&file=private-notes.txt`);
  await expect(page.getByRole('textbox', { name: 'File content' })).toBeVisible();
  let denied = true;
  await page.route('**/api/projects', async route => { const response = await route.fetch(); const projects = await response.json(); await route.fulfill({ json: denied ? projects.filter((item: { id: number }) => item.id !== project.id) : projects }); });
  await page.reload();
  await expect(page.getByText('View unavailable. Access may have ended or the resource may have been removed.')).toBeVisible();
  await expect(page.getByRole('tab', { name: /private-notes/ })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'File content' })).toHaveCount(0);
  denied = false;
  await page.getByRole('button', { name: 'Recheck availability' }).click();
  await expect(page.getByRole('textbox', { name: 'File content' })).toHaveText('Sensitive fixture text');
  await page.route(`**/api/files/project/${project.id}/read?*`, route => route.fulfill({ status: 404, json: { detail: 'Unavailable file' } }));
  await page.reload();
  await expect(page.getByText('File unavailable. Access ended or the file was removed.')).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('tab', { name: /private-notes/ })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'File content' })).toHaveCount(0);
});

test('read-only Chat shares working columns with one retained terminal and pauses hidden follow', async ({ page }) => {

  const featureState = await (await page.request.get('/api/admin-features')).json();
  const originalChat = featureState.features.find((row: { id: string; enabled: boolean }) => row.id === 'chat').enabled;
  try {
    expect((await page.request.put('/api/admin-features/chat', { headers: await csrfHeaders(page), data: { enabled: true, revision: featureState.revision } })).ok()).toBeTruthy(); const id = await sessionWithChanges(page, 'column-chat');
    const detail = await (await page.request.get(`/api/sessions/${id}`)).json();
    const envelope = { session_id: id, run_id: detail.runs.at(-1).id, state: 'ready', historical_run_attribution: 'unknown', observed_at: '2026-11-01T20:00:00Z',
      items: [{ id: 'a'.repeat(64), revision: 'b'.repeat(64), role: 'assistant', text: 'Read-only column conversation' }], omitted: 0, reset: false, more: false, reason: null, notices: { tools: 0 }, cursor: 'cursor', before_cursor: null,
      capabilities: { read_history: true, follow_messages: true, rich_tools: false, send_text: false } };
    let follows = 0, sockets = 0; const input: string[] = [];
    await page.context().route(`**/api/sessions/${id}/chat?*`, route => route.fulfill({ json: envelope }));
    await page.context().route(`**/api/sessions/${id}/chat/events?*`, route => { follows++; return route.fulfill({ contentType: 'text/event-stream', body: `event: chat\ndata: ${JSON.stringify(envelope)}\n\n` }); });
    page.on('websocket', socket => { if (socket.url().includes('/tty?')) { sockets++; socket.on('framesent', frame => { const msg = JSON.parse(String(frame.payload)); if (msg.t === 'i') input.push(msg.d); }); } });
    await page.goto(`/sessions/${id}`);
    const terminal = page.locator('.xterm-helper-textarea'); await expect(terminal).toBeVisible(); const original = await terminal.elementHandle();
    await page.getByRole('button', { name: 'Open Chat beside terminal' }).click();
    await expect(page.locator('[data-working-columns]')).toHaveAttribute('data-working-columns', '2');
    await expect(page.getByRole('region', { name: 'Chat messages', exact: true })).toContainText('Read-only column conversation');
    for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
      await page.setViewportSize({ width, height: 1000 }); await page.emulateMedia({ colorScheme }); await expectAxeClean(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.getByRole('button', { name: 'Open terminal beside Chat' }).click();
    await expect(terminal).toBeVisible();
    expect(await terminal.evaluate((node, prior) => node === prior, original)).toBe(true);
    const hiddenFollows = follows; await page.waitForTimeout(2500); expect(follows).toBe(hiddenFollows);
    expect(sockets).toBe(1); expect(input).toEqual([]);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0);
  } finally {
    await page.context().unrouteAll({ behavior: 'wait' });
    const state = await (await page.request.get('/api/admin-features')).json();
    expect((await page.request.put('/api/admin-features/chat', { headers: await csrfHeaders(page), data: { enabled: originalChat, revision: state.revision } })).ok()).toBeTruthy();
    const restored = await (await page.request.get('/api/admin-features')).json();
    expect(restored.features.find((row: { id: string }) => row.id === 'chat').enabled).toBe(originalChat);
  }
});

test('Round 2: archived history is directly readable and workspace Files without a session explains its next step', async ({ page }) => {
   const id = await sessionWithChanges(page, 'cedar-history');
  expect((await page.request.post(`/api/sessions/${id}/archive`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${id}`)).json()).observed_state).toBe('archived');
  await page.goto(`/sessions/${id}`);
  const archived = await (await page.request.get(`/api/sessions/${id}`)).json();
  await expect(page.getByRole('heading', { level: 1 })).toContainText(archived.name);
  await expect(page.getByText('View unavailable. Access may have ended or the resource may have been removed.')).toHaveCount(0);
  await page.goto('/workspaces/cedar/files');
  await expect(page.getByText('Open Files from the workspace card to choose its folder.')).toBeVisible();
});

for (const shell of ['direct', 'reload']) test(`Round 2: ${shell} sign-out removes private paths from every device-layout scope`, async ({ browser }) => {
  test.setTimeout(90000);
  // An independent login session keeps this sign-out from ending other specs' shared setup session.
  const context = await browser.newContext({ baseURL: info().origin });
  const fresh = await context.newPage();
  try {
    await freshStep(fresh);
    expect((await fresh.request.post('/api/auth/login', { headers: { Origin: info().origin }, data: { username: 'alex', password: PASSWORD, code: totp(loadSecret()) } })).ok()).toBeTruthy();
     const project = await gitProject(fresh, 'cedar-cleanup');
    expect((await fresh.request.post(`/api/files/project/${project.id}`, { headers: await csrfHeaders(fresh), data: { action: 'create', path: 'private-notes.txt', content: 'Fixture' } })).ok()).toBeTruthy();
    await fresh.goto(`/projects/${project.id}?tab=files&file=private-notes.txt`);
    await expect(fresh.getByRole('textbox', { name: 'File content' })).toBeVisible();
    const scope = (await (await fresh.request.get(`/api/projects/${project.id}/layouts/desktop/wide`)).json()).scope;
    const seeded = (keys: string[]) => fresh.evaluate(names => { for (const name of names) localStorage.setItem(name, '{}'); }, keys);
    const pending = (person: string, id: number) => 'ryn-layout-pending:v2:' + JSON.stringify([scope.installation, person, id, 'desktop', 'wide']);
    const stored = () => fresh.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('rynhult:working:') || key.startsWith('ryn-layout-pending:')));
    await seeded(['rynhult:working:v1:' + JSON.stringify([scope.installation, 'alex', `project:${project.id}`]), pending('another-person', project.id), pending(scope.person, 99999999)]);
    await fresh.reload();
    await expect(fresh.getByRole('textbox', { name: 'File content' })).toBeVisible();
    await expect.poll(stored).toEqual([]);
    await seeded([pending(scope.person, 99999998)]);
    if (shell === 'reload') await fresh.reload();
    await fresh.getByRole('button', { name: /^Account:/ }).click();
    await fresh.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(fresh).toHaveURL(/\/login$/);
    // A late protected read can cause a full navigation after the router reaches /login.
    // Wait for the destination document, not just its URL, before evaluating storage.
    await expect(fresh.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
    await fresh.waitForLoadState('load');
    await expect.poll(stored).toEqual([]);
  } finally { await context.close(); }
});

test('Round 2: explicit handover preserves partial input across reload and hidden release', async ({ page, context }) => {
  test.setTimeout(120000);  const id = await sessionWithChanges(page, 'cedar-handover');
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  const sent: string[] = []; let busy = true;
  // Observe this already-mounted terminal's actual outgoing frames through a new browser CDP session.
  const cdp = await context.newCDPSession(page); await cdp.send('Network.enable');
  cdp.on('Network.webSocketFrameSent', event => { try { const message = JSON.parse(event.response.payloadData); if (message.t === 'i') sent.push(message.d); if (message.t === 'pins') busy = message.busy; } catch {} });
  const input = page.locator('.xterm-helper-textarea'); await input.focus();
  await page.keyboard.press('ArrowLeft');
  await expect.poll(() => busy).toBe(false);
  await page.getByRole('button', { name: 'Release control', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Control terminal', exact: true }).click();
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  await input.focus(); await page.keyboard.press('Control+u'); await page.keyboard.type('unfinished');
  await expect.poll(() => busy).toBe(false);
  const other = await context.newPage();
  try {
    await other.goto(`/sessions/${id}`);
    await other.getByRole('button', { name: 'Terminal options', exact: true }).click();
    await other.getByRole('button', { name: 'Take control…', exact: true }).click();
    const dialog = other.getByRole('dialog', { name: 'Take terminal control' });
    await expect(dialog).toContainText('Partial input may remain');
    await dialog.getByLabel('Reason').selectOption('controller_unavailable');
    for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
      await other.setViewportSize({ width, height: 900 }); await other.emulateMedia({ colorScheme }); await expectAxeClean(other);
    }
    await other.setViewportSize({ width: 1440, height: 900 });
    const before = [...sent];
    await dialog.getByRole('button', { name: 'Take control', exact: true }).click();
    await expect(other.getByText('You control this terminal', { exact: true })).toBeVisible();
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    expect(sent).toEqual(before);
    const audit = await (await page.request.get('/api/audit?action=terminal.control')).json();
    expect(audit.some((entry: any) => entry.target === id && entry.actor === 'alex' && entry.detail.outcome === 'granted' && entry.detail.reason === 'controller_unavailable' && entry.detail.previous_actor === 'alex' && entry.detail.previous_channel === 'terminal' && entry.detail.new_channel === 'terminal')).toBe(true);
    expect(JSON.stringify(audit)).not.toContain('unfinished');
    await other.getByRole('button', { name: 'Close view', exact: true }).click();
    await other.evaluate(id => { history.pushState({ key: crypto.randomUUID() }, '', `/sessions/${id}`); window.dispatchEvent(new PopStateEvent('popstate')); }, id);
    await other.getByRole('button', { name: 'Release control', exact: true }).click();
    await other.getByRole('dialog', { name: 'Release with partial input?' }).getByRole('button', { name: 'Release without clearing' }).click();
    await expect(other.getByRole('button', { name: 'Release hidden control', exact: true })).toHaveCount(0);
    // The same person reclaims the released prompt after reload without clearing or replaying it.

    await other.reload();
    await expect(other.getByText('You control this terminal', { exact: true })).toBeVisible();
    await expect(other.getByRole('dialog', { name: 'Take terminal control' })).toHaveCount(0);
    expect(sent).toEqual(before);
    await other.locator('.xterm-helper-textarea').focus(); await other.keyboard.press('Enter');
    await expect.poll(() => existsSync(`${info().run_dir}/${id}/fake.received`) ? readFileSync(`${info().run_dir}/${id}/fake.received`, 'utf8') : '').toBe('unfinished\n');
  } finally { await other.close();  }
});

test('Round 2: cold Status board stays outside the frame', async ({ page }) => {

  try {
    await page.goto('/board');
    await expect(page.getByRole('heading', { name: 'Machines', exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toHaveCount(0);
    await expect(page.locator('.shell-header, .shell-sidebar')).toHaveCount(0);
    await page.goto('/sessions');
    await expect(page.getByRole('heading', { name: 'Sessions', exact: true, level: 1 })).toBeVisible();
  } finally {  }
});
