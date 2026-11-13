import { expect, test } from './fixtures';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, gitProject, info, PASSWORD, totp, verifyAgain } from './helpers';
test.use({ presentationChanges: ['admin_features', 'layouts'], storageState: AUTH_FILE });
async function enable(page: import('@playwright/test').Page, enabled = true) {
  if (enabled) {
    let state = await (await page.request.get('/api/admin-features')).json();
    if (!state.features.find((f: { id: string; enabled: boolean }) => f.id === 'chat')?.enabled) {
      expect((await page.request.put('/api/admin-features/chat', { headers: await csrfHeaders(page), data: { enabled: true, revision: state.revision } })).ok()).toBeTruthy();
      state = await (await page.request.get('/api/admin-features')).json();
    }

  }

}
// The integrated native Chat reader validates the complete page contract.
async function mockChat(page: import('@playwright/test').Page, sessionId: string, state: () => string) {
  const detail = await (await page.request.get(`/api/sessions/${sessionId}`)).json();
  await page.route(`**/api/sessions/${sessionId}/chat?*`, route => route.fulfill({ json: {
    session_id: sessionId, run_id: detail.runs.at(-1).id, state: state(), items: [],
    historical_run_attribution: 'unknown', observed_at: '2026-11-01T20:00:00Z', omitted: 0,
    reset: false, more: false, reason: null, notices: { tools: 0 }, cursor: 'fixture-cursor', before_cursor: null,
    capabilities: { read_history: state() === 'ready', follow_messages: false, rich_tools: false, send_text: false },
  } }));
}
async function shot(page: import('@playwright/test').Page, name: string) {
  await page.screenshot({ path: process.env.ACC_UPDATE_EXCEPTION_SCREENSHOTS === '1' ? `../docs/handoffs/shell-exception-states/${name}.png` : test.info().outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
}
async function joinMember(page: import('@playwright/test').Page, context: import('@playwright/test').BrowserContext) {
  const account = `recovery-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invite.ok(), await invite.text()).toBeTruthy();
  const token = (await invite.json()).link.split('/').pop();
  await context.clearCookies();
  const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
  const joined = await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
  expect(joined.ok(), await joined.text()).toBeTruthy();

}
for (const role of ['owner', 'member']) test(`${role}: recovery states in light and dark; no recovery mutations`, async ({ page, context }) => {
  test.setTimeout(120_000);
  await enable(page);
  if (role === 'member') await joinMember(page, context);
  const { id, headers } = await gitProject(page, `recovery-${role}`);
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Review notes' } });
  expect(made.ok(), await made.text()).toBeTruthy(); const session = await made.json();
  await mockChat(page, session.id, () => 'unsupported');
  await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  let offline = true;
  await page.route(`**/api/sessions/${session.id}`, async route => {
    const r = await route.fetch(); await route.fulfill({ json: { ...await r.json(), machine_offline: offline } });
  });
  await page.route('**/api/projects', async route => {
    const r = await route.fetch(); await route.fulfill({ json: (await r.json()).map((p: { id: number }) => p.id === id ? { ...p, machine_offline: offline } : p) });
  });
  if (role === 'owner') await page.route('**/api/board', async route => {
    const r = await route.fetch(); const data = await r.json();
    await route.fulfill({ json: { ...data, machines: data.machines.map((m: { local: boolean }) => m.local ? { ...m, last_seen: '2026-10-30T12:00:00Z' } : m) } });
  });
  await page.route('**/api/sessions', async route => {
    const r = await route.fetch(); await route.fulfill({ json: (await r.json()).map((s: { id: string }) => s.id === session.id ? { ...s, machine_offline: offline } : s) });
  });
  const writes: string[] = []; const sockets: string[] = []; const inventory: string[] = [];
  page.on('request', r => { if (r.method() === 'POST') writes.push(r.url()); if (new URL(r.url()).pathname === '/api/board') inventory.push(r.url()); });
  page.on('websocket', ws => sockets.push(ws.url()));
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); offline = true;
    await page.goto(`/projects/${id}`);
    await expect(page.getByRole('region', { name: 'Offline host' })).toContainText('Last seen');
    await expect(page.getByRole('region', { name: 'Sessions' }).getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
    await expectAxeClean(page); await shot(page, `${role}-${theme}-project-offline`);
    await page.goto(`/sessions/${session.id}`);
    await expect(page.getByRole('region', { name: 'Offline host' })).toBeVisible();
    if (role === 'owner') await expect(page.getByRole('region', { name: 'Offline host' }).locator('time')).toHaveAttribute('datetime', '2026-10-30T12:00:00.000Z');
    else await expect(page.getByRole('region', { name: 'Offline host' })).toContainText('not reported for this view');
    await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
    await expectAxeClean(page); await shot(page, `${role}-${theme}-session-offline`);
    offline = false;
    await page.goto(`/sessions/${session.id}?tab=chat`);
    await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open Terminal', exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}\\?tab=chat$`));
    await expect(page.locator('.xterm')).toHaveCount(0);
    await expectAxeClean(page); await shot(page, `${role}-${theme}-unsupported-chat`);
    await page.goto('/sessions/private-resource-that-must-not-be-repeated?tab=files');
    await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible();
    await expect(page.getByRole('main')).not.toContainText('private-resource-that-must-not-be-repeated');
    await expectAxeClean(page); await shot(page, `${role}-${theme}-unavailable`);
    await page.getByRole('button', { name: 'Recheck availability' }).click();
    await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible();
  }
  expect(writes).toEqual([]); expect(sockets).toEqual([]);
  if (role === 'member') expect(inventory).toEqual([]);
});
for (const role of ['owner', 'member']) test(`${role}: conflict review keeps both revisions and draft after reopening the same view`, async ({ page, context }) => {
  await enable(page);
  if (role === 'member') await joinMember(page, context);
  const { id, folder } = await gitProject(page, 'recovery-conflict');
  await page.goto(`/projects/${id}?tab=files`);
  await page.getByRole('button', { name: fileRow('app.py') }).click();
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill("print('my draft')");
  writeFileSync(`${folder}/app.py`, "print('concurrent disk')\n");
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
  const writes: string[] = []; page.on('request', r => { if (r.method() === 'POST') writes.push(r.url()); });
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await page.getByRole('button', { name: 'Review changes', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Current version on disk' })).toContainText('concurrent disk');
    await expect(page.getByText('Current disk revision', { exact: false })).toBeVisible();
    await expectAxeClean(page); await shot(page, `${role}-${theme}-file-conflict`);
    await page.getByRole('button', { name: 'Keep editing', exact: true }).click();
    await expect(editor).toContainText('my draft');
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  }
  expect(writes).toEqual([]); expect(readFileSync(`${folder}/app.py`, 'utf8')).toContain('concurrent disk');
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('button', { name: 'Keep draft and use current revision' }).click();
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('status', { name: 'File save status' })).toContainText('Saved');
  expect(readFileSync(`${folder}/app.py`, 'utf8')).toContain('my draft');
  await enable(page, false); await page.reload();
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
  await editor.fill('old layout draft'); writeFileSync(`${folder}/app.py`, 'old layout disk');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Review changes', exact: true })).toBeVisible();
});

