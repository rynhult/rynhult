import { expect, test, type Page } from './fixtures';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, info, resetTerminalThrottle, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE });
let sessionId: string | undefined;

test.beforeEach(async ({ page }) => {
  await resetTerminalThrottle(page);
  await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: false } });
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    (window as any).__terminalSockets = [];
    (window as any).__terminalSent = [];
    (window as any).__terminalTimeline = [];
    document.addEventListener('keydown', event => {
      if ((event.target as HTMLElement)?.classList.contains('xterm-helper-textarea'))
        (window as any).__terminalTimeline.push({stage:'browser.keydown', at:performance.timeOrigin+performance.now(), key:event.key});
    }, true);
    window.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        if (String(url).includes('/tty?')) (window as any).__terminalSockets.push(this);
      }
      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (this.url.includes('/tty?') && typeof data === 'string') {
          (window as any).__terminalSent.push(data);
          (window as any).__terminalTimeline.push({stage:'browser.send',at:performance.timeOrigin+performance.now(),frame:JSON.parse(data)});
        }
        super.send(data);
      }
    };
  });
  const { id, headers } = await gitProject(page, 'terminal-input');
  const made = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', name: 'Terminal input lead', worktree: true,
    orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 3, total: 10, depth: 2, hours: 8 },
  } });
  expect(made.ok(), await made.text()).toBe(true);
  sessionId = (await made.json()).id;
  await page.goto(`/sessions/${sessionId}`);
  await waitForSessionState(page, sessionId!, 'idle', 'claude_hook');
  await writable(page);
});

test.afterEach(async ({ page }, testInfo) => {
  if (!page.isClosed()) {
    const timeline=await page.evaluate(() => (window as any).__terminalTimeline);
    const recording=JSON.stringify({session:sessionId,timeline});
    writeFileSync(testInfo.outputPath('terminal-timeline.json'),recording);
    await testInfo.attach('terminal-timeline', {contentType:'application/json',body:recording});
  }
  if (testInfo.status !== testInfo.expectedStatus && !page.isClosed()) {
    // Fixture-only key evidence distinguishes a browser-handler failure from
    // input delayed between the bridge, broker and fake provider.
    const input = await page.evaluate(() => ({ sent: (window as any).__terminalSent,
      focused: document.activeElement?.className }));
    await testInfo.attach('terminal-input-frames', { contentType: 'application/json', body: JSON.stringify(input) });
    if (sessionId) {
      const pane = await page.request.get(`/api/sessions/${sessionId}/terminal-output`);
      if (pane.ok()) await testInfo.attach('terminal-input-pane', { contentType: 'application/json', body: await pane.body() });
    }
  }
  if (sessionId) expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  sessionId = undefined;
});

async function writable(page: Page) {
  const take = page.getByRole('button', { name: 'Take control', exact: true });
  await expect(page.getByText('Read-write', { exact: true }).or(take)).toBeVisible();
  if (await take.isVisible()) {
    await take.click();
    const dialog = page.getByRole('dialog', { name: 'Take terminal control' });
    await dialog.getByLabel('Reason').selectOption('manual_handover');
    await dialog.getByRole('button', { name: 'Take control', exact: true }).click();
  }
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await page.locator('.xterm-helper-textarea').focus();
}

async function submittedOnce(page: Page, expected: string) {
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${sessionId}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe(`${expected}\n`);
  const pane = await page.request.get(`/api/sessions/${sessionId}/terminal-output`);
  expect(pane.ok()).toBe(true);
  expect((await pane.json()).text).toContain(expected);
}

// Keyboard events only queue WebSocket frames. Observe PTY echo before tearing
// down an attachment: these cases prove no replay of already-delivered input,
// rather than assuming in-flight bytes survive a deliberately closed socket.
async function deliveredPrefix(page: Page, prefix: string) {
  await page.keyboard.type(prefix);
  await expect.poll(async () => {
    const pane = await page.request.get(`/api/sessions/${sessionId}/terminal-output`);
    expect(pane.ok()).toBe(true);
    return (await pane.json()).text as string;
  }).toContain(prefix);
}

test('ordinary keys including deliberately repeated text reach the pane once', async ({ page }) => {
  await page.keyboard.type('review review aabbcc');
  await submittedOnce(page, 'review review aabbcc');
});

test('native clipboard paste followed by typing reaches the pane once', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.keyboard.type('before ');
  await page.evaluate(() => navigator.clipboard.writeText('pasted text'));
  await page.keyboard.press('Control+Shift+V');
  await page.keyboard.type(' after');
  await submittedOnce(page, 'before pasted text after');
});

test('reconnect in the middle of typing never replays the old prefix', async ({ page }) => {
  await deliveredPrefix(page, 'before-');
  await page.evaluate(() => (window as any).__terminalSockets.at(-1).close());
  await page.getByRole('button', { name: 'Reconnect', exact: true }).click();
  await writable(page);
  await page.keyboard.type('after');
  await submittedOnce(page, 'before-after');
});

