import { randomUUID } from 'node:crypto';
import { openAdvanced, wikiShotPath } from "./helpers";
import { expect, test } from './fixtures';
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import AxeBuilder from '@axe-core/playwright';
import { loadPage } from './loaded-page';
import { AUTH_FILE, csrfHeaders, fileRow } from './helpers';

test.use({ storageState: AUTH_FILE });
let ownedSession: string | undefined;
test.beforeEach(() => { ownedSession = undefined; });
test.afterEach(async ({ page }) => {
  if (ownedSession) expect((await page.request.post(`/api/sessions/${ownedSession}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
});

test('Development workspace: local creation, capabilities and isolated session', async ({ page }, testInfo) => {
  // Includes a full accessibility audit, real worktree launch and Markdown preview.
  test.slow();
  const workspaceName = `Demo studio ${randomUUID().slice(0, 8)}`;
  await page.goto('/workspaces');
  await page.getByRole('button', { name: 'New workspace', exact: true }).first().click();
  await page.getByRole('combobox', { name: 'Kind', exact: true }).selectOption('development');
  await page.getByLabel('Workspace name', { exact: true }).fill(workspaceName);
  await page.screenshot({ path: wikiShotPath(testInfo, "workspaces-development-create.png") });
  const created = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/workspaces').then(response => {
      expect(response.status(), 'real development workspace creation').toBe(201);
      return response;
    });
  await Promise.all([created, loadPage(page,
    () => page.getByRole('button', { name: 'Create workspace', exact: true }).click(),
    ['ProjectPage'], ['/api/projects'])]);
  await expect(page.getByRole('heading', { name: workspaceName, exact: true })).toBeVisible();
  await expect(page.getByText('Open pull requests', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Open issues', { exact: true })).toHaveCount(0);
  await page.getByRole('tablist', { name: 'Project sections' }).getByRole('tab', { name: 'Ports', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Ports', exact: true })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "workspaces-development-details.png"), fullPage: true });
  const projectId = page.url().match(/projects\/(\d+)/)![1];
  const projects = await (await page.request.get('/api/projects')).json();
  const workspace = projects.find((p: { id: number }) => p.id === Number(projectId));
  expect(execFileSync('git', ['-C', workspace.workspace_path, 'remote']).toString()).toBe('');
  const a11y = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await page.getByRole('button', { name: 'New session', exact: true }).last().click();
  await openAdvanced(page.getByRole('dialog'));
  await expect(page.getByRole('checkbox', { name: /Own worktree/ })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: /Own worktree/ })).toBeDisabled();
  await expect(page.getByLabel('Start from', { exact: true })).toHaveCount(0);
  await page.getByRole('dialog').getByRole('button', { name: 'Rename', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill('Build demo');
  const launching = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions');
  await page.getByRole('button', { name: 'Launch', exact: true }).dblclick();
  expect((await launching).status()).toBe(201);
  expect(await (await launching).finished()).toBeNull();
  await expect(page).toHaveURL(/\/sessions\//);
  const sessionId = page.url().match(/sessions\/([^/?]+)/)![1];
  ownedSession = sessionId;
  const session = await (await page.request.get(`/api/sessions/${sessionId}`)).json();
  expect(session.worktree_branch).toMatch(/^acc\/build-demo-/);
  expect(session.cwd).not.toBe(workspace.workspace_path);
  await page.getByRole('tab', { name: 'Changes' }).click();
  writeFileSync(`${session.cwd}/demo.md`, '# Private preview\n');
  await page.getByRole('tab', { name: 'Files', exact: true }).click();
  await page.getByRole("button", { name: fileRow("demo.md") }).click();
  await expect(page.getByRole('heading', { name: 'Private preview', exact: true })).toBeVisible();
  await page.goto('/workspaces');
  const studio = page.locator('section').filter({ has: page.locator(`a[href="/projects/${projectId}"]`) });
  // The card title and footer both link to this same project.
  await expect(studio.getByRole('link', { name: workspaceName, exact: true }).first()).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "workspaces.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await studio.getByRole('button', { name: `Workspace actions for ${workspaceName}` }).click();
  const addSession = await page.getByRole('menuitem', { name: 'Add session', exact: true }).boundingBox();
  expect(addSession!.x + addSession!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: wikiShotPath(testInfo, "workspaces-development-phone.png"), fullPage: true });
});
