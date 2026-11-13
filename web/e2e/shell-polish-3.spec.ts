import { expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { test } from './shell-state';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, selectSessionView, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE });

const notice = (id: number) => ({ id, kind: 'info', severity: 'warn', source: 'system', verified: true,
  title: id === 1 ? 'Backup needs attention' : `Notice ${id}`, detail: 'Review this in Inbox.',
  persistent: id === 1 ? 'backup_failed' : null, session_id: null, session_name: null, project: null,
  created_at: new Date().toISOString(), updated_at: new Date().toISOString(), acked_at: null, resolved_at: null, publish_id: null });

test('Inbox badge replaces the banner and retains persistent notices, zero, capped and unavailable counts', async ({ page }) => {
  let count = 3; let unavailable = false;
  await page.route('**/api/attention*', route => unavailable ? route.fulfill({ status: 503, json: { detail: 'Fixture unavailable' } }) : route.fulfill({ json: Array.from({ length: count }, (_, i) => notice(i + 1)) }));
  await page.goto('/projects');
  const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
  const inbox = rail.getByRole('button', { name: 'Inbox, 3 open', exact: true });
  await expect(inbox).toBeVisible();
  await expect(inbox.locator('.shell-inbox-count')).toHaveText('3');
  expect((await rail.boundingBox())!.width).toBe(60);
  const badgeBox = (await inbox.locator('.shell-inbox-count').boundingBox())!;
  const iconBox = (await inbox.boundingBox())!;
  expect(badgeBox.x + badgeBox.width).toBeLessThanOrEqual(iconBox.x + iconBox.width);
  expect(badgeBox.y).toBeGreaterThanOrEqual(iconBox.y);
  await expectAxeClean(page);
  await expect(page.getByRole('region', { name: 'Inbox status' })).toHaveCount(0);
  await inbox.click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('list', { name: 'Inbox items' })).toContainText('Backup needs attention');
  count = 123; await page.reload();
  await expect(rail.getByRole('button', { name: 'Inbox, 99+ shown, 123 open', exact: true }).locator('.shell-inbox-count')).toHaveText('99+');
  await expectAxeClean(page);
  count = 0; await page.reload();
  await expect(rail.getByRole('button', { name: 'Inbox', exact: true })).toBeVisible();
  await expect(rail.locator('.shell-inbox-count')).toHaveCount(0);
  unavailable = true; await page.reload();
  await expect(rail.getByRole('button', { name: /Inbox.*unavailable/i })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Inbox status' })).toHaveCount(0);
});

test('Grid rail opens the destination directly without a context column, including reload', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Grid', exact: true }).click();
  await expect(page).toHaveURL(/\/grid(?:\?|$)/);
  await expect(page.getByRole('heading', { name: 'Grid', exact: true })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeHidden();
  await expect(page.getByRole('link', { name: 'Open grid', exact: true })).toHaveCount(0);
  await expect(page.getByRole('separator', { name: 'Sidebar width' })).toBeHidden();
  await page.reload();
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeHidden();
  // Other rail modes must still let the person browse away from Grid.
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole('complementary').getByRole('textbox', { name: 'Search projects' })).toBeVisible();
  await expectAxeClean(page);
});