test('switching tabs and remounting attaches only one input handler', async ({ page }) => {
  await deliveredPrefix(page, 'before-');
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await expect(page.locator('.xterm-helper-textarea')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await writable(page);
  await page.keyboard.type('after');
  await submittedOnce(page, 'before-after');
});

for (const reader of [true, false]) test(`native IME composition sends only committed characters (screen reader ${reader})`, async ({ page, context }) => {
  if (!reader) {
    await page.getByRole('checkbox', { name: 'Screen reader output', exact: true }).uncheck();
    await page.locator('.xterm-helper-textarea').focus();
  }
  await page.keyboard.type('review ');
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'é', selectionStart: 1, selectionEnd: 1 });
  await cdp.send('Input.insertText', { text: 'é' });
  await page.keyboard.type(' then ');
  await cdp.send('Input.imeSetComposition', { text: '日本', selectionStart: 2, selectionEnd: 2 });
  await cdp.send('Input.insertText', { text: '日本' });
  await page.keyboard.type(' done');
  await submittedOnce(page, 'review é then 日本 done');
});


test('dead key composition with IME Process key events never re-inserts earlier text', async ({ page, context }) => {
  await page.keyboard.type('review ');
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Dead', code: 'Quote', windowsVirtualKeyCode: 222 });
  await cdp.send('Input.imeSetComposition', { text: 'é', selectionStart: 1, selectionEnd: 1 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Unidentified', windowsVirtualKeyCode: 229 });
  await cdp.send('Input.insertText', { text: 'é' });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Unidentified', windowsVirtualKeyCode: 229 });
  await page.keyboard.type(' done');
  await submittedOnce(page, 'review é done');
});


test('composition commit followed by a key before timers flush never replays the prefix', async ({ page }) => {
  await page.keyboard.type('review ');
  await page.locator('.xterm-helper-textarea').evaluate((textarea: HTMLTextAreaElement) => {
    textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    textarea.value += 'é';
    textarea.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'é' }));
    textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'é' }));
    textarea.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'd', code: 'KeyD', keyCode: 68, which: 68 }));
  });
  await page.keyboard.type('one');
  await submittedOnce(page, 'review édone');
});


test('a Process-key fallback queued before composition never replays its commit', async ({ page }) => {
  await page.keyboard.type('review ');
  await page.locator('.xterm-helper-textarea').evaluate((textarea: HTMLTextAreaElement) => {
    // Keep fallback work pending until this whole transaction completes.
    textarea.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, keyCode: 229, which: 229 }));
    textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    textarea.value += 'é';
    textarea.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'é' }));
    textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'é' }));
    textarea.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'd', code: 'KeyD', keyCode: 68, which: 68 }));
  });
  await page.keyboard.type('one');
  await submittedOnce(page, 'review édone');
});


test('wake and reconnect do not replay input retained before sleep', async ({ page, context }) => {
  await deliveredPrefix(page, 'awake-');
  const cdp = await context.newCDPSession(page);
  await cdp.send('Page.setWebLifecycleState', { state: 'frozen' });
  await context.setOffline(true);
  await context.setOffline(false);
  await cdp.send('Page.setWebLifecycleState', { state: 'active' });
  // A sleeping socket can remain half-open; simulate its eventual close notice.
  await page.evaluate(() => (window as any).__terminalSockets.at(-1).close());
  await page.getByRole('button', { name: 'Reconnect', exact: true }).click();
  await writable(page);
  await page.keyboard.type('again');
  await submittedOnce(page, 'awake-again');
});


test('cancelled composition never replays the old prefix or loses the following key', async ({ page, context }) => {
  await page.keyboard.type('review ');
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'discard', selectionStart: 7, selectionEnd: 7 });
  await cdp.send('Input.imeSetComposition', { text: '', selectionStart: 0, selectionEnd: 0 });
  await page.keyboard.type('done');
  await submittedOnce(page, 'review done');
});

test('reloading with unsent text keeps writing for the same person and warns to inspect it', async ({ page }) => {
  const project = await gitProject(page, 'partial-reload');
  const created = await (await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'partial-reload' } })).json();
  sessionId = created.id; await waitForSessionState(page, created.id, 'idle');
  await page.goto(`/sessions/${created.id}`);
  const input = page.locator('.xterm-helper-textarea');
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await input.focus(); await page.keyboard.type('half');
  await expect.poll(() => page.evaluate(() => (window as any).__terminalSent.some((f: string) => JSON.parse(f).partial === true))).toBe(true);
  await page.waitForTimeout(300);
  await page.reload();
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await expect(page.locator('[data-terminal-notice]')).toContainText('Partial input may remain');
  await expect(page.getByRole('status', { name: 'Announcements' })).toContainText('Inspect');
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toHaveCount(0);
  const refused = await page.request.post(`/api/sessions/${created.id}/message`, { headers: await csrfHeaders(page), data: { text: 'automation must wait' } });
  expect(refused.ok()).toBeTruthy();
  await expect.poll(async () => JSON.stringify(await (await page.request.get(`/api/sessions/${created.id}/deliveries`)).json())).toMatch(/"state":"queued"/);
  await input.focus(); await page.keyboard.press('Control+u');
  await expect(page.locator('[data-terminal-notice]')).toHaveCount(0);
});
