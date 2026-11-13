import { test, expect } from './fixtures';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, info, sessionWithChanges, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['admin_features', 'layouts'] });

for (const failedLayout of [false, true]) test(`session Terminal claims free control and sends input (layout failure ${failedLayout})`, async ({ page, context }) => {

  const id = await sessionWithChanges(page, 'writer-regression');
  await waitForSessionState(page, id, 'idle');
  if (failedLayout) await page.route('**/layouts/**', route => route.fulfill({ status: 500, json: { detail: 'Fixture layout failure' } }));
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    const observer = await context.newPage();
    try {
      await observer.goto(`/sessions/${id}`);
      await expect(observer.getByText('Read-only terminal', { exact: true })).toBeVisible();
      await expect(observer.locator('[data-terminal-holder-chip]')).toHaveCount(1);
      await page.getByRole('button', { name: 'Release control', exact: true }).click();
      await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
      // One click claims a now-free terminal; no reload or second attempt.
      await observer.getByRole('button', { name: 'Control terminal', exact: true }).click();
      await expect(observer.getByText('You control this terminal', { exact: true })).toBeVisible();
      await observer.locator('.xterm-helper-textarea').focus();
      await observer.keyboard.type('writer-regression-input');
      await observer.keyboard.press('Enter');
      const received = `${info().run_dir}/${id}/fake.received`;
      await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('writer-regression-input\n');
      // A held terminal uses the existing reason/confirmation handover.
      await page.getByRole('button', { name: 'Control terminal', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: 'Take terminal control' });
      await dialog.getByLabel('Reason').selectOption('manual_handover');
      await dialog.getByRole('button', { name: 'Take control', exact: true }).click();
      await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
      await expect(observer.getByText('Read-only terminal', { exact: true })).toBeVisible();
    } finally { await observer.close(); }
  } finally {
    await page.unrouteAll({ behavior: 'wait' });
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
  }
});

for (const enabled of [true]) test(`Name and Rename are in the main New session form (new layout ${enabled})`, async ({ page }) => {

  await page.goto('/');
  await page.getByRole('button', { name: /^New session/ }).first().click();
  const form = page.getByRole(enabled ? 'region' : 'dialog', { name: 'New session', exact: true });
  await form.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Fix the login form error');
  await expect(form.getByTestId('automatic-name')).toHaveText('fix-the-login-form');
  const rename = form.getByRole('button', { name: 'Rename', exact: true });
  await expect(rename).toBeVisible();
  expect(await rename.evaluate(element => !!element.closest('details'))).toBe(false);
  await rename.click();
  const name = form.getByRole('textbox', { name: 'Name', exact: true });
  await expect(name).toBeFocused();
  await name.fill('login-fix');
  await form.getByRole('button', { name: enabled ? 'Back to workspace' : 'Close', exact: true }).click();
  await page.getByRole('button', { name: /^New session/ }).first().click();
  await expect(name).toHaveValue('login-fix');
  await form.getByRole('button', { name: 'Use automatic name', exact: true }).click();
  await expect(rename).toBeFocused();
  await expect(form.getByTestId('automatic-name')).toHaveText('fix-the-login-form');
});

test('a restored free terminal observes until its session tab is explicitly opened', async ({ page }) => {

  const id = await sessionWithChanges(page, 'restored-observer');
  await waitForSessionState(page, id, 'idle');
  const session = await (await page.request.get(`/api/sessions/${id}`)).json();
  const path = `/api/projects/${session.project_id}/layouts/desktop/wide`;
  const record = await (await page.request.get(path)).json();
  expect((await page.request.put(path, { headers: await csrfHeaders(page), data: { expected_revision: record.revision, layout: {
    schema_version: 2,
    views: [{ id: 'overview', kind: 'page', resource: { kind: 'project', id: session.project_id } }, { id: 'terminal', kind: 'terminal', resource: { kind: 'session', id } }],
    columns: [{ tabs: ['overview'], active: 'overview', width: 0.5 }, { tabs: ['terminal'], active: 'terminal', width: 0.5 }], focused: 0, filled: null,
  } } })).ok()).toBeTruthy();
  const controls: string[] = [];
  page.on('websocket', socket => socket.on('framesent', frame => {
    try { if (JSON.parse(String(frame.payload)).t === 'control') controls.push(String(frame.payload)); } catch { /* binary */ }
  }));
  try {
    await page.goto(`/projects/${session.project_id}`);
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    expect(controls).toEqual([]);
    await page.locator('[data-view-tab]').filter({ hasText: /Terminal/ }).click();
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    expect(controls).toHaveLength(1);
  } finally {
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
  }
});

