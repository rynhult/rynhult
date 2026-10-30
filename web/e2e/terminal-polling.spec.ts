import { expect, test } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, info, resetTerminalThrottle } from './helpers';

test.use({ storageState: AUTH_FILE, serviceWorkers: 'block' });
let id: string;
test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('terminal-'), 'Run in the focused terminal matrix.');
  await page.addInitScript(() => {
    (window as any).__resizeNotifications = new Set<() => void>();
    (window as any).__terminalResizes = [];
    const NativeObserver = window.ResizeObserver;
    window.ResizeObserver = class extends NativeObserver {
      notify: () => void;
      constructor(callback: ResizeObserverCallback) {
        super(callback);
        this.notify = () => callback([], this);
      }
      observe(target: Element, options?: ResizeObserverOptions) {
        super.observe(target, options);
        if (target.querySelector('.xterm')) (window as any).__resizeNotifications.add(this.notify);
      }
      disconnect() {
        super.disconnect();
        (window as any).__resizeNotifications.delete(this.notify);
      }
    };
    const NativeSocket = window.WebSocket;
    window.WebSocket = class extends NativeSocket {
      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (this.url.includes('/tty?') && typeof data === 'string') {
          const frame = JSON.parse(data);
          if (frame.t === 'r') (window as any).__terminalResizes.push([frame.rows, frame.cols]);
        }
        super.send(data);
      }
    };
  });
  await resetTerminalThrottle(page);
  const project = await gitProject(page, 'predictive-input');
  const created = await page.request.post('/api/sessions', { headers: project.headers,
    data: { project_id: project.id, provider: 'claude', name: 'Predictive input' } });
  expect(created.ok()).toBe(true);
  id = (await created.json()).id;
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await page.locator('.xterm-helper-textarea').focus();
});
test.afterEach(async ({ page }) => {
  if (id) await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) });
});

test('parent polling and unchanged resize notifications leave the terminal input untouched', async ({ page }) => {
  await page.bringToFront();
  let queued = true;
  await page.route(`**/api/sessions/${id}/deliveries`, route => route.fulfill({ json: { deliveries: queued
    ? [{ id: 'polling-demo', state: 'queued', preview: 'Queued context', notice: 'Waiting for terminal control' }] : [] } }));
  const geometry = (await page.locator('.xterm').boundingBox())!;
  const field = page.locator('.xterm-helper-textarea');
  const original = await field.elementHandle();
  let polls = 0;
  await page.route(`**/api/sessions/${id}`, async route => {
    const response = await route.fetch();
    const session = await response.json();
    polls++;
    await route.fulfill({ json: { ...session, name: `Polling session ${polls}` } });
  });
  await page.evaluate(() => {
    const field = document.querySelector('.xterm-helper-textarea')!;
    (window as any).__originalField = field;
    (window as any).__focusLoss = 0;
    field.addEventListener('blur', () => (window as any).__focusLoss++);
  });
  await page.keyboard.type('before-');
  const before = await field.inputValue();
  // Native preedit must remain in the same field through parent renders.
  await field.evaluate((field: HTMLTextAreaElement) => {
    field.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    field.value += 'composing';
    field.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'composing' }));
  });
  for (let index = 0; index < 16; index++) {
    await page.waitForTimeout(800);
    await page.evaluate(() => {
      // ResizeObserver can notify again without changing character dimensions.
      for (const notify of (window as any).__resizeNotifications) notify();
    });
    expect(await field.inputValue()).toBe(before + 'composing');
    expect(await original!.evaluate(node => node === document.querySelector('.xterm-helper-textarea'))).toBe(true);
    expect(await field.evaluate(node => document.activeElement === node)).toBe(true);
  }
  await expect(page.getByRole('region', { name: 'Message delivery status' })).toBeVisible();
  const withQueue = (await page.locator('.xterm').boundingBox())!;
  expect(Math.abs(withQueue.y - geometry.y)).toBeLessThan(1);
  expect(Math.abs(withQueue.height - geometry.height)).toBeLessThan(1);
  queued = false;
  await expect(page.getByRole('region', { name: 'Message delivery status' })).toHaveCount(0);
  expect(await field.inputValue()).toBe(before + 'composing');
  const settled = (await page.locator('.xterm').boundingBox())!;
  expect(Math.abs(settled.y - geometry.y)).toBeLessThan(1);
  expect(Math.abs(settled.height - geometry.height)).toBeLessThan(1);
  await expect.poll(() => polls).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window as any).__focusLoss)).toBe(0);
  const frames = await page.evaluate(() => (window as any).__terminalResizes);
  for (let index = 1; index < frames.length; index++) expect(frames[index]).not.toEqual(frames[index - 1]);
  await field.evaluate(field => field.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'composing' })));
  await page.waitForTimeout(30);
  await page.keyboard.type('-after');
  await page.keyboard.press('Enter');
  const received = `${info().run_dir}/${id}/fake.received`;
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('before-composing-after\n');
});

test('settled delivery history backs off and hidden panels do not poll', async ({ page }) => {
  let deliveries = 0;
  const hidden: string[] = [];
  page.on('request', request => {
    if (/\/(preview|gates|worklist|plan|changes)(?:\?|$)/.test(new URL(request.url()).pathname)) hidden.push(request.url());
  });
  await page.route(`**/api/sessions/${id}/deliveries`, route => {
    deliveries++;
    return route.fulfill({ json: { deliveries: [] } });
  });
  await page.waitForTimeout(6500);
  expect(deliveries).toBeLessThanOrEqual(1);
  expect(hidden).toEqual([]);
});
