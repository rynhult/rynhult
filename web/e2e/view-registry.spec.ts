import { test as base, expect, type Page } from './fixtures';
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Test-only source entry, never an app route or production build input. The
// worker owns an ephemeral loopback listener with no proxy to an app instance.
const test = base.extend<{}, { fixtureURL: string }>({
  fixtureURL: [async ({}, use) => {
    const cacheDir = await mkdtemp(join(tmpdir(), 'view-registry-vite-'));
    const server = await createServer({
      cacheDir,
      root: fileURLToPath(new URL('..', import.meta.url)),
      configFile: fileURLToPath(new URL('./fixtures/view-registry/vite.config.ts', import.meta.url)),
      configLoader: 'runner',
      server: { host: '127.0.0.1', port: 0, strictPort: true },
    });
    try {
      await server.listen();
      const address = server.httpServer?.address();
      if (!address || typeof address === 'string') throw new Error('Fixture listener unavailable.');
      await use(`http://127.0.0.1:${address.port}`);
    } finally { await server.close(); }
  }, { scope: 'worker' }],
  baseURL: async ({ fixtureURL }, use) => { await use(fixtureURL); },
});

async function fakePTY(page: Page, firstLine = 'SCROLLBACK-SENTINEL', initialLines = 120, stream = true) {
  const timers = new Set<ReturnType<typeof setInterval>>();
  const closeSockets: ((code: number) => Promise<void>)[] = [];
  page.once('close', () => { for (const timer of timers) clearInterval(timer); });
  const events = { connections: 0, closed: 0, urls: [] as string[], inputs: [] as string[], resizes: 0, disconnect: (code: number) => Promise.all(closeSockets.map(close => close(code))) };
  await page.routeWebSocket('**/api/sessions/*/tty?*', socket => {
    events.connections++; events.urls.push(socket.url());
    const write = new URL(socket.url()).searchParams.get('mode') === 'write';
    socket.send(JSON.stringify({ t: 'mode', write, ...(write ? {} : {run: 'fake-run', instance: 'test', protocol: 'run-observer-v1', rows: 24, cols: 80}) }));
    socket.send(Buffer.from(firstLine + '\r\n' + Array.from({ length: initialLines }, (_, n) => `seed-${n}\r\n`).join('')));
    let sequence = 0;
    const output = setInterval(() => { if (stream) socket.send(Buffer.from(`stream-${sequence++}\r\n`)); }, 25);
    timers.add(output);
    closeSockets.push(async code => { clearInterval(output); timers.delete(output); await socket.close({ code, reason: 'Access or run ended' }); });
    socket.onMessage(data => {
      const message = JSON.parse(String(data));
      if (message.t === 'i') events.inputs.push(message.d);
      if (message.t === 'r') events.resizes++;
    });
    socket.onClose(() => { clearInterval(output); timers.delete(output); events.closed++; });
  });
  return events;
}
const snapshot = (page: Page) => page.evaluate(() => (window as any).fixture.snapshot());
async function ready(page: Page, query = '') {
  await page.goto(`/e2e/fixtures/view-registry/index.html${query}`);
  await page.waitForFunction(() => (window as any).fixture?.snapshot().state.state === 'open');
  await expect(page.locator('.xterm')).toHaveCount(1);
  await page.waitForFunction(() => (window as any).fixture.resources[0].terminal.buffer.active.getLine(0)?.translateToString(true).length > 0);
}

