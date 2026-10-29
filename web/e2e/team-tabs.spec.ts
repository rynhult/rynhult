import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, expandTeamPanels, csrfHeaders, gitProject, expectAxeClean, wikiShotPath, info as serverInfo } from './helpers';
test.use({ storageState: AUTH_FILE, serviceWorkers: 'block' });
let id: string;
async function team(action: string, args: object = {}) {
  const requestId = randomUUID().replaceAll('-', '');
  appendFileSync(`${serverInfo().run_dir}/${id}/events.jsonl`, JSON.stringify({ acc_session: id,
    hook: { hook_event_name: 'AccTeam', request: { id: requestId, action, ...args } } }) + '\n');
  const replyPath = `${serverInfo().run_dir}/${id}/team/${requestId}.json`;
  await expect.poll(() => existsSync(replyPath)).toBe(true);
  const reply = JSON.parse(readFileSync(replyPath, 'utf8'));
  expect(reply.ok, JSON.stringify(reply)).toBe(true);
  return reply.result;
}

test.beforeEach(async ({ page }) => {
  const project = await gitProject(page, 'team-tabs');
  const reference = await page.request.post(`/api/project-brief-references/${project.id}`, { headers: project.headers, data: { caption: 'Archive layout notes', guidance: 'Keep the restore preview separate from writing files.', url: 'https://example.com/archive-notes' } });
  expect(reference.ok(), await reference.text()).toBeTruthy();
  const brief = await page.request.post(`/api/projects/${project.id}/brief`, { headers: project.headers, data: { version: 0, references: [(await reference.json()).reference_id], fields: { product: 'A private archive for a small team', people_problem: 'Morgan and the workshop team need to recover their notes.', run_mode: 'prepare' } } });
  expect(brief.ok(), await brief.text()).toBeTruthy();
  const made = await page.request.post('/api/sessions', { headers: project.headers, data: {
    project_id: project.id, provider: 'claude', name: 'Morgan lead', worktree: true,
    orchestrator: { brief_version: 1, run_mode: 'prepare', agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 2 },
  } });
  expect(made.status()).toBe(201); id = (await made.json()).id;
  const current = await (await page.request.get(`/api/teams/${id}/plan`)).json();
  const plan = await page.request.patch(`/api/teams/${id}/plan`, { headers: project.headers, data: { version: current.version,
    idea: 'Keep workshop notes in one private archive.', scope: { in: ['Local archive', 'Restore preview'], out: ['Public sharing'], parked: [] },
    phases: [{ key: 'archive', title: 'Save the workshop notes' }, { key: 'restore', title: 'Preview a restore', depends_on: ['archive'] }],
    constraints: [{ text: 'Keep all notes private.' }], requirements: [{ text: 'Preview a restore before writing files.' }],
  } });
  expect(plan.ok(), await plan.text()).toBeTruthy();
  await team('now', { text: 'Reviewing the archive plan; waiting for your recovery expectations.' });
  await team('worklist_add', { kind: 'task', title: 'Check the restore preview flow' });
  await team('brief');
  await team('brief_interpret', { brief_version: 1, text: 'Start with a private archive and a restore preview. Confirm recovery expectations before building.',
    references: { 'REF-1': { status: 'unreadable', note: 'Synthetic link; no remote content fetched in this test.' } } });
});
test.afterEach(async ({ page }) => { if (id) await page.request.post(`/api/sessions/${id}/team/stop`, { headers: await csrfHeaders(page) }); });

