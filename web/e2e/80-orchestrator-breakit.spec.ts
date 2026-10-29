import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { expandTeamPanels, AUTH_FILE, gitProject, info, waitForSessionState } from './helpers';

// Break-it pass on the Orchestrator v2 with the switch on (docs/reviews/2026-10-26-orchestrator-v2-breakit.md).
// Needs the working list and plan on the server: playwright.v2.config.ts starts it with ACC_ORCHESTRATOR_V2=1. The lead
// is driven through its spool, as an agent would, with the e2e harness's fake provider.
test.use({ storageState: AUTH_FILE });

async function teamLead(page: Page) {
  const { id, headers } = await gitProject(page, 'breakit');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Lead',
    worktree: true, orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 2 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const lead = (await created.json()).id as string;
  test.skip((await page.request.get(`/api/teams/${lead}/worklist`)).status() === 404,
    'needs ACC_ORCHESTRATOR_V2=1 on the server (playwright.v2.config.ts)');
  // A real lead cannot call a tool before launch and its first hook have settled.
  await waitForSessionState(page, lead, "idle", "claude_hook");
  return lead;
}

async function team(lead: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${lead}/events.jsonl`, JSON.stringify({ acc_session: lead,
    hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } } }) + '\n');
  const file = `${info().run_dir}/${lead}/team/${id}.json`;
  await expect.poll(() => existsSync(file), {
    message: `Waiting for ${action} reply from lead ${lead}`,
    timeout: test.info().timeout,
    intervals: [100, 250, 500],
  }).toBeTruthy();
  const reply = JSON.parse(readFileSync(file, 'utf8'));
  expect(reply.ok, JSON.stringify(reply)).toBeTruthy();
  return reply.result;
}

const PLAN = { reason: 'First draft', idea: 'Import customer lists.', scope: { in: ['CSV'], out: [], parked: [] },
  phases: [{ key: 'schema', title: 'Contact schema' }, { key: 'importer', title: 'Importer', depends_on: ['schema'] }] };

test('a plan editor open in one tab never reverts a newer save from another', async ({ page, context }) => {
  const lead = await teamLead(page);
  await team(lead, 'plan_update', PLAN);
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.getByRole('button', { name: 'Edit plan' }).click();
  const formA = page.getByRole('form', { name: 'Edit the plan' });

  const other = await context.newPage();  // the same person, in a second tab
  await other.goto(`/sessions/${lead}?tab=team#overview`);
  await other.getByRole('button', { name: 'Edit plan' }).click();
  const formB = other.getByRole('form', { name: 'Edit the plan' });
  await formB.getByLabel('Idea').fill('Import customer lists from CSV and TSV.');
  await formB.getByLabel('In scope (one per line)').fill('CSV and TSV imports');
  await formB.getByRole('button', { name: 'Save as a new version' }).click();
  await expect(other.getByRole('region', { name: 'Idea' })).toContainText('CSV and TSV');

  // The editor now presents newer scope before saving, then applies only the person's changed fields.
  const latest = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
  const review = formA.getByRole('status').filter({ hasText: 'The plan changed while you were editing' });
  await expect(review).toContainText(`now version ${latest.version}`, { timeout: 15_000 });
  await expect(review).toContainText('Idea is now: Import customer lists from CSV and TSV.');
  await formA.getByLabel('Out of scope (one per line)').fill('Excel files');
  await expect(formA.getByLabel('Idea')).toHaveValue('Import customer lists.');
  const saveRequest = page.waitForRequest(r => new URL(r.url()).pathname === `/api/teams/${lead}/plan` && r.method() === 'PATCH');
  await formA.getByRole('button', { name: 'Save as a new version' }).click();
  const body = (await saveRequest).postDataJSON();
  expect(body.version).toBe(latest.version);
  expect(body.scope).toEqual({ in: ['CSV and TSV imports'], out: ['Excel files'], parked: [] });
  expect(body).not.toHaveProperty('idea');
  await expect(formA).toHaveCount(0);
  const plan = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
  expect(plan.idea).toBe('Import customer lists from CSV and TSV.');
  expect(plan.scope.out).toEqual(['Excel files']);
  expect(plan.scope.in).toEqual(['CSV and TSV imports']);
  expect(plan.version).toBe(latest.version + 1);
});

