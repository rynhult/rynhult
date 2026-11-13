import { test, expect, type Page } from './fixtures';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { AUTH_FILE, csrfHeaders, gitProject, verifyAgain } from './helpers';
import { BASE } from './fixture-paths';

test.use({ actionTimeout: 10000, storageState: AUTH_FILE, presentationChanges: ['admin_features', 'layouts'] });

test('terminal rows fill the retained pane and resize its PTY', async ({ page, context, browserName }) => {
  await verifyAgain(page);

  const project = await gitProject(page, 'terminal-height');
  const made = await page.request.post('/api/sessions', { headers: project.headers, data: { project_id: project.id, provider: 'claude', name: 'Terminal height' } });
  expect(made.ok(), await made.text()).toBe(true);
  const sid = (await made.json()).id;
  const socket = JSON.parse(readFileSync(`${BASE}/processes.json`, 'utf8')).socket;
  async function fills(page: Page) {
    await expect(page.locator('.xterm-screen')).toBeVisible();
    await expect.poll(() => page.locator('.registered-terminal-slot').evaluate(slot => {
      const panel = slot.closest('.shell-terminal-panel')!;
      return panel.getBoundingClientRect().bottom - slot.getBoundingClientRect().bottom;
    })).toBeLessThanOrEqual(1);
    await expect.poll(() => page.locator('.xterm-screen').evaluate(screen => {
      const host = screen.closest('.xterm')!.parentElement!;
      const rows = screen.querySelectorAll('.xterm-rows > div');
      const cell = rows[0].getBoundingClientRect().height;
      const padding = getComputedStyle(screen.closest('.xterm')!);
      return Math.abs(host.clientHeight - parseFloat(padding.paddingTop) - parseFloat(padding.paddingBottom) - screen.getBoundingClientRect().height) / cell;
    })).toBeLessThanOrEqual(1);
    const session = await (await page.request.get(`/api/sessions/${sid}`)).json();
    await expect.poll(async () => {
      const grid = execFileSync('tmux', ['-L', socket, 'display-message', '-p', '-t', session.tmux_name, '#{pane_height}'], { encoding: 'utf8', timeout: 5000 }).trim();
      return page.locator('.xterm-screen').evaluate((screen, count) => {
        const row = screen.querySelector('.xterm-rows > div')!;
        return Math.abs(Number(count) * row.getBoundingClientRect().height - screen.getBoundingClientRect().height);
      }, grid);
    }).toBeLessThanOrEqual(1);
  }
  const observer = await context.newPage();
  const observerFrames: string[] = [];
  observer.on('websocket', socket => socket.on('framesent', frame => {
    try { const message = JSON.parse(String(frame.payload)); if (['control', 'r', 'i'].includes(message.t)) observerFrames.push(message.t); } catch { /* binary */ }
  }));
  async function matchesWriter() {
    const session = await (await page.request.get(`/api/sessions/${sid}`)).json();
    await expect.poll(async () => {
      const grid = execFileSync('tmux', ['-L', socket, 'display-message', '-p', '-t', session.tmux_name, '#{pane_height} #{pane_width}'], { encoding: 'utf8', timeout: 5000 }).trim().split(' ').map(Number);
      const writerWidth = await page.locator('.xterm-screen').evaluate(screen => parseFloat((screen as HTMLElement).style.width));
      return observer.locator('.xterm-screen').evaluate((screen, { grid: [rows], writerWidth }) => {
        const rendered = screen.querySelectorAll('.xterm-rows > div');
        const host = screen.closest('.xterm')!.parentElement!;
        return rendered.length === rows && Math.abs(parseFloat((screen as HTMLElement).style.width) - writerWidth) < 1
          && getComputedStyle(host).overflow === 'auto' && host.scrollWidth >= screen.getBoundingClientRect().width - 1;
      }, { grid, writerWidth });
    }).toBe(true);
    await expect(observer.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await expect(page.getByTitle('You control this terminal', { exact: true })).toBeVisible();
    expect(observerFrames).toEqual([]);
  }
  try {
    await page.goto(`/sessions/${sid}`);
    await expect(page.getByTitle('You control this terminal', { exact: true })).toBeVisible();
    await observer.setViewportSize({ width: 800, height: 600 });
    await observer.addInitScript(() => {
      const style = document.createElement('style'); style.id = 'hidden-terminal-fixture';
      style.textContent = '[data-terminal-host-layer] > [role=region] { display: none !important; }';
      document.addEventListener('DOMContentLoaded', () => document.head.append(style), { once: true });
    });
    await observer.goto(`/sessions/${sid}`);
    await expect(observer.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await expect(observer.locator('.xterm-screen')).toBeHidden();
    await observer.evaluate(() => document.getElementById('hidden-terminal-fixture')?.remove());
    await matchesWriter();
    await observer.setViewportSize({ width: 1100, height: 750 });
    await matchesWriter();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1920, height: 1080 }]) {
      await page.setViewportSize(viewport); await fills(page); await matchesWriter();
      await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click(); await fills(page);
      await page.getByRole('button', { name: 'Expand sidebar', exact: true }).click(); await fills(page);
      await page.locator('[role=tab][data-tab=Files]:visible').click();
      await expect(page.getByRole('tab', { name: 'Terminal height · Files', exact: true })).toHaveAttribute('aria-selected', 'true');
      await page.getByRole('tab', { name: 'Terminal height · Terminal', exact: true }).click(); await fills(page);
    }
    if (browserName === 'chromium') {
      // Display scaling can change cell rounding without resizing the host.
      const cdp = await page.context().newCDPSession(page);
      try {
        for (const deviceScaleFactor of [1.25, 2, 1]) {
          await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor, mobile: false });
          await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(deviceScaleFactor);
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          await fills(page);
        }
      } finally { await cdp.detach(); }
    }
  } finally {
    await observer.close();
    await page.request.post(`/api/sessions/${sid}/stop`, { headers: await csrfHeaders(page) });
  }
});
