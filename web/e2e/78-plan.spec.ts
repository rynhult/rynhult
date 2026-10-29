import { expect, test, type Page } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, gitProject, wikiShotPath } from './helpers';

// Orchestrator v2, slice 6: the plan and roadmap on the Team tab. The browser suite's server runs with orchestrator v2
// off, so the first test checks it stays hidden; the others answer the plan API in the page (route) to drive the UI.
test.use({ storageState: AUTH_FILE });

async function teamLead(page: Page) {
  const { id, headers } = await gitProject(page, 'plan');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Riley lead',
    worktree: true, orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 2 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  return (await created.json()).id as string;
}

const at = new Date().toISOString();
const phase = (key: string, title: string, status: string, depends_on: string[], blocked_by: string[], extra: object = {}) =>
  ({ key, title, summary: '', check: '', status, depends_on, blocked_by, ...extra });

test('the plan stays hidden while orchestrator v2 is off', async ({ page }) => {
  const lead = await teamLead(page);
  const live = await page.request.get(`/api/teams/${lead}/plan`);
  test.skip(live.status() !== 404, 'this server runs with orchestrator v2 on');
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await expect(page.getByText('Team limits', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Plan', exact: true })).toHaveCount(0);
});

test('a planning session says no helpers start yet', async ({ page }) => {
  const lead = await teamLead(page);
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: {
    state: 'planning', idea: '', version: 0, ready_at: null, scope: { in: [], out: [], parked: [] }, phases: [], person_edits: {},
    open_questions: [], mermaid: 'flowchart TD', history: [] } }));
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await expect(page.getByText('Planning session: no helpers start until the lead finishes the plan.')).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'The lead is still planning with you' })).toBeVisible();
});

const MERMAID = [
  'flowchart TD',
  '  p0["Database schema · done"]', '  p1["Importer · active"]', '  p2["Import page · planned"]', '  p3["Excel support · dropped"]',
  '  p0 --> p1', '  p1 --> p2', '  p1 --> p3',
].join('\n');

function readyPlan() {
  return {
    state: 'ready', idea: 'Import customer lists from CSV files into the address book.', version: 3, ready_at: at,
    scope: { in: ['CSV import', 'A preview before saving'], out: ['Excel files'], parked: ['Scheduled imports'] },
    person_edits: { 'phase:excel': { version: 3, by: 'alex' } } as Record<string, { version: number; by: string }>,
    phases: [
      phase('schema', 'Database schema', 'done', [], [], { check: 'Migration tests' }),
      phase('importer', 'Importer', 'active', ['schema'], [], { summary: 'Parse, validate and stage rows.' }),
      phase('ui', 'Import page', 'planned', ['importer'], ['importer']),
      phase('excel', 'Excel support', 'dropped', ['importer'], ['importer']),
    ],
    open_questions: [{ id: 7, kind: 'decision', title: 'Which delimiter by default?' }],
    mermaid: MERMAID,
    history: [{ version: 3, reason: 'Excel moved out of scope', actor: 'alex', at },
              { version: 2, reason: 'Planning session with the person', actor: 'session:lead', at },
              { version: 1, reason: 'First draft', actor: 'session:lead', at }],
  };
}