test('a refresh or Back keeps an unsaved plan edit, NOW draft and typed answer', async ({ page }) => {
  const lead = await teamLead(page);
  await team(lead, 'plan_update', PLAN);
  await team(lead, 'worklist_add', { kind: 'question', title: 'Who uses the import page?' });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.getByRole('button', { name: 'Edit plan' }).click();
  await page.getByRole('form', { name: 'Edit the plan' }).getByLabel('Idea').fill('Import customer lists, half typed');
  await page.getByRole('tab', { name: 'Working list', exact: true }).click();
  const now = page.getByRole('region', { name: 'Now' });
  await now.getByRole('button', { name: 'Edit' }).click();
  await now.getByLabel('Now summary').fill('Waiting on my review');
  await page.getByLabel('Answer "Who uses the import page?"').fill('Office staff');
  await page.reload();
  await expect(page.getByRole('region', { name: 'Now' }).getByLabel('Now summary')).toHaveValue('Waiting on my review');
  await expect(page.getByLabel('Answer "Who uses the import page?"')).toHaveValue('Office staff');
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expandTeamPanels(page);
  const form = page.getByRole('form', { name: 'Edit the plan' });
  await expect(form.getByText('Restored your unsaved edit.')).toBeVisible();
  await expect(form.getByLabel('Idea')).toHaveValue('Import customer lists, half typed');
  await page.goto('/sessions');
  await page.goBack();
  await expect(page.getByRole('form', { name: 'Edit the plan' }).getByLabel('Idea')).toHaveValue('Import customer lists, half typed');
  await page.getByRole('button', { name: 'Discard my edits' }).click();  // and a deliberate discard forgets it
  await page.reload();
  await expect(page.getByRole('button', { name: 'Edit plan' })).toBeVisible();
  await expect(page.getByRole('form', { name: 'Edit the plan' })).toHaveCount(0);
});

test('a decision has no Mark done or Dismiss until it is answered', async ({ page }) => {
  const lead = await teamLead(page);
  await team(lead, 'worklist_add', { kind: 'decision', title: 'Which delimiter?', options: [{ id: 'comma', label: 'Comma' },
    { id: 'tab', label: 'Tab' }], recommendation: { option: 'comma', reason: 'Most files' }, default: { option: 'comma' } });
  await team(lead, 'worklist_add', { kind: 'question', title: 'Who uses it?' });
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const yours = page.getByRole('region', { name: 'What the lead needs from you' });
  const decision = yours.getByRole('listitem', { name: 'Which delimiter?' });
  await expect(decision.getByRole('button', { name: 'Approve' })).toBeVisible();
  await expect(decision.getByRole('button', { name: 'Mark done' })).toHaveCount(0);
  await expect(decision.getByRole('button', { name: 'Dismiss' })).toHaveCount(0);
  await expect(yours.getByRole('listitem', { name: 'Who uses it?' }).getByRole('button', { name: 'Dismiss' })).toBeVisible();
  await decision.getByRole('button', { name: 'Approve' }).click();
  const answered = page.getByRole('region', { name: 'Answered', exact: true }).getByRole('listitem', { name: 'Which delimiter?' });
  await expect(answered.getByRole('button', { name: 'Mark done' })).toBeVisible();
});