test('Chat fallback preserves another terminal writer; denied refresh hides cached resource', async ({ page, context }) => {
  await enable(page); const { id, headers } = await gitProject(page, 'recovery-writer');
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Cedar private review' } });
  expect(made.ok(), await made.text()).toBeTruthy(); const session = await made.json();
  await mockChat(page, session.id, () => 'unsupported');
  const writer = await context.newPage();
  try {
    // Separate saved-layout buckets: the Chat window has no previously requested
    // terminal to restore, while the medium-width window retains its writer.
    await writer.setViewportSize({ width: 1000, height: 900 });
    await writer.goto(`/sessions/${session.id}`);
    await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    const sockets: string[] = []; const writes: string[] = []; const controlOrInput: string[] = [];
    page.on('websocket', ws => {
      sockets.push(ws.url());
      ws.on('framesent', event => {
        const frame = JSON.parse(String(event.payload));
        if (frame.t === 'control' || frame.t === 'i') controlOrInput.push(frame.t);
      });
    });
    page.on('request', r => { if (r.method() === 'POST') writes.push(r.url()); });
    await page.goto(`/sessions/${session.id}?tab=chat`);
    await expect(page.getByRole('button', { name: 'Open Terminal', exact: true })).toBeVisible();
    expect(sockets).toEqual([]);
    await page.getByRole('button', { name: 'Open Terminal', exact: true }).click();
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Control terminal', exact: true })).toBeEnabled();
    await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    expect(writes).toEqual([]); expect(sockets.every(url => !url.includes('take=1'))).toBe(true);
    expect(sockets).toHaveLength(1);
    expect(new URL(sockets[0]).searchParams.get('mode')).toBe('read');
    expect(controlOrInput).toEqual([]);
    await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({ status: 403, json: { detail: 'Sensitive denied resource metadata' } }));
    await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('main')).not.toContainText('Cedar private review');
    await expect(page.getByRole('main')).not.toContainText('Sensitive denied resource metadata');
    await expect(page.locator('.xterm')).toHaveCount(0);
  } finally { await writer.close(); }
});

