import { expect, test, type Page } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { AUTH_FILE, control, gitProject, info, wikiShotPath } from './helpers';

// The Guide's "Orchestrate a project" walkthrough (docs/wiki/User-Guide/Orchestrate-a-Project.md), one test per step,
// each saving its screenshot. The browser suite's server runs with the Orchestrator v2 on (tests/e2e_server.py)
// and fake providers; the team's plan, working list, tree and selections are answered in the page (route), with
// artificial names only, because the wiki is public.
test.use({ storageState: AUTH_FILE });

async function teamLead(page: Page) {
  const { id, headers } = await gitProject(page, 'orchestrate');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Lead',
    worktree: true, orchestrator: { agents: [{ agent: 'claude', profile: null }, { agent: 'codex', profile: null }],
      machines: ['local'], at_once: 3, total: 10, depth: 2, hours: 8 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  return (await created.json()).id as string;
}

const at = new Date().toISOString();
const item = (id: number, position: number, kind: string, title: string, extra: object = {}) => ({
  id, position, kind, title, steps: '', options: null, recommendation: null, default: null, in_effect: null, state: 'open',
  answer: null, answered_by: null, answered_at: null, ingested_at: null, done_at: null, note: null, source: 'lead',
  owner_edited: false, links: [], revision: 1, created_at: at, updated_at: at, ...extra });

function planningList() {
  return {
    now: 'Planning the address book import with you. Draft roadmap is on the Plan card; three questions are open.',
    now_updated_at: at, now_updated_by: 'lead', revision: 4, idle_minutes: 5,
    items: [
      item(1, 1, 'decision', 'Which file formats in the first version?', {
        options: [{ id: 'csv', label: 'CSV only', consequence: 'Ships first; Excel users export to CSV' },
                  { id: 'both', label: 'CSV and Excel', consequence: 'About two more days' }],
        recommendation: { option: 'csv', reason: 'Most customer files are CSV; Excel can follow as its own phase.' },
        default: { option: 'csv' }, in_effect: { from: 'default', value: 'csv' } }),
      item(2, 2, 'decision', 'What happens to duplicate contacts?', {
        options: [{ id: 'skip', label: 'Skip them', consequence: 'Nothing is overwritten' },
                  { id: 'merge', label: 'Merge them', consequence: 'Needs a review screen' }],
        recommendation: { option: 'skip', reason: 'Safe by default; merging can be added later.' },
        default: { option: 'skip' }, in_effect: { from: 'default', value: 'skip' } }),
      item(3, 3, 'question', 'Who will use the import page?', {
        default: { text: 'Office staff, a handful of people' }, in_effect: { from: 'default', value: 'Office staff, a handful of people' } }),
      item(4, 4, 'task', 'Write the roadmap to docs/ROADMAP.md'),
    ],
  };
}

const MERMAID = ['flowchart TD', '  p0["Contact schema · planned"]', '  p1["CSV importer · planned"]', '  p2["Import page · planned"]',
  '  p0 --> p1', '  p1 --> p2'].join('\n');
const phase = (key: string, title: string, depends_on: string[], blocked_by: string[], extra: object = {}) =>
  ({ key, title, summary: '', check: '', status: 'planned', depends_on, blocked_by, ...extra });

function planningPlan() {
  return {
    state: 'planning', idea: 'Let office staff import customer lists into the address book.', version: 2, ready_at: null,
    scope: { in: ['Import from a file', 'A preview before saving'], out: ['Syncing with other systems'], parked: ['Scheduled imports'] },
    person_edits: {},
    phases: [phase('schema', 'Contact schema', [], [], { check: 'Migration tests' }),
             phase('importer', 'CSV importer', ['schema'], ['schema'], { summary: 'Read, check and stage rows.' }),
             phase('page', 'Import page', ['importer'], ['importer'])],
    open_questions: [{ id: 1, kind: 'decision', title: 'Which file formats in the first version?' },
                     { id: 2, kind: 'decision', title: 'What happens to duplicate contacts?' },
                     { id: 3, kind: 'question', title: 'Who will use the import page?' }],
    mermaid: MERMAID,
    history: [{ version: 2, reason: 'Scope and phases after our first talk', actor: 'session:lead', at },
              { version: 1, reason: 'First draft', actor: 'session:lead', at }],
  };
}

async function planning(page: Page, lead: string) {
  const list = planningList();
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: planningPlan() }));
  await page.route(`**/api/teams/${lead}/worklist**`, async (route) => {
    const request = route.request();
    if (request.method() === 'GET') return route.fulfill({ json: list });
    const body = request.postDataJSON();
    const target = list.items.find((x) => x.id === Number(new URL(request.url()).pathname.split('/').pop()))!;
    Object.assign(target, body, { revision: target.revision + 1 });
    if ('answer' in body) Object.assign(target, { state: 'answered', answered_by: 'alex', answered_at: at,
                                                  in_effect: { from: 'answer', value: body.answer } });
    list.revision += 1;
    return route.fulfill({ json: target });
  });
}

