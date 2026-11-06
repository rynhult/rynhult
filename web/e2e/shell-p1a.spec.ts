import { test } from './shell-state';
import { expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['admin_features', 'ui_shell', 'layouts'] });
// Finish intercepted polling reads before Playwright disposes the request context.
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });
// Normal gate order leaves open notices. Navigation must not depend on an empty Inbox.
async function withOpenNotices(page: import('@playwright/test').Page) {
  await page.route('**/api/attention*', route => route.fulfill({ json: Array.from({ length: 3 }, (_, i) => ({
    id: 901 + i, kind: 'info', severity: 'info', source: 'system', verified: true,
    title: `Cedar fixture notice ${i + 1}`, detail: null, persistent: null,
    session_id: null, session_name: null, project: null,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), acked_at: null, resolved_at: null,
  })) }));
}
async function headers(page: import('@playwright/test').Page) {
  return { 'x-acc-csrf': (await page.context().cookies()).find(c => /acc_csrf$/.test(c.name))!.value };
}
async function feature(page: import('@playwright/test').Page, enabled: boolean) {
  const put = async () => {
    const state = await (await page.request.get('/api/admin-features')).json();
    return page.request.put('/api/admin-features/new_shell', { headers: await headers(page), data: { enabled, revision: state.revision } });
  };
  let result = await put();
  // new_shell is a Preview, so turning it on needs a recent sign-in verification (test_rc1_defaults).
  if (!result.ok() && enabled) { await verifyAgain(page); result = await put(); }
  expect(result.ok()).toBeTruthy();
}
async function preference(page: import('@playwright/test').Page, enabled: boolean) {
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await headers(page), data: { enabled } })).ok()).toBeTruthy();
}

// Older callers can still save the retired feature choice; it has no effect.
async function saveRetiredFeatureChoice(page: import('@playwright/test').Page) {
  await feature(page, false);
  expect((await (await page.request.get('/api/auth/state')).json()).instance.features.new_shell).toBe(true);
}

