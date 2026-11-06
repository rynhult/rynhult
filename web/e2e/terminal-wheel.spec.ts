import { test, expect } from './fixtures';
import { writeFileSync, readFileSync, existsSync } from 'node:fs';
import { AUTH_FILE, control, csrfHeaders, gitProject, info } from './helpers';
test.use({ storageState: AUTH_FILE });
test('Codex wheel scrolls tmux history instead of sending prompt-history arrows', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const { id: project, headers } = await gitProject(page, 'codex-wheel');
  const created = await page.request.post('/api/sessions', { headers,
    data: { project_id: project, provider: 'codex', name: 'Wheel regression' } });
  expect(created.ok(), await created.text()).toBe(true);
  const id = (await created.json()).id;
  const input: string[] = [];
  const scroll: number[] = [];
  page.on('websocket', ws => ws.on('framesent', frame => {
    try { const value = JSON.parse(String(frame.payload)); if (value.t === 'i') input.push(value.d); if (value.t === 'wheel') scroll.push(value.lines); } catch { /* control frame */ }
  }));
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
    const tree = page.locator('.xterm-accessibility-tree');
    await expect(tree).toContainText('fake-codex ready');
    // Captured Codex 0.163.0 inline modes: paste/focus, no mouse or alt
    // screen. The real broker's tmux attach adds the alternate buffer.
    const lines = Array.from({ length: 180 }, (_, i) => `wheel-history-${String(i).padStart(3, '0')}`);
    writeFileSync(`${info().run_dir}/${id}/fake.screen`, '\x1b[?2004h\x1b[?1004h' + lines.join('\r\n') + '\r\nwheel-bottom-marker\r\n');
    control(id, 'screen');
    await expect(tree).toContainText('wheel-bottom-marker');
    const box = (await page.locator('.xterm-screen').boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -480);
    await expect(tree).toContainText('wheel-history-');
    for (let notch = 0; notch < 10; notch++) await page.mouse.wheel(0, -120);
    await expect(tree).not.toContainText('wheel-bottom-marker');
    await expect(tree).toContainText('wheel-history-');
    const start = scroll.length;
    const beforeTrackpad = await tree.textContent();
    await page.locator('.xterm-screen').evaluate(el => {
      for (let i = 0; i < 20; i++) el.dispatchEvent(new WheelEvent('wheel', {deltaY: -4, bubbles: true, cancelable: true}));
    });
    await expect.poll(() => scroll.slice(start).reduce((sum, n) => sum + n, 0)).toBe(-2);
    await expect(tree).not.toHaveText(beforeTrackpad!);
    expect(scroll.slice(1, start).every(lines => lines === -3)).toBe(true);
    expect(input.join('')).not.toMatch(/\x1b(?:\[|O)[AB]/);
    await page.mouse.wheel(0, 4000);
    await expect(tree).toContainText('wheel-bottom-marker');
    await page.mouse.wheel(0, -800);
    await expect(tree).not.toContainText('wheel-bottom-marker');

    await page.keyboard.type('wheel-input-still-works');
    await page.keyboard.press('Enter');
    const received = `${info().run_dir}/${id}/fake.received`;
    await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toContain('wheel-input-still-works');
    // Plain drag selection must stay in the browser, with no tmux mouse mode.
    writeFileSync(`${info().run_dir}/${id}/fake.screen`, '\x1b[2J\x1b[HBrowser selection marker\r\n');
    control(id, 'screen');
    await expect(tree).toContainText('Browser selection marker');
    const rowHeight = await tree.locator('> div').first().evaluate(el => el.getBoundingClientRect().height);
    await page.mouse.move(box.x + 1, box.y + rowHeight / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 180, box.y + rowHeight / 2, { steps: 12 });
    await page.mouse.up();
    await page.getByRole('button', { name: 'Copy selection', exact: true }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('Browser selection');

  } finally {
    await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) });
  }
});
