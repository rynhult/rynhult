import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { expandTeamPanels, AUTH_FILE, gitProject, info, openAdvanced, waitForSessionState } from './helpers';

// Usability walkthrough of the Guide's "Orchestrate a project" (docs/wiki/User-Guide/Orchestrate-a-Project.md) as a new
// person would follow it: every click uses the words the Guide gives. The server runs the preview with the plan and
// working list (playwright.v2.config.ts) and fake providers; the lead's own actions go through its tool spool, as an
// agent's would. One test, in the Guide's order, so a broken step names the Guide section it breaks.
test.use({ storageState: AUTH_FILE });
test.setTimeout(240_000);

async function team(lead: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${lead}/events.jsonl`, JSON.stringify({ acc_session: lead,
    hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } } }) + '\n');
  const file = `${info().run_dir}/${lead}/team/${id}.json`;
  await expect.poll(() => existsSync(file)).toBeTruthy();
  return JSON.parse(readFileSync(file, 'utf8')) as { ok: boolean; result?: any; error?: string };
}

async function teamTab(page: Page, lead: string, section = 'Working list') {
  await page.goto(`/sessions/${lead}`);
  await page.getByRole('tab', { name: 'Team', exact: true }).click();
  await page.getByRole('tab', { name: section, exact: true }).click();
  if (section !== 'Working list') await expandTeamPanels(page);
}

test('a new person follows "Orchestrate a project" from the preview switch to Stop team', async ({ page }) => {
  // Before you start, step 3: "the Settings section chooser lists Selection allow-lists, and managed projects have a
  // Gate queue card."
  await page.goto('/settings');
  await expect(page.getByLabel('Settings section').getByRole('option', { name: 'Selection allow-lists' })).toBeAttached();
  const { id: projectId } = await gitProject(page, 'guide-walk');
  await page.goto(`/projects/${projectId}`);
  await expect(page.getByText('Gate queue', { exact: true }).first()).toBeVisible();

  // 1. Start the lead.
  await page.goto('/sessions');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(projectId));
  await dialog.getByLabel(/^Task/).fill('Let office staff import customer lists into the address book.');
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  for (const label of ['At once', 'In total', 'Depth', 'Hours']) await expect(dialog.getByLabel(label, { exact: true })).toBeVisible();
  const launched = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/sessions' && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  const response = await launched;
  expect(response.ok(), await response.text()).toBeTruthy();
  const lead = (await response.json()).id as string;
  await expect(page).toHaveURL(new RegExp(`/sessions/${lead}`));
  await waitForSessionState(page, lead, 'idle', 'claude_hook');
  // "What you should see: ... Its Team tab shows the team."
  await page.getByRole('tab', { name: 'Team', exact: true }).click();
  await expandTeamPanels(page);

  // 2. Plan the work together: "No helper can start until the plan is ready."
  const early = await team(lead, 'start', { name: 'Early helper', agent: 'claude', task: 'Too soon.' });
  expect(early.ok).toBe(false);
  await team(lead, 'plan_update', { reason: 'First draft', idea: 'Let office staff import customer lists.',
    scope: { in: ['Import from a CSV file'], out: ['Syncing'], parked: ['Scheduled imports'] },
    phases: [{ key: 'schema', title: 'Contact schema', check: 'Migration tests' },
             { key: 'importer', title: 'CSV importer', depends_on: ['schema'] }] });
  const decision = await team(lead, 'worklist_add', { kind: 'decision', title: 'Which file formats first?',
    options: [{ id: 'csv', label: 'CSV only', consequence: 'Ships first' }, { id: 'both', label: 'CSV and Excel', consequence: 'Two more days' }],
    recommendation: { option: 'csv', reason: 'Most files are CSV.' }, default: { option: 'csv' } });
  const question = await team(lead, 'worklist_add', { kind: 'question', title: 'Who will use the import page?',
    default: { text: 'Office staff' } });
  await team(lead, 'worklist_add', { kind: 'need', title: 'Create the test address book account', steps: '1. Open the admin page.\n2. Add a test account.' });
  await team(lead, 'worklist_add', { kind: 'need', title: 'Approve the staging port', steps: '1. Open Inbox.\n2. Approve the port request.' });
  await team(lead, 'now', { text: 'Planning the importer with you.' });
  await teamTab(page, lead, 'Overview');
  await expect(page.getByRole('heading', { name: 'Plan', exact: true })).toBeVisible();
  await expect(page.getByText('Roadmap', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/^Plan history/).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Edit plan' })).toBeVisible();

  // 3. Answer the cards when you have time.
  await page.getByRole('tab', { name: 'Working list', exact: true }).click();
  const yours = page.getByRole('region', { name: 'What the lead needs from you' });
  const card = yours.getByRole('listitem', { name: 'Which file formats first?' });
  await expect(card.getByText(/Default until you answer/)).toBeVisible();
  await card.getByRole('button', { name: 'Change', exact: true }).click();
  await card.getByRole('combobox', { name: 'Choose for "Which file formats first?"' }).selectOption('both');
  await card.getByRole('button', { name: 'Save answer' }).click();
  await expect(card).toHaveCount(0);
  await page.getByLabel('Answer "Who will use the import page?"').fill('Two people in accounts');
  await yours.getByRole('listitem', { name: 'Who will use the import page?' }).getByRole('button', { name: 'Answer' }).click();
  await team(lead, 'worklist_ingest', { item: decision.result.id, note: 'Phase 2 adds Excel.' });
  await team(lead, 'worklist_ingest', { item: question.result.id, note: 'Kept the page simple.' });
  await expect(page.getByText('Read by the lead', { exact: false }).first()).toBeVisible();
  // "Now ... If it's wrong, select Edit and correct it."
  const now = page.getByRole('region', { name: 'Now' });
  await now.getByRole('button', { name: 'Edit' }).click();
  await now.getByLabel('Now summary').fill('Planning: formats decided.');
  await now.getByRole('button', { name: 'Save' }).click();
  await expect(now).toContainText('Planning: formats decided.');
  // "For a Needs you card, do the steps, then select Mark done": it leaves the Inbox; the other stays.
  await page.goto('/inbox');
  await expect(page.getByText('Create the test address book account', { exact: false }).first()).toBeVisible();
  await teamTab(page, lead);
  await yours.getByRole('listitem', { name: 'Create the test address book account' }).getByRole('button', { name: 'Mark done' }).click();
  await expect(yours.getByRole('listitem', { name: 'Create the test address book account' })).toHaveCount(0);
  await page.goto('/inbox');
  await expect(page.getByText('Approve the staging port', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Create the test address book account', { exact: false })).toHaveCount(0);

  // 4. Talk to the lead directly: its Terminal tab, as in any session.
  await page.goto(`/sessions/${lead}`);
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await page.locator('.xterm').first().click();
  await page.keyboard.type('Please keep the first version small.');
  await page.keyboard.press('Enter');
  await expect.poll(() => existsSync(`${info().run_dir}/${lead}/fake.received`)
    && readFileSync(`${info().run_dir}/${lead}/fake.received`, 'utf8').includes('keep the first version small')).toBeTruthy();

  // 5. How helpers are chosen: once the plan is ready the lead starts helpers; the tree and reasons explain them.
  expect((await team(lead, 'plan_ready', { reason: 'Agreed with the person' })).ok).toBe(true);
  const assignment = await team(lead, 'assign', { goal: 'Write the contact schema migration.', task_type: 'implementation',
    acceptance: ['Migration tests pass'], requirement_ids: [],
    need: { role: 'backend', classification_reason: 'Schema migration needs the granted backend profile' } });
  expect(assignment.ok, JSON.stringify(assignment)).toBe(true);
  const chosen = await team(lead, 'select', { assignment: assignment.result.id });
  expect(chosen.ok, JSON.stringify(chosen)).toBe(true);
  expect(chosen.result.chosen, JSON.stringify(chosen.result)).not.toBeNull();
  const candidate = chosen.result.candidates[chosen.result.chosen];
  expect(candidate.profile).toBe('backend');
  expect(['claude', 'codex']).toContain(candidate.agent);
  const started = await team(lead, 'start', { name: 'Schema helper', assignment: assignment.result.id,
    selection: chosen.result.id });
  expect(started.ok, JSON.stringify(started)).toBe(true);
  expect(started.result.provider).toBe(candidate.agent);
  await waitForSessionState(page, started.result.id, 'idle', `${candidate.agent}_hook`);
  await teamTab(page, lead, 'Settings');
  await expect(page.getByText('Live agent tree', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Schema helper', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Selection reasons', { exact: false }).first()).toBeVisible();
  await expect(page.getByText("The owner's allow-lists can only narrow the limits set when the team started", { exact: false })).toBeVisible();
  // A helper's agent badge, with its long model name, stays inside its column.
  const helperRow = page.getByRole('row').filter({ hasText: 'Schema helper' }).last();
  const [badge, activity] = [await helperRow.getByRole('cell').nth(3).boundingBox(), await helperRow.getByRole('cell').nth(4).boundingBox()];
  const inner = await helperRow.getByRole('cell').nth(3).locator('span').first().boundingBox();
  expect(inner!.x + inner!.width).toBeLessThanOrEqual(activity!.x + 1);
  expect(badge).not.toBeNull();

  // 7. Stop everything.
  await page.getByRole('button', { name: 'Stop team', exact: true }).click();
  await page.getByRole('button', { name: 'Stop team', exact: true }).last().click();
  await expect(page.getByRole('heading', { name: 'Stop confirmed', exact: true })).toBeVisible();
  const late = await team(lead, 'start', { name: 'Late helper', agent: 'claude', task: 'After the stop.' });
  expect(late.ok).toBe(false);
  // "The working list stays on the Team tab for the record, but its questions leave your Inbox."
  await page.getByRole('tab', { name: 'Working list', exact: true }).click();
  await expect(page.getByText('Approve the staging port', { exact: false }).first()).toBeVisible();
  await page.goto('/inbox');
  await expect(page.getByText('Approve the staging port', { exact: false })).toHaveCount(0);
});
