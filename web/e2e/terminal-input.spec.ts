import { expect, test, type Page } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, info, resetTerminalThrottle } from './helpers';

test.use({ storageState: AUTH_FILE });
let sessionId: string | undefined;

test.beforeEach(async ({ page }) => {
  await resetTerminalThrottle(page);
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    (window as any).__terminalSockets = [];
    window.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        if (String(url).includes('/tty?')) (window as any).__terminalSockets.push(this);
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
  await writable(page);
});

test.afterEach(async ({ page }) => {
  if (sessionId) expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  sessionId = undefined;
});

async function writable(page: Page) {
  const take = page.getByRole('button', { name: 'Take control', exact: true });
  await expect(page.getByText('Read-write', { exact: true }).or(take)).toBeVisible();
  if (await take.isVisible()) await take.click();
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