test('step 2: the Team tab during the planning session', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await planning(page, lead);
  await page.setViewportSize({ width: 1440, height: 2300 });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await expect(page.getByText('Planning session: no helpers start until the lead finishes the plan.')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Dependency diagram' }).getByRole('img', { name: /Roadmap diagram/ }).locator('svg')).toBeVisible({ timeout: 15_000 });
  await page.getByRole('tab', { name: 'Working list', exact: true }).click();
  const yours = page.getByRole('region', { name: 'What the lead needs from you' });
  await expect(yours.getByRole('listitem', { name: 'Which file formats in the first version?' })).toContainText('Default until you answer');
  await page.screenshot({ path: testInfo.outputPath('orchestrate-planning.png') });
});

test('step 3: answering one card while the others keep their defaults', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await planning(page, lead);
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const card = page.getByRole('region', { name: 'What the lead needs from you' })
    .getByRole('listitem', { name: 'Which file formats in the first version?' });
  await card.screenshot({ path: testInfo.outputPath('orchestrate-answer.png') });
  await card.getByRole('button', { name: 'CSV and Excel', exact: true }).click();
  await expect(page.getByText('Answer from alex', { exact: false }).first()).toBeVisible();
});

test("step 4: talking to the lead in its own terminal", async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await page.goto(`/sessions/${lead}`);
  await expect(page.locator('.xterm')).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Team' })).toBeVisible();
  // What a conversation with the lead looks like, printed by the fake provider (its "screen" control).
  writeFileSync(`${info().run_dir}/${lead}/fake.screen`, [
    '\x1b[2J\x1b[H',
    '\x1b[1m> \x1b[0mCan the import page show how many rows it will add before I confirm?',
    '',
    '\x1b[36m●\x1b[0m Yes. I added it to the "Import page" phase as a check: the preview shows the row count',
    '  and any rows it will skip. Plan version 4, reason "row count in the preview".',
    '  I also added a note to the working list so it is on record.',
    '',
    '\x1b[1m> \x1b[0m',
  ].join('\r\n'));
  control(lead, 'screen');
  await expect(page.locator('.xterm-rows')).toContainText('Plan version 4');
  await page.screenshot({ path: wikiShotPath(testInfo, 'orchestrate-talk.png') });
});

