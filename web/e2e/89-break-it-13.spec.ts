import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { environmentValue } from '../../src/acc/env.mjs';
import { AUTH_FILE, expectAxeClean, gitProject, info } from './helpers';

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

for (const colorScheme of ['dark', 'light'] as const) test(`50 assignments and 491 attempts remain usable on a phone in ${colorScheme}`, async ({ page }, ti) => {
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
    // Seed historical rows in THIS disposable DB, avoiding hundreds of live fake CLIs.
    execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c', `
import sys, uuid
from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from acc.server.models import utcnow
from acc.server.orchestrator.assignments import Assignment, Attempt
from acc.server.orchestrator.delivery import TaskReport
with Session(create_engine('sqlite:///' + sys.argv[1])) as db:
    lead, helper = sys.argv[2:]
    for i in range(49):
        assignment = Assignment(id=uuid.uuid4().hex, team_id=lead, created_by=lead,
            task_type='implementation', goal=f'Synthetic history {i:02d} ' + 'x'*120,
            need={'role':'general'}, state='reported', created_at=utcnow()+timedelta(seconds=i))
        db.add(assignment); db.flush()
        for j in range(10):
            attempt = Attempt(id=uuid.uuid4().hex, assignment_id=assignment.id, session_id=helper,
                sequence=2+i*10+j, state='reported', branch='acc/synthetic-history', head='a'*40)
            db.add(attempt); db.flush()
            for v in range(1,4):
                db.add(TaskReport(session_id=helper, team_id=lead, task_id=f'{attempt.id}:{v}',
                    attempt_id=attempt.id, version=v, status='done', summary='<img src=x onerror=window.pwned=1> '+('evidence'*80),
                    findings_md='# Synthetic findings', branch='acc/synthetic-history', head='a'*40))
    db.commit()
`, `${info().state}/data/acc.db`, lead, first.id], { cwd: '..', env: { ...process.env, PYTHONPATH: 'src' } });
    await page.goto(`/sessions/${lead}?tab=team#settings`);
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
