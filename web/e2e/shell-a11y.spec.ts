import { test } from './shell-state';
import { expect, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE });
async function enable(page: Page) {
  const put = async () => {
    const state = await (await page.request.get('/api/admin-features')).json();
    return page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: state.revision } });
  };
  let response = await put();
  if (response.status() === 403) { await verifyAgain(page); response = await put(); }
  expect(response.ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
}
async function keyboardOpen(page: Page, name: string) {
  const target = page.getByRole('button', { name, exact: true });
  await expect(target).toBeEnabled();
  await target.focus(); await expect(target).toBeFocused();
  await page.keyboard.press('Enter');
}

test('rail navigation leaves modified shortcuts and IME alone', async ({ page }) => {
  // Gate neighbours can leave open notices; exercise the counted Inbox name.
  await page.route('**/api/attention*', route => route.fulfill({ json: [{
    id: 901, kind: 'info', severity: 'info', source: 'system', verified: true,
    title: 'Rail keyboard fixture notice', detail: null, persistent: null,
    session_id: null, session_name: null, project: null,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), acked_at: null, resolved_at: null,
  }] }));
  await enable(page); await page.goto('/projects');
  const inbox = page.locator('#shell-rail').getByRole('button', { name: 'Inbox, 1 open', exact: true });
  await expect(inbox).toBeVisible();
  const first = page.locator('#shell-rail').getByRole('button', { name: 'Overview', exact: true });
  await first.focus(); await page.keyboard.press('Control+ArrowDown'); await expect(first).toBeFocused();
  await first.dispatchEvent('keydown', { key: 'ArrowDown', isComposing: true }); await expect(first).toBeFocused();
  await page.keyboard.press('ArrowDown'); await expect(inbox).toBeFocused();
});

test('short reflow viewport keeps content scrollable and all shell controls reachable', async ({ page }) => {
  await enable(page);
  // This geometry case owns a quiet Inbox; earlier approval/backup fixtures are unrelated.
  await page.route('**/api/attention', route => route.fulfill({ json: [] }));
  // Hold the policy loading state while checking the compact required shield
  // and reachable controls across short viewports.
  let releaseSecurity!: () => void;
  const securityPending = new Promise<void>(resolve => { releaseSecurity = resolve; });
  await page.route('**/api/security/effective*', async route => { await securityPending; await route.abort(); });
  try {
    for (const theme of ['light', 'dark'] as const) for (const viewport of [{ width: 720, height: 450 }, { width: 360, height: 225 }, { width: 320, height: 256 }]) {
      await page.emulateMedia({ colorScheme: theme });
      await page.setViewportSize(viewport); await page.goto('/projects');
      await expect(page.locator('#shell-phone-rail')).toBeVisible();
      await expect(page.locator('#shell-header').getByRole('link', { name: /^Security: Security state loading/ })).toBeVisible();
      await page.evaluate(async () => { await document.fonts.ready; });
      await expect.poll(async () => (await page.getByRole('main').boundingBox())?.height ?? 0,
        { message: `content height at ${viewport.width}×${viewport.height} with Security loading` }).toBeGreaterThanOrEqual(60);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.locator('#shell-header').focus(); await page.keyboard.press('F6');
      await expect(page.locator('#shell-phone-rail')).toBeFocused();
      await page.keyboard.press('F6'); await expect(page.getByRole('main')).toBeFocused();
      await keyboardOpen(page, 'Header tools');
      await expect(page.getByRole('group', { name: 'Header tools', exact: true })).toBeVisible();
      await keyboardOpen(page, 'Customize header');
      const dialog = page.getByRole('dialog', { name: 'Customize header', exact: true });
      await expect(dialog.getByRole('button', { name: 'Save header', exact: true })).toBeInViewport();
      await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: 'Header tools', exact: true })).toBeFocused();
    }
  } finally { releaseSecurity(); await page.unrouteAll({ behavior: 'wait' }); }
});

