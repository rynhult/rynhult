import { test, expect } from './fixtures';
import { writeFileSync } from 'node:fs';
import { AUTH_FILE, control, csrfHeaders, gitProject, info, waitForSessionState } from './helpers';
test.use({ storageState: AUTH_FILE });
test.skip(process.env.ACC_E2E_P2_TERMINAL_HOST !== '1', 'Opt-in P2 build qualification only.');

test('P2 host keeps one socket editor listener set and native process across routes tabs and Grid', async ({ page }) => {
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    (window as any).ttySockets = [];
    window.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        if (String(url).includes('/tty?')) (window as any).ttySockets.push(this);
      }
    };
    const add = EventTarget.prototype.addEventListener, remove = EventTarget.prototype.removeEventListener;
    const listeners = new WeakMap<object, Map<string, Set<any>>>();
    EventTarget.prototype.addEventListener = function(type, listener, options) {
      if (this instanceof HTMLTextAreaElement && listener) {
        let events = listeners.get(this); if (!events) listeners.set(this, events = new Map());
        let set = events.get(type); if (!set) events.set(type, set = new Set());
        set.add(listener);
      }
      return add.call(this, type, listener, options);
    };
    EventTarget.prototype.removeEventListener = function(type, listener, options) {
      if (listener) listeners.get(this)?.get(type)?.delete(listener);
      return remove.call(this, type, listener, options);
    };
    (window as any).terminalListenerCount = (element: object) =>
      [...(listeners.get(element)?.values() ?? [])].reduce((sum, set) => sum + set.size, 0);
  });
  const { id, headers } = await gitProject(page, 'host-lifetime');
  const made = await page.request.post('/api/sessions', {headers, data: {project_id: id, provider: 'claude', name: 'Persistent host proof'}});
  expect(made.ok(), await made.text()).toBe(true);
  const sid = (await made.json()).id;
  try {
    await page.goto(`/sessions/${sid}`);
    await waitForSessionState(page, sid, 'idle', 'claude_hook');
    const textarea = page.locator('.xterm-helper-textarea');
    await expect(textarea).toHaveCount(1);
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    let unboundSnapshots = 0;
    await page.route(`**/api/sessions/${sid}/terminal-output`, async route => { unboundSnapshots++; await route.continue(); });
    await page.getByRole('button', {name: 'Open as text', exact: true}).click();
    const output = page.getByRole('dialog', {name: 'Recent terminal output', exact: true});
    await expect(output.getByText(/Showing only output held in this browser for this run/)).toBeVisible();
    expect(unboundSnapshots).toBe(0);
    await output.getByRole('button', {name: 'Close', exact: true}).click();
    const before = (await (await page.request.get(`/api/sessions/${sid}`)).json()).runs;
    await page.evaluate(() => {
      const element = document.querySelector('.xterm-helper-textarea');
      (window as any).originalTerminal = element;
      (window as any).originalListeners = (window as any).terminalListenerCount(element);
    });
    for (let round = 0; round < 3; round++) {
      await page.getByRole('tab', {name: 'Activity', exact: true}).click();
      expect(await page.evaluate(() => (window as any).originalTerminal.isConnected)).toBe(true);
      await expect(textarea).toBeHidden();
      await page.getByRole('tab', {name: 'Terminal', exact: true}).click();
      await expect(textarea).toBeVisible();
      // Router navigation (not reload): the host must outlive SessionPage.
      await page.getByRole('link', {name: 'Projects', exact: true}).first().click();
      await expect(textarea).toBeHidden();
      await page.goBack(); await expect(textarea).toBeVisible();
      await page.getByRole('button', {name: 'Expand terminal', exact: true}).click();
      await expect(page.getByRole('dialog', {name: 'Expanded terminal', exact: true})).toBeVisible();
      await page.getByRole('button', {name: 'Collapse terminal', exact: true}).click();
      expect(await page.evaluate(() => ({
        same: document.querySelector('.xterm-helper-textarea') === (window as any).originalTerminal,
        sockets: (window as any).ttySockets.length,
        listeners: (window as any).terminalListenerCount((window as any).originalTerminal),
        initial: (window as any).originalListeners,
      }))).toMatchObject({same: true, sockets: 1});
      expect(await page.evaluate(() => (window as any).terminalListenerCount((window as any).originalTerminal)))
        .toBe(await page.evaluate(() => (window as any).originalListeners));
    }
    expect(await page.evaluate(() => (window as any).ttySockets[0].url)).toContain('run=');
    await page.getByRole('link', {name: 'Grid', exact: true}).first().click();
    await expect(textarea).toBeVisible();
    expect(await page.evaluate(() => document.querySelector('.xterm-helper-textarea') === (window as any).originalTerminal)).toBe(true);
    expect(await page.evaluate(() => (window as any).ttySockets.length)).toBe(1);
    await page.goBack();
    await expect(page.getByRole('checkbox', {name: 'Screen reader output', exact: true})).toBeChecked();
    await expect(page.locator('.xterm-accessibility-tree')).toHaveCount(1);
    await page.setViewportSize({width: 390, height: 844});
    await expect(textarea).toBeVisible();
    expect(await page.evaluate(() => document.querySelector('.xterm-helper-textarea') === (window as any).originalTerminal)).toBe(true);
    expect(await page.evaluate(() => (window as any).ttySockets.length)).toBe(1);
    const after = (await (await page.request.get(`/api/sessions/${sid}`)).json()).runs;
    expect(after).toHaveLength(before.length); expect(after.at(-1).id).toBe(before.at(-1).id);
    expect(after.at(-1).pid).toBe(before.at(-1).pid);
    const measured = await page.evaluate(() => ({
      hosts: document.querySelectorAll('.xterm').length,
      sockets: (window as any).ttySockets.length,
      inputDOMListeners: (window as any).terminalListenerCount((window as any).originalTerminal),
    }));
    expect(measured).toMatchObject({hosts: 1, sockets: 1});
    expect(measured.inputDOMListeners).toBeGreaterThan(0);
    expect(measured.inputDOMListeners).toBe(await page.evaluate(() => (window as any).originalListeners));
    console.log('P2 host identity measurements', measured);
    await page.screenshot({path: 'e2e/screenshots/p0-terminal-host-phone.png'});
  } finally {
    await page.request.post(`/api/sessions/${sid}/stop`, {headers: await csrfHeaders(page)});
  }
});