test('P2 moves one observer between three slots 20 times, retaining editor, history, selection and composition', async ({ page }) => {
  const events = await fakePTY(page); await ready(page);
  const initialBox = await page.locator('[data-terminal-host-layer] > div').boundingBox();
  await page.evaluate(() => {
    const resource = (window as any).fixture.resources[0];
    resource.terminal.scrollToTop(); resource.terminal.select(0, 0, 'SCROLLBACK-SENTINEL'.length); resource.terminal.focus();
    const input = resource.terminal.textarea;
    input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    input.value = '日本'; input.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: '日本' }));
  });
  for (let i = 0; i < 20; i++) {
    const slot = ['two', 'fill', 'one'][i % 3];
    await page.evaluate(slot => (window as any).fixture.move(slot), slot);
    const result = await snapshot(page);
    expect(result).toMatchObject({ inputSubscribers: 1, sameTerm: true, sameSocket: true, sameHost: true, sameParent: true, composing: true, writer: false, focused: true, count: 1 });
    expect(result.selection).toBe('SCROLLBACK-SENTINEL');
    expect(result.history[0]).toBe('SCROLLBACK-SENTINEL');
    expect(await page.locator('.xterm-helper-textarea').inputValue()).toBe('日本');
    // Native preedit keeps its geometry; the latest placement applies at commit.
    expect(await page.locator('[data-terminal-host-layer] > div').boundingBox()).toEqual(initialBox);
  }
  await page.evaluate(() => {
    const input = (window as any).fixture.resources[0].terminal.textarea;
    input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '日本' }));
  });
  await expect.poll(async () => page.evaluate(() => {
    const host = (window as any).fixture.resources[0].host.getBoundingClientRect();
    const target = document.getElementById('fill')!.getBoundingClientRect();
    return Math.abs(host.left - target.left) + Math.abs(host.top - target.top)
      + Math.abs(host.width - target.width) + Math.abs(host.height - target.height);
  })).toBeLessThan(1);
  expect(events.connections).toBe(1); expect(events.closed).toBe(0);
  expect(events.inputs).toEqual([]); expect(events.resizes).toBe(0);
  expect(new URL(events.urls[0]).searchParams.get('mode')).toBe('read');
  expect(new URL(events.urls[0]).searchParams.has('take')).toBe(false);
  const result = await snapshot(page);
  expect(result.history.slice(0, 121)).toEqual(['SCROLLBACK-SENTINEL', ...Array.from({ length: 120 }, (_, n) => `seed-${n}`)]);
  await page.evaluate(() => { (window as any).fixture.shutdown(); (window as any).fixture.shutdown(); });
  await expect.poll(() => events.closed).toBe(1);
  await expect(page.locator('.xterm')).toHaveCount(0);
});

test('P1 flag claims only once; hidden views retain output/claim but cannot type; second view creates no attachment', async ({ page }) => {
  const events = await fakePTY(page); await ready(page, '?p1');
  await page.locator('.xterm-helper-textarea').focus(); await page.keyboard.type('once');
  await expect.poll(() => events.inputs.join('')).toBe('once');
  await page.evaluate(() => (window as any).fixture.second());
  expect(events.connections).toBe(1);
  await page.evaluate(() => (window as any).fixture.hide());
  const hidden = await snapshot(page);
  expect(hidden).toMatchObject({ inert: true, stdinDisabled: true, writer: true, count: 1 });
  await expect.poll(async () => (await snapshot(page)).history.length).toBeGreaterThan(hidden.history.length);
  expect(events.closed).toBe(0);
  await page.keyboard.type('hidden'); expect(events.inputs.join('')).toBe('once');
  await page.evaluate(() => (window as any).fixture.show());
  await page.locator('.xterm-helper-textarea').focus(); await page.keyboard.type('again');
  await expect.poll(() => events.inputs.join('')).toBe('onceagain');
  expect(events.connections).toBe(1); expect(events.resizes).toBeGreaterThan(0);
});

test('slot removal hides without disposing; explicit revocation removes editor and socket', async ({ page }) => {
  const events = await fakePTY(page); await ready(page);
  await page.evaluate(() => (window as any).fixture.unregister());
  expect((await snapshot(page)).inert).toBe(true); expect(events.closed).toBe(0);
  await page.evaluate(() => { const f = (window as any).fixture; f.registry.dispose(f.first.resourceKey, 'permission-revoked'); });
  await expect.poll(() => events.closed).toBe(1);
  await expect(page.locator('.xterm')).toHaveCount(0);
});

for (const code of [4403, 4409]) test(`server close ${code} removes the buffered snapshot and requires explicit recheck`, async ({ page }) => {
  const events = await fakePTY(page, 'Private fixture output'); await ready(page);
  await events.disconnect(code);
  await expect(page.locator('.xterm')).toHaveCount(0);
  const state = await page.evaluate(() => { const resource = (window as any).fixture.resources[0]; return { editor: !!resource.terminal, writer: !!resource.writerToken, status: resource.status.kind }; });
  expect(state).toEqual({ editor: false, writer: false, status: 'closed' });
  await page.waitForTimeout(100); expect(events.connections).toBe(1);
});