test('hidden terminals evict at the cap and preserve partial input in one toolbar chip', async ({ page }) => {
  test.setTimeout(120_000);

  const ids: string[] = [];
  const visit = async (id: string) => {
    await page.evaluate(id => { history.pushState({ key: crypto.randomUUID() }, '', `/sessions/${id}`); window.dispatchEvent(new PopStateEvent('popstate')); }, id);
    await expect(page.locator(`[data-working-view="session:${id}:terminal"] .terminal-control-label`)).toHaveText('You control this terminal');
  };
  try {
    for (let i = 0; i < 10; i++) {
      const id = await sessionWithChanges(page, `hidden-control-${i}`); ids.push(id);
      await waitForSessionState(page, id, 'idle');
    }
    await page.goto(`/sessions/${ids[0]}`);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    // Deterministic document visibility event, using the real grace timer.
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, value: true }); document.dispatchEvent(new Event('visibilitychange')); });
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, value: false }); document.dispatchEvent(new Event('visibilitychange')); });
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    for (const id of ids.slice(1)) await visit(id);
    await visit(ids[0]);
    await expect(page.getByRole('button', { name: 'Release hidden control', exact: true })).toHaveCount(0);
    await page.getByRole('region', { name: 'Session terminal', exact: true }).locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('unsent-partial');
    await visit(ids[1]);
    await expect(page.locator('[data-working-view]:not([aria-hidden="true"]) [data-terminal-partial-chip]')).toHaveCount(1);
    await visit(ids[2]);
    await expect(page.locator('[data-working-view]:not([aria-hidden="true"]) [data-terminal-partial-chip]')).toHaveCount(1);
    await page.locator('[data-working-view]:not([aria-hidden="true"]) [data-terminal-partial-chip]').getByRole('button', { name: 'Show', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Release hidden control', exact: true })).toHaveCount(0);
    await visit(ids[1]);
    await page.getByRole('region', { name: 'Session terminal', exact: true }).locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('second-partial');
    await visit(ids[2]);
    await expect(page.locator('[data-working-view]:not([aria-hidden="true"]) [data-terminal-partial-chip]')).toHaveCount(1);
    await expect(page.getByText(/hidden terminals? (still )?hold/)).toHaveCount(0);
    await page.evaluate(id => { history.pushState({ key: crypto.randomUUID() }, '', `/grid?ids=${id}`); window.dispatchEvent(new PopStateEvent('popstate')); }, ids[0]);
    await expect(page.getByText(/^Read-only ·/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Release hidden control', exact: true })).toHaveCount(0);
  } finally {
    for (const id of ids) await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) });
  }
});

 test('open Terminal rechecks a restarted run and claims free control automatically', async ({ page }) => {

  const id = await sessionWithChanges(page, 'restart-control');
  await waitForSessionState(page, id, 'idle');
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    const before = await (await page.request.get(`/api/sessions/${id}/terminal-reference`)).json();
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${id}`)).json()).observed_state).toMatch(/^(stopped|exited)$/);
    expect((await page.request.post(`/api/sessions/${id}/resume`, { headers: await csrfHeaders(page), data: {} })).ok()).toBeTruthy();
    await waitForSessionState(page, id, 'idle');
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${id}/terminal-reference`)).json()).runId).not.toBe(before.runId);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('after-restart'); await page.keyboard.press('Enter');
    await expect.poll(() => existsSync(`${info().run_dir}/${id}/fake.received`) ? readFileSync(`${info().run_dir}/${id}/fake.received`, 'utf8') : '').toContain('after-restart\n');
  } finally { await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) }); }
});

test('another person holds control in one inline toolbar chip', async ({ page, otherOwner: other }) => {

  const id = await sessionWithChanges(page, 'other-controller');
  try {
    const controller = await other.newPage();

    await waitForSessionState(page, id, 'idle');
    await controller.goto(`/sessions/${id}`);
    await expect(controller.getByText('You control this terminal', { exact: true })).toBeVisible();
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    const chip = page.locator('[data-terminal-holder-chip]');
    await expect(chip).toHaveCount(1);
    expect(await chip.evaluate(el => !!el.closest('.session-terminal-toolbar'))).toBe(true);
    await expect(controller.getByText('You control this terminal', { exact: true })).toBeVisible();
    await expect(page.getByText(/hidden terminals? (still )?hold/)).toHaveCount(0);
  } finally { await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) }); }
});

test('own paste-upload partial input is reclaimed after reload; another person still confirms', async ({ page, context, otherOwner: other }) => {
  test.slow();

  const id = await sessionWithChanges(page, 'partial-reclaim');
  try {
    await waitForSessionState(page, id, 'idle');
    const writer = await context.newPage();
    await writer.goto(`/sessions/${id}`);
    await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    await writer.locator('.xterm-helper-textarea').evaluate(element => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], 'reclaim.png', { type: 'image/png' }));
      element.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
    });
    await expect(writer.getByRole('status').filter({ hasText: 'Upload done.' })).toBeVisible();
    await writer.close();  // the path waits in the prompt without Enter

    const person = await other.newPage();

    await person.goto(`/sessions/${id}`);
    await expect(person.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await expect(person.locator('[data-terminal-unsent-chip]')).toHaveCount(1);
    await person.getByRole('button', { name: 'Control terminal', exact: true }).click();
    const confirmation = person.getByRole('dialog', { name: 'Control with partial input?' });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('button', { name: 'Cancel', exact: true }).click();
    await person.close();

    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
    await expect(page.getByRole('dialog', { name: 'Control with partial input?' })).toHaveCount(0);
    await expect(page.locator('[data-terminal-unsent-chip]')).toHaveCount(0);
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.press('Enter');
    await expect.poll(() => existsSync(`${info().run_dir}/${id}/fake.received`) ? readFileSync(`${info().run_dir}/${id}/fake.received`, 'utf8') : '')
      .toMatch(/\.rynhult\/uploads\/\S*reclaim\.png\n/);
  } finally { await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) }); }
});
