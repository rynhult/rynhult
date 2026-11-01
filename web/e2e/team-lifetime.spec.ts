import { expect, test as base } from '@playwright/test';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, expectAxeClean, gitProject, info, waitForSessionState, waitForTeamStopped, wikiShotPath, settleSession } from './helpers';
// Real process startup has its own fixture budget; UI assertions keep the normal timeout.
const test = base.extend<{ lead: { id: string; project: string; headers: Record<string, string> } }>({
  lead: [async ({ page }, use) => {
    const { id: project, headers } = await gitProject(page, 'lifetime');
    const made = await page.request.post('/api/sessions', { headers, data: { project_id: project, provider: 'claude', name: 'Lifetime lead', worktree: true,
      orchestrator: { agents: [{ agent: 'claude', profile: 'fullstack' }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: null } } });
    expect(made.status()).toBe(201);
    const id = (await made.json()).id;
    try {
      await waitForSessionState(page, id, 'idle', 'claude_hook');
      await use({ id, project, headers });
    } finally {
      expect((await page.request.post(`/api/sessions/${id}/team/stop`, { headers })).ok()).toBeTruthy();
    }
  }, { timeout: 90000 }],
});
test.use({ storageState: AUTH_FILE });
async function tool(page: import('@playwright/test').Page, lead: string, action: string, extra = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${lead}/events.jsonl`, JSON.stringify({ acc_session: lead, hook: { hook_event_name: 'AccTeam', request: { id, action, ...extra } } }) + '\n');
  await settleSession(page, lead);
  const file = `${info().run_dir}/${lead}/team/${id}.json`;
  await expect.poll(() => existsSync(file), { timeout: test.info().timeout,
    message: `Waiting for owned team request ${action} (${id})` }).toBeTruthy();
  const reply = JSON.parse(readFileSync(file, 'utf8')); expect(reply.ok, JSON.stringify(reply)).toBeTruthy(); return reply.result;
}
for (const width of [1440, 390]) {
 test(`Optional lifetime and acknowledged extension at ${width}px`, async ({ page, lead: owned }) => {
  await page.setViewportSize({ width, height: 900 });
  await page.clock.setFixedTime(new Date('2035-01-01T00:00:00Z'));
  const { id: lead, project: id, headers } = owned;
  // Real grant form exposes the default as an empty optional number input.
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await expect(dialog.getByLabel('Hours', { exact: true })).toHaveValue('');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.goto(`/sessions/${lead}?tab=team#overview`);
    await expect(page.getByText('No time limit for this team.', { exact: true })).toBeVisible();
    const url = `/api/sessions/${lead}/team`;
    expect((await page.request.post(url + '/lifetime', { headers, data: { hours: 1 } })).ok()).toBeTruthy();
    const warning = await page.request.post(`/e2e-test/teams/${lead}/deadline`, { data: { phase: 'warning' } });
    expect(warning.ok()).toBeTruthy();
    const deadline = Date.parse((await warning.json()).expires_at);
    await page.goto('/inbox');
    await expect(page.locator(`a[href="/sessions/${lead}?tab=team#overview"]`).filter({ hasText: 'Review team time limit' })).toBeVisible({ timeout: 20000 });
    await page.locator(`a[href="/sessions/${lead}?tab=team#overview"]`).filter({ hasText: 'Review team time limit' }).click();
    await expect(page.getByText(/This team stops at/)).toBeVisible();
    const [extended] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === url + '/lifetime' && r.request().method() === 'POST'),
      page.getByRole('button', { name: 'Extend 4 h', exact: true }).click(),
    ]);
    expect(extended.ok()).toBeTruthy();
    await expect.poll(async () => Date.parse((await (await page.request.get(url)).json()).expires_at)).toBe(deadline + 4 * 3600000);
    const [removed] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === url + '/lifetime' && r.request().method() === 'POST'),
      page.getByRole('button', { name: 'Remove limit', exact: true }).click(),
    ]);
    expect(removed.ok()).toBeTruthy();
    await expect(page.getByText('No time limit for this team.', { exact: true })).toBeVisible();
 });
 test(`Expiry and saved helper resume at ${width}px`, async ({ page, lead: owned }, ti) => {
    await page.setViewportSize({ width, height: 900 });
    const { id: lead, headers } = owned;
    const url = `/api/sessions/${lead}/team`;
    await page.goto(`/sessions/${lead}?tab=team#overview`);
    await tool(page, lead, 'plan_update', { reason: 'Bounded fixture work', idea: 'Lifetime fixtures', scope: { in: ['Fixture'], out: [], parked: [] }, phases: [{ key: 'work', title: 'Work' }] });
    await tool(page, lead, 'plan_ready', { reason: 'Bounded fixture plan' });
    async function helper(name: string) {
      const assignment = await tool(page, lead, 'assign', { goal: name, task_type: 'implementation', acceptance: ['Fixture passes'], need: { role: 'fullstack', classification_reason: 'Fixture needs the allowed fullstack profile' } });
      const selection = await tool(page, lead, 'select', { assignment: assignment.id, pin: { agent: 'claude' } });
      expect(selection.chosen, JSON.stringify(selection)).not.toBeNull();
      return tool(page, lead, 'start', { assignment: assignment.id, selection: selection.id, name });
    }
    const worker = await helper('Unfinished worker');
    const finished = await helper('Finished worker');
    await waitForSessionState(page, worker.id, 'idle', 'claude_hook');
    await waitForSessionState(page, finished.id, 'idle', 'claude_hook');
    expect((await page.request.post(`/api/sessions/${finished.id}/archive`, { headers })).ok()).toBeTruthy();
    // Expire this team beyond every configured wrap-up; never change the
    // shared policy while other desktop workers are exercising their teams.
    expect((await page.request.post(`/e2e-test/teams/${lead}/deadline`, { data: { phase: 'expired' } })).ok()).toBeTruthy();
    await expect.poll(async () => (await (await page.request.get(url)).json()).stopped, { timeout: 20000 }).toBe(true);
    await waitForTeamStopped(page, lead);
    await page.reload();
    await expect(page.getByText('This team stopped because its time limit ended.', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Resume team', exact: true }).click();
    const resume = page.getByRole('dialog', { name: 'Resume this team?', exact: true });
    await expect(resume.getByText(/Unfinished worker/)).toBeVisible();
    await expect(resume.getByText(/Finished worker/)).toHaveCount(0);
    await expect(resume.getByLabel('Hours after resume')).toHaveValue('');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await resume.screenshot({ path: wikiShotPath(ti, `team-lifetime-${width}.png`) });
    await resume.getByRole('button', { name: 'Confirm resume', exact: true }).click();
    await expect(resume).toHaveCount(0);
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${worker.id}`)).json()).desired_state).toBe('running');
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${worker.id}`)).json()).observed_state, { timeout: 20000 }).toBe('idle');
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${lead}`)).json()).observed_state, { timeout: 20000 }).toBe('idle');
    expect((await (await page.request.get(`/api/sessions/${finished.id}`)).json()).desired_state).toBe('archived');
 });
}

test('resume consent shows provider-specific permissions and an explicit permissive warning', async ({ page, lead: owned }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const url = `/api/sessions/${owned.id}/team`;
  expect((await page.request.post(url + '/stop', { headers: owned.headers })).ok()).toBeTruthy();
  await waitForTeamStopped(page, owned.id);
  // Serializer coverage uses real saved non-Claude inputs in test_team_lifetime.py.
  // Here vary only the preview presentation; never launch a permissive provider.
  await page.route(`**${url}/resume`, async route => {
    const response = await route.fetch();
    const plan = await response.json();
    await route.fulfill({ json: { ...plan, sessions: plan.sessions.map((s: object) => ({ ...s,
      provider: 'copilot', permissions: { Permissions: 'Allow everything ⚠' }, dangerous_permissions: true })) } });
  });
  await page.goto(`/sessions/${owned.id}?tab=team#overview`);
  await page.getByRole('button', { name: 'Resume team', exact: true }).click();
  const consent = page.getByRole('dialog', { name: 'Resume this team?', exact: true });
  await expect(consent).toContainText('Permissions: Allow everything ⚠');
  await expect(consent).toContainText('Saved permissions allow actions without asking. Resuming keeps these permissions.');
  await expect(consent).not.toContainText('acceptEdits');
  await expect(consent).not.toContainText('workspace-write');
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await consent.getByRole('button', { name: 'Cancel', exact: true }).click();
});