test('Team tabs support deep links, keyboard navigation and phone layout', async ({ page }, info) => {
  await page.goto(`/sessions/${id}?tab=team#overview`);
  const tabs = page.getByRole('tablist', { name: 'Team sections' });
  await expect(tabs.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: 'Plan', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Pinned project brief · version 1' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Brief references' })).toContainText('Archive layout notes');
  await expect(page.getByRole('region', { name: 'Dependency diagram' })).toBeVisible();
  await expectAxeClean(page);
  const viewport = page.viewportSize()!;
  if (!info.project.name.endsWith('phone')) await page.setViewportSize({ width: 1440, height: 2800 });
  await page.screenshot({ path: info.project.name.endsWith('phone') ? info.outputPath('overview-phone.png') : wikiShotPath(info, 'orchestrator-team-overview.png'), fullPage: true });
  await page.setViewportSize(viewport);
  await tabs.getByRole('tab', { name: 'Overview', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page).toHaveURL(/#worklist$/);
  await expect(page.getByRole('region', { name: 'Now', exact: true })).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(page).toHaveURL(/#settings$/);
  await expandTeamPanels(page);
  await expect(page.getByRole('combobox', { name: 'Run mode', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop team', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Number reservations' })).toContainText('No migration or decision numbers reserved');
  const modeTop = (await page.getByRole('combobox', { name: 'Run mode', exact: true }).boundingBox())!.y;
  const treeTop = (await page.getByRole('tree', { name: 'Live agent tree', exact: true }).boundingBox())!.y;
  expect(modeTop).toBeLessThan(treeTop);
  await expectAxeClean(page);
  if (!info.project.name.endsWith('phone')) await page.setViewportSize({ width: 1440, height: 2200 });
  await page.screenshot({ path: wikiShotPath(info, `orchestrator-team-settings${info.project.name.endsWith('phone') ? '-phone' : ''}.png`), fullPage: true });
  await page.setViewportSize(viewport);
  await page.goto(`/sessions/${id}?tab=team#worklist`);
  await expect(tabs.getByRole('tab', { name: 'Working list', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: wikiShotPath(info, `orchestrator-team-worklist${info.project.name.endsWith('phone') ? '-phone' : ''}.png`), fullPage: true });
});

test('decision Approve, Change and Discuss preserve card revision and open discussion', async ({ page }, info) => {
  const headers = await csrfHeaders(page);
  const url = `/api/teams/${id}/worklist/items`;
  const add = async (title: string) => {
    const response = await page.request.post(url, { headers, data: { kind: 'decision', title,
      options: [{ id: 'local', label: 'Local storage' }, { id: 'remote', label: 'Remote storage' }],
      recommendation: { option: 'local', reason: 'Keep the first release small.' } } });
    expect(response.status()).toBe(201); return response.json();
  };
  const first = await add('Choose a store');
  await page.goto(`/sessions/${id}?tab=team#worklist`);
  const card = page.getByRole('listitem', { name: 'Choose a store', exact: true });
  await card.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(card).toContainText('Answer from');
  const second = await add('Choose an archive');
  await page.reload();
  const change = page.getByRole('listitem', { name: 'Choose an archive', exact: true });
  await change.getByRole('button', { name: 'Change', exact: true }).click();
  await change.getByRole('textbox', { name: 'Different answer', exact: true }).fill('Use an independent archive');
  await change.getByRole('button', { name: 'Save answer', exact: true }).click();
  await expect(change).toContainText('Use an independent archive');
  const third = await add('Choose a recovery plan');
  await page.reload();
  const discuss = page.getByRole('listitem', { name: 'Choose a recovery plan', exact: true });
  await discuss.getByRole('button', { name: 'Discuss', exact: true }).click();
  await discuss.getByRole('textbox', { name: 'Message to lead', exact: true }).fill('What happens if the machine is offline?');
  await discuss.getByRole('button', { name: 'Send to lead', exact: true }).click();
  await expect(discuss.getByRole('link', { name: 'Open lead conversation' })).toBeVisible();
  await team('worklist_update', { item: third.id, note: 'Keep a local copy until the machine reconnects.' });
  await expect(discuss).toContainText("Lead's note: Keep a local copy until the machine reconnects.");
  const list = await (await page.request.get(`/api/teams/${id}/worklist`)).json();
  expect(list.items.find((row: any) => row.id === first.id).answer).toBe('local');
  expect(list.items.find((row: any) => row.id === second.id).revision).toBeGreaterThan(second.revision);
  const open = list.items.find((row: any) => row.id === third.id);
  expect(open.state).toBe('open'); expect(open.answer).toBeNull(); expect(open.links[0].kind).toBe('delivery');
  await expectAxeClean(page);
  if (!info.project.name.endsWith('phone')) await page.setViewportSize({ width: 1440, height: 1600 });
  await page.screenshot({ path: info.project.name.endsWith('phone') ? info.outputPath('decisions-phone.png') : wikiShotPath(info, 'orchestrator-team-decisions.png'), fullPage: true });
});


test('the retained team keeps limits accessible when orchestration controls are hidden', async ({ page }) => {
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.instance.orchestrator = false;
    await route.fulfill({ response, json: body });
  });
  await page.goto(`/sessions/${id}?tab=team#settings`);
  await expect(page.getByRole('heading', { name: 'Team limits', exact: true })).toBeVisible();
  await expect(page.getByRole('tablist', { name: 'Team sections' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Stop team', exact: true })).toBeVisible();
  await expectAxeClean(page);
});

test('Overview and Settings panels collapse, persist per viewer, and expand through deep links', async ({ page }, ti) => {
  await page.goto(`/sessions/${id}?tab=team`);
  for (const title of ['Project brief', 'Plan']) {
    await expect(page.getByRole('button', { name: title, exact: true })).toHaveAttribute('aria-expanded', 'false');
  }
  const plan = page.getByRole('button', { name: 'Plan', exact: true });
  await plan.focus();
  await page.keyboard.press('Enter');
  await expect(plan).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('region', { name: 'Idea', exact: true })).toBeVisible();
  await page.reload();
  await expect(plan).toHaveAttribute('aria-expanded', 'true');
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), username: 'Taylor Example' } });
  });
  await page.reload();
  await expect(plan).toHaveAttribute('aria-expanded', 'false');
  await page.unroute('**/api/auth/state');
  await page.reload();
  await expect(plan).toHaveAttribute('aria-expanded', 'true');
  await plan.press('Space');
  await expect(plan).toHaveAttribute('aria-expanded', 'false');
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  const titles = ['Run mode', 'Live agent tree', 'Gate queue', 'Number reservations', 'Team limits', 'Team members', 'Decision log', 'Selection reasons'];
  for (const title of titles) await expect(page.getByRole('button', { name: title, exact: true })).toHaveAttribute('aria-expanded', 'false');
  const panels = page.locator('[data-team-panel]');
  await expect(panels.last()).toHaveAttribute('data-team-panel', 'selection-reasons');
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(ti, `orchestrator-team-settings${ti.project.name.endsWith('phone') ? '-phone' : ''}.png`), fullPage: true });
  for (const [anchor, title] of [['run-mode', 'Run mode'], ['agent-tree', 'Live agent tree'],
    ['gate-queue', 'Gate queue'], ['reservations', 'Number reservations'], ['team-limits', 'Team limits'],
    ['team-members', 'Team members'], ['decision-log', 'Decision log'], ['selection-reasons', 'Selection reasons'],
    ['brief', 'Project brief'], ['plan', 'Plan']]) {
    await page.goto(`/sessions/${id}?tab=team#${anchor}`);
    const toggle = page.getByRole('button', { name: title, exact: true });
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(toggle, JSON.stringify(await page.evaluate(() => ({
      tag: document.activeElement?.tagName, id: document.activeElement?.id,
      label: document.activeElement?.getAttribute('aria-label'), focused: document.hasFocus(),
    })))).toBeFocused();
  }
  await page.goto(`/sessions/${id}?tab=team#overview`);
  await expect(page.getByRole('button', { name: 'Project brief', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(plan).toHaveAttribute('aria-expanded', 'true');
  await page.getByRole('button', { name: 'Project brief', exact: true }).click();
  await plan.click();
  await page.screenshot({ path: wikiShotPath(ti, `orchestrator-team-overview${ti.project.name.endsWith('phone') ? '-phone' : ''}.png`), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('answered decisions have their own section below open cards and can be reopened', async ({ page }) => {
  const answered = await team('worklist_add', { kind: 'decision', title: 'Keep a restore preview?', options: [{ id: 'yes', label: 'Yes' }, { id: 'no', label: 'No' }], recommendation: { option: 'yes', reason: 'Avoid surprises.' } });
  await team('worklist_add', { kind: 'question', title: 'How many notes need recovery?' });
  await page.goto(`/sessions/${id}?tab=team#worklist`);
  const row = page.getByRole('listitem').filter({ has: page.getByRole('heading', { name: 'Keep a restore preview?', exact: true }) });
  await row.getByRole('button', { name: 'Approve', exact: true }).click();
  const section = page.getByRole('region', { name: 'Answered', exact: true });
  await expect(section).toContainText('Keep a restore preview?');
  await expect(section).toContainText(/Answer from .+ · .+\d{2}:\d{2}/);
  await expect(section).toContainText('Yes');
  await expect(page.getByRole('region', { name: 'What the lead needs from you', exact: true })).toContainText('How many notes');
  await section.getByRole('button', { name: 'Reopen', exact: true }).click();
  await expect(page.getByRole('region', { name: 'What the lead needs from you', exact: true })).toContainText('Keep a restore preview?');
  const current = await (await page.request.get(`/api/teams/${id}/worklist`)).json();
  expect(current.items.find((item: { id: number }) => item.id === answered.id).state).toBe('open');
});


test('collapsing a panel preserves its draft through polling when preference storage is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    const read = Storage.prototype.getItem;
    const write = Storage.prototype.setItem;
    Storage.prototype.getItem = function(key) {
      if (key.startsWith('acc-team-panel:')) throw new Error('Preference storage unavailable');
      return read.call(this, key);
    };
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith('acc-team-panel:')) throw new Error('Preference storage unavailable');
      return write.call(this, key, value);
    };
  });
  await page.goto(`/sessions/${id}?tab=team`);
  const toggle = page.getByRole('button', { name: 'Plan', exact: true });
  await toggle.click();
  await page.getByRole('button', { name: 'Edit plan', exact: true }).click();
  const idea = page.getByRole('form', { name: 'Edit the plan', exact: true }).getByRole('textbox', { name: 'Idea', exact: true });
  await idea.fill('Keep this unsaved archive idea.');
  await toggle.click();
  await expect(idea).toBeHidden();
  await page.waitForResponse(response => response.request().method() === 'GET' && new URL(response.url()).pathname === `/api/sessions/${id}/team`);
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await toggle.click();
  await expect(idea).toHaveValue('Keep this unsaved archive idea.');
});

test('unknown inherited-object fragments fall back to Overview', async ({ page }) => {
  for (const anchor of ['constructor', 'toString', '__proto__']) {
    await page.goto(`/sessions/${id}?tab=team#${anchor}`);
    await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toHaveAttribute('aria-expanded', 'false');
  }
});
