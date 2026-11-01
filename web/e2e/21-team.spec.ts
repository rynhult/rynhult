import { expandTeamPanels, openAdvanced, wikiShotPath } from "./helpers";
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, control, csrfHeaders, info, waitForSessionState, waitForTeamStopped, gitProject, settleSession, teamRequest, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
const owned: string[] = [];
test.beforeEach(() => { owned.length = 0; });
test.afterEach(async ({ page }) => {
  for (const id of owned) {
    expect((await page.request.post(`/api/sessions/${id}/team/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    await waitForTeamStopped(page, id);
  }
});
test('person explicitly grants Orchestrator and stops the team', async ({ page }, testInfo) => {
  const suffix = randomUUID().slice(0, 8);
  const projectName = `Team demo ${suffix}`;
  const folder = `${info().workspace}/team-demo-${suffix}`;
  mkdirSync(folder, { recursive: true });
  const git = (...args: string[]) => execFileSync('git', ['-C', folder, '-c', 'user.name=Taylor', '-c', 'user.email=taylor@example.com', ...args]);
  git('init', '-q', '-b', 'main');
  writeFileSync(`${folder}/.gitattributes`, '*.psd filter=lfs diff=lfs merge=lfs -text\n');
  git('add', '.gitattributes');
  git('commit', '-qm', 'Start');
  await page.goto('/projects');
  await page.getByRole('button', { name: 'Register folder' }).click();
  await page.getByLabel('Name', { exact: true }).fill(projectName);
  await page.getByLabel('Workspace path').fill(folder);
  await page.getByRole('button', { name: 'Register', exact: true }).dblclick();
  await page.getByPlaceholder('Filter projects').fill(projectName);
  await page.getByRole('button', { name: 'Trust', exact: true }).click();
  const trusted = page.waitForResponse(response => response.request().method() === 'POST'
    && /\/api\/projects\/\d+\/trust$/.test(new URL(response.url()).pathname));
  await page.getByRole('button', { name: 'Trust this workspace', exact: true }).dblclick();
  expect((await trusted).ok()).toBe(true);
  await expect(page.getByText('not trusted', { exact: true })).toHaveCount(0);
  const projects = await (await page.request.get('/api/projects')).json();
  const project = projects.find((x: { name: string }) => x.name === projectName);

  await page.goto('/sessions');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const launchDialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await launchDialog.getByRole('button', { name: 'Rename', exact: true }).click();
  await launchDialog.getByLabel('Name', { exact: true }).fill('Taylor lead');
  await openAdvanced(launchDialog);
  await expect(page.getByText('--session-id', { exact: false }).first()).toBeVisible();
  await launchDialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(project.id));
  await expect(launchDialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(project.id));
  const role = launchDialog.getByRole('radio', { name: /With helpers/ });
  await expect(role).not.toBeChecked();
  await role.check();
  await expect(launchDialog.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
  await expect(launchDialog.getByLabel('At once', { exact: true })).toHaveValue('3');
  await expect(launchDialog.getByLabel('In total', { exact: true })).toHaveValue('10');
  await expect(launchDialog.getByLabel('Depth', { exact: true })).toHaveValue('2');
  await expect(launchDialog.getByLabel('Hours', { exact: true })).toHaveValue('');
  await page.getByRole('combobox', { name: 'Helper 1: Permissions', exact: true }).selectOption('bypassPermissions');
  await expect(page.getByRole('button', { name: 'Launch', exact: true })).toBeDisabled();
  await page.getByRole('combobox', { name: 'Helper 1: Permissions', exact: true }).selectOption('acceptEdits');
  await page.getByRole('combobox', { name: 'Helper agent 1', exact: true }).selectOption('cursor');
  await expect(page.getByRole('checkbox', { name: /Helper 1: Run everything without asking/ })).not.toBeChecked();
  await page.getByRole('combobox', { name: 'Helper agent 1', exact: true }).selectOption('claude');
  await launchDialog.getByRole('button', { name: 'Add allowed agent/profile' }).click();
  await launchDialog.getByRole('combobox', { name: 'Helper agent 2', exact: true }).selectOption('cursor');
  await launchDialog.getByRole('checkbox', { name: /Helper 2: Run everything without asking/ }).check();
  await launchDialog.getByRole('button', { name: 'Add allowed agent/profile' }).click();
  await launchDialog.getByRole('combobox', { name: 'Helper agent 3', exact: true }).selectOption('copilot');
  await launchDialog.getByRole('combobox', { name: 'Helper 3: Permissions', exact: true }).selectOption('all');
  await launchDialog.getByRole('checkbox', { name: /The selected helpers can run commands without asking/ }).check();
  await page.screenshot({ path: wikiShotPath(testInfo, "orchestrator-new-session.png"), fullPage: true });
  await expect(launchDialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(project.id));
  const launched = page.waitForResponse(response => new URL(response.url()).pathname === '/api/sessions' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Launch', exact: true }).dblclick();
  const response = await launched;
  expect(response.ok()).toBe(true);
  expect(response.request().postDataJSON().orchestrator).toMatchObject({ at_once: 3, total: 10, depth: 2, hours: null });
  const leadId = (await response.json()).id;
  owned.push(leadId);
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await waitForSessionState(page, leadId, 'idle', 'claude_hook');
  await page.getByRole('tab', { name: 'Team', exact: true }).click();
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await expect(page.getByText('cursor · No profile · Runs without asking', { exact: true })).toBeVisible();
  await expect(page.getByText('copilot · No profile · All permissions allowed', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Stop team', exact: true }).click();
  await page.getByRole('dialog', { name: 'Stop team?', exact: true }).getByRole('button', { name: 'Stop team', exact: true }).click();
  await waitForTeamStopped(page, leadId);
  owned.length = 0;
  await expect(page.getByRole('heading', { name: 'Stop confirmed', exact: true })).toBeVisible({ timeout: testInfo.timeout });
});

// Runtime coverage owns a fresh grant. It does not depend on the preceding UI
// test or spend its machine-confirmation budget repeating grant-form editing.
const runtimeTest = test.extend<{ runtimeTeam: { leadId: string; helperId: string } }>({
  runtimeTeam: [async ({ page }, use) => {
    const { id: projectId, folder, headers } = await gitProject(page, 'team-runtime');
    writeFileSync(`${folder}/.gitattributes`, '*.psd filter=lfs diff=lfs merge=lfs -text\n');
    execFileSync('git', ['-C', folder, 'add', '.gitattributes']);
    execFileSync('git', ['-C', folder, '-c', 'user.name=Taylor', '-c', 'user.email=taylor@example.com',
      '-c', 'commit.gpgsign=false', 'commit', '-qm', 'LFS fixture']);
    const response = await page.request.post('/api/sessions', { headers, data: {
      project_id: projectId, name: 'Taylor lead', provider: 'claude', worktree: true,
      orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'],
        at_once: 3, total: 10, depth: 2, hours: 8 },
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const leadId = (await response.json()).id;
    owned.push(leadId);
    await page.goto(`/sessions/${leadId}`);
    await waitForSessionState(page, leadId, 'idle', 'claude_hook');
    async function team(action: string, args: object = {}) {
      const [reply] = await Promise.all([
        teamRequest(leadId, action, args),
        settleSession(page, leadId),
      ]);
      expect(reply.ok, JSON.stringify(reply)).toBeTruthy();
      return reply.result;
    }
    await team('plan_update', { reason: 'Bounded demo', idea: 'Review the demo changes.',
      scope: { in: ['Demo review'], out: [], parked: [] }, phases: [{ key: 'review', title: 'Review demo' }] });
    await team('plan_ready', { reason: 'Demo scope confirmed' });
    const assignment = await team('assign', { goal: 'Review the demo changes.', task_type: 'implementation',
      acceptance: [], requirement_ids: [], need: { role: 'general', classification_reason: 'Bounded demo helper' } });
    const selected = await team('select', { assignment: assignment.id, pin: { agent: 'claude' } });
    const helper = await team('start', { assignment: assignment.id, selection: selected.id, name: 'Morgan helper' });
    const helperId = helper.id;
    await waitForSessionState(page, helperId, 'idle', 'claude_hook');
    await use({ leadId, helperId });
  }, { timeout: 90000 }],
});
runtimeTest('a granted team reports a helper permission and stops every member', async ({ page, runtimeTeam }, testInfo) => {
  const { leadId, helperId } = runtimeTeam;
  await page.getByRole('tab', { name: 'Team', exact: true }).click();
  await expandTeamPanels(page);
  await page.getByRole('tab', { name: 'Settings', exact: true }).click();
  await expandTeamPanels(page);
  await expect(page.getByText('1 / 10')).toBeVisible();
  await expect(page.getByRole('link', { name: /Morgan helper/ }).first()).toBeVisible();
  await expect(page.getByText('LFS files arrive as pointers; run git lfs checkout', { exact: true })).toBeVisible();
  control(helperId, 'permission');
  await waitForSessionState(page, helperId, 'waiting_input', 'claude_hook');
  await expect(page.getByRole('table', { name: 'Sessions', exact: true }).getByText('Needs you', { exact: true })).toBeVisible();
  await expect(page.getByText('Role granted')).toBeVisible();
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, "orchestrator-team-settings.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: wikiShotPath(testInfo, "orchestrator-team-settings-phone.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/inbox');
  await expect(page.getByText(` · Morgan helper · team ${leadId}`, { exact: false }).first()).toBeVisible();
  await page.goto(`/sessions/${leadId}?tab=team#settings`);
  await page.getByRole('button', { name: 'Stop team', exact: true }).click();
  await page.getByRole('dialog', { name: 'Stop team?', exact: true }).getByRole('button', { name: 'Stop team', exact: true }).click();
  await waitForTeamStopped(page, leadId);
  // Confirmation covers both members. Re-stopping them in afterEach adds two
  // unnecessary broker round trips to an already completed UI journey.
  owned.length = 0;
  await expect(page.getByRole('heading', { name: 'Stop confirmed', exact: true })).toBeVisible({ timeout: testInfo.timeout });
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  await openAdvanced(page.getByRole('dialog', { name: 'New session', exact: true }));
  await expect(page.getByRole('radio', { name: /With helpers/ })).not.toBeChecked();
  await expect(page.getByRole('radio', { name: /Once/ })).toBeChecked();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
});
