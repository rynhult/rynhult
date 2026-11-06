import { test, expect } from './fixtures';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, expectAxeClean, gitProject, info } from './helpers';

import { gotoLoadedPage } from './loaded-page';
test.use({ storageState: AUTH_FILE });
async function team(session: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${session}/events.jsonl`, JSON.stringify({ acc_session: session,
    hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } } }) + '\n');
  const reply = `${info().run_dir}/${session}/team/${id}.json`;
  await expect.poll(() => existsSync(reply), { timeout: 20000 }).toBeTruthy();
  const result = JSON.parse(readFileSync(reply, 'utf8'));
  expect(result.ok, JSON.stringify(result)).toBeTruthy();
  return result.result;
}

for (const colorScheme of ['dark', 'light'] as const) test(`50 assignments and 491 attempts remain usable on a phone in ${colorScheme}`, async ({ page, arrangeTeam }, ti) => {
  test.setTimeout(120000);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.emulateMedia({ colorScheme });
  const { id, headers } = await gitProject(page, 'break-it-13');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude',
    name: 'Synthetic review lead', worktree: true,
    orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 1 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const lead = (await created.json()).id;
  try {
    await team(lead, 'plan_update', { idea: 'Adversarial phone history', reason: 'Bounded fixture',
      scope: { in: ['Synthetic history'], out: [], parked: [] }, phases: [{ key: 'history', title: 'Check history' }] });
    await team(lead, 'plan_ready', { reason: 'Fixture only' });
    const task = await team(lead, 'assign', { goal: 'Real idempotent fixture assignment', task_type: 'implementation',
      need: { role: 'general', classification_reason: 'Bounded synthetic work' } });
    const selected = await team(lead, 'select', { assignment: task.id });
    const args = { assignment: task.id, selection: selected.id, name: 'Synthetic worker' };
    const [first, second] = await Promise.all([team(lead, 'start', args), team(lead, 'start', args)]);
    expect(first.id).toBe(second.id);
    await team(first.id, 'report', { status: 'blocked', summary: 'Waiting for the person' });
    await team(first.id, 'report', { status: 'done', summary: 'Reviewed fixture history' });
    // Seed through server writer admission, so this fixture cannot invalidate
    // another worker's in-flight team action/result transaction.
    await arrangeTeam('history', lead, first.id);
    await gotoLoadedPage(page, `/sessions/${lead}?tab=team#settings`, ['SessionPage', 'TeamPanel'],
      [`/api/sessions/${lead}/team`, `/api/teams/${lead}/selections`]);
    const assignments = page.getByRole('region', { name: 'Assignments', exact: true });
    await expect(assignments.getByRole('article')).toHaveCount(25);
    await expect(page.getByRole('navigation', { name: 'Assignment pages', exact: true })).toContainText('1–25 of 50');
    await expect(assignments.getByText(/Attempt 10: reported/)).toHaveCount(25);
    expect(await assignments.locator('img').count()).toBe(0);
    expect(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned)).toBeUndefined();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    const next = page.getByRole('button', { name: 'Next assignments', exact: true });
    // Hold a real periodic refresh in flight. Background refresh must neither
    // disable the current page's controls nor discard keyboard focus.
    let releaseRefresh!: () => void;
    const heldRefresh = new Promise<void>(resolve => { releaseRefresh = resolve; });
    let intercepted = false;
    let finishRefresh!: () => void;
    const refreshForwarded = new Promise<void>(resolve => { finishRefresh = resolve; });
    const currentPageUrl = `**/api/teams/${lead}/selections?assignment_offset=0`;
    const refreshStarted = page.waitForRequest(r => new URL(r.url()).pathname === `/api/teams/${lead}/selections`
      && new URL(r.url()).searchParams.get('assignment_offset') === '0');
    await page.route(currentPageUrl, async route => {
      intercepted = true;
      await heldRefresh;
      try { await route.continue(); } finally { finishRefresh(); }
    });
    try {
      await expect(next).toBeEnabled();
      await next.focus();
      await refreshStarted;
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect(next).toBeEnabled();
      await expect(next).toBeFocused();
      await page.keyboard.press('Enter');
      await expect(page.getByRole('navigation', { name: 'Assignment pages', exact: true })).toContainText('26–50 of 50');
    } finally {
      releaseRefresh();
      if (intercepted) await refreshForwarded;
      await page.unroute(currentPageUrl);
    }
    await expect(assignments.getByText('Real idempotent fixture assignment', { exact: true })).toBeVisible();
    await expect(next).toBeDisabled();
    await expectAxeClean(page);
    await page.getByRole('navigation', { name: 'Assignment pages', exact: true }).screenshot({ path: ti.outputPath(`history-pagination-${colorScheme}.png`) });
    await page.getByRole('button', { name: 'Previous assignments', exact: true }).click();
    await expect(page.getByRole('navigation', { name: 'Assignment pages', exact: true })).toContainText('1–25 of 50');
    // Capture the real phone viewport; a whole long article produces a mostly
    // clipped image because the application scrolls inside its own pane.
    await assignments.getByRole('article').first().locator('p').first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: ti.outputPath(`many-attempts-${colorScheme}.png`) });
  } finally { expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBeTruthy(); }
});
