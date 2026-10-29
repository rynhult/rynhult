import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { environmentValue } from '../../src/acc/env.mjs';
import { expect, test } from '@playwright/test';
import { expandTeamPanels, AUTH_FILE, expectAxeClean, gitProject, info, openAdvanced } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const codexSignedIn of [false, true]) test(`new teams show task profiles and let the person trim them (Codex signed in: ${codexSignedIn})`, async ({ page }, testInfo) => {
  const { id } = await gitProject(page, 'profile-defaults');
  const choices = [
    ...['fullstack', 'frontend', 'backend', 'code-reviewer', 'qa'].map(profile => ({ agent: 'claude', profile, options: {} })),
    ...(codexSignedIn ? ['fullstack', 'backend'].map(profile => ({ agent: 'codex', profile, options: {} })) : []),
  ];
  await page.route('**/api/providers/registry?*', async route => route.fulfill({
    json: { ...await (await route.fetch()).json(), team_agents: choices },
  }));
  await page.goto('/sessions');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  const registry = page.waitForResponse(r => r.url().includes(`/api/providers/registry?project_id=${id}`));
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(id));
  await registry;
  await openAdvanced(dialog);
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await expect(dialog.getByRole('combobox', { name: /^Helper profile / })).toHaveCount(choices.length);
  for (const [index, choice] of choices.entries()) {
    await expect(dialog.getByRole('combobox', { name: `Helper profile ${index + 1}`, exact: true })).toHaveValue(choice.profile);
    await expect(dialog.getByRole('combobox', { name: `Helper agent ${index + 1}`, exact: true })).toHaveValue(choice.agent);
  }
  await expect(dialog.getByRole('combobox', { name: 'Helper 4: Permissions', exact: true })).toHaveValue('plan');
  await expect(dialog.getByText('Assignments select the matching role from these allowed profiles. Remove choices you do not want.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Remove helper choice 5', exact: true }).click();
  await expect(dialog.getByRole('combobox', { name: /^Helper profile / })).toHaveCount(choices.length - 1);
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  if (codexSignedIn) {
    await dialog.getByText('Assignments select the matching role from these allowed profiles. Remove choices you do not want.').scrollIntoViewIfNeeded();
    await dialog.screenshot({ path: testInfo.outputPath('orchestrator-default-profiles.png') });
  }
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  const lead = page.url().split('/').pop();
  const team = (await (await page.request.get(`/api/sessions/${lead}/team`)).json()).limits;
  expect(team.agents.map((a: { agent: string; profile: string }) => [a.agent, a.profile]))
    .toEqual(choices.filter(c => c.profile !== 'qa').map(c => [c.agent, c.profile]));
});