test('every rail item opens its primary page, without an intermediate open-page link', async ({ page }) => {
  await page.goto('/projects');
  const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
  await rail.getByRole('button', { name: 'Overview', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await rail.getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(rail.getByRole('button', { name: 'Overview', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('complementary', { name: 'Context sidebar' })).toBeHidden();
  await page.goForward();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(rail.getByRole('button', { name: 'Projects', exact: true })).toHaveAttribute('aria-pressed', 'true');
  for (const [label, path] of [['Overview', '/'], ['Inbox', '/inbox'], ['Sessions', '/sessions'], ['Projects', '/projects'], ['Workspaces', '/workspaces'], ['Hosts', '/hosts'], ['Grid', '/grid'], ['Tools', '/catalog'], ['Settings', '/settings']]) {
    const button = rail.getByRole('button', { name: new RegExp(`^${label}(,|$)`) });
    await button.focus(); await page.keyboard.press('Enter');
    await expect.poll(() => new URL(page.url()).pathname).toBe(path);
    await expect(button).toHaveAttribute('aria-pressed', 'true');
    const sidebar = page.getByRole('complementary', { name: 'Context sidebar' });
    if (label === 'Overview' || label === 'Grid') await expect(sidebar).toBeHidden();
    else {
      await expect(sidebar).toBeVisible();
      await expect(sidebar.getByRole('link', { name: /^(Open overview|All sessions|All projects|All workspaces|All hosts|All settings|Catalog)$/ })).toHaveCount(0);
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: /^Inbox(,|$)/ }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('dialog', { name: 'Browse', exact: true })).toHaveCount(0);
});

test('direct Tools and nested routes select their owning rail section after reload and history', async ({ page }) => {
  const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
  for (const path of ['/providers', '/search', '/audit', '/guide']) {
    await page.goto(path);
    await expect.soft(rail.getByRole('button', { name: 'Tools', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect.soft(rail.getByRole('button', { name: 'Projects', exact: true })).toHaveAttribute('aria-pressed', 'false');
  }
  await page.reload();
  await expect(rail.getByRole('button', { name: 'Tools', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await rail.getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.goBack();
  await expect(rail.getByRole('button', { name: 'Tools', exact: true })).toHaveAttribute('aria-pressed', 'true');
});

test('Grid uses the shell page and workbar with the same title scale and left edge', async ({ page }) => {
  await page.goto('/sessions');
  const sessionsTitle = page.getByRole('heading', { name: 'Sessions', exact: true, level: 1 });
  const reference = await sessionsTitle.evaluate(node => getComputedStyle(node).fontSize);
  await page.goto('/grid?ids=');
  const title = page.getByRole('heading', { name: 'Grid', exact: true, level: 1 });
  await expect.soft(page.locator('[data-shell-page] > .shell-workbar')).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
  expect.soft(await title.evaluate(node => getComputedStyle(node).fontSize)).toBe(reference);
  // Grid has no context column, so compare its gutter relative to its own content frame.
  const frame = page.locator('#main');
  const gutter = (await title.boundingBox())!.x - (await frame.boundingBox())!.x;
  expect.soft(gutter).toBeLessThanOrEqual(24);
  await expectAxeClean(page);
});

test('Hosts context lists names only and links straight to the existing host page', async ({ page }) => {
  await page.route('**/api/board', route => route.fulfill({ json: { generated_at: Date.now() / 1000, projects: [], machines: [
    { name: 'local', label: 'Cedar main', local: true, connected: true, light: null, alerts: [] },
    { name: 'edge space', label: 'Maple node', local: false, connected: false, light: null, alerts: [] },
  ] } }));
  await page.goto('/projects');
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Hosts', exact: true }).click();
  const sidebar = page.getByRole('complementary', { name: 'Context sidebar' });
  await expect(sidebar.getByRole('link', { name: 'Cedar main', exact: true })).toHaveAttribute('href', '/host');
  await expect(sidebar.getByRole('link', { name: 'Maple node', exact: true })).toHaveAttribute('href', '/host?machine=edge%20space');
  await expect(sidebar).not.toContainText(/CPU|Memory|Active work|Last seen|Node summary|Work:/);
  await sidebar.getByRole('link', { name: 'Cedar main', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/host$/);
  await expect(page.getByRole('heading', { name: 'Host', exact: true, level: 1 })).toBeVisible();
});

test('session keeps one compact line plus tabs and all details, delivery and terminal actions are reachable', async ({ page, browser }) => {
  const project = await gitProject(page, 'compact-header');
  const made = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Review cedar notes' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json();
  try {
    await waitForSessionState(page, session.id, 'idle');
    await page.route(`**/api/sessions/${session.id}`, async route => {
      const response = await route.fetch(); await route.fulfill({ json: { ...await response.json(), observed_state: 'active', native_id: 'cedar-conversation', remote_url: 'https://example.com/remote', editor: { ssh_host: 'example.lan', path: project.folder } } });
    });
    await page.route(`**/api/sessions/${session.id}/deliveries*`, route => route.fulfill({ json: { deliveries: [{ id: 'fixture-delivery', state: 'queued', notice: null, preview: 'Review cedar' }] } }));
    await page.route('**/api/deliveries/fixture-delivery/retry', route => route.fulfill({ json: {} }));
    await page.goto(`/sessions/${session.id}`);
    const header = page.locator('.session-heading');
    await expect(header.getByRole('heading', { name: 'Review cedar notes', exact: true })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Interrupt', exact: true })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    const delivery = header.getByRole('region', { name: 'Message delivery status' });
    await expect(delivery).toContainText('1 message waiting to deliver');
    await delivery.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(header.getByRole('tablist', { name: 'Session views' })).toBeVisible();
    const sessionTabs = header.getByRole('tablist', { name: 'Session views' });
    for (const name of ['Setup', 'Activity', 'Handoffs']) await expect.soft(sessionTabs.getByRole('tab', { name, exact: true })).toBeHidden();
    const moreViews = header.locator('.session-tab-line').getByRole('button', { name: 'More', exact: true });
    await expect.soft(moreViews).toBeVisible();
    await moreViews.focus(); await page.keyboard.press('Enter');
    const viewsMenu = page.getByRole('menu', { name: 'More', exact: true });
    for (const name of ['Setup', 'Activity', 'Handoffs']) await expect(viewsMenu.getByRole('menuitem', { name, exact: true })).toBeVisible();
    await viewsMenu.getByRole('menuitem', { name: 'Activity', exact: true }).focus(); await page.keyboard.press('Enter');
    const active = sessionTabs.getByRole('tab', { name: 'Activity', exact: true });
    await expect(active).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tabpanel', { name: 'Activity', exact: true })).toHaveAttribute('aria-labelledby', (await active.getAttribute('id'))!);
    await sessionTabs.getByRole('tab', { name: 'Terminal', exact: true }).click();
    await expect(sessionTabs.getByRole('tab', { name: 'Activity', exact: true })).toBeHidden();
    await expect(header.locator('.session-tab-line').getByRole('button', { name: 'Open as text', exact: true })).toBeVisible();
    expect((await header.boundingBox())!.height).toBeLessThanOrEqual(90);
    expect((await page.locator('.shell-header-line').boundingBox())!.height).toBeLessThanOrEqual(64);
    expect(await header.getByRole('button', { name: 'Session details', exact: true }).evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThan(0);
    await expect.soft(page.locator('.shell-session-page:visible h1')).toHaveCount(1);
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Sessions', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect.soft(page.getByLabel('Terminal keys', { exact: true })).toBeHidden();
    await expect.soft(page.getByText('More terminal keys', { exact: true })).toBeHidden();
    mkdirSync('../.acc/artifacts/shell-polish-3', { recursive: true });
    await page.screenshot({ path: '../.acc/artifacts/shell-polish-3/session-desktop.png', fullPage: true });
    await header.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Stop Review cedar notes?' })).toBeVisible();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    const asText = header.getByRole('button', { name: 'Open as text', exact: true });
    await expect(asText).toBeEnabled(); await asText.click();
    await expect(page.getByRole('dialog', { name: 'Terminal output', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await header.getByRole('button', { name: 'Terminal options', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Paste text…', exact: true })).toBeVisible();
    const options = header.getByRole('group', { name: 'Terminal options', exact: true });
    await expect(options.getByRole('button', { name: 'Copy selected text', exact: true })).toBeVisible();
    await expect(options.getByRole('checkbox', { name: 'Screen reader output' })).toBeChecked();
    await page.keyboard.press('Escape');
    const details = header.getByRole('button', { name: 'Session details', exact: true });
    await details.focus(); await page.keyboard.press('Enter');
    const panel = header.getByRole('group', { name: 'Session details', exact: true });
    await expect(panel.getByRole('link', { name: 'Export', exact: true })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Copy conversation ID', exact: true })).toBeVisible();
    await expect(panel.getByRole('link', { name: 'Open in Remote Control', exact: true })).toBeVisible();
    await expect(panel).toContainText(project.slug);
    await expect(panel.getByRole('button', { name: 'Open in editor', exact: true })).toBeVisible();
    await expect(panel.getByRole('group', { name: 'Session deliverables' })).toBeVisible();
    await expect(panel).toContainText('cedar-conversation');
    await page.keyboard.press('Escape'); await expect(details).toBeFocused();
    await delivery.getByRole('button', { name: 'Details', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Message delivery history' })).toBeVisible();
    await expect(header.locator('.session-tab-line').getByRole('button', { name: 'Open as text', exact: true })).toHaveCount(0);
    await expectAxeClean(page);
    await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
    await expect(header.getByRole('button', { name: 'Open as text', exact: true })).toBeVisible();
    await expectAxeClean(page);
    await page.getByRole('combobox', { name: 'Move view to column' }).selectOption('2');
    await expect(page.getByRole('tablist', { name: 'Column 3 views', exact: true })).toBeVisible();
    const column = page.getByRole('region', { name: 'Column 3', exact: true });
    expect(await column.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(0);
    const narrowHeader = page.locator('.shell-session-heading').filter({ has: page.getByRole('tab', { name: 'Terminal', exact: true, selected: true }) });
    expect.soft(await narrowHeader.locator('..').evaluate(node => getComputedStyle(node).paddingLeft)).toBe('12px');
    expect.soft(await narrowHeader.locator('h1').evaluate(node => getComputedStyle(node).fontSize)).toBe('12px');
    await narrowHeader.getByRole('button', { name: 'Session details', exact: true }).click();
    const narrowPanel = narrowHeader.getByRole('group', { name: 'Session details', exact: true });
    const paneBox = (await narrowHeader.boundingBox())!;
    const panelBox = (await narrowPanel.boundingBox())!;
    expect(panelBox.x).toBeGreaterThanOrEqual(paneBox.x);
    expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(paneBox.x + paneBox.width);
    await expect(narrowPanel.getByRole('link', { name: 'Export', exact: true })).toBeInViewport();
    await narrowPanel.getByRole('button', { name: 'Open in editor', exact: true }).click();
    const codeLink = narrowPanel.getByRole('link', { name: 'VS Code (Remote-SSH)', exact: true });
    await expect(codeLink).toBeInViewport();
    const linkBox = (await codeLink.boundingBox())!;
    expect(linkBox.x).toBeGreaterThanOrEqual(panelBox.x);
    expect(linkBox.x + linkBox.width).toBeLessThanOrEqual(panelBox.x + panelBox.width);
    await expect(narrowPanel.getByRole('button', { name: 'Copy command', exact: true })).toBeInViewport();
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'Fill working panel', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByLabel('Terminal keys', { exact: true })).toBeHidden(); // Narrow desktop is still a fine pointer.
    const controlLabel = header.locator('.terminal-control-label');
    await expect(controlLabel).toHaveText('You control this terminal');
    expect((await controlLabel.boundingBox())!.width).toBeGreaterThanOrEqual(60);
    await page.screenshot({ path: '../.acc/artifacts/shell-polish-3/session-phone.png', fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const touch = await browser.newContext({ storageState: await page.context().storageState(), viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    try {
      const phone = await touch.newPage();
      await phone.goto(`/sessions/${session.id}`);
      await expect(phone.getByLabel('Terminal keys', { exact: true })).toBeVisible();
      await expect(phone.getByRole('button', { name: 'Esc', exact: true })).toBeVisible();
      await expect(phone.getByText('More terminal keys', { exact: true })).toBeVisible();
    } finally { await touch.close(); }
  } finally {
    await page.unrouteAll({ behavior: 'wait' });
    expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
  }
});

test.describe('sole layout navigation', () => {
  test.use({ });
  test('secondary session views remain reachable through More', async ({ page }) => {
    const project = await gitProject(page, 'legacy-session-tabs');
    const made = await page.request.post('/api/sessions', { headers: await csrfHeaders(page), data: { project_id: project.id, provider: 'claude', name: 'Cedar standard tabs' } });
    expect(made.ok()).toBeTruthy(); const session = await made.json();
    try {
      await waitForSessionState(page, session.id, 'idle');
      await page.goto(`/sessions/${session.id}`);
      const tabs = page.getByRole('tablist', { name: 'Session views' });
      for (const name of ['Setup', 'Activity', 'Handoffs']) { await selectSessionView(page, name); await expect(tabs.getByRole('tab', { name, exact: true })).toHaveAttribute('aria-selected', 'true'); }
    } finally { expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy(); }
  });
  test('retains the existing notice row and navigation', async ({ page }) => {
    await page.route('**/api/attention*', route => route.fulfill({ json: [notice(1), notice(2)] }));
    await page.goto('/projects');
    await expect(page.getByRole('region', { name: 'Inbox status' })).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Inbox, 2 open', exact: true })).toBeVisible();
  });
});