test('unavailable Files hides details and retains an existing draft during read-only rechecks', async ({ page }) => {
  await enable(page); const { id } = await gitProject(page, 'recovery-file-access');
  await page.goto(`/projects/${id}?tab=files`);
  await page.getByRole('button', { name: fileRow('app.py') }).click();
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill('retained draft after access returns');
  let denied = true;
  await page.route(url => url.pathname === `/api/files/project/${id}`, async route => {
    if (denied) await route.fulfill({ status: 403, json: { detail: 'Private file error metadata' } });
    else await route.continue();
  });
  const writes: string[] = [];
  page.on('request', r => { if (r.method() === 'POST') writes.push(r.url()); });
  await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible({ timeout: 15000 });
  await expect(editor).toHaveCount(0);
  await expect(page.getByRole('main')).not.toContainText('Private file error metadata');
  await expect(page.getByRole('main')).not.toContainText('app.py');
  await page.getByRole('button', { name: 'Recheck availability' }).click();
  await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible();
  denied = false;
  await page.getByRole('button', { name: 'Recheck availability' }).click();
  await expect(editor).toContainText('retained draft after access returns');
  expect(writes).toEqual([]);
});

test('E1: machine offline disables Resume only, keeping Stop Interrupt and Archive conditions', async ({ page }) => {
  await enable(page); const { id, headers } = await gitProject(page, 'offline-actions');
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Offline work' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json();
  await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  let running = true;
  await page.route(`**/api/sessions/${session.id}`, async route => {
    const r = await route.fetch(); await route.fulfill({ json: { ...await r.json(), machine_offline: true, observed_state: running ? 'active' : 'stopped', desired_state: running ? 'running' : 'stopped' } });
  });
  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Interrupt', exact: true })).toBeEnabled();
  running = false; await page.reload();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
  await page.locator('.session-heading').getByRole('button', { name: 'Session details', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Archive', exact: true })).toBeEnabled();
});

test('E2: an unseen disk revision cannot be adopted or discarded; a second race still conflicts', async ({ page }) => {
  await enable(page); const { id, folder } = await gitProject(page, 'unseen-conflict');
  await page.goto(`/projects/${id}?tab=files`); await page.getByRole('button', { name: fileRow('app.py') }).click();
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill('my retained draft'); writeFileSync(`${folder}/app.py`, 'first disk change');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Current version on disk' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Keep draft and use current revision' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Use disk version', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Current version on disk' })).toContainText('first disk change');
  await page.getByRole('button', { name: 'Keep draft and use current revision' }).click();
  writeFileSync(`${folder}/app.py`, 'second disk change');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Keep draft and use current revision' })).toBeDisabled();
  await expect(page.getByRole('region', { name: 'Current version on disk' })).toHaveCount(0);
  expect(readFileSync(`${folder}/app.py`, 'utf8')).toBe('second disk change');
  await expect(editor).toContainText('my retained draft');
});