for (const width of [1440, 390]) for (const theme of ['light', 'dark'] as const) {
  test(`shell keyboard and axe at ${width} ${theme}`, async ({ page }) => {
    test.slow(); await enable(page); await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    const project = await gitProject(page, 'a11y-cedar');
    const created = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Cedar accessibility' } });
    expect(created.ok()).toBeTruthy();
    await page.goto(`/projects/${project.id}`);
    await expect(page.getByRole('main').getByRole('heading', { name: project.slug, exact: true, level: 1 })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('region', { name: 'Sessions' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Continue Cedar accessibility', exact: true })).toBeVisible();
    await expect(page.getByRole('main')).toHaveCount(1);
    expect(await page.locator('.shell').evaluate(shell => [...shell.querySelectorAll('*')].every(node => {
      const style = getComputedStyle(node);
      return style.animationName === 'none' || style.animationDuration.split(',').every(duration => parseFloat(duration) <= .01);
    }))).toBe(true);
    await expectAxeClean(page);
    mkdirSync(`${process.env.ACC_E2E_BASE}/shots`, { recursive: true });
    await page.screenshot({ path: `${process.env.ACC_E2E_BASE}/shots/${width}-${theme}.png` });
    await page.keyboard.press('Tab'); await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
    await page.keyboard.press('Enter'); await expect(page.getByRole('main')).toBeFocused();
    expect(await page.getByRole('main').evaluate(node => parseFloat(getComputedStyle(node).outlineWidth))).toBeGreaterThanOrEqual(2);
    await page.locator('#shell-header').focus(); await page.keyboard.press('F6');
    await expect(page.locator(width === 1440 ? '#shell-rail' : '#shell-phone-rail')).toBeFocused();
    if (width === 1440) { await page.keyboard.press('F6'); await expect(page.locator('#shell-sidebar')).toBeFocused(); }
    await page.keyboard.press('F6'); await expect(page.getByRole('main')).toBeFocused();
    await page.keyboard.press('Shift+F6');
    await expect(page.locator(width === 1440 ? '#shell-sidebar' : '#shell-phone-rail')).toBeFocused();
    if (width === 390) await keyboardOpen(page, 'More');
    const rail = page.getByRole('navigation', { name: width === 1440 ? 'Rail' : 'Rail modes', exact: true });
    for (const [name, path] of [['Overview', '/'], ['Inbox', '/inbox'], ['Sessions', '/sessions'], ['Projects', '/projects'], ['Workspaces', '/workspaces'], ['Hosts', '/hosts'], ['Grid', '/grid'], ['Tools', '/catalog'], ['Settings', '/settings']]) {
      if (width === 390 && !await page.getByRole('dialog', { name: 'Browse', exact: true }).isVisible()) await keyboardOpen(page, 'More');
      const button = rail.getByRole('button', { name: name === 'Inbox' ? /^Inbox(?:,|$)/ : name, exact: true });
      await button.focus(); await page.keyboard.press('Enter');
      await expect.poll(() => new URL(page.url()).pathname).toBe(path);
      await expect(page.getByRole('main').getByRole('heading', { name: name === 'Projects' ? 'All projects' : name === 'Tools' ? 'Catalog' : name, exact: true, level: 1 })).toBeVisible();
      if (width === 390) await keyboardOpen(page, 'More');
      await expect(button).toHaveAttribute('aria-pressed', 'true');
    }
    await rail.getByRole('button', { name: 'Projects', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/\/projects$/);
    const sidebar = width === 1440 ? page.locator('#shell-sidebar') : page.getByRole('dialog', { name: 'Browse', exact: true });
    await expect(page.getByRole('main').getByRole('heading', { name: 'All projects', exact: true, level: 1 })).toBeVisible();
    if (width === 390) await keyboardOpen(page, 'More');
    await sidebar.getByRole('textbox', { name: 'Search projects' }).fill('no-such-a11y-project');
    await expect(sidebar.getByText('No projects match.', { exact: true })).toBeVisible();
    await sidebar.getByRole('textbox', { name: 'Search projects' }).fill(project.slug);
    await sidebar.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${project.slug}`) }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('main').getByRole('heading', { name: project.slug, exact: true, level: 1 })).toBeVisible();
    if (width === 390) await keyboardOpen(page, 'More');
    await expect(sidebar.getByRole('region', { name: `${project.slug} context` })).toBeVisible();
    await expectAxeClean(page);
    if (width === 390) { await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: 'More', exact: true })).toBeFocused(); }
    if (width === 390) await keyboardOpen(page, 'Header tools');
    await keyboardOpen(page, 'Customize header');
    const editor = page.getByRole('dialog', { name: 'Customize header', exact: true });
    await editor.getByRole('button', { name: 'Move Find project files up', exact: true }).focus();
    await page.keyboard.press('Enter'); await expect(editor.getByRole('listitem').nth(3)).toContainText('Find project files');
    await expectAxeClean(page);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button', { name: width === 1440 ? 'Customize header' : 'Header tools', exact: true })).toBeFocused();
    await page.keyboard.press('Control+Shift+f');
    const search = page.getByRole('dialog', { name: 'Find project files', exact: true });
    await expect(search).toBeVisible(); await expect(search.getByRole('combobox', { name: 'Search project' })).toBeFocused();
    await page.keyboard.press('Tab'); await expect(search.getByRole('searchbox', { name: 'Filename' })).toBeFocused();
    await search.getByRole('searchbox', { name: 'Filename' }).fill('app');
    await expect(search.getByRole('list', { name: 'Matching files' })).toBeVisible();
    await page.keyboard.press('ArrowDown'); await expect(search.getByRole('link', { name: 'app.py', exact: true })).toBeFocused();
    await expectAxeClean(page);
    const failedSearch = new RegExp(`/api/projects/${project.id}/files/search`);
    await page.route(failedSearch, route => route.fulfill({ status: 503, json: { state: 'unavailable', detail: 'Search is unavailable for this location.' } }));
    await search.getByRole('searchbox', { name: 'Filename' }).fill('unavailable');
    await expect(search.getByText(/^Search unavailable/)).toBeVisible();
    await expect(page.getByRole('status', { name: 'Announcements', exact: true })).toContainText('Search unavailable');
    await expectAxeClean(page); await page.unroute(failedSearch); await page.keyboard.press('Escape');
    await page.keyboard.press('Control+k'); await expect(page.getByRole('dialog', { name: 'Jump to…' })).toBeVisible();
    await page.keyboard.press('Escape');
    if (width === 390) await keyboardOpen(page, 'Header tools');
    const conversations = page.getByRole('link', { name: 'Search conversations', exact: true });
    await conversations.focus(); await page.keyboard.press('Enter'); await expect(page).toHaveURL(/\/search$/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}


test('focused rail control survives changing to phone layout', async ({ page }) => {
  await enable(page); await page.goto('/projects');
  await page.locator('#shell-rail').getByRole('button', { name: 'Tools', exact: true }).focus();
  await page.setViewportSize({ width: 390, height: 900 });
  await expect(page.getByRole('button', { name: 'More', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await page.getByRole('navigation', { name: 'Rail modes', exact: true }).getByRole('button', { name: 'Tools', exact: true }).focus();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole('dialog', { name: 'Browse', exact: true })).toBeHidden();
  await expect(page.locator('#shell-rail').getByRole('button', { name: 'Tools', exact: true })).toBeFocused();
});

test('project search and paging announce once through the shared announcer', async ({ page }) => {
  await enable(page); const project = await gitProject(page, 'a11y-page');
  await page.route('**/api/projects', async route => {
    const response = await route.fetch(); const rows = await response.json();
    const original = rows.find((row: { id: number }) => row.id === project.id);
    await route.fulfill({ json: [...rows, ...Array.from({ length: 25 }, (_, i) => ({ ...original, id: 9000 + i, name: `Zed project ${i}`, slug: `zed-${String(i).padStart(2, '0')}` }))] });
  });
  await page.goto('/projects');
  await page.locator('#shell-rail').getByRole('button', { name: 'Projects', exact: true }).click();
  const sidebar = page.locator('#shell-sidebar');
  await sidebar.getByRole('button', { name: 'Next projects' }).focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('status', { name: 'Announcements', exact: true })).toContainText('Projects 21–');
  await sidebar.getByRole('textbox', { name: 'Search projects' }).fill('nothing-matches');
  await expect(page.getByRole('status', { name: 'Announcements', exact: true })).toContainText('No projects match');
  await expect(sidebar.locator('[aria-live]')).toHaveCount(0);
});

test.describe('coarse pointer', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 900 } });
  test('phone shell navigation has 44px targets', async ({ page }) => {
    await enable(page); await page.goto('/projects');
    await expect(page.locator('#shell-phone-rail')).toBeVisible();
    for (const selector of ['#shell-header .shell-brand', '#shell-phone-rail button']) {
      for (const target of await page.locator(selector).all()) {
        const bounds = await target.boundingBox();
        expect(bounds!.height, selector).toBeGreaterThanOrEqual(44);
        expect(bounds!.width, selector).toBeGreaterThanOrEqual(44);
      }
    }
    await keyboardOpen(page, 'More');
    const drawer = page.getByRole('dialog', { name: 'Browse', exact: true });
    await drawer.getByRole('button', { name: 'Tools', exact: true }).click();
    for (const link of await drawer.locator('.shell-link').all()) {
      expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
  });
});


test('terminal F6 still takes precedence over shell cycling', async ({ page }) => {
  await enable(page); const project = await gitProject(page, 'a11y-terminal');
  const created = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Terminal escape' } });
  expect(created.ok()).toBeTruthy(); const session = await created.json();
  await page.goto(`/projects/${project.id}`);
  await page.getByRole('link', { name: 'Continue Terminal escape', exact: true }).focus();
  await page.keyboard.press('Enter'); await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}$`));
  await page.locator('.xterm-helper-textarea').focus(); await page.keyboard.press('F6');
  await expect(page.locator('[data-view-tab][aria-selected="true"]')).toBeFocused();
  await expectAxeClean(page);
});

test('long project launch label stays inside the phone header', async ({ page }) => {
  await enable(page); const project = await gitProject(page, 'long-project-name-without-spaces');
  await page.setViewportSize({ width: 390, height: 900 }); await page.goto(`/projects/${project.id}`);
  const launch = page.locator('#shell-header').getByRole('button', { name: /^New session/ });
  await expect(launch).toBeVisible();
  await expect(launch).toHaveAccessibleName(`New session · ${project.slug}`);
  // The header is the one launch action on a project page.
  await expect(page.getByRole('main').getByRole('button', { name: 'New session', exact: true })).toHaveCount(0);
  const bounds = await launch.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await launch.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('region', { name: 'New session', exact: true })).toBeVisible();
});