test('Settings offers no layout choice', async ({ page }) => {
  await preference(page, true);
  await page.goto('/settings#layout');
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
  await expect(page.getByLabel('Use the classic layout')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Switch now' })).toHaveCount(0);
});

test('rail navigation opens each primary page and every mode is keyboard reachable', async ({ page }) => {
  await withOpenNotices(page);
  await feature(page, true); await preference(page, true);
  await page.goto('/projects');
  const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
  await expect(rail).toBeVisible();
  await expect(rail.getByRole('button', { name: 'Inbox, 3 open', exact: true })).toBeVisible();
  for (const [name, path] of [['Overview', '/'], ['Inbox', '/inbox'], ['Sessions', '/sessions'], ['Projects', '/projects'], ['Workspaces', '/workspaces'], ['Hosts', '/hosts'], ['Grid', '/grid'], ['Tools', '/catalog'], ['Settings', '/settings']]) {
    await rail.getByRole('button', { name: name === 'Inbox' ? /^Inbox(?:,|$)/ : name, exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe(path);
    await expect(rail.getByRole('button', { name: name === 'Inbox' ? /^Inbox(?:,|$)/ : name, exact: true })).toHaveAttribute('aria-pressed', 'true');
  }
  await rail.getByRole('button', { name: 'Overview', exact: true }).focus();
  await page.keyboard.press('ArrowDown');
  await expect(rail.getByRole('button', { name: /^Inbox(?:,|$)/ })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(rail.getByRole('button', { name: /^Inbox(?:,|$)/ })).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('F6');
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeFocused();
  await page.keyboard.press('F6');
  await expect(page.getByRole('main')).toBeFocused();
});

test('Continue opens the same terminal without resuming or taking another writer; rail preserves its instance', async ({ page, context }) => {
  await withOpenNotices(page);
  await feature(page, true); await preference(page, true);
  const { info } = await import('./helpers');
  const h = await headers(page);
  const p = await page.request.post('/api/projects', { headers: h, data: { slug: `shell-${Date.now()}`, name: 'Cedar archive', workspace_path: info().workspace } });
  expect(p.ok()).toBeTruthy(); const project = await p.json();
  expect((await page.request.post(`/api/projects/${project.id}/trust`, { headers: h })).ok()).toBeTruthy();
  const s = await page.request.post('/api/sessions', { headers: h, data: { project_id: project.id, provider: 'claude', name: 'Restore preview' } });
  expect(s.ok()).toBeTruthy(); const session = await s.json();
  const writer = await context.newPage();
  try {
    await writer.goto(`/sessions/${session.id}`);
    await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    await page.goto(`/projects/${project.id}`);
    const mutations: string[] = []; const sockets: string[] = [];
    page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/api/sessions/')) mutations.push(r.url()); });
    page.on('websocket', ws => sockets.push(ws.url()));
    await page.getByRole('link', { name: 'Continue Restore preview', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}$`));
    await expect(page.getByText('Read-only terminal', { exact: true })).toBeVisible();
    await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    await shot(page, '04-continue-read-only');
    expect(mutations).toEqual([]); expect(sockets.every(url => !url.includes('take=1'))).toBe(true);
    const terminal = await page.locator('.xterm').elementHandle();
    const count = sockets.length;
    // Grid otherwise defaults to up to four sessions left by preceding cases.
    // Exercise its real saved choice, while keeping exact connection counts.
    await page.evaluate(id => localStorage.setItem('acc-grid', JSON.stringify([id])), session.id);
    let gridAttachments = 0;
    for (const [name, path] of [['Overview', '/'], ['Inbox', '/inbox'], ['Sessions', '/sessions'], ['Projects', '/projects'], ['Workspaces', '/workspaces'], ['Hosts', '/hosts'], ['Grid', '/grid'], ['Tools', '/catalog'], ['Settings', '/settings']]) {
      await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: name === 'Inbox' ? /^Inbox(?:,|$)/ : name, exact: true }).click();
      await expect.poll(() => new URL(page.url()).pathname).toBe(path);
      if (name === 'Projects') await expect(page.getByRole('complementary').getByRole('textbox', { name: 'Search projects' })).toBeVisible();
      if (name === 'Grid') {
        // Direct Grid navigation intentionally mounts its own one live-session tile.
        gridAttachments = 1;
        await expect.poll(() => sockets.length).toBe(count + gridAttachments);
      }
      expect(await terminal!.evaluate(node => node.isConnected && node === document.querySelector('[data-terminal-host-layer] .xterm'))).toBe(true);
      await expect(writer.getByText('You control this terminal', { exact: true })).toBeVisible();
    }
    expect(sockets.length).toBe(count + gridAttachments);
    await saveRetiredFeatureChoice(page);
    await expect(page.getByText(/An owner turned the new layout off/)).toHaveCount(0);
    expect(await terminal!.evaluate(node => node.isConnected)).toBe(true);
    expect(sockets.length).toBe(count + gridAttachments);
  } finally {
    await writer.close();
    expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers: h })).ok()).toBeTruthy();
  }
});

test('offline Continue stays a link; older settings do not disable Resume and refusals stay verbatim', async ({ page }) => {
  await feature(page, true); await preference(page, true);
  const { info } = await import('./helpers'); const h = await headers(page);
  const p = await page.request.post('/api/projects', { headers: h, data: { slug: `facts-${Date.now()}`, name: 'Field notes', workspace_path: info().workspace } });
  const project = await p.json(); await page.request.post(`/api/projects/${project.id}/trust`, { headers: h });
  const s = await page.request.post('/api/sessions', { headers: h, data: { project_id: project.id, provider: 'claude', name: 'Review notes' } }); const session = await s.json();
  await page.request.post(`/api/sessions/${session.id}/stop`, { headers: h });
  await page.route('**/api/security/effective*', async route => {
    const response = await route.fetch(); await route.fulfill({ json: { ...await response.json(), older_sessions: [session.id] } });
  });
  const refusal = 'Start a new session with the reviewed settings. This exact refusal must remain visible.';
  await page.route(`**/api/sessions/${session.id}/resume`, route => route.fulfill({ status: 409, json: { detail: refusal } }));
  await page.goto(`/projects/${project.id}`);
  const block = page.getByRole('region', { name: 'Sessions' });
  await expect(block).toBeVisible();
  await expect(block.getByText('older settings', { exact: true })).toBeVisible();
  await block.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect(block.getByText(refusal, { exact: true })).toBeVisible();
  await shot(page, '03-resume-refusal');
  await page.route('**/api/sessions', async route => {
    const response = await route.fetch(); const rows = await response.json();
    await route.fulfill({ json: rows.map((row: { id: string }) => row.id === session.id ? { ...row, machine_offline: true } : row) });
  });
  await page.reload();
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toHaveAttribute('title', /Machine offline/);
  await expect(block.getByRole('link', { name: 'Continue Review notes', exact: true })).toHaveAttribute('href', `/sessions/${session.id}`);
  await shot(page, '03-offline-continue');
  await page.route(`**/api/sessions/${session.id}`, async route => {
    const response = await route.fetch(); await route.fulfill({ json: { ...await response.json(), machine_offline: true } });
  });
  await block.getByRole('link', { name: 'Continue Review notes', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}$`));
  await expect(page.getByRole('region', { name: 'Offline host', exact: true })).toContainText('Resume is unavailable until the machine checks in.');
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
});

test('S1: offline machine disables only Resume; old and new shell retain other action conditions', async ({ page, browser }) => {
  test.slow(); await feature(page, true);
  const { gitProject } = await import('./helpers'); const h = await headers(page);
  const project = await gitProject(page, 'cedar-offline-actions');
  const response = await page.request.post('/api/sessions', { headers: h, data: { project_id: project.id, provider: 'claude', name: 'Offline controls' } });
  expect(response.ok()).toBeTruthy(); const session = await response.json();
  expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers: h })).ok()).toBeTruthy();
  const detail = await (await page.request.get(`/api/sessions/${session.id}`)).json();
  let state = 'active';
  const offlineSession = () => ({ ...detail, machine_offline: true, observed_state: state, desired_state: state === 'active' ? 'running' : 'stopped' });
  await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({ json: offlineSession() }));
  const phoneContext = await browser.newContext({ storageState: await page.context().storageState(), hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });
  const phone = await phoneContext.newPage();
  await phone.route(`**/api/sessions/${session.id}`, route => route.fulfill({ json: offlineSession() }));
  try {
    for (const shell of [false, true]) {
      await preference(page, shell); state = 'active';
      await page.goto(`/sessions/${session.id}`);
      const controls = page.locator('.session-heading');
      for (const name of ['Stop', 'Interrupt']) {
        await expect(controls.getByRole('button', { name, exact: true })).toBeEnabled();
        await expect(controls.getByRole('button', { name, exact: true })).not.toHaveAttribute('title', /Machine offline/);
      }
      await controls.getByRole('button', { name: 'Stop', exact: true }).click();
      const stop = page.getByRole('dialog', { name: 'Stop Offline controls?', exact: true });
      await expect(stop).toBeVisible(); await stop.getByRole('button', { name: 'Cancel', exact: true }).click();
      // Touch actions use the same old enable conditions in their overflow menu.
      await phone.goto(`/sessions/${session.id}`);
      await phone.locator('.session-heading').getByRole('button', { name: 'Actions for Offline controls', exact: true }).click();
      await expect(phone.getByRole('menuitem', { name: 'Interrupt the current turn', exact: true })).toBeEnabled();
      await expect(phone.getByRole('menuitem', { name: 'Stop session…', exact: true })).toBeEnabled();
      await phone.keyboard.press('Escape');
      state = 'stopped'; await page.reload();
      const resume = controls.getByRole('button', { name: 'Resume', exact: true });
      if (shell) {
        await expect(resume).toBeDisabled(); await expect(resume).toHaveAttribute('title', /Machine offline/);
      } else await expect(resume).toBeEnabled();
      if (shell) await controls.getByRole('button', { name: 'Session details', exact: true }).click();
      await expect(controls.getByRole('button', { name: 'Archive', exact: true })).toBeEnabled();
      await controls.getByRole('button', { name: 'Archive', exact: true }).click();
      const archive = page.getByRole('dialog', { name: 'Archive Offline controls?', exact: true });
      await expect(archive).toBeVisible(); await archive.getByRole('button', { name: 'Cancel', exact: true }).click();
      // Rynhult itself being unreachable still disables these actions in either layout.
      await page.route('**/api/sessions', route => route.fulfill({ status: 503, json: { detail: 'Fixture connection unavailable' } }));
      await page.reload();
      if (shell) await controls.getByRole('button', { name: 'Session details', exact: true }).click();
      await expect(controls.getByRole('button', { name: 'Archive', exact: true })).toBeDisabled({ timeout: 15000 });
      await expect(controls.getByRole('button', { name: 'Archive', exact: true })).toHaveAttribute('title', /Rynhult isn't answering/);
      await expect(resume).toBeDisabled();
      state = 'active'; await page.reload();
      for (const name of ['Stop', 'Interrupt']) {
        await expect(controls.getByRole('button', { name, exact: true })).toBeDisabled({ timeout: 15000 });
        await expect(controls.getByRole('button', { name, exact: true })).toHaveAttribute('title', /Rynhult isn't answering/);
      }
      await page.unroute('**/api/sessions');
    }
  } finally { await phoneContext.close(); }
});

async function shot(page: import('@playwright/test').Page, name: string, guide = false) {
  if (!process.env.ACC_UPDATE_SHELL_SCREENSHOTS) return;
  await page.screenshot({ path: `../docs/${guide ? 'wiki/.attachments' : 'handoffs/shell-p1a'}/${name}.png`, animations: 'disabled', fullPage: true });
}

test('dirty Files survive rail modes, retired feature writes and navigation; close retains the discard guard', async ({ page }) => {
  await withOpenNotices(page);
  await feature(page, true); await preference(page, true);
  const { gitProject, fileRow } = await import('./helpers');
  const { id, slug } = await gitProject(page, 'shell-draft');
  await page.goto(`/projects/${id}?tab=files`);
  await page.getByRole('button', { name: fileRow('app.py') }).click();
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill("print('keep my unsaved draft')");
  const original = await editor.elementHandle();
  const returnToDraft = async () => {
    // Global section pages hide project columns. Return through that project's
    // real navigation before selecting its retained file tab (never reload).
    await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
    await page.getByRole('textbox', { name: 'Search projects' }).fill(slug);
    await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${slug}`) }).click();
    await page.locator('[data-view-tab]').filter({ hasText: 'app.py' }).click();
  };
  for (const name of ['Overview', 'Inbox', 'Sessions', 'Projects', 'Workspaces', 'Hosts', 'Grid', 'Tools', 'Settings']) {
    await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: name === 'Inbox' ? /^Inbox(?:,|$)/ : name, exact: true }).click();
    await returnToDraft();
    await expect(editor).toContainText('keep my unsaved draft');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(await original!.evaluate(node => node.isConnected)).toBe(true);
  }
  await preference(page, false);
  await saveRetiredFeatureChoice(page);
  await expect(page.getByText(/An owner turned the new layout off/)).toHaveCount(0);
  await expect(editor).toContainText('keep my unsaved draft');
  await shot(page, '08-disabled-dirty-files');
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search projects' }).fill(slug);
  await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${slug}`) }).click();
  await expect(page.getByRole('dialog', { name: 'Discard unsaved edits?' })).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/projects$/);
  await returnToDraft();
  await expect(editor).toContainText('keep my unsaved draft');
  await page.getByRole('button', { name: 'Close view', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Discard unsaved edits?' });
  await expect(dialog).toBeVisible(); await shot(page, '08-explicit-link-discard');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(editor).toContainText('keep my unsaved draft');
  await expect(page).toHaveURL(new RegExp(`/projects/${id}\\?tab=files&file=app.py$`));
  await shot(page, '08-draft-after-cancel');
});

