import { openAdvanced, wikiShotPath } from "./helpers";
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, control, csrfHeaders, info, waitForSessionState } from './helpers';

test.use({ storageState: AUTH_FILE });
test('person starts alternatives, compares files, and keeps one without deleting branches', async ({ page }, testInfo) => {
  const suffix = randomUUID().slice(0, 8);
  const projectName = `Multi-run demo ${suffix}`;
  const sessionName = `Improve greeting ${suffix}`;
  const owned: string[] = [];
  try {
  const folder = `${info().workspace}/multirun-demo-${suffix}`;
  mkdirSync(folder, { recursive: true });
  const git = (...args: string[]) => execFileSync('git', ['-C', folder, '-c', 'user.name=Taylor', '-c', 'user.email=taylor@example.com', ...args]);
  git('init', '-q', '-b', 'main');
  writeFileSync(`${folder}/app.py`, 'print("hello")\n');
  git('add', '.');
  git('commit', '-qm', 'Start');
  await page.goto('/projects');
  await page.getByRole('button', { name: 'Register folder' }).click();
  await page.getByLabel('Name', { exact: true }).fill(projectName);
  await page.getByLabel('Workspace path').fill(folder);
  await page.getByRole('button', { name: 'Register', exact: true }).dblclick();
  await page.getByPlaceholder('Filter projects').fill(projectName);
  await page.getByRole('button', { name: 'Trust', exact: true }).click();
  await page.getByRole('button', { name: 'Trust this workspace', exact: true }).dblclick();
  await expect(page.getByText('not trusted', { exact: true })).toHaveCount(0);
  const projects = await (await page.request.get('/api/projects')).json();
  const project = projects.find((x: { name: string }) => x.name === projectName);

  await page.goto('/sessions');
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: 'Rename', exact: true }).click();
  await dialog.getByLabel('Name', { exact: true }).fill(sessionName);
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(project.id));
  await openAdvanced(dialog);
  await dialog.getByRole('radio', { name: /Several agents/ }).check();
  await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('status')).toHaveText('Add a task to start 2 runs.');
  await dialog.getByLabel('Task', { exact: true }).fill('Improve the demo greeting.');
  await dialog.getByRole('combobox', { name: 'Run 2: Agent', exact: true }).selectOption('claude');
  // The same model choice as one session (R6): the CLI's list, else Custom… for any id.
  await expect(dialog.getByRole('combobox', { name: 'Run 2: Model', exact: true })).toHaveValue('');
  await dialog.getByRole('combobox', { name: 'Run 2: Model', exact: true }).selectOption('__custom');
  await dialog.getByRole('textbox', { name: 'Run 2: Model', exact: true }).fill('opus');
  await dialog.getByRole('combobox', { name: 'Run 2: Permissions', exact: true }).selectOption('bypassPermissions');
  await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeDisabled();
  await dialog.getByRole('checkbox', { name: /Run 2: The agent will run commands without asking/ }).check();
  await dialog.getByRole('button', { name: 'Add run' }).click();
  await dialog.getByRole('combobox', { name: 'Run 3: Agent', exact: true }).selectOption('copilot');
  await expect(dialog.getByText('Exactly what will run')).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
  await dialog.getByLabel('Name', { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, "multirun-new-session.png"), fullPage: true });
  const launched = page.waitForResponse(response => new URL(response.url()).pathname === '/api/multiruns' && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).dblclick();
  expect((await launched).ok()).toBe(true);
  await expect(page).toHaveURL(/\/multiruns\/[0-9a-f]{12}$/);
  const url = page.url();
  const groupId = url.split('/').pop()!;
  const group = await (await page.request.get(`/api/multiruns/${groupId}`)).json();
  expect(group.runs).toHaveLength(3);
  const [left, right, third] = group.runs.map((r: { session: { id: string; worktree: string } }) => r.session);
  owned.push(left.id, right.id, third.id);
  await Promise.all([left, right].map(run => waitForSessionState(page, run.id, 'idle', 'claude_hook')));
  const usageReady = page.waitForResponse(async response => {
    if (new URL(response.url()).pathname !== `/api/multiruns/${groupId}` || response.request().method() !== 'GET') return false;
    if (!response.ok()) return true;
    const value = await response.json();
    return value.runs.some((run: { session?: { id: string }; usage?: { cost?: { total_cost_usd: number } } }) =>
      run.session?.id === left.id && run.usage?.cost?.total_cost_usd === 0.12);
  });
  writeFileSync(`${left.worktree}/app.py`, 'print("Hello, visitor")\n');
  writeFileSync(`${right.worktree}/other.txt`, 'An alternative greeting\n');
  control(left.id, 'tool');
  control(right.id, 'tool');
  writeFileSync(`${info().run_dir}/${left.id}/statusline.json`, JSON.stringify({
    cost: { total_cost_usd: 0.12 }, context_window: { total_input_tokens: 12, total_output_tokens: 7 },
  }));
  expect((await usageReady).ok()).toBe(true);
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${left.id}`)).json()).changes?.files).toBe(1);
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${right.id}`)).json()).changes?.files).toBe(1);
  // Status lines are read by the background ingest pass, which can trail the change count under load.
  await expect.poll(async () => (await (await page.request.get(`/api/multiruns/${groupId}`)).json())
    .runs.find((r: { session?: { id: string } }) => r.session?.id === left.id)?.usage?.cost?.total_cost_usd).toBe(0.12);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Compare Changes', exact: true })).toBeVisible();
  await expect(page.getByText('+print("Hello, visitor")', { exact: true })).toBeVisible();
  await expect(page.getByText('+An alternative greeting', { exact: true })).toBeVisible();
  await expect(page.getByText('Not changed in this run.', { exact: true }).first()).toBeVisible();
  // Each side names its real comparison base, as its Changes tab does (it used to read "uncommitted changes only").
  await expect(page.getByText(/: current branch merge-base with \S+ \([0-9a-f]{12}\)$/)).toHaveCount(2);
  await expect(page.getByText(/uncommitted changes only/)).toHaveCount(0);
  await expect(page.getByText('Cost: $0.12', { exact: true })).toBeVisible();
  await expect(page.getByText('Tokens: 12 in / 7 out', { exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: 'Right run', exact: true }).selectOption(third.id);
  await expect(page).toHaveURL(new RegExp(`right=${third.id}`));
  await expect(page.getByText('+An alternative greeting', { exact: true })).toHaveCount(0);
  await page.getByRole('combobox', { name: 'Right run', exact: true }).selectOption(right.id);
  await expect(page.getByText('+An alternative greeting', { exact: true })).toBeVisible();
  expect((await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()).violations).toEqual([]);
  await page.screenshot({ path: wikiShotPath(testInfo, "multirun-compare.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.getByRole('heading', { name: 'Compare Changes', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: wikiShotPath(testInfo, "multirun-compare-phone.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: 'Keep this one', exact: true }).first().click();
  await expect(page.getByText('Winner', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Stop and archive others', exact: true }).click();
  const confirm = page.getByRole('dialog');
  await expect(confirm).toContainText('branches');
  await confirm.getByRole('button', { name: 'Stop and archive others', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${right.id}`)).json()).desired_state).toBe('archived');
  const branches = git('branch', '--format=%(refname:short)').toString();
  expect(branches).toContain(group.runs[1].session.worktree_branch);
  await page.goto('/sessions');
  const row = page.locator('details').filter({ has: page.locator('summary', { hasText: sessionName }) });
  await expect(row).toBeVisible();
  await row.locator('summary').click();
  await expect(row.getByRole('link', { name: 'Open the comparison' })).toBeVisible();
  await expect(row.getByRole('link', { name: /Improve greeting/ }).first()).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "multirun-sessions.png"), fullPage: true });
  await page.goto(url);
  await expect(page.getByText('+An alternative greeting', { exact: true })).toBeVisible();
  } finally {
    for (const id of owned) {
      expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    }
  }
});
