import { openAdvanced, wikiShotPath } from "./helpers";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, gitProject } from "./helpers";
import { randomUUID } from "node:crypto";

test.use({ storageState: AUTH_FILE });
const ownedSessions: string[] = [];
const ownedKeys: string[] = [];
const ownedServers: string[] = [];
test.beforeEach(() => {
  ownedSessions.length = 0; ownedKeys.length = 0; ownedServers.length = 0;
});
test.afterEach(async ({ page }) => {
  const headers = await csrfHeaders(page);
  for (const id of ownedSessions) expect((await page.request.post(`/api/sessions/${id}/stop`, { headers })).ok()).toBe(true);
  for (const id of ownedKeys) expect((await page.request.post(`/api/vault/keys/${id}/remove`, { headers })).ok()).toBe(true);
  for (const id of ownedServers) expect((await page.request.post(`/api/issue-servers/${id}/remove`, { headers })).ok()).toBe(true);
});

async function launchIssueSession(page: Page, wizard: Locator) {
  const launched = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/sessions");
  await wizard.getByRole("button", { name: "Launch", exact: true }).dblclick();
  const response = await launched;
  expect(response.ok(), await response.text()).toBe(true);
  const id = (await response.json()).id;
  ownedSessions.push(id);
  await expect(page).toHaveURL(new RegExp(`/sessions/${id}$`));
}

// Issue sources live in the project's "Issues and pull requests" tab; hidden tab panels stay mounted but aren't visible.
const openIssues = (page: import("@playwright/test").Page) =>
  page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Issues and pull requests", exact: true }).click();

test("connect sources with vault keys, filter lists and launch from GitLab and Linear", async ({ page }, testInfo) => {
  const project = await gitProject(page, "issue-demo");
  const { headers } = project;
  const key = await page.request.post("/api/vault/keys", { headers, data: {
    name: `Demo issue reader ${project.slug}`, variable: `DEMO_ISSUE_${randomUUID().slice(0, 8).toUpperCase()}`, value: "e2e-source-placeholder",
  } }).then((r) => r.json());
  ownedKeys.push(key.id);
  expect((await page.request.post(`/api/vault/keys/${key.id}/give`, { headers, data: { project_id: project.id } })).ok()).toBe(true);
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await page.getByRole("button", { name: "Connect issue sources" }).click();
  const config = page.getByRole("dialog", { name: "Issue sources" });
  await config.getByLabel("Repository remote").fill("git@gitlab.example.com:example/demo.git");
  await config.getByLabel("GitLab vault key").selectOption(key.id);
  await config.getByLabel("GitLab base URL").fill("https://gitlab.example.com");
  await config.getByLabel("Linear vault key").selectOption(key.id);
  await config.getByLabel("Linear team ID").fill("demo-team");
  await expect(config.getByLabel("Comment PR link on Linear")).not.toBeChecked();
  await config.getByRole("button", { name: "Save connections" }).dblclick();
  await expect(config).not.toBeVisible();
  const list = page.getByRole("region", { name: "External issues" });
  await expect(list.getByText("Fix demo parser", { exact: false })).toBeVisible();
  await list.getByLabel("Issue state").selectOption("closed");
  await expect(list.getByText("Closed demo task", { exact: false })).toBeVisible();
  await list.getByLabel("Issue state").selectOption("open");
  await list.getByLabel("Issue label").fill("bug");
  await list.getByLabel("Issue assignee").fill("alex");
  await list.getByRole("button", { name: "Apply filters" }).dblclick();
  await expect(list.getByText("Fix demo parser", { exact: false })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "project-issue-sources.png"), fullPage: true });
  const axe = await new AxeBuilder({ page }).include('[aria-label="External issues"]')
    .withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
  expect(axe.violations).toEqual([]);
  await list.getByRole("button", { name: "Start session for #7" }).click();
  const wizard = page.getByRole("dialog", { name: "New session" });
  await expect(wizard.getByLabel("Name", { exact: true })).toHaveValue("gitlab-7");
  await openAdvanced(wizard);
  await wizard.getByLabel("Own worktree").uncheck();
  await expect(wizard.getByText(/untrusted external issue data/).first()).toBeVisible();
  await launchIssueSession(page, wizard);
  await page.getByRole("tab", { name: "Setup" }).click();
  await page.getByRole("tabpanel").locator("summary").filter({ hasText: /^Exact inputs/ }).click();
  await expect(page.getByText(/Fix demo parser/).first()).toBeVisible();
  await expect(page.getByText("e2e-source-placeholder")).toHaveCount(0);

  await page.getByRole("button", { name: "New session", exact: true }).click();
  await wizard.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
  await wizard.getByLabel("Start from").selectOption("linear:issue");
  await wizard.getByRole("button", { name: "Use DEMO-7" }).click();
  await openAdvanced(wizard);  // the command preview shows the issue as untrusted context
  await expect(wizard.getByText(/Fix demo queue/).first()).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, "new-session-issue-source.png"), fullPage: true });
  await launchIssueSession(page, wizard);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await expect(list.getByText("Fix demo parser", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await list.screenshot({ path: wikiShotPath(testInfo, "project-issue-sources-mobile.png") });
});

