/** Slice 2 only: real temporary server and spool, no installed agent tools. */
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { execFileSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, control, info } from './helpers';

test.use({ storageState: AUTH_FILE });
test('live tree: committed helpers, keyboard, phone, finished retention and stale recovery', async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const folder = `${info().workspace}/live-tree`;
  mkdirSync(folder, { recursive: true });
  const git = (...args: string[]) => execFileSync('git', ['-C', folder, '-c', 'user.name=Taylor', '-c', 'user.email=taylor@example.com', ...args]);
  git('init', '-q', '-b', 'main'); git('commit', '--allow-empty', '-qm', 'Start');
  await page.goto('/projects');
  const csrf = (await page.context().cookies()).find(c => /^(?:__Host-)?acc_csrf$/.test(c.name))!.value;
  const post = (url: string, data: unknown = {}) => page.request.post(url, { data, headers: { 'X-ACC-CSRF': csrf, Origin: info().origin } });
  const created = await post('/api/projects', { name: 'Live tree demo', slug: 'live-tree-demo', workspace_path: folder });
  expect(created.status()).toBe(201);
  const project = await created.json();
  expect((await post(`/api/projects/${project.id}/trust`)).ok()).toBeTruthy();
  const launch = await post('/api/sessions', { project_id: project.id, provider: 'claude', name: 'Taylor lead', worktree: true,
    orchestrator: { agents: [{agent: 'claude', profile: null}], machines: ['local'], at_once: 3, total: 10, depth: 2, hours: 8 } });
  expect(launch.status()).toBe(201);
  const lead = await launch.json();
  async function team(caller: string, action: string, args: object) {
    const id = randomUUID().replaceAll('-', '');
    appendFileSync(`${info().run_dir}/${caller}/events.jsonl`, JSON.stringify({ acc_session: caller,
      hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } } }) + '\n');
    const file = `${info().run_dir}/${caller}/team/${id}.json`;
    await expect.poll(() => existsSync(file)).toBeTruthy();
    const reply = JSON.parse(readFileSync(file, 'utf8'));
    expect(reply.ok, JSON.stringify(reply)).toBeTruthy();
    return reply.result;
  }
  await team(lead.id, 'plan_update', { reason: 'Bounded tree fixture', idea: 'Review and check the demo.',
    scope: { in: ['Demo'], out: [], parked: [] }, phases: [{ key: 'demo', title: 'Check demo' }] });
  await team(lead.id, 'plan_ready', { reason: 'Demo scope confirmed' });
  async function helper(caller: string, name: string, goal: string) {
    const assignment = await team(caller, 'assign', { goal, task_type: 'implementation', acceptance: [], requirement_ids: [],
      need: { role: 'general', classification_reason: 'Bounded tree fixture helper' } });
    const selected = await team(caller, 'select', { assignment: assignment.id, pin: { agent: 'claude' } });
    return team(caller, 'start', { assignment: assignment.id, selection: selected.id, name });
  }
  await page.goto(`/sessions/${lead.id}?tab=team#settings`);
  const tree = page.getByRole('tree', { name: 'Live agent tree' });
  await expect(tree.getByRole('treeitem')).toHaveCount(1);
  const child = await helper(lead.id, 'Morgan helper', 'Review the demo.');
  await expect(tree.getByRole('treeitem', { name: /Morgan helper/ })).toBeVisible();
  const grandchild = await helper(child.id, 'Robin tests', 'Check the demo.');
  await expect(tree.getByRole('treeitem')).toHaveCount(3);
  const rootItem = tree.getByRole('treeitem', { name: /Taylor lead/ });
  const childItem = tree.getByRole('treeitem', { name: /Morgan helper/ });
  await rootItem.focus(); await page.keyboard.press('ArrowDown');
  await expect(childItem).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(tree.getByRole('treeitem')).toHaveCount(2);
  await page.keyboard.press('ArrowRight');
  await expect(tree.getByRole('treeitem')).toHaveCount(3);
  control(child.id, 'permission');
  await expect(childItem).toContainText('Needs you');
  await expect(childItem).toBeFocused(); // refreshing never steals keyboard focus
  await expect(page.getByRole('region', { name: 'Selected agent' })).toContainText('Morgan helper');
  await expect(page.getByRole('region', { name: 'Selected agent' }).getByRole('link', { name: 'Answer in Inbox' })).toBeVisible();
  const axe = await new AxeBuilder({ page }).include('[aria-label="Live agent tree panel"]').withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(axe.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('tree-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(tree.getByRole('treeitem')).toHaveCount(2);
  await childItem.focus(); await page.keyboard.press('ArrowRight');
  await expect(tree.getByRole('treeitem')).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath('tree-phone.png'), fullPage: true });
  // Only this view's polling is interrupted. Existing data must remain labelled stale.
  await page.route(`**/api/teams/${lead.id}/events?*`, route => route.fulfill({ status: 503, json: { detail: 'Temporary test interruption' } }));
  // Finish a live poll to reach the intercepted next request without a fixed delay.
  control(grandchild.id, 'permission');
  await expect(page.getByText(/Live updates paused/)).toBeVisible({ timeout: 35_000 });
  await page.unroute(`**/api/teams/${lead.id}/events?*`);
  await page.getByRole('button', { name: 'Retry live updates' }).click();
  await expect(page.getByText(/Live updates paused/)).toHaveCount(0);
  await tree.getByRole('treeitem', {name: /Robin tests/}).focus();
  await team(lead.id, 'finish', { helper: child.id });
  await expect(childItem).toContainText('finished');
  await expect(childItem).toBeFocused();
  await expect(childItem).toBeVisible();
  await expect(tree.getByRole('treeitem', { name: /Robin tests/ })).toHaveCount(0);
  // Sibling slices supply these event projections later. Treat every word as text.
  const snapshot = await (await page.request.get(`/api/teams/${lead.id}/tree`)).json();
  const hostile = '<img src=x onerror="alert(1)">';
  const node = snapshot.members.find((row: {id: string}) => row.id === child.id);
  node.selection_reason = hostile; node.report_status = 'done';
  node.deliveries = [{id: 9, session: child.id, state: 'acknowledged'}];
  await page.route(`**/api/teams/${lead.id}/tree`, route => route.fulfill({json: snapshot}));
  await page.reload();
  await childItem.focus();
  const details = page.getByRole('region', {name: 'Selected agent'});
  await expect(details).toContainText(hostile);
  await expect(details).toContainText('Message 9: acknowledged');
  await expect(details).toContainText('Report: done');
  await expect(details.locator('img')).toHaveCount(0);
  await childItem.press('Enter');
  await expect(page).toHaveURL(new RegExp(`/sessions/${child.id}$`));
});
