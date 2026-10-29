import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { environmentValue } from '../../src/acc/env.mjs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, wikiShotPath } from './helpers';

// The working list on the Team tab, including conflicts through the fixture server's real API.
test.use({ storageState: AUTH_FILE });

function fixtureSql(sql: string, args: unknown[]) {
  execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c',
    'import sqlite3,sys,json\nwith sqlite3.connect(sys.argv[1]) as db: db.execute(sys.argv[2],json.loads(sys.argv[3]))',
    `${info().state}/data/acc.db`, sql, JSON.stringify(args)]);
}

async function teamLead(page: Page) {
  const { id, headers } = await gitProject(page, 'worklist');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Riley lead',
    worktree: true, orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 2 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  return (await created.json()).id as string;
}

test('the working list stays hidden while orchestrator v2 is off', async ({ page }) => {
  const lead = await teamLead(page);
  const live = await page.request.get(`/api/teams/${lead}/worklist`);
  test.skip(live.status() !== 404, 'this server runs with orchestrator v2 on');
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  await expect(page.getByText('Team limits', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Working list' })).toHaveCount(0);
});

for (const state of ['open', 'done'] as const) test(`an answer survives a same-card conflict while the card becomes ${state} and resends once`, async ({ page }, ti) => {
  const lead = await teamLead(page);
  const headers = await csrfHeaders(page);
  const root = `/api/teams/${lead}/worklist`;
  try {
    const created = await page.request.post(`${root}/items`, { headers, data: { kind: 'question', title: 'Which booking details are public?' } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const original = await created.json();
    const url = `${root}/items/${original.id}`;
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem', { name: original.title, exact: true });
    await card.getByRole('textbox', { name: `Answer "${original.title}"`, exact: true }).fill('Only the available times; keep guest names private.');
    let raced = false;
    await page.route(`**${url}`, async route => {
      if (!raced && route.request().method() === 'PATCH') {
        raced = true;
        const changed = await page.request.patch(url, { headers, data: {
          revision: original.revision, title: 'Which booking details can guests see?',
          steps: 'Consider the calendar and confirmation screen.', state,
        } });
        expect(changed.ok(), await changed.text()).toBeTruthy();
      }
      await route.continue();
    });
    await card.getByRole('button', { name: 'Answer', exact: true }).click();
    const latest = page.getByRole('listitem', { name: 'Which booking details can guests see?', exact: true });
    const review = latest.getByRole('alert');
    await expect(review).toContainText('This card changed');
    await expect(review).toContainText(original.title);
    await expect(review).toContainText('Consider the calendar and confirmation screen.');
    const reply = latest.getByRole('textbox', { name: /Answer "/ });
    await expect(reply).toHaveValue('Only the available times; keep guest names private.');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    if (state === 'open') await latest.screenshot({ path: wikiShotPath(ti, /390|phone|mobile/.test(ti.project.name) ? 'worklist-conflict-phone.png' : 'worklist-conflict.png') });
    const saved = page.waitForResponse(r => new URL(r.url()).pathname === url && r.request().method() === 'PATCH');
    await latest.getByRole('button', { name: 'Resend answer', exact: true }).click();
    expect((await saved).ok()).toBeTruthy();
    await expect(latest).toContainText('Answer from alex');
    const stored = (await (await page.request.get(root)).json()).items.find((i: { id: number }) => i.id === original.id);
    expect(stored.answer).toBe('Only the available times; keep guest names private.');
    expect(stored.state).toBe('answered');
    await expect(page.getByRole('alert').filter({ hasText: 'This card changed' })).toHaveCount(0);
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

test('a lead decision sends its own revision, not the much newer list revision', async ({ page }) => {
  const lead = await teamLead(page);
  const headers = await csrfHeaders(page);
  const root = `/api/teams/${lead}/worklist`;
  try {
    const created = await page.request.post(`${root}/items`, { headers, data: {
      kind: 'decision', title: 'Keep bookings private?',
      options: [{ id: 'private', label: 'Keep names private' }, { id: 'public', label: 'Publish names' }],
    } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const original = await created.json();
    fixtureSql("UPDATE work_item SET source='lead', owner_edited=0 WHERE id=?", [original.id]);
    fixtureSql('UPDATE orchestration_run SET revision=75 WHERE team_id=?', [lead]);
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem', { name: original.title, exact: true });
    await expect(card).toContainText('Written by the team lead (an agent)');
    await card.getByRole('button', { name: 'Change', exact: true }).click();
    await card.getByRole('combobox').selectOption('private');
    const sent = page.waitForRequest(r => new URL(r.url()).pathname === `${root}/items/${original.id}` && r.method() === 'PATCH');
    await card.getByRole('button', { name: 'Save answer', exact: true }).click();
    expect((await sent).postDataJSON()).toEqual({ answer: 'private', revision: 1 });
    await expect(card).toContainText('Answer from alex');
    const stored = (await (await page.request.get(root)).json()).items.find((i: { id: number }) => i.id === original.id);
    expect(stored.answer).toBe('private');
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

test('a removed decision option stays visible but cannot be resent as a different choice', async ({ page }) => {
  const lead = await teamLead(page);
  const headers = await csrfHeaders(page);
  const root = `/api/teams/${lead}/worklist`;
  try {
    const response = await page.request.post(`${root}/items`, { headers, data: {
      kind: 'decision', title: 'What can guests see?',
      options: [{ id: 'public', label: 'Names and times' }, { id: 'private', label: 'Times only' }],
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const original = await response.json();
    const url = `${root}/items/${original.id}`;
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem', { name: original.title, exact: true });
    await card.getByRole('button', { name: 'Change', exact: true }).click();
    await card.getByRole('combobox').selectOption('public');
    let raced = false;
    await page.route(`**${url}`, async route => {
      if (!raced && route.request().method() === 'PATCH') {
        raced = true;
        const changed = await page.request.patch(url, { headers, data: {
          revision: original.revision, options: [{ id: 'private', label: 'Times only' }, { id: 'later', label: 'Decide later' }],
        } });
        expect(changed.ok(), await changed.text()).toBeTruthy();
      }
      await route.continue();
    });
    await card.getByRole('button', { name: 'Save answer', exact: true }).click();
    await expect(card.getByRole('alert')).toContainText('This card changed');
    await expect(card.getByRole('combobox')).toHaveValue('public');
    await expect(card.getByRole('button', { name: 'Resend decision', exact: true })).toBeDisabled();
    await card.getByRole('combobox').selectOption('private');
    await card.getByRole('button', { name: 'Resend decision', exact: true }).click();
    await expect(card).toContainText('Answer from alex');
    expect((await (await page.request.get(root)).json()).items.find((i: { id: number }) => i.id === original.id).answer).toBe('private');
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

for (const state of ['open', 'done'] as const) test(`blocking-link conflict keeps the edit when the card becomes ${state} and retries with its reviewed revision`, async ({ page }) => {
  const lead = await teamLead(page);
  const headers = await csrfHeaders(page);
  const root = `/api/teams/${lead}/worklist`;
  try {
    expect((await page.request.patch(`/api/teams/${lead}/plan`, { headers, data: {
      version: 0, requirements: [{ text: 'Keep guest names private' }],
    } })).ok()).toBeTruthy();
    const response = await page.request.post(`${root}/items`, { headers, data: { kind: 'question', title: 'Approve the booking flow?' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const original = await response.json();
    const url = `${root}/items/${original.id}`;
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem', { name: original.title, exact: true });
    await card.getByRole('button', { name: 'Edit blocking links', exact: true }).click();
    await card.getByRole('textbox', { name: /Blocking links for/ }).fill('R-1');
    let raced = false;
    await page.route(`**${url}`, async route => {
      if (!raced && route.request().method() === 'PATCH') {
        raced = true;
        expect((await page.request.patch(url, { headers, data: {
          revision: original.revision, steps: 'Check the confirmation screen too.', state,
        } })).ok()).toBeTruthy();
      }
      await route.continue();
    });
    await card.getByRole('button', { name: 'Save blocking links', exact: true }).click();
    await expect(card.getByRole('alert')).toContainText('Check the confirmation screen too.');
    await expect(card.getByRole('textbox', { name: /Blocking links for/ })).toHaveValue('R-1');
    await card.getByRole('button', { name: 'Resend blocking links', exact: true }).click();
    await expect(card.getByRole('button', { name: 'Resend blocking links', exact: true })).toHaveCount(0);
    const saved = (await (await page.request.get(root)).json()).items.find((i: { id: number }) => i.id === original.id);
    expect(saved.blocks).toEqual(['R-1']);
    expect(saved.steps).toBe('Check the confirmation screen too.');
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

test('person reads NOW, answers a decision, edits, reorders and sees a conflict', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  const at = new Date().toISOString();
  const item = (id: number, position: number, kind: string, title: string, extra: object = {}) => ({
    id, position, kind, title, steps: '', options: null, recommendation: null, state: 'open', answer: null, answered_by: null,
    answered_at: null, ingested_at: null, done_at: null, note: null, source: 'lead', owner_edited: false, links: [], revision: 1,
    created_at: at, updated_at: at, ...extra });
  const list = {
    now: 'Building the importer; waiting on the database choice.', now_updated_at: at, now_updated_by: 'lead', revision: 5, idle_minutes: 5,
    items: [
      item(1, 1, 'need', 'Approve the staging port', { steps: '1. Open Inbox.\n2. Approve the port request for staging.example.lan.' }),
      item(2, 2, 'decision', 'Which database?', {
        options: [{ id: 'sqlite', label: 'SQLite', consequence: 'One machine only' }, { id: 'pg', label: 'PostgreSQL', consequence: 'Needs a database service' }],
        recommendation: { option: 'sqlite', reason: 'One host for now; moving later is a dump and load.' } }),
      item(3, 3, 'task', 'Write the importer'),
      item(4, 4, 'task', 'Add the export button'),
      item(5, 0, 'note', 'Kickoff notes read', { state: 'done', done_at: at }),
    ],
  };
  let conflictOnce = true;
  await page.route(`**/api/teams/${lead}/worklist**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'GET') return route.fulfill({ json: list });
    const body = request.postDataJSON();
    if (url.pathname.endsWith('/now')) {
      list.now = body.text; list.now_updated_by = 'alex'; list.revision += 1;
      return route.fulfill({ json: { now: list.now, revision: list.revision } });
    }
    const target = list.items.find((x) => x.id === Number(url.pathname.split('/').pop()))!;
    if ('position' in body && conflictOnce) {
      conflictOnce = false;
      target.title = 'Write the importer (lead renamed it)'; target.revision += 1; list.revision += 1;
      return route.fulfill({ status: 409, json: { detail: 'the item changed since you loaded it', worklist: list } });
    }
    if (body.revision !== target.revision) return route.fulfill({ status: 409, json: { detail: 'stale', worklist: list } });
    Object.assign(target, body, { revision: target.revision + 1 });
    if ('answer' in body) Object.assign(target, { state: 'answered', answered_by: 'alex', answered_at: at });
    list.revision += 1;
    return route.fulfill({ json: target });
  });

  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const card = page.getByRole('heading', { name: 'Working list' }).locator('xpath=ancestor::section[1]');
  await expect(card.getByText('Building the importer; waiting on the database choice.')).toBeVisible();
  const yours = page.getByRole('region', { name: 'What the lead needs from you' });
  await expect(yours.getByRole('listitem', { name: 'Approve the staging port' })).toContainText('staging.example.lan');
  // Agent text is labelled as an agent's, never Rynhult's (security review 2026-10-25).
  await expect(yours.getByRole('listitem', { name: 'Approve the staging port' })).toContainText('Written by the team lead (an agent)');
  await expect(yours.getByText('From Rynhult')).toHaveCount(0);
  const decision = yours.getByRole('listitem', { name: 'Which database?' });
  await expect(decision.getByText('Recommended')).toBeVisible();
  await decision.getByRole('button', { name: 'Change', exact: true }).click();
  await expect(decision.getByRole('combobox', { name: 'Choose for "Which database?"' })).toHaveValue('sqlite');
  await expect(page.getByText('Done and dismissed (1)')).toBeVisible();
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, 'orchestrator-worklist.png'), fullPage: true });

  await decision.getByRole('combobox', { name: 'Choose for "Which database?"' }).selectOption('pg');
  await decision.getByRole('button', { name: 'Save answer' }).click();
  const tasks = page.getByRole('region', { name: 'Team tasks and notes' });
  await expect(page.getByRole('region', { name: 'Answered', exact: true }).getByRole('listitem', { name: 'Which database?' })).toContainText('Answer from alex');
  await expect(page.getByRole('region', { name: 'Answered', exact: true }).getByRole('listitem', { name: 'Which database?' })).toContainText('PostgreSQL');

  await page.getByRole('region', { name: 'Now' }).getByRole('button', { name: 'Edit' }).click();
  await page.getByLabel('Now summary').fill('Pause the importer until the database is set up.');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Pause the importer until the database is set up.')).toBeVisible();

  // Someone else changed the item first: the list reloads and says so; the second try lands.
  await tasks.getByRole('button', { name: 'Move "Write the importer" down' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'This card changed' })).toBeVisible();
  await expect(tasks.getByRole('listitem', { name: 'Write the importer (lead renamed it)' })).toBeVisible();
  await tasks.getByRole('button', { name: 'Move "Write the importer (lead renamed it)" down' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'This card changed' })).toHaveCount(0);

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: wikiShotPath(testInfo, 'orchestrator-worklist-phone.png'), fullPage: true });
});

// The tabs merge adds free-text decision answers to the same 409 recovery path.
test('a custom decision answer survives a card closing during save and resends against its current revision', async ({ page }) => {
  const lead = await teamLead(page);
  const headers = await csrfHeaders(page);
  const root = `/api/teams/${lead}/worklist`;
  try {
    const response = await page.request.post(`${root}/items`, { headers, data: {
      kind: 'decision', title: 'Choose the archive', options: [{ id: 'local', label: 'Local' }, { id: 'remote', label: 'Remote' }],
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const original = await response.json();
    const url = `${root}/items/${original.id}`;
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    const card = page.getByRole('listitem', { name: original.title, exact: true });
    await card.getByRole('button', { name: 'Change', exact: true }).click();
    await card.getByRole('textbox', { name: 'Different answer', exact: true }).fill('Use a separate encrypted archive');
    let raced = false;
    let currentRevision = 0;
    await page.route(`**${url}`, async route => {
      if (!raced && route.request().method() === 'PATCH') {
        raced = true;
        const changed = await page.request.patch(url, { headers, data: { revision: original.revision, answer: 'local', state: 'done' } });
        expect(changed.ok(), await changed.text()).toBeTruthy();
        currentRevision = (await changed.json()).revision;
      }
      await route.continue();
    });
    await card.getByRole('button', { name: 'Save answer', exact: true }).click();
    await expect(card.getByRole('alert')).toContainText('This card changed');
    await expect(card.getByRole('textbox', { name: 'Different answer', exact: true })).toHaveValue('Use a separate encrypted archive');
    const sent = page.waitForRequest(r => new URL(r.url()).pathname === url && r.method() === 'PATCH');
    await card.getByRole('button', { name: 'Resend decision', exact: true }).click();
    expect((await sent).postDataJSON()).toEqual({ answer_text: 'Use a separate encrypted archive', revision: currentRevision });
    await expect(card).toContainText('Use a separate encrypted archive');
    await expect(card.getByRole('alert')).toHaveCount(0);
    const stored = (await (await page.request.get(root)).json()).items.find((i: { id: number }) => i.id === original.id);
    expect(stored.state).toBe('answered');
    expect(stored.options.find((o: { id: string }) => o.id === stored.answer).label).toBe('Use a separate encrypted archive');
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});