test('step 5: helpers chosen across agents, machines and quota', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await planning(page, lead);
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: { ...planningPlan(), state: 'ready', ready_at: at } }));
  const member = (id: string, parent: string | null, depth: number, name: string, agent: string, machine: string,
                  observed: string, extra: object = {}) => ({
    id, parent, depth, name, agent, profile: depth ? 'backend' : null, model: agent === 'codex' ? 'example-coder' : 'example-large', machine, observed,
    needs_you: observed === 'waiting_input', activity: null, archived: false, merged: false, selection_reason: null,
    report_status: null, deliveries: [], ...extra });
  const tree = { cursor: 1, members: [
    member(lead, null, 0, 'Lead', 'claude', 'local', 'active'),
    member('a'.repeat(12), lead, 1, 'Schema', 'codex', 'local', 'active',
           { selection_reason: 'First allowed choice for implementation; Codex has 80% quota headroom' }),
    member('b'.repeat(12), lead, 1, 'Importer', 'claude', 'build-1', 'waiting_input',
           { selection_reason: 'Codex skipped: its quota is over your 90% limit' }),
    member('c'.repeat(12), lead, 1, 'Schema review', 'claude', 'local', 'idle',
           { selection_reason: 'A review runs as a separate helper from the author' }),
  ] };
  await page.route(`**/api/teams/${lead}/tree`, (route) => route.fulfill({ json: tree }));
  await page.route(`**/api/teams/${lead}/events?*`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 20_000));  // a quiet live feed: nothing changes while we look
    await route.fulfill({ json: { events: [], cursor: 1, has_more: false } }).catch(() => undefined);
  });
  const candidate = (agent: string, machine: string, score: number, quota: object, extra: object = {}) => ({
    agent, profile: 'backend', model: agent === 'codex' ? 'example-coder' : 'example-large', machine, permission: 'implement', quota, score,
    reasons: ['Backend profile for a backend task', 'Owner allow-list preference'], eligible: true, rejections: [],
    est_cost: { value: 1, unit: 'usage_percent', source: 'price-class heuristic' }, ...extra });
  await page.route(`**/api/teams/${lead}/selections?*`, (route) => route.fulfill({ json: { selections: [{
    id: 's'.repeat(32), need: { task_type: 'implementation', role: 'backend' }, state: 'started', chosen: 0,
    reason: 'First available allow-list choice within the grant and budget and fresh provider quota',
    override_reason: null, budget: { providers: { codex: { remaining: 40 } } }, candidates: [
      candidate('claude', 'build-1', 62, { status: 'fresh', used_percent: 35, headroom_percent: 65 }),
      candidate('codex', 'local', 70, { status: 'fresh', used_percent: 94, headroom_percent: 6 },
                { eligible: false, rejections: ['fresh provider quota reaches owner threshold'] }),
    ] }] } }));
  await page.setViewportSize({ width: 1440, height: 2600 });
  await page.goto(`/sessions/${lead}?tab=team#settings`);
  const treeView = page.getByRole('tree');
  await expect(treeView.getByRole('treeitem', { name: /Importer/ })).toBeVisible();
  await treeView.getByRole('treeitem', { name: /Importer/ }).focus();
  await expect(page.getByRole('region', { name: 'Selected agent' })).toContainText('quota is over your 90% limit');
  await page.getByText('Candidates and filters').first().click();
  const tree2 = page.getByRole('heading', { name: 'Selection reasons', exact: true }).locator('xpath=ancestor::section[1]');
  await expect(tree2).toContainText('fresh provider quota reaches owner threshold');
  // Just the live tree and the selection card: the working list above them is the earlier steps' picture.
  const treeCard = page.getByRole('heading', { name: 'Live agent tree' }).locator('xpath=ancestor::section[1]');
  await treeCard.scrollIntoViewIfNeeded();
  const top = (await treeCard.boundingBox())!, bottom = (await tree2.boundingBox())!;
  await page.screenshot({ path: testInfo.outputPath('orchestrate-workers.png'),
    clip: { x: top.x - 8, y: top.y - 8, width: top.width + 16, height: bottom.y + bottom.height - top.y + 16 } });
});

test('step 5: the owner orders providers and models in Settings', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.goto('/settings#selection-allow-lists');
  const section = page.locator('#selection-allow-lists');  // the Settings section's anchor
  await expect(section.getByRole('heading', { name: 'Selection allow-lists' })).toBeVisible();
  await expect(section.getByText('Allow-list 2', { exact: true })).toBeVisible();
  // The top of the section: provider quota and the first allow-list (providers in order). The rest repeats it.
  await section.evaluate((element) => element.scrollIntoView({ block: 'start' }));
  const second = section.getByText('Allow-list 2', { exact: true });
  const box = (await section.boundingBox())!, end = (await second.boundingBox())!;
  await page.screenshot({ path: wikiShotPath(testInfo, 'orchestrate-allow-lists.png'),
    clip: { x: box.x, y: box.y, width: box.width, height: end.y - box.y - 4 } });
});

test('step 7: stopping the whole team', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await page.goto(`/sessions/${lead}?tab=team#settings`);
  await page.getByRole('button', { name: 'Stop team' }).click();
  const dialog = page.getByRole('dialog', { name: 'Stop team?' });
  await expect(dialog).toContainText('Stop the lead and every helper.');
  await page.screenshot({ path: wikiShotPath(testInfo, 'orchestrate-stop.png') });
  await dialog.getByRole('button', { name: 'Stop team' }).click();
  await expect(page.getByRole('heading', { name: 'Stop confirmed', exact: true })).toBeVisible();
});