test('E3: Chat recovery uses adapter state and read_history, not the provider name', async ({ page }) => {
  await enable(page); const { id, headers } = await gitProject(page, 'chat-capabilities');
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Chat capability' } });
  const session = await made.json(); await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  let state = 'ready';
  await mockChat(page, session.id, () => state);
  await page.goto(`/sessions/${session.id}?tab=chat`);
  await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('main')).toContainText('Messages from the native conversation.');
  await expect(page.locator('.xterm')).toHaveCount(0);
  const words: Record<string, string> = { unknown: 'The Chat source is not established yet.', offline: 'Machine offline. Retained messages may be stale;', busy: 'Chat source is busy.', error: 'The account service could not read Chat.' };
  for (const unknown of ['unknown', 'offline', 'busy', 'error']) {
    state = unknown; await page.reload();
    await expect(page.getByRole('main')).toContainText(words[unknown]);
    await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.', { exact: true })).toHaveCount(0);
  }
  state = 'unsupported'; await page.reload();
  await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.', { exact: true })).toBeVisible();
  await page.unroute(`**/api/sessions/${session.id}/chat?*`);
  await page.route(`**/api/sessions/${session.id}/chat?*`, route => route.fulfill({ status: 404, json: { detail: 'No Chat here.' } }));
  await page.reload();
  await expect(page.getByRole('main')).toContainText('You no longer have access to this Chat.');
  await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.', { exact: true })).toHaveCount(0);
});

test('E4: standard Files keeps the default single retry', async ({ page }) => {
  await enable(page, false); const { id } = await gitProject(page, 'legacy-read-retry');
  let reads = 0;
  await page.route(url => url.pathname === `/api/files/project/${id}`, route => { reads++; return route.fulfill({ status: 503, json: { detail: 'Unavailable for test' } }); });
  await page.goto(`/projects/${id}?tab=files`);
  await expect(page.getByRole('region', { name: 'Folder tree' }).getByRole('alert')).toBeVisible({ timeout: 15000 });
  expect(reads).toBe(2);
});

test('E5: offline uses inventory label, real remote check-in and space before Terminal', async ({ page }) => {
  await enable(page); const { id, headers } = await gitProject(page, 'offline-card');
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Offline card' } });
  const session = await made.json(); await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  let remote = false;
  await page.route(`**/api/sessions/${session.id}`, async route => { const r = await route.fetch(); await route.fulfill({ json: { ...await r.json(), host: remote ? 'birch' : 'local', machine_offline: true } }); });
  await page.route('**/api/board', route => route.fulfill({ json: { machines: [{ name: 'acc-host', label: 'Cedar host', local: true }, { name: 'birch', label: 'Birch workstation', local: false, last_seen: '2026-10-30T12:00:00Z' }] } }));
  await page.goto(`/sessions/${session.id}`);
  const card = page.getByRole('region', { name: 'Offline host' });
  await expect(card.getByRole('heading')).toHaveText('Cedar host is offline');
  await expect(card).toContainText('not reported for this view');
  expect(await card.evaluate(el => parseFloat(getComputedStyle(el).marginBottom))).toBeGreaterThan(0);
  await expect(page.getByText('Machine offline. This view returns when the machine checks in.', { exact: true })).toHaveCount(0);
  remote = true; await page.reload();
  await expect(card.getByRole('heading')).toHaveText('Birch workstation is offline');
  await expect(card.locator('time')).toHaveAttribute('datetime', '2026-10-30T12:00:00.000Z');
});

test('E6: unavailable and unknown session tabs stay neutral and never attach a terminal', async ({ page }) => {
  await enable(page); const { id, headers } = await gitProject(page, 'neutral-tab');
  const made = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Valid session' } });
  const session = await made.json(); await page.request.post(`/api/sessions/${session.id}/stop`, { headers });
  // No saved Terminal in this project: test unavailable links independently of
  // the writer scenario's explicitly opened, restorable terminal arrangement.
  const sockets: string[] = []; const writes: string[] = [];
  page.on('websocket', socket => sockets.push(socket.url()));
  page.on('request', request => { if (request.method() === 'POST') writes.push(request.url()); });
  for (const tab of ['team', 'missing-private-tab']) {
    await page.goto(`/sessions/${session.id}?tab=${tab}`);
    await expect(page.getByRole('heading', { name: 'This saved view can’t be opened' })).toBeVisible();
    await expect(page.getByRole('main')).not.toContainText('your access has changed');
    await expect(page.getByRole('link', { name: 'Open Terminal', exact: true })).toHaveAttribute('href', `/sessions/${session.id}`);
    await expect(page.locator('.xterm')).toHaveCount(0);
    expect(sockets).toEqual([]); expect(writes).toEqual([]);
  }
});