test('60 extra projects: sidebar pages, search and phone scrolling stay usable in a shared fixture', async ({ page, browser }) => {
  test.slow(); await feature(page, true); await preference(page, true);
  const { info, PASSWORD, totp, expectAxeClean } = await import('./helpers'); const h = await headers(page);
  const prefix = `aaa-volume-${Date.now()}`;
  const ownedProjects: number[] = [];
  try {
    for (let i = 0; i < 60; i++) {
      const result = await page.request.post('/api/projects', { headers: h, data: { slug: `${prefix}-${String(i).padStart(2, '0')}`, name: `Cedar volume ${i}`, workspace_path: info().workspace } });
      expect(result.ok(), await result.text()).toBeTruthy();
      ownedProjects.push((await result.json()).id);
    }
    // The full stateful suite's terminal-access test already assigns this account.
    // Reproduce that condition in focused runs too; later shell tests must not reuse it.
    const users = await (await page.request.get('/api/users')).json();
    if (!users.users.some((user: { account: string }) => user.account === 'finn-agent')) {
      const invite = await page.request.post('/api/users/invites', { headers: h, data: { role: 'member', account: 'finn-agent' } });
      expect(invite.ok(), await invite.text()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop();
      const other = await browser.newContext();
      try {
        const begin = await other.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
        const joined = await other.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `finn-volume-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
        expect(joined.ok(), await joined.text()).toBeTruthy();
      } finally { await other.close(); }
    }
    let projectReads = 0;
    await page.route('**/api/projects', async route => {
      const response = await route.fetch(); const rows = await response.json(); projectReads++;
      // Presentation must keep slug/id order even if a refreshed transport returns another order.
      await route.fulfill({ json: projectReads % 2 ? rows.reverse() : rows });
    });
    await page.clock.install();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 }); await page.goto('/projects');
      await page.getByRole('navigation', { name: width === 390 ? 'Phone rail' : 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      const available = page.getByRole('region', { name: 'Available projects' });
      await expect(available.locator('.shell-project')).toHaveCount(20);
      const firstPage = await available.locator('.shell-project > a').allTextContents();
      const first = await available.locator('.shell-project').first().elementHandle();
      const reads = projectReads;
      await page.clock.fastForward(11000); await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')));
      await expect.poll(() => projectReads).toBeGreaterThan(reads);
      expect(await available.locator('.shell-project > a').allTextContents()).toEqual(firstPage);
      expect(await first!.evaluate(node => node.isConnected && node === document.querySelector('[aria-label="Available projects"] .shell-project'))).toBe(true);
      // Scroll to a real row below the fold before navigating, then return to the list.
      await available.locator('.shell-project > a').last().scrollIntoViewIfNeeded();
      if (width === 390) {
        const scroller = page.getByRole('dialog', { name: 'Browse', exact: true }).locator('.shell-sidebar-content');
        expect(await scroller.evaluate(node => node.clientHeight < 900 && node.scrollHeight > node.clientHeight && node.scrollTop > 0)).toBe(true);
      }
      const selectedName = await available.locator('.shell-project > a').last().locator('span').first().innerText();
      await available.locator('.shell-project > a').last().click();
      await expect(page.getByRole('main').getByRole('heading', { name: selectedName, exact: true, level: 1 })).toBeVisible();
      await expect(page).toHaveURL(/\/projects\/\d+$/);
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      await page.getByRole('link', { name: 'Back to all projects', exact: true }).click();
      await expect(page.getByRole('main').getByRole('heading', { name: 'All projects', exact: true, level: 1 })).toBeVisible();
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      await available.getByRole('button', { name: 'Next projects', exact: true }).click();
      await expect(available).toContainText('Projects 21–40 of');
      await expect(available.locator('.shell-project')).toHaveCount(20);
      const search = page.getByRole('textbox', { name: 'Search projects', exact: true });
      await search.fill('Cedar volume 59');
      await expect(available.locator('.shell-project')).toHaveCount(1);
      await available.getByRole('link', { name: /^Cedar volume 59/ }).click();
      await expect(page.getByRole('main').getByRole('heading', { name: 'Cedar volume 59', exact: true, level: 1 })).toBeVisible();
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      await expect(page.getByRole('region', { name: 'Cedar volume 59 context' })).toBeVisible();
      await expect(page).toHaveURL(/\/projects\/\d+$/);
      await page.getByRole('link', { name: 'Back to all projects', exact: true }).click();
      await expect(page.getByRole('main').getByRole('heading', { name: 'All projects', exact: true, level: 1 })).toBeVisible();
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      await search.fill('no-such-project-volume'); await expect(available).toContainText('No projects match.');
      await search.fill(''); await expect(available).toContainText('Projects 1–20 of');
      await expectAxeClean(page);
    }
  } finally {
    await page.unrouteAll({ behavior: 'wait' });
    for (const id of ownedProjects) {
      const archived = await page.request.post(`/api/projects/${id}/archive`, { headers: h });
      expect(archived.ok(), await archived.text()).toBeTruthy();
    }
  }
  const remaining = await (await page.request.get('/api/projects')).json();
  expect(remaining.filter((project: { id: number; archived: boolean }) => ownedProjects.includes(project.id) && !project.archived)).toEqual([]);
});

test('owner desktop and phone, light and dark: project context, tools, keyboard, palette and accessibility', async ({ page }) => {
  test.slow();
  await feature(page, true); await preference(page, true);
  const { gitProject, expectAxeClean } = await import('./helpers');
  const { id, slug } = await gitProject(page, 'cedar');
  for (const theme of ['light', 'dark'] as const) for (const width of [1440, 390]) {
    await page.emulateMedia({ colorScheme: theme }); await page.setViewportSize({ width, height: 900 });
    await page.goto(`/projects/${id}`);
    await expect(page.getByRole('region', { name: 'Sessions' })).toContainText('No sessions yet.');
    await expect(page.getByRole('button', { name: `New session · ${slug}`, exact: true }).first()).toBeVisible();
    await page.getByRole('navigation', { name: width === 390 ? 'Phone rail' : 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
    await expect(page).toHaveURL(/\/projects$/);
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('textbox', { name: 'Search projects' }).fill(slug);
    await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${slug}`) }).click();
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    const modes = page.getByRole('navigation', { name: width === 390 ? 'Rail modes' : 'Rail', exact: true });
    await expect(page.getByRole('region', { name: `${slug} context` }).getByRole('link', { name: 'Files', exact: true })).toHaveAttribute('href', `/projects/${id}?tab=files`);
    await shot(page, `new-layout-${width}-${theme}`, true);
    await expectAxeClean(page);
    for (const mode of ['Overview', 'Tools']) {
      if (width === 390 && !await page.getByRole('dialog', { name: 'Browse', exact: true }).isVisible()) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      await modes.getByRole('button', { name: mode, exact: true }).click();
      await shot(page, `02-${mode.toLowerCase()}-${width}-${theme}`);
    }
    await expect(page).toHaveURL(/\/catalog$/);
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    const sidebar = width === 390 ? page.getByRole('dialog', { name: 'Browse', exact: true }) : page.getByRole('complementary', { name: 'Context sidebar' });
    await expect(sidebar.locator('a[href="/catalog"]')).toHaveCount(0);
    for (const path of ['/providers', '/board', '/audit', '/search', '/guide']) await expect(sidebar.locator(`a[href="${path}"]`)).toBeVisible();
    if (width === 390) await page.keyboard.press('Escape');
    else {
      const range = page.getByRole('separator', { name: 'Sidebar width' });
      await range.focus(); const before = await range.getAttribute('aria-valuenow'); await page.keyboard.press('ArrowRight'); expect(await range.getAttribute('aria-valuenow')).not.toBe(before);
    }
    await page.keyboard.press('Control+k');
    await expect(page.getByRole('dialog', { name: 'Jump to…', exact: true })).toBeVisible(); await page.keyboard.press('Escape');
    await page.getByRole('main').focus(); await page.keyboard.press('c');
    await expect(page.getByRole('region', { name: 'New session', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await shot(page, `03-empty-${width}-${theme}`);
  }
  await page.goto('/settings#layout');
  await expect(page.getByLabel('Use the classic layout')).toHaveCount(0);
  await shot(page, '06-preference-saved');
  await page.setViewportSize({ width: 1440, height: 900 });
  await preference(page, false); await page.goto(`/projects/${id}`);
  await expect(page.getByRole('region', { name: 'Sessions' })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toBeVisible();
});

test('same browser different person: no prior pins, recents or owner-only header requests', async ({ page, context }) => {
  test.slow();
  await feature(page, true); await preference(page, true);
  const { gitProject, info, PASSWORD, totp, expectAxeClean } = await import('./helpers');
  const { slug } = await gitProject(page, 'private-cedar');
  await page.goto('/projects');
  await page.getByRole('textbox', { name: 'Search projects', exact: true }).fill(slug);
  await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${slug}`) }).click();
  await page.getByRole('region', { name: `${slug} context` }).getByRole('button', { name: `Pin ${slug}`, exact: true }).click();
  await page.getByRole('link', { name: 'Back to all projects', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search projects', exact: true }).fill('');
  await expect(page.getByRole('region', { name: 'Pinned projects' })).toContainText(slug);
  const ownerId = (await (await page.request.get('/api/preferences/ui-shell')).json()).person_id;
  const account = `shell-person-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invite = await page.request.post('/api/users/invites', { headers: await headers(page), data: { role: 'member', account } });
  expect(invite.ok(), await invite.text()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop();
  await context.clearCookies();
  const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
  const joined = await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
  expect(joined.ok(), await joined.text()).toBeTruthy();
  await preference(page, true);
  const memberId = (await (await page.request.get('/api/preferences/ui-shell')).json()).person_id;
  expect(memberId).not.toBe(ownerId);
  const requests: string[] = [];
  page.on('request', r => requests.push(new URL(r.url()).pathname));
  for (const theme of ['light', 'dark'] as const) for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme: theme });
    await page.goto('/projects');
    await page.getByRole('navigation', { name: width === 390 ? 'Phone rail' : 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Pinned projects' })).toHaveCount(0);
    await expect(page.getByRole('region', { name: 'Recent projects' })).toHaveCount(0);
    await expect(page.getByText(slug, { exact: true })).toHaveCount(0);
    await shot(page, `05-member-${width}-${theme}`);
    await expectAxeClean(page);
    const rail = page.getByRole('navigation', { name: width === 390 ? 'Rail modes' : 'Rail', exact: true });
    await rail.getByRole('button', { name: 'Hosts', exact: true }).click();
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    await rail.getByRole('button', { name: 'Tools', exact: true }).click();
    if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
    await expect(page.locator('a[href="/board"], a[href="/audit"]')).toHaveCount(0);
    if (width === 390) await page.keyboard.press('Escape');
  }
  // The member's own older-settings fact comes from the authorized session
  // detail route, never the owner's cross-person security inventory.
  const h = await headers(page);
  const made = await page.request.post('/api/projects', { headers: h, data: { slug: `member-${Date.now()}`, name: 'Blair notes', workspace_path: info().workspace } });
  expect(made.ok(), await made.text()).toBeTruthy(); const ownProject = await made.json();
  expect((await page.request.post(`/api/projects/${ownProject.id}/trust`, { headers: h })).ok()).toBeTruthy();
  const launched = await page.request.post('/api/sessions', { headers: h, data: { project_id: ownProject.id, provider: 'claude', name: 'Blair review' } });
  expect(launched.ok(), await launched.text()).toBeTruthy(); const ownSession = await launched.json();
  await page.request.post(`/api/sessions/${ownSession.id}/stop`, { headers: h });
  await page.route(`**/api/sessions/${ownSession.id}`, async route => {
    const r = await route.fetch(); await route.fulfill({ json: { ...await r.json(), security_older_settings: true } });
  });
  await page.setViewportSize({ width: 1440, height: 900 }); await page.goto(`/projects/${ownProject.id}`);
  await expect(page.getByRole('main').getByRole('heading', { name: ownProject.name, exact: true, level: 1 })).toBeVisible({ timeout: 15000 });
  const continuing = page.getByRole('region', { name: 'Sessions' });
  await expect(continuing.getByText('older settings', { exact: true })).toBeVisible();
  await expect(continuing.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  const detail = await (await page.request.get(`/api/sessions/${ownSession.id}`)).json();
  const detailReads: string[] = [];
  await page.route('**/api/sessions', route => route.fulfill({ json: Array.from({ length: 25 }, (_, i) => ({ ...detail, id: `member-row-${i}`, name: `Review ${i}`, status_stale_since: null })) }));
  await page.route('**/api/sessions/member-row-*', route => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!; detailReads.push(id);
    return route.fulfill({ json: { ...detail, id, security_older_settings: true } });
  });
  await page.reload();
  await expect(continuing.getByText('Sessions 1–20 of 25', { exact: true })).toBeVisible();
  await expect(continuing.getByRole('link', { name: /^Continue / })).toHaveCount(20);
  await expect.poll(() => new Set(detailReads).size).toBe(20);
  await continuing.getByRole('button', { name: 'Next sessions', exact: true }).click();
  await expect(continuing.getByRole('link', { name: /^Continue / })).toHaveCount(5);
  await expect.poll(() => new Set(detailReads).size).toBe(25);
  expect(requests.filter(p => ['/api/board', '/api/providers', '/api/accounts', '/api/nodes', '/api/security/effective'].includes(p))).toEqual([]);
  const stored = await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('rynhult:shell:')));
  expect(stored.some(k => k.includes(ownerId) && k.endsWith(':pins'))).toBe(true);
  expect(stored.some(k => k.includes(memberId) && k.endsWith(':pins'))).toBe(false);
});

test('Continue states and header source facts remain distinct; capture shell payload requests', async ({ page, context }) => {
  test.slow();
  await feature(page, true);
  const { info } = await import('./helpers'); const h = await headers(page);
  const response = await page.request.post('/api/projects', { headers: h, data: { slug: `states-${Date.now()}`, name: 'Cedar archive', workspace_path: info().workspace } });
  const project = await response.json(); await page.request.post(`/api/projects/${project.id}/trust`, { headers: h });
  const created = await page.request.post('/api/sessions', { headers: h, data: { project_id: project.id, provider: 'claude', name: 'Cedar review' } });
  const session = await created.json(); await page.request.post(`/api/sessions/${session.id}/stop`, { headers: h });
  // Fixed-duration cold-navigation observation, no request assertions tied to polling timing.
  const measurements: Record<string, unknown> = {};
  for (const enabled of [false, true]) {
    await preference(page, enabled);
    const fresh = await context.newPage(); const requests: string[] = [];
    fresh.on('request', r => requests.push(new URL(r.url()).pathname));
    await fresh.goto(`/projects/${project.id}`); await fresh.getByRole('heading', { name: 'Cedar archive', exact: true, level: 1 }).waitFor();
    await fresh.waitForTimeout(2000); // the documented measurement window
    measurements[enabled ? 'on' : 'off'] = { total: requests.length, api: requests.filter(p => p.startsWith('/api/')).length, uniqueAPI: [...new Set(requests.filter(p => p.startsWith('/api/')))].sort(), assets: await fresh.evaluate(() => performance.getEntriesByType('resource').filter(e => /\.(js|css)(\?|$)/.test(e.name)).map(e => ({ path: new URL(e.name).pathname, bytes: (e as PerformanceResourceTiming).encodedBodySize }))) };
    await fresh.close();
  }
  if (process.env.ACC_UPDATE_SHELL_SCREENSHOTS) {
    const { writeFileSync } = await import('node:fs'); writeFileSync('../docs/handoffs/shell-p1a/initial-requests.json', JSON.stringify(measurements, null, 2) + '\n');
  }
  const real = (await (await page.request.get('/api/sessions')).json()).find((s: { id: string }) => s.id === session.id);
  const states = [
    { id: 'needs', name: 'Review the proposed change', observed_state: 'waiting_input' },
    { id: 'work', name: 'Build the archive', observed_state: 'active' },
    { id: 'idle', name: 'Ready for another task', observed_state: 'idle' },
    { id: 'stop', name: 'Stopped review', observed_state: 'stopped', desired_state: 'stopped' },
    { id: 'older', name: 'Earlier launch settings', observed_state: 'stopped', desired_state: 'stopped' },
    { id: 'offline', name: 'Remote archive', observed_state: 'stopped', desired_state: 'stopped', machine_offline: true, host: 'cedar.example.lan' },
    { id: 'recovery', name: 'Recovery review', observed_state: 'recovery_needed' },
    { id: 'pending', name: 'Unconfirmed stop', observed_state: 'stopped', desired_state: 'stopped', last_error: 'stop not confirmed: machine did not acknowledge' },
  ];
  await page.route('**/api/sessions', route => route.fulfill({ json: states.map(s => ({ ...real, ...s, project_id: project.id, status_stale_since: null, status_at: new Date().toISOString(), worktree_branch: 'review/archive', workdir: '/work/cedar', team_id: null, parent_session_id: null })) }));
  // These presentation-only IDs are not stored sessions. Supply their read-only
  // eligibility too; the earlier cold-navigation measurement uses the real API.
  await page.route('**/api/sessions/*/resume-check', route => {
    const id = new URL(route.request().url()).pathname.split('/')[3];
    return states.some(state => state.id === id)
      ? route.fulfill({ json: { eligible: true, reason: null } }) : route.continue();
  });
  let unrestricted = false;
  await page.route('**/api/security/effective*', async route => {
    const r = await route.fetch(); const policy = await r.json();
    await route.fulfill({ json: { ...policy, mode: unrestricted ? 'unrestricted' : policy.mode, switches: unrestricted ? Object.fromEntries(Object.keys(policy.switches).map(k => [k, false])) : policy.switches, older_sessions: unrestricted ? [] : ['older'] } });
  });
  await page.route('**/api/providers', async route => {
    const r = await route.fetch(); await route.fulfill({ json: { ...await r.json(), observations: {
      'claude.rate_limits': { source: 'provider account observation', observed_at: Date.now() / 1000 - 120, value: { five_hour: { used_percentage: 23 }, seven_day: { used_percentage: 41 } } },
      'codex.rate_limits': { source: 'provider account observation', observed_at: Date.now() / 1000 - 240, value: { primary: { used_percent: 18, window_minutes: 300 } } },
    } } });
  });
  await page.goto(`/projects/${project.id}`);
  const block = page.getByRole('region', { name: 'Sessions' });
  for (const name of ['Needs you', 'Working', 'Idle', 'Stopped', 'Recovery needed', 'Machine offline', 'older settings']) await expect(block.getByText(name, { exact: true }).first()).toBeVisible();
  await expect(block.getByRole('link', { name: 'Continue Remote archive', exact: true })).toBeVisible();
  await expect(block.getByRole('row').filter({ hasText: 'Remote archive' }).getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
  await block.getByRole('row').filter({ hasText: 'Earlier launch settings' }).scrollIntoViewIfNeeded();
  await expect(block.getByRole('row').filter({ hasText: 'Earlier launch settings' }).getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: /Plan usage details$/ }).click();
  await expect(page.getByRole('link', { name: /Claude · 5-hour · 23%/ })).toBeVisible();
  await expect(page.getByRole('link', { name: /Codex · 5-hour · 18%/ })).toBeVisible();
  await page.getByRole('main').evaluate(node => { node.scrollTop = 0; });
  await shot(page, '03-session-states-dark');
  for (const state of states) {
    await block.getByRole('row').filter({ hasText: state.name }).scrollIntoViewIfNeeded();
    await shot(page, `03-row-${state.id}`);
  }
  await shot(page, '05-older-security-header');
  await page.emulateMedia({ colorScheme: 'light' }); await shot(page, '03-session-states-light');
  unrestricted = true; await page.reload();
  await expect(page.locator('#shell-header').getByRole('link', { name: /^Security:.*Unrestricted/ })).toBeVisible();
  await shot(page, '05-unrestricted-header');
  await page.setViewportSize({ width: 390, height: 900 }); await shot(page, '03-session-states-phone');
});

test('retained New session draft is untouched by rollout changes; layout latch survives Status board', async ({ page }) => {
  await feature(page, true); await preference(page, true);
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('region', { name: 'New session', exact: true });
  await dialog.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Keep this unsent planning task.');
  await preference(page, false); await saveRetiredFeatureChoice(page);
  await expect(page.getByText(/An owner turned the new layout off/)).toHaveCount(0);
  await expect(dialog.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('Keep this unsent planning task.');
  await page.keyboard.press('Escape');
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Tools', exact: true }).click();
  // Direct rail navigation must commit before reopening the retained launcher.
  await expect(page).toHaveURL(/\/catalog$/);
  await expect(page.getByRole('main').getByRole('heading', { name: 'Catalog', exact: true, level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  await expect(dialog.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('Keep this unsent planning task.');
  await page.keyboard.press('Escape');
  await page.getByRole('complementary', { name: 'Context sidebar' }).getByRole('link', { name: 'Status board', exact: true }).click();
  await expect(page).toHaveURL(/\/board$/);
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: 'Back to Rynhult', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
});
