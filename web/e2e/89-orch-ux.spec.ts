/** New-person walkthrough on disposable fake-provider state only. */
import { test, expect, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { environmentValue } from '../../src/acc/env.mjs';
import { expandTeamPanels, AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, waitForSessionState, waitForTeamStopped } from './helpers';

test.use({ storageState: AUTH_FILE });
const uploadLabel = 'Or upload PNG, JPG, WebP or PDF (10 MiB maximum)';
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
}
async function drawing(page: Page, label: string) {
  return Buffer.from(await page.evaluate(text => {
    const c = document.createElement('canvas'); c.width = 480; c.height = 180;
    const ctx = c.getContext('2d')!; ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 480, 180);
    ctx.strokeStyle = '#222'; ctx.strokeRect(20, 20, 440, 140); ctx.fillStyle = '#222';
    ctx.font = '18px sans-serif'; ctx.fillText(text, 36, 60); ctx.fillText('Add appointment → Pick time → Confirm', 36, 110);
    return c.toDataURL('image/png').split(',')[1];
  }, label), 'base64');
}
// Simulate time/results in this harness's own DB; never use the installed server.
function fixtureSql(sql: string, args: unknown[]) {
  execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c',
    'import sqlite3,sys,json\nwith sqlite3.connect(sys.argv[1]) as db: db.execute(sys.argv[2],json.loads(sys.argv[3]))',
    `${info().state}/data/acc.db`, sql, JSON.stringify(args)]);
}