test('saving NOW never silently replaces a newer NOW from the lead', async ({ page }) => {
  const lead = await teamLead(page);
  await team(lead, 'plan_update', PLAN);
  await team(lead, 'now', { text: 'Writing the schema.' });
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const now = page.getByRole('region', { name: 'Now' });
  await now.getByRole('button', { name: 'Edit' }).click();
  await now.getByLabel('Now summary').fill('Paused: wait for my answer on delimiters.');
  await team(lead, 'worklist');
  await team(lead, 'now', { text: 'Schema done; starting the importer.' });
  await page.waitForTimeout(4_000);  // the list refreshes (every 3 s) under the open editor
  await now.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('Someone changed the list while you were editing.')).toBeVisible();
  const list = await (await page.request.get(`/api/teams/${lead}/worklist`)).json();
  expect(list.now).toBe('Schema done; starting the importer.');
  await now.getByRole('button', { name: 'Save' }).click();  // after seeing it, saving again is a deliberate choice
  await expect(now).toContainText('Paused: wait for my answer on delimiters.');
});

test('an answer chosen before the lead rewrote the options is refused, not recorded with a new meaning', async ({ page }) => {
  const lead = await teamLead(page);
  const card = await team(lead, 'worklist_add', { kind: 'decision', title: 'Which delimiter?',
    options: [{ id: 'comma', label: 'Comma' }, { id: 'tab', label: 'Tab' }],
    recommendation: { option: 'comma', reason: 'Most files' }, default: { option: 'comma' } });
  await page.goto(`/sessions/${lead}?tab=team#worklist`);
  const row = page.getByRole('region', { name: 'What the lead needs from you' }).getByRole('listitem', { name: 'Which delimiter?' });
  await row.getByRole('button', { name: 'Change', exact: true }).click();
  await row.getByRole('combobox', { name: 'Choose for "Which delimiter?"' }).selectOption('tab');
  await team(lead, 'worklist_update', { item: card.id, options: [{ id: 'comma', label: 'Tab' }, { id: 'tab', label: 'Semicolon' }],
    recommendation: { option: 'comma', reason: 'Tab-separated exports are the norm here' } });
  await page.waitForTimeout(4_000);  // the list refreshes under the person's choice
  await expect(row.getByRole('alert')).toContainText('This card changed');
  const list = await (await page.request.get(`/api/teams/${lead}/worklist`)).json();
  const item = list.items.find((x: { id: number }) => x.id === card.id);
  expect(item.answer).toBeNull();
  expect(item.state).toBe('open');
  await row.getByRole('button', { name: 'Resend decision' }).click();  // having seen the new labels, answering is deliberate
  await expect(row).toHaveCount(0);  // answered: it leaves "What the lead needs from you"
  const after = (await (await page.request.get(`/api/teams/${lead}/worklist`)).json()).items.find((x: { id: number }) => x.id === card.id);
  expect(after.answer).toBe('tab');
});

test('saving the plan with nothing changed just closes the editor', async ({ page }) => {
  const lead = await teamLead(page);
  await team(lead, 'plan_update', PLAN);
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await page.getByRole('button', { name: 'Edit plan' }).click();
  await page.getByRole('form', { name: 'Edit the plan' }).getByRole('button', { name: 'Save as a new version' }).click();
  await expect(page.getByRole('form', { name: 'Edit the plan' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Edit plan' })).toBeVisible();
  const plan = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
  expect(plan.version).toBe(1);  // no empty version
});

test('the planning session, editor and working list fit a phone', async ({ page }) => {
  const lead = await teamLead(page);
  await team(lead, 'plan_update', PLAN);
  await team(lead, 'worklist_add', { kind: 'decision', title: 'Which delimiter for files exported from the old address book?',
    options: [{ id: 'comma', label: 'Comma, the usual one' }, { id: 'tab', label: 'Tab' }],
    recommendation: { option: 'comma', reason: 'Most files' }, default: { option: 'comma' } });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/sessions/${lead}?tab=team#overview`);
  await expect(page.getByRole('heading', { name: 'Plan', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Edit plan' }).click();
  await expect(page.getByRole('form', { name: 'Edit the plan' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});