test('unsupported reference never falls back to a writer; explicit disconnect and recheck create observers', async ({ page }) => {
  let attaches = 0;
  page.on('websocket', socket => { if (socket.url().includes('/tty?')) attaches++; });
  const {id, headers} = await gitProject(page, 'host-reference');
  const made = await page.request.post('/api/sessions', {headers, data: {project_id: id, provider: 'claude', name: 'Reference refusal'}});
  expect(made.ok()).toBe(true);
  const sid = (await made.json()).id;
  try {
    await page.route(`**/api/sessions/${sid}/terminal-reference`, async route => {
      const response = await route.fetch();
      await route.fulfill({json: {...await response.json(), protocol: 'unsupported'}});
    });
    await page.goto(`/sessions/${sid}`);
    await waitForSessionState(page, sid, 'idle', 'claude_hook');
    await expect(page.getByText(/Terminal attachment unavailable/)).toBeVisible();
    expect(attaches).toBe(0);
    await expect(page.locator('.xterm')).toHaveCount(0);
    await page.unroute(`**/api/sessions/${sid}/terminal-reference`);
    await page.getByRole('button', {name: 'Recheck', exact: true}).click();
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    expect(attaches).toBe(1);
    await page.getByRole('button', {name: 'Disconnect', exact: true}).click();
    await expect(page.locator('.xterm')).toHaveCount(0);
    await page.getByRole('button', {name: 'Recheck', exact: true}).click();
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    expect(attaches).toBe(2);
    const state = await (await page.request.get(`/api/sessions/${sid}`)).json();
    expect(state.runs).toHaveLength(1);
    await expect(page.getByRole('button', {name: 'Take control', exact: true})).toHaveCount(0);
  } finally {
    await page.request.post(`/api/sessions/${sid}/stop`, {headers: await csrfHeaders(page)});
  }
});