test('disposal before lazy Safari input binding completes opens no socket or orphan editor', async ({ page }) => {
  const events = await fakePTY(page);
  await page.addInitScript(() => Object.defineProperty(navigator, 'userAgent', { value: 'Mozilla/5.0 Macintosh Version/18 Safari/605.1.15' }));
  await page.goto('/e2e/fixtures/view-registry/index.html?disposeEarly');
  await page.waitForFunction(() => (window as any).fixture);
  expect(events.connections).toBe(0);
  await expect(page.locator('.xterm')).toHaveCount(0);
  await expect(page.locator('[data-terminal-host-layer]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).fixture.resources[0].state)).toBe('closed');
});

test('attachment construction failure cleans the editor and shows only a neutral placeholder', async ({ page }) => {
  const events = await fakePTY(page);
  await page.addInitScript(() => { window.WebSocket = class { constructor() { throw new Error('private token should not appear'); } } as any; });
  await page.goto('/e2e/fixtures/view-registry/index.html');
  await page.waitForFunction(() => (window as any).fixture);
  await expect(page.getByText('Terminal attachment unavailable.', { exact: true })).toBeVisible();
  await expect(page.locator('.xterm')).toHaveCount(0);
  expect(events.connections).toBe(0);
  expect(await page.locator('body').textContent()).not.toContain('private token');
  await page.evaluate(() => (window as any).fixture.shutdown());
});


test('selection restoration uses cell columns for wide Unicode, not string offsets', async ({ page }) => {
  await fakePTY(page, '界A漢Z'); await ready(page);
  await page.evaluate(() => { const term = (window as any).fixture.resources[0].terminal; term.scrollToTop(); term.select(3, 0, 2); });
  expect((await snapshot(page)).selection).toBe('漢');
  await page.evaluate(() => (window as any).fixture.move('two'));
  expect((await snapshot(page)).selection).toBe('漢');
  await page.evaluate(() => (window as any).fixture.move('fill'));
  expect((await snapshot(page)).selection).toBe('漢');
});

test('multiline selection and reading position survive column reflow', async ({ page }) => {
  const text = 'Reading anchor ' + 'wide 漢字 content '.repeat(12);
  await fakePTY(page, text); await ready(page);
  await page.evaluate(() => { const term = (window as any).fixture.resources[0].terminal; term.scrollToTop(); term.selectLines(0, 4); });
  const selected = (await snapshot(page)).selection;
  expect(selected.length).toBeGreaterThan(80);
  for (const slot of ['two', 'fill', 'one']) {
    await page.evaluate(slot => (window as any).fixture.move(slot), slot);
    await expect.poll(async () => (await snapshot(page)).selection).toBe(selected);
    expect(await page.evaluate(() => (window as any).fixture.resources[0].terminal.buffer.active.viewportY)).toBe(0);
  }
});

test('eight attachments evict the LRU hidden terminal and reattach without replay', async ({ page, context }) => {
  const events = await fakePTY(page, 'budget', 12000, false); await ready(page);
  await page.evaluate(async () => {
    const f = (window as any).fixture;
    const reference = f.registry.inspect(f.first.resourceKey).reference;
    for (let i = 1; i < 8; i++) f.registry.acquire({ ...reference, sessionId: `budget-${i}` });
    await Promise.all(f.resources.map((resource: any) => resource.ready));
  });
  await expect(page.locator('.xterm')).toHaveCount(8);
  await page.waitForFunction(() => (window as any).fixture.resources.every((resource: any) => resource.terminal.buffer.active.length >= 10000));
  await page.evaluate(async () => {
    const f = (window as any).fixture;
    const ref = f.registry.inspect(f.first.resourceKey).reference;
    // The oldest hidden terminal is protected by unsent input; evict the next.
    f.resources[2].terminal.scrollToLine(42);
    const pinned = f.registry.acquire({ ...ref, sessionId: 'budget-1' });
    f.registry.setPins(pinned.resourceKey, { unsent: true });
    const next = f.registry.acquire({ ...ref, sessionId: 'ninth' });
    await f.resources.at(-1).ready;
    if (f.registry.inspect(pinned.resourceKey).state === 'detached') throw new Error('Lost partial input');
    const evicted = f.resources.find((r: any) => r.isDisposed);
    if (!evicted) throw new Error('No hidden terminal evicted');
    const evictedKey = JSON.stringify([ref.installation, ref.person, 'budget-2', ref.runId, ref.node, ref.account]);
    if (f.registry.inspect(evictedKey).state !== 'detached') throw new Error('Did not evict the next LRU');
    const old = f.registry.acquire({ ...ref, sessionId: 'budget-2' });
    await f.resources.at(-1).ready;
    if (f.registry.inspect(old.resourceKey).state === 'detached') throw new Error('Failed reattach');
    f.registry.closeView(next.viewId);
  });
  await expect.poll(() => page.evaluate(() => (window as any).fixture.resources.at(-1).terminal.buffer.active.viewportY)).toBe(42);
  await expect.poll(() => events.connections).toBe(10);
  await expect.poll(() => events.closed).toBe(2);
  await expect(page.locator('.xterm')).toHaveCount(8);
  const buffers = await page.evaluate(() => (window as any).fixture.resources.filter((r: any) => !r.isDisposed).map((resource: any) => ({ rows: resource.terminal.buffer.active.length, visibleRows: resource.terminal.rows, hidden: resource.host.inert })));
  for (const buffer of buffers) expect(buffer.rows).toBeLessThanOrEqual(10000 + buffer.visibleRows);
  expect(buffers.filter((buffer: any) => buffer.hidden)).toHaveLength(7);
  const cdp = await context.newCDPSession(page); const heap = await cdp.send('Runtime.getHeapUsage');
  console.log(`Registry fixture: 8 sockets, 7 hidden, <=10,000 scrollback rows each; JS heap used ${Math.round(heap.usedSize / 1024 / 1024)} MiB (not whole-browser memory).`);
  expect(events.inputs).toEqual([]); expect(events.resizes).toBe(0);
  await page.evaluate(() => (window as any).fixture.shutdown()); await expect.poll(() => events.closed).toBe(10);
  await expect(page.locator('.xterm')).toHaveCount(0);
});

test('Safari capture adapter cannot hide active composition from the registry pin', async ({ page }) => {
  const events = await fakePTY(page);
  await page.addInitScript(() => Object.defineProperty(navigator, 'userAgent', { value: 'Mozilla/5.0 Macintosh Version/18 Safari/605.1.15' }));
  await ready(page);
  await page.evaluate(() => {
    const textarea = (window as any).fixture.resources[0].terminal.textarea;
    textarea.focus(); textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    textarea.value = '日本'; textarea.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: '日本' }));
  });
  expect((await snapshot(page)).composing).toBe(true);
  await page.evaluate(() => (window as any).fixture.move('two'));
  expect(await page.locator('.xterm-helper-textarea').inputValue()).toBe('日本');
  expect((await snapshot(page)).composing).toBe(true);
  await page.evaluate(() => (window as any).fixture.resources[0].terminal.textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '日本' })));
  expect((await snapshot(page)).composing).toBe(false);
  expect(events.inputs).toEqual([]);
});

