import { openAdvanced, wikiShotPath } from "./helpers";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, info } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Issue sources live in the project's "Issues and pull requests" tab; hidden tab panels stay mounted but aren't visible.
const openIssues = (page: import("@playwright/test").Page) =>
  page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Issues and pull requests", exact: true }).click();

test("connect sources with vault keys, filter lists and launch from GitLab and Linear", async ({ page }, testInfo) => {
  await page.goto("/projects");
  const csrf = await page.context().cookies().then((cookies) => cookies.find((c) => c.name === "acc_csrf")!.value);
  const headers = { "X-ACC-CSRF": csrf };
  const created = await page.request.post("/api/projects", { headers, data: {
    slug: "issue-demo", name: "Issue demo", workspace_path: info().workspace,
  } });
  expect(created.status()).toBe(201);
  const project = await created.json();
  expect((await page.request.post(`/api/projects/${project.id}/trust`, { headers })).ok()).toBeTruthy();
  const key = await page.request.post("/api/vault/keys", { headers, data: {
    name: "Demo issue reader", variable: "DEMO_ISSUE_TOKEN", value: "e2e-source-placeholder",
  } }).then((r) => r.json());
  await page.request.post(`/api/vault/keys/${key.id}/give`, { headers, data: { project_id: project.id } });
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
  await wizard.getByLabel("Own worktree").uncheck(); // the shared fake checkout is not a git worktree
  await expect(wizard.getByText(/untrusted external issue data/).first()).toBeVisible();
  await wizard.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
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
  await wizard.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await expect(list.getByText("Fix demo parser", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await list.screenshot({ path: wikiShotPath(testInfo, "project-issue-sources-mobile.png") });
});

test("source errors explain fixes rather than showing an empty list", async ({ page }, testInfo) => {
  await page.route("**/api/projects/*/issue-sources", (route) => route.fulfill({ json: {
    remote_url: null, linear: { key_id: "0000000000000000", team_id: "demo-team", comment_pr: false },
  } }));
  await page.route("**/api/projects/*/issue-sources/linear?*", (route) => route.fulfill({
    status: 422, json: { detail: "token expired; replace it in Settings > Keys" },
  }));
  await page.goto("/projects");
  const projects = await page.request.get("/api/projects").then((r) => r.json());
  await page.goto(`/projects/${projects[0].id}`);
  await openIssues(page);
  await expect(page.getByRole("alert").filter({ hasText: "token expired" })).toBeVisible();
  await expect(page.getByText("No matching issues.")).toHaveCount(0);
});

test("owner confirms a self-hosted server and connects Forgejo pull requests", async ({ page }, testInfo) => {
  await page.goto('/settings#issue-sources');
  await page.getByRole('button', { name: 'Add git server' }).click();
  const server = page.getByRole('dialog', { name: 'Allow git server' });
  await server.getByLabel('Server HTTPS base URL').fill('https://forge.example.lan');
  await server.getByRole('button', { name: 'Resolve addresses' }).click();
  await expect(server.getByText('10.0.0.8', { exact: true })).toBeVisible();
  await expect(server.getByRole('button', { name: 'Allow server', exact: true })).toBeDisabled();
  const axe = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(['wcag2a', 'wcag2aa', 'wcag22aa']).analyze();
  expect(axe.violations).toEqual([]);
  await server.screenshot({ path: wikiShotPath(testInfo, 'settings-issue-server-confirm.png') });
  await server.getByLabel('I confirm these addresses belong to this git server').check();
  await server.getByRole('button', { name: 'Allow server', exact: true }).click();
  await expect(server).not.toBeVisible();
  await page.getByRole('heading', { name: 'Issue sources', exact: true }).scrollIntoViewIfNeeded();
  const card = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Issue sources', exact: true }) });
  await card.screenshot({ path: wikiShotPath(testInfo, 'settings-issue-servers.png') });
  await card.getByRole('button', { name: 'Re-confirm addresses' }).click();
  const reconfirm = page.getByRole('dialog', { name: 'Re-confirm git server' });
  await reconfirm.getByRole('button', { name: 'Resolve addresses' }).click();
  await reconfirm.getByLabel('I confirm these addresses belong to this git server').check();
  await reconfirm.getByRole('button', { name: 'Confirm server', exact: true }).click();
  await expect(reconfirm).not.toBeVisible();
  await card.getByRole('button', { name: 'Remove', exact: true }).click();
  await page.getByRole('dialog', { name: 'Remove allowed server?' }).getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(card.getByText('https://forge.example.lan', { exact: true })).toBeVisible();
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const headers = { 'X-ACC-CSRF': csrf };
  const project = await page.request.post('/api/projects', { headers, data: {
    slug: 'forge-demo', name: 'Forge demo', workspace_path: info().workspace,
  } }).then(r => r.json());
  await page.request.post(`/api/projects/${project.id}/trust`, { headers });
  const key = await page.request.post('/api/vault/keys', { headers, data: {
    name: 'Forge reader', variable: 'FORGE_TOKEN', value: 'e2e-forge-placeholder',
  } }).then(r => r.json());
  await page.request.post(`/api/vault/keys/${key.id}/give`, { headers, data: { project_id: project.id } });
  await page.goto(`/projects/${project.id}`);
  await openIssues(page);
  await page.getByRole('button', { name: 'Connect issue sources' }).click();
  const dialog = page.getByRole('dialog', { name: 'Issue sources' });
  await dialog.getByLabel('Repository remote').fill('https://forge.example.lan/example/demo.git');
  await dialog.getByLabel('Gitea / Forgejo vault key').selectOption(key.id);
  const entries = await page.request.get('/api/issue-servers').then(r => r.json());
  await dialog.getByLabel('Gitea / Forgejo server').selectOption(entries.find((s: { base_url: string }) => s.base_url === 'https://forge.example.lan').id);
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
  await wizard.getByRole('button', { name: 'Launch', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
});