test('person reads the roadmap as a diagram, its dependencies, open questions and history', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: readyPlan() }));
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const roadmap = page.getByRole('region', { name: 'Roadmap' });
  await expect(page.getByText('Version 3. Answer its open questions', { exact: false })).toBeVisible();
  // The diagram is SVG drawn in the browser, one node per phase and an edge per dependency.
  const diagram = page.getByRole('region', { name: 'Dependency diagram' }).getByRole('img', { name: /Roadmap diagram: 4 phases/ });
  await expect(diagram.locator('svg')).toBeVisible({ timeout: 15_000 });  // Mermaid loads on first use; slow on a busy machine
  await expect(diagram.locator('svg')).toContainText('Import page');  // SVG text wraps, so not the whole label
  await expect(diagram.locator('svg .node')).toHaveCount(4);
  await expect(roadmap.getByRole('listitem', { name: 'Database schema' })).toContainText('Done');
  await expect(roadmap.getByRole('listitem', { name: 'Importer' })).toContainText('Ready to start');
  await expect(roadmap.getByRole('listitem', { name: 'Import page' })).toContainText('Waits for Importer');
  await expect(roadmap.getByRole('listitem', { name: 'Excel support' })).toContainText('Dropped');
  await expect(roadmap.getByRole('listitem', { name: 'Excel support' })).toContainText('Edited by alex · version 3');
  await expect(roadmap.getByRole('listitem', { name: 'Excel support' })).not.toContainText('Waits for');
  await expect(page.getByRole('region', { name: 'Scope' })).toContainText('Scheduled imports');
  await expect(page.getByRole('region', { name: 'Open questions' })).toContainText('Which delimiter by default?');
  await page.getByText('Plan history (3 versions)').click();
  await expect(page.getByText('Excel moved out of scope')).toBeVisible();
  await expect(page.getByText('Version 2 · the lead', { exact: false })).toBeVisible();
  await expectAxeClean(page);
  // The app scrolls inside its own pane, so capture the Plan card itself in a viewport tall enough to hold it.
  await page.setViewportSize({ width: 1440, height: 1900 });
  const card = page.getByRole('heading', { name: 'Plan', exact: true }).locator('xpath=ancestor::section[1]');
  await card.scrollIntoViewIfNeeded();
  await card.screenshot({ path: wikiShotPath(testInfo, 'orchestrator-plan.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});

test('a hostile diagram source renders no script, HTML, handler or link', async ({ page }) => {
  const lead = await teamLead(page);
  const hostile = readyPlan();
  hostile.mermaid = [
    '%%{init: {"securityLevel": "loose", "flowchart": {"htmlLabels": true}}}%%',
    'flowchart TD',
    '  p0["<img src=x onerror=window.__pwned=1><script>window.__pwned=2</script>Schema"]',
    '  p1["<a href=javascript:window.__pwned=3>Importer</a>"]',
    '  p0 --> p1',
    '  click p0 call window.__pwnedCall()',
    '  click p1 href "javascript:window.__pwned=4"',
  ].join('\n');
  await page.addInitScript(() => { (window as unknown as { __pwnedCall: () => void }).__pwnedCall = () => { (window as unknown as { __pwned: number }).__pwned = 5; }; });
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: hostile }));
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const diagram = page.getByRole('region', { name: 'Dependency diagram' }).getByRole('img', { name: /Roadmap diagram/ });
  await expect(diagram.locator('svg')).toBeVisible({ timeout: 15_000 });  // Mermaid loads on first use; slow on a busy machine
  await diagram.locator('svg .node').first().click({ force: true });
  await diagram.locator('svg .node').last().click({ force: true });
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  for (const selector of ['script', 'img', 'a', 'foreignObject', '[onerror]', '[onclick]', '[href]']) {
    await expect(diagram.locator(selector)).toHaveCount(0);
  }
  expect(await diagram.innerHTML()).not.toContain('javascript:');
});

test('a diagram directive cannot restyle the diagram or the page', async ({ page }) => {
  // Review 2026-10-26: securityLevel strict still applies an init directive's themeCSS, fonts and theme variables.
  const lead = await teamLead(page);
  const hostile = readyPlan();
  hostile.mermaid = [
    '%%{init: {"themeCSS": ".node rect { fill: rgb(1, 2, 3) !important; } svg { position: fixed; inset: 0; }", '
      + '"themeVariables": {"primaryColor": "#010203"}, "fontFamily": "Pwned"}}%%',
    'flowchart TD',
    '  p0["Schema"]',
    '  p1["Importer"]',
    '  p0 --> p1',
  ].join('\n');
  await page.route(`**/api/teams/${lead}/plan`, (route) => route.fulfill({ json: hostile }));
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const diagram = page.getByRole('region', { name: 'Dependency diagram' }).getByRole('img', { name: /Roadmap diagram/ });
  await expect(diagram.locator('svg .node').first()).toBeVisible({ timeout: 15_000 });  // Mermaid loads on first use; slow on a busy machine
  const styled = await diagram.locator('svg .node rect, svg .node polygon, svg .node path').first()
    .evaluate((shape) => getComputedStyle(shape).fill);
  expect(styled).not.toBe('rgb(1, 2, 3)');
  expect(await diagram.locator('svg').first().evaluate((svg) => getComputedStyle(svg).position)).not.toBe('fixed');
  expect(await diagram.innerHTML()).not.toContain('Pwned');
  expect(await diagram.innerHTML()).not.toContain('#010203');
});