test("source errors explain fixes rather than showing an empty list", async ({ page }, testInfo) => {
  const project = await gitProject(page, "issue-errors");
  await page.route("**/api/projects/*/issue-sources", (route) => route.fulfill({ json: {
    remote_url: null, linear: { key_id: "0000000000000000", team_id: "demo-team", comment_pr: false },
  } }));
  await page.route("**/api/projects/*/issue-sources/linear?*", (route) => route.fulfill({
    status: 422, json: { detail: "token expired; replace it in Settings > Keys" },
  }));
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await expect(page.getByRole("alert").filter({ hasText: "token expired" })).toBeVisible();
  await expect(page.getByText("No matching issues.")).toHaveCount(0);
});

test("owner confirms a self-hosted server and connects Forgejo pull requests", async ({ page }, testInfo) => {
  const project = await gitProject(page, "forge-demo");
  const { headers } = project;
  const base = `https://forge.example.lan/${project.slug}`;
  await page.goto('/settings#issue-sources');
  await page.getByRole('button', { name: 'Add git server' }).click();
  const server = page.getByRole('dialog', { name: 'Allow git server' });
  await server.getByLabel('Server HTTPS base URL').fill(base);
  await server.getByRole('button', { name: 'Resolve addresses' }).click();
  await expect(server.getByText('10.0.0.8', { exact: true })).toBeVisible();
  await expect(server.getByRole('button', { name: 'Allow server', exact: true })).toBeDisabled();
  const axe = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(['wcag2a', 'wcag2aa', 'wcag22aa']).analyze();
  expect(axe.violations).toEqual([]);
  await server.screenshot({ path: wikiShotPath(testInfo, 'settings-issue-server-confirm.png') });
  await server.getByLabel('I confirm these addresses belong to this git server').check();
  const allowed = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/issue-servers');
  await server.getByRole('button', { name: 'Allow server', exact: true }).click();
  const allowedResponse = await allowed;
  expect(allowedResponse.ok()).toBe(true);
  const serverId = (await allowedResponse.json()).id;
  ownedServers.push(serverId);
  await expect(server).not.toBeVisible();
  await page.getByRole('heading', { name: 'Issue sources', exact: true }).scrollIntoViewIfNeeded();
  const card = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Issue sources', exact: true }) });
  const serverRow = card.locator('div.flex.flex-wrap').filter({ has: page.getByText(base, { exact: true }) });
  await card.screenshot({ path: wikiShotPath(testInfo, 'settings-issue-servers.png') });
  await serverRow.getByRole('button', { name: 'Re-confirm addresses' }).click();
  const reconfirm = page.getByRole('dialog', { name: 'Re-confirm git server' });
  await reconfirm.getByRole('button', { name: 'Resolve addresses' }).click();
  await reconfirm.getByLabel('I confirm these addresses belong to this git server').check();
  await reconfirm.getByRole('button', { name: 'Confirm server', exact: true }).click();
  await expect(reconfirm).not.toBeVisible();
  await serverRow.getByRole('button', { name: 'Remove', exact: true }).click();
  await page.getByRole('dialog', { name: 'Remove allowed server?' }).getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(serverRow.getByText(base, { exact: true })).toBeVisible();
  const key = await page.request.post('/api/vault/keys', { headers, data: {
    name: `Forge reader ${project.slug}`, variable: `FORGE_${randomUUID().slice(0, 8).toUpperCase()}`, value: 'e2e-forge-placeholder',
  } }).then(r => r.json());
  ownedKeys.push(key.id);
  expect((await page.request.post(`/api/vault/keys/${key.id}/give`, { headers, data: { project_id: project.id } })).ok()).toBe(true);
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await page.getByRole('button', { name: 'Connect issue sources' }).click();
  const dialog = page.getByRole('dialog', { name: 'Issue sources' });
  await dialog.getByLabel('Repository remote').fill(`${base}/example/demo.git`);
  await dialog.getByLabel('Gitea / Forgejo vault key').selectOption(key.id);
  await dialog.getByLabel('Gitea / Forgejo server').selectOption(serverId);
  await dialog.screenshot({ path: wikiShotPath(testInfo, 'project-forge-connection.png') });
  await dialog.getByRole('button', { name: 'Save connections' }).click();
  await expect(dialog).not.toBeVisible();
  const list = page.getByRole('region', { name: 'External issues' });
  await expect(list.getByText('Fix forge parser', { exact: false })).toBeVisible();
  await page.getByRole('combobox', { name: 'Issue source', exact: true }).selectOption('gitea:pull_request');
  await expect(list.getByText('Fix forge parser', { exact: false })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await list.screenshot({ path: wikiShotPath(testInfo, 'project-forge-issues-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 900 });
  await list.getByRole('button', { name: 'Start session for #7' }).click();
  const wizard = page.getByRole('dialog', { name: 'New session' });
  await openAdvanced(wizard);
  await wizard.getByLabel('Own worktree').uncheck();
  await expect(wizard.getByText('gitea PR 7', { exact: true })).toBeVisible();
  await expect(wizard.getByText(/untrusted external issue data/).first()).toBeVisible();
  await launchIssueSession(page, wizard);
});