test('person records boundaries and switches a running prepare lead to build', async ({ page }, testInfo) => {
  const { id, headers } = await gitProject(page, 'planning-boundaries');
  const created = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', name: 'Riley prepares', permission_mode: 'auto', worktree: true,
    orchestrator: { run_mode: 'prepare', agents: [{ agent: 'claude', profile: null }], machines: ['local'],
      at_once: 2, total: 4, depth: 1, hours: 2 },
  } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const lead = (await created.json()).id;
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const boundaries = page.getByRole('region', { name: 'Constraints and requirements', exact: true });
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await expect(page.getByRole('heading', { name: 'Run mode: Prepare', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expandTeamPanels(page);
  await boundaries.getByRole('button', { name: 'Add constraint', exact: true }).click();
  const constraint = boundaries.getByRole('form', { name: 'Edit constraints', exact: true });
  await constraint.getByLabel('Text', { exact: true }).fill('Keep customer files on this machine.');
  await constraint.getByLabel('Reason', { exact: true }).fill('Privacy boundary');
  await constraint.getByRole('button', { name: 'Save boundary', exact: true }).click();
  await expect(boundaries.getByRole('button', { name: 'Edit C-1', exact: true })).toBeVisible();
  await boundaries.getByRole('button', { name: 'Add requirement', exact: true }).click();
  const requirement = boundaries.getByRole('form', { name: 'Edit requirements', exact: true });
  await requirement.getByLabel('Text', { exact: true }).fill('Import a CSV file.');
  await requirement.getByLabel('Acceptance', { exact: true }).fill('Valid rows appear in the local preview.');
  await requirement.getByLabel('Reason', { exact: true }).fill('First release');
  await requirement.getByRole('button', { name: 'Save boundary', exact: true }).click();
  await expect(boundaries.getByRole('button', { name: 'Edit R-1', exact: true })).toBeVisible();
  await expectAxeClean(page);
  const downloadReady = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export roadmap', exact: true }).click();
  const download = await downloadReady;
  expect(download.suggestedFilename()).toBe('ROADMAP.md');
  await download.saveAs(testInfo.outputPath('ROADMAP.md'));
  const exported = readFileSync(testInfo.outputPath('ROADMAP.md'), 'utf8');
  expect(exported).toContain('C-1');
  expect(exported).toContain('Keep customer files on this machine.');
  expect(exported).toContain('R-1');
  expect(exported).toContain('Valid rows appear in the local preview.');
  await boundaries.screenshot({ path: testInfo.outputPath('orchestrator-plan-boundaries.png') });
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('build');
  await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Run mode: Build', exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Lead resumed' })).toContainText('Lead resumed');
  const resumed = await (await page.request.get(`/api/sessions/${lead}`)).json();
  expect(resumed.desired_state).toBe('running');
  expect(resumed.launch_spec.inputs.permission_mode).toBe('auto');
  expect(JSON.parse(resumed.launch_spec.files['settings.json']).permissions.defaultMode).toBe('auto');
  const notices = (await (await page.request.get(`/api/sessions/${lead}/deliveries`)).json()).deliveries;
  expect(notices.some((n: { preview: string }) => n.preview.includes('Run mode is now build') && n.preview.includes('Plan version'))).toBeTruthy();
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${lead}/deliveries`)).json()).deliveries
    .find((n: { preview: string }) => n.preview.includes('Run mode is now build'))?.state, { timeout: 15_000 }).toBe('acknowledged');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
});

test('switching a running build lead to prepare stops helpers first and resumes with read-only permissions', async ({ page }) => {
  const { lead, headers } = await buildLead(page);
  const original = await (await page.request.get(`/api/sessions/${lead}`)).json();
  const made = await page.request.post('/api/sessions', { headers, data: {
    project_id: original.project_id, provider: 'claude', name: 'Riley helper', worktree: true,
  } });
  expect(made.ok(), await made.text()).toBeTruthy();
  const helper = (await made.json()).id;
  // Only arrange an already-running member in this suite's disposable fixture DB.
  execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c',
    'import sqlite3,sys\nwith sqlite3.connect(sys.argv[1]) as db: db.execute("UPDATE session SET team_id=?,parent_session_id=? WHERE id=?",sys.argv[2:])',
    `${info().state}/data/acc.db`, lead, lead, helper]);
  const stopped: string[] = [];
  page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/stop')) stopped.push(new URL(r.url()).pathname); });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const boundaries = page.getByRole('region', { name: 'Constraints and requirements', exact: true });
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('prepare');
  await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Lead resumed' })).toContainText('Lead resumed');
  const resumed = await (await page.request.get(`/api/sessions/${lead}`)).json();
  expect(resumed.desired_state).toBe('running');
  expect(resumed.launch_spec.inputs.permission_mode).toBe('plan');
  expect(stopped[0]).toBe(`/api/sessions/${helper}/stop`);
  expect(stopped[1]).toBe(`/api/sessions/${lead}/stop`);
  const stoppedHelper = await (await page.request.get(`/api/sessions/${helper}`)).json();
  expect(stoppedHelper.desired_state).toBe('stopped');
  expect(stoppedHelper.team_stop_pending).toBe(false);
  await expect(page.getByRole('heading', { name: 'Run mode: Prepare', exact: true })).toBeVisible();
  await expectAxeClean(page);
});

test('a failed stop does not switch mode or resume and explains the failed step', async ({ page }) => {
  const { lead } = await buildLead(page);
  await page.route(`**/api/sessions/${lead}/stop`, route => route.fulfill({ status: 503, json: { detail: 'The test machine is offline.' } }));
  const subsequent: string[] = [];
  page.on('request', r => { if (r.method() === 'PATCH' || r.url().endsWith(`/${lead}/resume`)) subsequent.push(r.url()); });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const boundaries = page.getByRole('region', { name: 'Constraints and requirements', exact: true });
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('prepare');
  await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Stopping the lead failed');
  await expect(page.getByRole('alert')).toContainText('The test machine is offline.');
  expect(subsequent).toEqual([]);
});

for (const savedLatest of [false, true]) test(`a restored plan draft reviews the current plan version, not stale metadata (saved latest: ${savedLatest})`, async ({ page }) => {
  const { lead, headers } = await buildLead(page);
  const url = `/api/teams/${lead}/plan`;
  expect((await page.request.patch(url, { headers, data: { version: 0,
    phases: [{ key: 'booking', title: 'Booking phase', check: 'Original check' }] } })).ok()).toBeTruthy();
  const original = await (await page.request.get(url)).json();
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.evaluate(({ lead, original, savedLatest }) => sessionStorage.setItem(`acc-draft:plan:${lead}`, JSON.stringify({
    origin: original, base: 75, latest: savedLatest ? { ...original, version: 75 } : null, idea: 'Private booking calendar',
    scope: { in: 'Private bookings', out: '', parked: '' }, phases: [{
      key: 'booking', title: 'Private booking phase', summary: '', check: 'Original check',
      status: 'planned', depends_on: [], requirement_ids: [],
    }], reason: 'Keep my draft',
  })), { lead, original, savedLatest });
  const changed = await page.request.patch(url, { headers, data: { version: original.version,
    scope: { in: ['Bookings'], out: ['Payments'], parked: [] },
    phases: [{ key: 'booking', check: 'Test booking with only the keyboard' }],
  } });
  expect(changed.ok(), await changed.text()).toBeTruthy();
  const current = await changed.json();
  await page.reload();
  const form = page.getByRole('form', { name: 'Edit the plan', exact: true });
  await expect(form.getByRole('status')).toContainText(`now version ${current.version}`);
  await expect(form.getByRole('status')).toContainText('Payments');
  await expect(form.getByRole('textbox', { name: 'Idea', exact: true })).toHaveValue('Private booking calendar');
  const sent = page.waitForRequest(r => new URL(r.url()).pathname === url && r.method() === 'PATCH');
  await form.getByRole('button', { name: 'Save as a new version', exact: true }).click();
  expect((await sent).postDataJSON().version).toBe(current.version);
  await expect(form).toHaveCount(0);
  const stored = await (await page.request.get(url)).json();
  expect(stored.idea).toBe('Private booking calendar');
  expect(stored.scope.in).toEqual(['Private bookings']);
  expect(stored.scope.out).toEqual(['Payments']);
  expect(stored.phases[0].title).toBe('Private booking phase');
  expect(stored.phases[0].check).toBe('Test booking with only the keyboard');
});

test('a failed resume keeps the saved mode and retries only resume, without another mode write', async ({ page }) => {
  const { lead } = await buildLead(page);
  let resumes = 0;
  let writes = 0;
  await page.route(`**/api/sessions/${lead}/resume`, route => ++resumes === 1
    ? route.fulfill({ status: 503, json: { detail: 'The test machine did not accept resume.' } }) : route.continue());
  page.on('request', r => { if (r.method() === 'PATCH' && new URL(r.url()).pathname === `/api/teams/${lead}/plan`) writes++; });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const settings = page.getByRole('tabpanel', { name: 'Settings', exact: true });
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('prepare');
  await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
  await expect(settings.getByRole('alert').filter({ hasText: 'Resuming the lead failed' })).toContainText('Resuming the lead failed');
  await expect(page.getByRole('heading', { name: 'Run mode: Prepare', exact: true })).toBeVisible();
  const stopped = await (await page.request.get(`/api/sessions/${lead}`)).json();
  expect(stopped.desired_state).toBe('stopped');
  await settings.getByRole('button', { name: 'Resume lead', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Lead resumed' })).toContainText('Lead resumed');
  expect(writes).toBe(1);
  expect(resumes).toBe(2);
});

test('run-mode save waits for confirmed stop and does not resume after a rejected plan write', async ({ page }) => {
  const { lead } = await buildLead(page);
  let stopping = false;
  let release = () => {};
  const confirmed = new Promise<void>(resolve => { release = resolve; });
  let readStop = () => {};
  const checking = new Promise<void>(resolve => { readStop = resolve; });
  let resumed = false;
  await page.route(`**/api/sessions/${lead}/stop`, async route => {
    const response = await route.fetch();
    stopping = true;
    await route.fulfill({ response, json: { ...await response.json(), team_stop_pending: true } });
  });
  await page.route(`**/api/sessions/${lead}`, async route => {
    if (stopping) { readStop(); await confirmed; }
    await route.continue();
  });
  await page.route(`**/api/sessions/${lead}/resume`, async route => { resumed = true; await route.continue(); });
  await page.route(`**/api/teams/${lead}/plan`, async route => {
    if (route.request().method() === 'PATCH') return route.fulfill({ status: 409, json: { detail: 'The plan changed during the stop. Review it first.' } });
    await route.continue();
  });
  try {
    await page.goto(`/sessions/${lead}?tab=team#overview`);
    const boundaries = page.getByRole('region', { name: 'Constraints and requirements', exact: true });
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('prepare');
    await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
    await checking;
    await expect(page.getByRole('status').filter({ hasText: 'Stopping the lead' })).toContainText('Stopping the lead');
    expect(resumed).toBe(false);
    release();
    await expect(page.getByRole('alert')).toContainText('Saving Prepare mode failed');
    await expect(page.getByRole('alert')).toContainText('Review it first');
    expect(resumed).toBe(false);
    expect((await (await page.request.get(`/api/teams/${lead}/plan`)).json()).run_mode).toBe('build');
  } finally { release(); }
});

