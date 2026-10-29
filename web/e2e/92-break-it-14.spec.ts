/** Break-it 14: a careless person edits boundaries while the lead keeps changing the plan, and reloads mid-brief. */
import { test, expect, type Page } from '@playwright/test';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { expandTeamPanels, AUTH_FILE, expectAxeClean, gitProject, info } from './helpers';

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
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
}
/** Keep answering the plan as the page first loaded it, as if the 5 s refresh had not come round yet. */
async function holdPlan(page: Page, lead: string) {
  const url = `**/api/teams/${lead}/plan`;
  let stale: unknown;
  await page.route(url, async route => {
    if (route.request().method() !== 'GET') return route.continue();
    if (stale === undefined) stale = await (await route.fetch()).json();
    await route.fulfill({ json: stale });
  });
  return () => page.unroute(url);
}

test('a boundary or run-mode save after the lead changed the plan keeps the text and saves again on purpose', async ({ page }, ti) => {
  test.setTimeout(120000);
  const { id, headers } = await gitProject(page, 'break-it-14-boundaries');
  const created = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Boundary lead',
    worktree: true, orchestrator: { run_mode: 'build', agents: [{ agent: 'claude', profile: null }], machines: ['local'],
      at_once: 2, total: 4, depth: 1, hours: 1 } } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const lead = (await created.json()).id;
  try {
    await team(lead, 'plan_update', { idea: 'Room booking', reason: 'Start', scope: { in: ['Booking'], out: [], parked: [] },
      phases: [{ key: 'booking', title: 'Booking page' }] });
    await page.goto(`/sessions/${lead}?tab=team#overview`);
    const boundaries = page.getByRole('region', { name: 'Constraints and requirements', exact: true });
    await expect(page.getByRole('heading', { name: 'Effective run mode: Build', exact: true })).toBeVisible();
    const release = await holdPlan(page, lead);
    await boundaries.getByRole('button', { name: 'Add constraint', exact: true }).click();
    const form = boundaries.getByRole('form', { name: 'Edit constraints', exact: true });
    await form.getByRole('textbox', { name: 'Text', exact: true }).fill('Keep guest names on this machine.');
    await form.getByRole('textbox', { name: 'Reason', exact: true }).fill('Privacy');
    await team(lead, 'plan', {});
    await team(lead, 'plan_update', { reason: 'Refine', phases: [{ key: 'booking', summary: 'Form and list' }] });
    await form.getByRole('button', { name: 'Save boundary', exact: true }).click();
    const note = form.getByText(/The plan changed since you loaded it\. It now shows the latest version; your text is kept/);
    await expect(note).toBeVisible();
    await expect(form.getByRole('textbox', { name: 'Text', exact: true })).toHaveValue('Keep guest names on this machine.');
    await expectAxeClean(page); await noOverflow(page);
    await form.screenshot({ path: ti.outputPath('boundary-conflict.png') });
    await release();
    await expect.poll(async () => (await (await page.request.get(`/api/teams/${lead}/plan`)).json()).version).toBeGreaterThan(1);
    await page.waitForResponse(r => new URL(r.url()).pathname === `/api/teams/${lead}/plan` && r.request().method() === 'GET');
    await form.getByRole('button', { name: 'Save boundary', exact: true }).dblclick();
    await expect(boundaries.getByRole('button', { name: 'Edit C-1', exact: true })).toBeVisible();
    await expect(boundaries.getByText('Keep guest names on this machine.', { exact: true })).toBeVisible();
    const plan = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
    expect(plan.constraints.map((c: { id: string }) => c.id)).toEqual(['C-1']);
    expect(plan.phases?.[0]?.summary ?? 'Form and list').toBe('Form and list');  // the lead's change stays

    // Run mode: the person stops the lead first, as the panel says; a plan change meanwhile is a conflict, not a dead end.
    expect((await page.request.post(`/api/sessions/${lead}/stop`, { headers })).ok()).toBeTruthy();
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    const releaseMode = await holdPlan(page, lead);
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Run mode: Build', exact: true })).toBeVisible();
    const current = await (await page.request.get(`/api/teams/${lead}/plan`)).json();
    expect((await page.request.patch(`/api/teams/${lead}/plan`, { headers, data: { version: current.version,
      requirements: [{ text: 'Book a room.' }], reason: 'Another tab' } })).ok()).toBeTruthy();
    await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('prepare');
    await releaseMode();
    await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
    // Either the held version conflicts (then the plan refreshes) or the refresh landed first; both end in Prepare.
    const conflict = page.getByText(/The plan changed since you loaded it/);
    const prepared = page.getByRole('heading', { name: 'Run mode: Prepare', exact: true });
    await expect(conflict.or(prepared)).toBeVisible();
    if (await conflict.isVisible()) await page.getByRole('button', { name: 'Save run mode', exact: true }).click();
    await expect(prepared).toBeVisible();
    await noOverflow(page);
  } finally { expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('an unsaved brief survives a reload, saves once on a double click, and can be discarded', async ({ page }, ti) => {
  const { id } = await gitProject(page, 'break-it-14-brief');
  await page.goto(`/projects/${id}#project-brief`);
  const product = page.getByRole('textbox', { name: 'Product or feature', exact: true });
  await product.fill('A booking board for the shared room');
  await page.getByRole('textbox', { name: 'People and problem', exact: true }).fill('Neighbours double-book the room.');
  await page.reload();
  await expect(product).toHaveValue('A booking board for the shared room');
  await expect(page.getByRole('textbox', { name: 'People and problem', exact: true })).toHaveValue('Neighbours double-book the room.');
  const restored = page.getByRole('status').filter({ hasText: 'Unsaved draft restored.' });
  await expect(restored).toBeVisible();
  await expectAxeClean(page); await noOverflow(page);
  await page.getByRole('heading', { name: 'Project brief', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: ti.outputPath('brief-restored.png') });
  await page.getByRole('button', { name: 'Save project brief', exact: true }).dblclick();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 1 saved.' })).toBeVisible();
  await expect(restored).toHaveCount(0);
  expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(1);
  await page.reload();
  await expect(product).toHaveValue('A booking board for the shared room');
  await expect(restored).toHaveCount(0);  // saved: nothing unsaved to bring back
  await product.fill('Something I do not want');
  await page.reload();
  await expect(product).toHaveValue('Something I do not want');
  await page.getByRole('button', { name: 'Discard draft', exact: true }).click();
  await expect(product).toHaveValue('A booking board for the shared room');
  await page.reload();
  await expect(product).toHaveValue('A booking board for the shared room');
});