test('person edits the plan as a new version, and a conflict keeps the edit to re-apply', async ({ page }) => {
  const lead = await teamLead(page);
  const current = readyPlan();
  const sent: Record<string, unknown>[] = [];
  let conflictOnce = true;
  await page.route(`**/api/teams/${lead}/plan`, async (route) => {
    const request = route.request();
    if (request.method() === 'GET') return route.fulfill({ json: current });
    const body = request.postDataJSON();
    sent.push(body);
    if (conflictOnce) {
      conflictOnce = false;
      current.version = 4; current.idea = 'Import customer lists (the lead refined it).';
      current.history.unshift({ version: 4, reason: 'refined', actor: 'session:lead', at });
      return route.fulfill({ status: 409, json: { detail: 'the plan changed since you loaded it', plan: current } });
    }
    if (body.idea) current.idea = body.idea;
    current.version += 1;
    current.person_edits = { ...current.person_edits, idea: { version: current.version, by: 'alex' } };
    current.history.unshift({ version: current.version, reason: body.reason ?? 'Edited by alex', actor: 'alex', at });
    return route.fulfill({ json: current });
  });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.getByRole('button', { name: 'Edit plan' }).click();
  const form = page.getByRole('form', { name: 'Edit the plan' });
  await form.getByLabel('Idea').fill('Import customer lists from CSV and TSV files.');
  await form.getByRole('button', { name: 'Save as a new version' }).click();
  // The edit is kept (owner decision 2026-10-26), next to what changed meanwhile.
  const conflict = page.getByRole('status').filter({ hasText: 'The plan changed while you were editing' });
  await expect(conflict).toBeVisible();
  await expect(conflict).toContainText('it is now version 4, by the lead');
  await expect(conflict).toContainText('Idea is now: Import customer lists (the lead refined it).');
  await expect(form.getByLabel('Idea')).toHaveValue('Import customer lists from CSV and TSV files.');
  expect(sent[0]).toEqual({ version: 3, idea: 'Import customer lists from CSV and TSV files.' });  // only what changed

  await form.getByLabel('Why (shown in the plan history)').fill('TSV too');
  await expectAxeClean(page);
  await form.getByRole('button', { name: 'Save as a new version' }).click();
  await expect(page.getByRole('region', { name: 'Idea' })).toContainText('Edited by alex · version 5');
  expect(sent[1]).toEqual({ version: 4, idea: 'Import customer lists from CSV and TSV files.', reason: 'TSV too' });
});

test('a card shows the default the lead goes ahead with until you answer', async ({ page }, testInfo) => {
  const lead = await teamLead(page);
  const list = { now: '', now_updated_at: null, now_updated_by: null, revision: 1, idle_minutes: 5, items: [{
    id: 7, position: 1, kind: 'decision', title: 'Which delimiter by default?', steps: '', state: 'open', revision: 1,
    options: [{ id: 'comma', label: 'Comma', consequence: 'Most files' }, { id: 'detect', label: 'Detect it', consequence: 'Slower' }],
    recommendation: { option: 'comma', reason: 'Most customer files use commas.' }, default: { option: 'comma' },
    in_effect: { from: 'default', value: 'comma' }, answer: null, answered_by: null, answered_at: null, ingested_at: null,
    done_at: null, note: null, source: 'lead', owner_edited: false, links: [], created_at: at, updated_at: at }] };
  await page.route(`**/api/teams/${lead}/worklist`, (route) => route.fulfill({ json: list }));
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const card = page.getByRole('region', { name: 'What the lead needs from you' }).getByRole('listitem', { name: 'Which delimiter by default?' });
  await expect(card.getByText('Default until you answer')).toBeVisible();
  await expect(card).toContainText('The lead goes ahead with Comma. Your answer replaces it.');
  await expectAxeClean(page);
  await card.screenshot({ path: wikiShotPath(testInfo, 'orchestrator-worklist-default.png') });
});