test('new person creates a captioned visual brief, imports Markdown and reviews versions with the keyboard', async ({ page }, ti) => {
  const { id, headers, folder } = await gitProject(page, 'ux-brief');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('A small appointment booking web app');
  await page.getByLabel('People and problem', { exact: true }).fill('Neighbours need a simple way to reserve a shared room.');
  await page.getByLabel('First release', { exact: true }).fill('List free times and confirm a booking.');
  await page.getByLabel('Constraints', { exact: true }).fill('Keyboard accessible; keep names private.');
  await page.getByText('References with captions (0/20)', { exact: true }).press('Enter');
  for (const label of ['Booking form sketch', 'Calendar layout', 'Confirmation screen']) {
    await page.getByLabel('Reference caption', { exact: true }).fill(label);
    await page.getByLabel('What to copy, keep, change or avoid', { exact: true }).fill('Keep the clear sequence; avoid small touch targets.');
    await page.getByLabel(uploadLabel, { exact: true }).setInputFiles({ name: `${label}.png`, mimeType: 'image/png', buffer: await drawing(page, label) });
    await page.getByRole('button', { name: 'Add reference', exact: true }).click();
    await expect(page.getByText(new RegExp(`REF-\\d: ${label}`))).toBeVisible();
    await expect(page.getByLabel(uploadLabel, { exact: true })).toHaveValue('');
  }
  await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 1 saved.' })).toBeVisible();
  await page.getByLabel('Success', { exact: true }).fill('A neighbour can book with only a keyboard.');
  await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 2 saved.' })).toBeVisible();
  const saved = await (await page.request.get(`/api/projects/${id}/brief`)).json();
  expect(saved.current.references).toHaveLength(3);
  await page.getByText('Markdown import and export', { exact: true }).press('Enter');
  await page.getByLabel('Import Markdown as a proposed version', { exact: true }).fill(saved.current.markdown.replace('A small appointment booking web app', 'Room booking, revised'));
  await page.getByRole('button', { name: 'Propose imported Markdown', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Proposed version 3;' })).toBeVisible();
  await page.getByText('Version history', { exact: true }).press('Enter');
  await page.getByText('Read submitted text for version 3', { exact: true }).press('Enter');
  await expect(page.getByText(/Product or feature: Room booking, revised/)).toBeVisible();
  const historyText = page.getByRole('region', { name: 'Submitted text for brief version 3', exact: true });
  await historyText.focus(); await page.keyboard.press('ArrowDown');
  await expect(historyText).toBeFocused();
  if (ti.project.name.endsWith('390')) writeFileSync(`${folder}/docs/PROJECT-BRIEF.md`, '# Independent Markdown edit\n');
  await page.getByRole('checkbox', { name: 'Product or feature', exact: true }).check();
  await page.getByRole('button', { name: 'Accept 1 field from version 3', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 4 saved.' })).toBeVisible();
  expect((await (await page.request.get(`/api/projects/${id}/brief`, { headers })).json()).current_version).toBe(4);
  if (ti.project.name.endsWith('390')) {
    expect(readFileSync(`${folder}/docs/PROJECT-BRIEF.md`, 'utf8')).toBe('# Independent Markdown edit\n');
    await expect(page.getByRole('status').filter({ hasText: 'Brief version 4 saved.' })).toContainText('Import the changed Markdown');
  } else expect(readFileSync(`${folder}/docs/PROJECT-BRIEF.md`, 'utf8')).toContain('Room booking, revised');
  await expectAxeClean(page); await noOverflow(page);
  await page.screenshot({ path: ti.outputPath('brief-versions.png'), fullPage: true });
});

test('upload failures preserve the brief and explain how to recover', async ({ page }, ti) => {
  const { id } = await gitProject(page, 'ux-upload');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Keep this unsaved booking idea');
  await page.getByText('References with captions (0/20)', { exact: true }).click();
  await page.getByLabel('Reference caption', { exact: true }).fill('A reference that cannot be read');
  await page.getByLabel('What to copy, keep, change or avoid', { exact: true }).fill('Keep its spacing.');
  for (const file of [
    { name: 'too-big.png', mimeType: 'image/png', buffer: Buffer.alloc(10 * 1024 * 1024 + 1) },
    { name: 'wrong.txt', mimeType: 'text/plain', buffer: Buffer.from('not a picture') },
    { name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('unreadable image') },
  ]) {
    await page.getByLabel(uploadLabel, { exact: true }).setInputFiles(file);
    await page.getByRole('button', { name: 'Add reference', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: /Couldn't update the project brief/ })).toBeInViewport();
    await expect(page.getByRole('region', { name: 'Project brief feedback', exact: true })).toBeFocused();
    await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('Keep this unsaved booking idea');
    await expect(page.getByText('References with captions (0/20)', { exact: true })).toBeVisible();
  }
  // The OS read failure cannot be produced by setInputFiles; emulate FileReader's error event only.
  await page.evaluate(() => {
    const original = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function () {
      FileReader.prototype.readAsDataURL = original;
      queueMicrotask(() => this.dispatchEvent(new Event('error')));
    };
  });
  await page.getByLabel(uploadLabel, { exact: true }).setInputFiles({ name: 'unreadable.png', mimeType: 'image/png', buffer: await drawing(page, 'Read failure') });
  await page.getByRole('button', { name: 'Add reference', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Could not read the file');
  await expect(page.getByRole('region', { name: 'Project brief feedback', exact: true })).toBeFocused();
  await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('Keep this unsaved booking idea');
  await expectAxeClean(page); await noOverflow(page);
  await page.screenshot({ path: ti.outputPath('upload-error.png'), fullPage: true });
});

for (const mode of ['prepare', 'build'] as const) test(`person grants a ${mode} team, reads boundaries and stops it`, async ({ page }, ti) => {
  const { id, headers } = await gitProject(page, `ux-${mode}`);
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await expect(dialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(id));
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await dialog.getByRole('combobox', { name: 'Team run mode', exact: true }).selectOption(mode);
  if (mode === 'prepare') await dialog.getByRole('combobox', { name: 'Helper 1: Permissions', exact: true }).selectOption('plan');
  await expectAxeClean(page, '[role=dialog]'); await noOverflow(page);
  await dialog.screenshot({ path: ti.outputPath(`${mode}-grant.png`) });
  const responseReady = page.waitForResponse(r => new URL(r.url()).pathname === '/api/sessions' && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  const created = await responseReady; expect(created.ok(), await created.text()).toBeTruthy();
  const lead = (await created.json()).id;
  try {
    await waitForSessionState(page, lead, 'idle', 'claude_hook');
    await page.goto(`/sessions/${lead}?tab=team#settings`);
    const plan = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
    expect((await page.request.patch(`/api/teams/${lead}/plan`, { headers, data: { version: plan.version,
      constraints: [{ text: 'Keep booking details private.' }], requirements: [{ text: 'Book a room.' }],
      phases: [{ key: 'booking', title: 'Booking page', requirement_ids: ['R-1'] }],
    } })).ok()).toBeTruthy();
    expect((await page.request.post(`/api/teams/${lead}/worklist/items`, { headers, data: {
      kind: 'question', title: 'Can guests see other names?', blocks: ['R-1'],
    } })).ok()).toBeTruthy();
    await page.reload();
    await expect(page.getByRole('heading', { name: `Run mode: ${mode === 'prepare' ? 'Prepare' : 'Build'}`, exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expandTeamPanels(page);
    await expect(page.getByRole('region', { name: 'Constraints and requirements' })).toContainText('Keep booking details private.');
    await expect(page.getByRole('listitem', { name: 'Booking page', exact: true })).toContainText('Can guests see other names?');
    await page.getByRole('region', { name: 'Constraints and requirements', exact: true }).screenshot({ path: ti.outputPath(`${mode}-boundaries.png`) });
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    const tree = page.getByRole('tree', { name: 'Live agent tree', exact: true });
    await expect(tree.getByRole('treeitem')).toHaveCount(1);
    await tree.getByRole('treeitem').focus(); await page.keyboard.press('Home');
    await expect(tree.getByRole('treeitem')).toBeFocused();
    await expectAxeClean(page); await noOverflow(page);
    await page.screenshot({ path: ti.outputPath(`${mode}-plan.png`), fullPage: true });
    await page.getByRole('button', { name: 'Stop team', exact: true }).click();
    const stop = page.getByRole('dialog', { name: 'Stop team?', exact: true });
    await stop.getByRole('button', { name: 'Cancel', exact: true }).press('Enter');
    await expect(page.getByRole('button', { name: 'Stop team', exact: true })).toBeFocused();
    await page.getByRole('button', { name: 'Stop team', exact: true }).click();
    await page.getByRole('dialog', { name: 'Stop team?', exact: true }).getByRole('button', { name: 'Stop team', exact: true }).click();
    await waitForTeamStopped(page, lead);
    await expect(tree.getByRole('treeitem')).toContainText('stop confirmed', { timeout: ti.timeout });
    await expect(page.getByRole('heading', { name: 'Stop confirmed', exact: true })).toBeVisible({ timeout: ti.timeout });
    if (mode === 'prepare') {
      // Offline stop transport is Python-qualified. Here verify the pending projection
      // cannot be presented as a confirmed stop, even with an old stopped observation.
      const snapshot = await (await page.request.get(`/api/teams/${lead}/tree`)).json();
      snapshot.members[0].stop_state = 'stop requested';
      snapshot.members[0].observed = 'stopped';
      await page.route(`**/api/teams/${lead}/tree`, route => route.fulfill({ json: snapshot }));
      await page.reload();
      await expect(tree.getByRole('treeitem')).toContainText('stop requested');
      await expect(tree.getByRole('treeitem')).not.toContainText('stop confirmed');
      await expectAxeClean(page); await noOverflow(page);
      await tree.screenshot({ path: ti.outputPath('stop-pending-projection.png') });
      await page.unroute(`**/api/teams/${lead}/tree`);
    }
  } finally {
    expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBe(true);
    await waitForTeamStopped(page, lead);
  }
});

test('owner saves flake and wrap-up settings and sees expiry in the tree', async ({ page }, ti) => {
  const { id, headers } = await gitProject(page, 'ux-expiry');
  const previous = (await (await page.request.get('/api/gates/wrapup')).json()).minutes;
  let lead: string | undefined;
  try {
    await page.goto(`/projects/${id}#project-gates`);
    const flakes = page.getByRole('form', { name: 'Known flakes', exact: true });
    await flakes.getByLabel('Known flaky test IDs', { exact: true }).fill('tests/test_demo.py::test_booking');
    await flakes.getByRole('button', { name: 'Save known flakes', exact: true }).click();
    await expect.poll(async () => (await (await page.request.get(`/api/gates/projects/${id}/flakes`)).json()).tests).toEqual(['tests/test_demo.py::test_booking']);
    const wrap = page.getByRole('form', { name: 'Expiry wrap-up', exact: true });
    await wrap.getByLabel('Lead wrap-up (minutes)', { exact: true }).fill('5');
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === '/api/gates/wrapup');
    await wrap.getByRole('button', { name: 'Save wrap-up', exact: true }).click();
    expect((await saved).ok()).toBe(true);
    expect((await (await page.request.get('/api/gates/wrapup')).json()).minutes).toBe(5);
    await wrap.getByLabel('Lead wrap-up (minutes)', { exact: true }).fill('121');
    await expect(wrap.getByRole('button', { name: 'Save wrap-up', exact: true })).toBeDisabled();
    await expectAxeClean(page); await noOverflow(page);
    const launch = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', worktree: true, name: 'Booking expiry lead',
      orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 1, total: 2, depth: 1, hours: 1 } } });
    expect(launch.ok(), await launch.text()).toBeTruthy();
    lead = (await launch.json()).id as string;
    await waitForSessionState(page, lead, 'idle', 'claude_hook');
    fixtureSql("UPDATE team SET expires_at = datetime('now', '-1 second') WHERE id = ?", [lead]);
    await page.goto(`/sessions/${lead}?tab=team#settings`);
    await expect(page.getByRole('heading', { name: 'Team expiry wrap-up', exact: true })).toBeVisible({ timeout: 20000 });
    await expect(page.getByRole('tree', { name: 'Live agent tree', exact: true })).toContainText('expiry wrap-up');
    await page.getByRole('region', { name: 'Live agent tree panel', exact: true }).screenshot({ path: ti.outputPath('expiry.png') });
    fixtureSql("UPDATE team SET wrapup_until = datetime('now', '-1 second') WHERE id = ?", [lead]);
    await expect(page.getByRole('tree', { name: 'Live agent tree', exact: true })).toContainText('stop confirmed', { timeout: 20000 });
    await expectAxeClean(page); await noOverflow(page);
  } finally {
    try {
      if (lead) {
        expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBe(true);
        await waitForTeamStopped(page, lead);
      }
    } finally {
      expect((await page.request.put('/api/gates/wrapup', { headers, data: { minutes: previous } })).ok()).toBe(true);
    }
  }
});

test('owner triages a retry with a real team task and errors never look empty', async ({ page }, ti) => {
  const { id, headers, folder } = await gitProject(page, 'ux-retry');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', worktree: true, name: 'Retry triage lead',
    orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 1, total: 2, depth: 1, hours: 1 } } });
  expect(created.ok(), await created.text()).toBeTruthy(); const lead = (await created.json()).id;
  try {
    const task = await page.request.post(`/api/teams/${lead}/worklist/items`, { headers, data: { kind: 'task', title: 'Investigate intermittent booking test' } });
    expect(task.ok(), await task.text()).toBeTruthy(); const item = (await task.json()).id;
    const head = execFileSync('git', ['-C', folder, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
    const job = `ux${lead}`;
    fixtureSql(`INSERT INTO gate_job (id, project_id, team_id, branch, base, head, suite, priority, state, attempt, host, account, requested_by, results, created_at, finished_at)
      VALUES (?, ?, ?, 'main', 'main', ?, 'related', 'check', 'retry-passed', 2, 'local', 'fixture', 'alex', ?, datetime('now'), datetime('now'))`,
    [job, id, lead, head, JSON.stringify({ flaky: ['tests/test_demo.py::test_booking'] })]);
    await page.goto(`/projects/${id}#project-gates`);
    const accept = page.getByRole('form', { name: 'Accept flake task', exact: true });
    await expect(accept.getByRole('button', { name: 'Accept flake task', exact: true })).toBeDisabled();
    await accept.getByRole('spinbutton', { name: 'Flake task number', exact: true }).fill('9999999');
    await accept.getByRole('button', { name: 'Accept flake task', exact: true }).click();
    await expect(accept.getByRole('alert')).toContainText('this team’s working list');
    await accept.getByRole('spinbutton', { name: 'Flake task number', exact: true }).fill(String(item));
    await accept.getByRole('button', { name: 'Accept flake task', exact: true }).click();
    await expect(page.getByText(new RegExp(`Flake task ${item} accepted by`))).toBeVisible();
    await expectAxeClean(page); await noOverflow(page);
    await page.screenshot({ path: ti.outputPath('retry-triage.png'), fullPage: true });
    await page.route(`**/api/projects/${id}/brief`, route => route.fulfill({ status: 503, json: { detail: 'Fixture brief unavailable' } }));
    await page.goto(`/projects/${id}#project-brief`);
    await page.reload(); // A hash-only navigation otherwise keeps the already-mounted brief query.
    await expect(page.getByRole('alert')).toContainText("isn't available");
    await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
    await page.unroute(`**/api/projects/${id}/brief`);
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toBeVisible();
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

test('plain Markdown starts an incomplete brief as a proposal, never silent scope', async ({ page }) => {
  const { id } = await gitProject(page, 'ux-plain-import');
  await page.goto(`/projects/${id}#project-brief`);
  await expect(page.getByRole('button', { name: 'Save project brief', exact: true })).toBeDisabled();
  await page.getByText('Markdown import and export', { exact: true }).press('Enter');
  await page.getByRole('textbox', { name: 'Import Markdown as a proposed version', exact: true }).fill('# Neighbourhood booking board\n\nBuild a small web app with a calendar and private confirmations.');
  await page.getByRole('button', { name: 'Propose imported Markdown', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('');
  await page.getByText('Version history', { exact: true }).press('Enter');
  await page.getByRole('checkbox', { name: 'Product or feature', exact: true }).check();
  await page.getByRole('button', { name: 'Accept 1 field from version 1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('Neighbourhood booking board');
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 2 saved.' })).toBeVisible();
  await expectAxeClean(page); await noOverflow(page);
});