test('observer geometry stays fixed across viewports and any lost connection erases its cache', async ({ page }) => {
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    (window as any).observerSockets = [];
    window.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        if (String(url).includes('/tty?')) (window as any).observerSockets.push(this);
      }
    };
  });
  const {id, headers} = await gitProject(page, 'observer-grid');
  const made = await page.request.post('/api/sessions', {headers, data: {project_id: id, provider: 'claude', name: 'Observer geometry'}});
  expect(made.ok()).toBe(true);
  const sid = (await made.json()).id;
  try {
    await page.goto(`/sessions/${sid}`);
    await waitForSessionState(page, sid, 'idle', 'claude_hook');
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    const grid = page.locator('.xterm-screen');
    const original = await grid.evaluate(el => ({width: (el as HTMLElement).style.width, height: (el as HTMLElement).style.height}));
    const rows = page.locator('.xterm-accessibility-tree [role=listitem]');
    expect(await rows.count()).toBeGreaterThan(20);
    const row = Math.min((await rows.count()) - 4, 40);
    writeFileSync(`${info().run_dir}/${sid}/fake.screen`, `\x1b[2J\x1b[HGRID-TOP\x1b[${row};1HGRID-LOWER-ROW\x1b[H`);
    control(sid, 'screen');
    await expect(rows.nth(row - 1)).toContainText('GRID-LOWER-ROW');
    await page.setViewportSize({width: 390, height: 844});
    await expect(grid).toBeVisible();
    await expect.poll(() => grid.evaluate(el => ({width: (el as HTMLElement).style.width, height: (el as HTMLElement).style.height}))).toEqual(original);
    await expect(rows.nth(row - 1)).toContainText('GRID-LOWER-ROW');
    await page.evaluate(() => (window as any).observerSockets[0].close(4000, 'unclassified connection loss'));
    await expect(page.locator('.xterm')).toHaveCount(0);
    await expect(page.getByText(/Terminal detached/)).toBeVisible();
    await page.getByRole('button', {name: 'Recheck', exact: true}).click();
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    expect((await (await page.request.get(`/api/sessions/${sid}`)).json()).runs).toHaveLength(1);
  } finally {
    await page.request.post(`/api/sessions/${sid}/stop`, {headers: await csrfHeaders(page)});
  }
});

test('hidden P2 host is erased on sign-out while its native session is left running', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'host-revocation');
  const made = await page.request.post('/api/sessions', {headers, data: {project_id: id, provider: 'claude', name: 'Hidden observer'}});
  expect(made.ok()).toBe(true);
  const sid = (await made.json()).id;
  try {
    await page.goto(`/sessions/${sid}`);
    await waitForSessionState(page, sid, 'idle', 'claude_hook');
    await expect(page.locator('.xterm-helper-textarea')).toHaveCount(1);
    await expect(page.getByText('Read-only · observing this session', {exact: true})).toBeVisible();
    await page.getByRole('tab', {name: 'Activity', exact: true}).click();
    await expect(page.getByRole('complementary', {name: 'Background terminals'})).toBeVisible();
    await page.getByTitle('Sign out', {exact: true}).click();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('.xterm')).toHaveCount(0);
    await expect(page.locator('[data-terminal-host-layer]')).toHaveCount(0);
    await expect(page.getByRole('complementary', {name: 'Background terminals'})).toHaveCount(0);
  } finally {
    // The setup fixture's installation token, exclusively in this test's data dir.
    const {readFileSync} = await import('node:fs');
    const {info} = await import('./helpers');
    const token = readFileSync(`${info().state}/data/secrets/api_token`, 'utf8').trim();
    await page.request.post(`/api/sessions/${sid}/stop`, {headers: {Authorization: `Bearer ${token}`}});
  }
});