test('P2 attach is bound to the supplied run and refuses an older-server acknowledgement', async ({ page }) => {
  let url = '';
  await page.routeWebSocket('**/api/sessions/*/tty?*', socket => {
    url = socket.url();
    socket.send(JSON.stringify({ t: 'mode', write: false }));
    socket.send(Buffer.from('UNVERIFIED OUTPUT'));
  });
  await page.goto('/e2e/fixtures/view-registry/index.html');
  await page.waitForFunction(() => (window as any).fixture);
  expect(new URL(url).searchParams.get('run')).toBe('fake-run');
  expect(new URL(url).searchParams.get('instance')).toBe('test');
  await expect.poll(async () => (await snapshot(page)).state.state).toBe('closed');
  expect((await snapshot(page)).history.join('')).not.toContain('UNVERIFIED OUTPUT');
});

test('all pinned attachments allow one overflow and evict oldest only after input clears', async ({ page }) => {
  const events = await fakePTY(page); await ready(page);
  await page.evaluate(async () => {
    const f = (window as any).fixture;
    const reference = f.registry.inspect(f.first.resourceKey).reference;
    f.registry.setPins(f.first.resourceKey, { unsent: true });
    for (let i = 1; i < 9; i++) {
      const view = f.registry.acquire({ ...reference, sessionId: `pinned-${i}` });
      f.registry.setPins(view.resourceKey, { unsent: true });
    }
    f.queued = f.registry.acquire({ ...reference, sessionId: 'queued' });
    await Promise.all(f.resources.map((resource: any) => resource.ready));
  });
  await expect.poll(() => events.connections).toBe(9);
  expect(events.closed).toBe(0);
  await expect(page.locator('.xterm')).toHaveCount(9);
  expect(await page.evaluate(() => (window as any).fixture.registry.inspect((window as any).fixture.queued.resourceKey).reason)).toBeUndefined();
  await page.evaluate(() => {
    const f = (window as any).fixture;
    f.registry.setVisibility(f.first.viewId, false);
    if (f.resources[0].isDisposed) throw new Error('Disposed unsent input');
    f.registry.setPins(f.first.resourceKey, { unsent: false });
  });
  await expect.poll(() => events.closed).toBe(1);
  await expect(page.locator('.xterm')).toHaveCount(8);
  await page.evaluate(() => (window as any).fixture.shutdown());
  await expect.poll(() => events.closed).toBe(9);
  expect(events.inputs).toEqual([]);
});