async function buildLead(page: import('@playwright/test').Page) {
  const { id, headers } = await gitProject(page, 'planning-links');
  const created = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', name: 'Riley plans', worktree: true,
    orchestrator: { run_mode: 'build', agents: [{ agent: 'claude', profile: null }], machines: ['local'],
      at_once: 2, total: 4, depth: 1, hours: 2 },
  } });
  expect(created.ok(), await created.text()).toBeTruthy();
  return { lead: (await created.json()).id as string, headers };
}

test('material card blocks only linked phases and the person edits its links', async ({ page }) => {
  const { lead, headers } = await buildLead(page);
  const saved = await page.request.patch(`/api/teams/${lead}/plan`, { headers, data: {
    version: 0, requirements: [{ text: 'Import CSV' }, { text: 'Export CSV' }],
    phases: [{ key: 'import', title: 'Import phase', requirement_ids: ['R-1'] },
      { key: 'export', title: 'Export phase', requirement_ids: ['R-2'] }],
  } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  const added = await page.request.post(`/api/teams/${lead}/worklist/items`, { headers, data: {
    kind: 'decision', title: 'Approve local import', blocks: ['import', 'R-1'], default: { option: 'yes' },
    options: [{ id: 'yes', label: 'Approve' }, { id: 'no', label: 'Decline' }],
  } });
  expect(added.ok(), await added.text()).toBeTruthy();
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  const roadmap = page.getByRole('region', { name: 'Roadmap', exact: true });
  await expect(roadmap.getByRole('listitem', { name: 'Import phase', exact: true })).toContainText('Approve local import');
  await expect(roadmap.getByRole('listitem', { name: 'Export phase', exact: true })).toContainText('Ready to start');
  await page.getByRole('tab', { name: 'Working list', exact: true }).click();
  const card = page.getByRole('listitem', { name: 'Approve local import', exact: true });
  await expect(card).toContainText('Blocks: import, R-1');
  await expect(card.getByText('Assumed default', { exact: true })).toHaveCount(0);
  await card.getByRole('button', { name: 'Edit blocking links', exact: true }).click();
  await card.getByRole('textbox', { name: 'Blocking links for "Approve local import"', exact: true }).fill('R-1');
  await card.getByRole('button', { name: 'Save blocking links', exact: true }).click();
  await expect(card).toContainText('Blocks: R-1');
  await card.getByRole('button', { name: 'Approve', exact: true }).click();
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expandTeamPanels(page);
  await expect(roadmap.getByRole('listitem', { name: 'Import phase', exact: true })).toContainText('Ready to start');
  await expectAxeClean(page);
});

test('person links a roadmap phase to an accepted requirement', async ({ page }) => {
  const { lead, headers } = await buildLead(page);
  expect((await page.request.patch(`/api/teams/${lead}/plan`, { headers, data: {
    version: 0, requirements: [{ text: 'Import CSV' }],
  } })).ok()).toBeTruthy();
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.getByRole('button', { name: 'Edit plan', exact: true }).click();
  const form = page.getByRole('form', { name: 'Edit the plan', exact: true });
  await form.getByLabel('Idea', { exact: true }).fill('Local CSV preview');
  await form.getByLabel('In scope (one per line)', { exact: true }).fill('CSV preview');
  await form.getByRole('button', { name: 'Add phase', exact: true }).click();
  await form.getByRole('textbox', { name: 'Title of phase 1', exact: true }).fill('Preview phase');
  await form.getByRole('checkbox', { name: 'R-1: Import CSV', exact: true }).check();
  await form.getByRole('button', { name: 'Save as a new version', exact: true }).click();
  await expect(page.getByRole('listitem', { name: 'Preview phase', exact: true })).toContainText('Requirements: R-1');
  const current = (await page.request.get(`/api/teams/${lead}/plan`)).json();
  expect((await current).phases[0].requirement_ids).toEqual(['R-1']);
});
