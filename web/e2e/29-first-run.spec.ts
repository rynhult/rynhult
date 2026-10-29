import { expect, test } from "@playwright/test";
import { existsSync } from "node:fs";
import { AUTH_FILE, csrfHeaders, expectAxeClean, info, openAdvanced, waitForSessionState, wikiShotPath } from "./helpers";

/* UX review 2, beta item 1: from setup to a first project and session in the browser, with no server shell. */
test.use({ storageState: AUTH_FILE });
test.describe.configure({ mode: "serial" });

const workspaces = () => info().workspace.replace(/\/demo$/, "");

test("an empty install shows Get started, and each step has its action", async ({ page }, testInfo) => {
  const at = new Date().toISOString();
  await page.route("**/api/attention", route => route.fulfill({ json: [{
    id: 2901, kind: "telemetry_consent", severity: "info", source: "telemetry", verified: true,
    title: "Optional usage statistics and bug reports are available", detail: "Both choices are off by default.",
    session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
    acked_at: null, resolved_at: null, snoozed_until: null, publish_id: null, persistent: null,
  }] }));
  await page.route("**/api/projects", (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
  await page.route("**/api/sessions", (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
  await page.route("**/api/accounts", (route) => route.fulfill({ json: { claude: { logged_in: false }, account: "alex-agent", accounts: [] } }));
  await page.addInitScript(() => {  // a fresh browser once; a reload keeps what was chosen
    if (sessionStorage.getItem("e2e-fresh")) return;
    sessionStorage.setItem("e2e-fresh", "1");
    localStorage.removeItem("acc.getStarted");
    localStorage.removeItem("acc.getStarted.phone");
  });
  const headers = await csrfHeaders(page);
  await page.request.put("/api/preferences/phone-banner", { headers, data: { dismissed: false } });
  await page.goto("/");
  const card = page.getByRole("list", { name: "Get started steps" });
  await expect(card.getByRole("listitem")).toHaveCount(3);
  await expect(page.getByText("0 of 3 done")).toBeVisible();
  await expect(card.getByText("Connect your phone")).toHaveCount(0);  // a banner now, not a step (owner, 2026-10-23)
  await expect(card.getByRole("link", { name: "Open Providers" })).toHaveAttribute("href", "/providers");
  await expect(page.getByRole("button", { name: "Start a session" })).toHaveCount(2);  // the step and the empty live list
  await expect(page.getByRole("button", { name: "Add project" })).toBeVisible();  // the empty Projects card
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, "get-started.png") });

  await page.getByRole("button", { name: "Hide" }).click();
  await expect(card).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await expect(page.getByRole("list", { name: "Get started steps" })).toHaveCount(0);  // stays hidden
});

test("Connect your phone is a one-line banner until the person dismisses it", async ({ page, browser }, testInfo) => {
  const headers = await csrfHeaders(page);
  await page.request.put("/api/preferences/phone-banner", { headers, data: { dismissed: false } });
  await page.addInitScript(() => localStorage.removeItem("acc.getStarted.phone"));
  await page.goto("/");
  const banner = page.getByRole("complementary", { name: "Connect your phone" });
  await expect(banner.getByRole("link", { name: "Connect your phone" })).toHaveAttribute("href", "/guide/User-Guide/Connect-Your-Phone");
  expect((await banner.boundingBox())!.height).toBeLessThan(56);  // one line, not a card of steps
  await expectAxeClean(page);
  await banner.screenshot({ path: wikiShotPath(testInfo, "phone-banner.png") });
  await banner.getByRole("button", { name: "Dismiss" }).click();
  await expect(banner).toHaveCount(0);
  // Per person, not per browser: a fresh browser signed in as the same person doesn't show it either.
  const other = await browser.newContext({ storageState: AUTH_FILE });
  const fresh = await other.newPage();
  const asked = fresh.waitForResponse("**/api/preferences/phone-banner");
  await fresh.goto("/");
  expect(await (await asked).json()).toEqual({ dismissed: true });
  await expect(fresh.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await expect(fresh.getByRole("complementary", { name: "Connect your phone" })).toHaveCount(0);
  await other.close();
});

test("New project makes an empty, trusted folder under the workspaces root", async ({ page }, testInfo) => {
  // The folder line shows the server's workspaces root; the wiki's screenshot shows the usual one, not this run's.
  await page.route("**/api/auth/state", async (route) => {
    const state = await (await route.fetch()).json();
    await route.fulfill({ json: { ...state, instance: { ...state.instance, workspaces_root: "/srv/acc/workspaces" } } });
  });
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await expect(dialog.getByRole("radio", { name: "Empty folder" })).toHaveAttribute("aria-checked", "true");
  await dialog.getByLabel("Name", { exact: true }).fill("Hello app");
  await expect(dialog.getByText("/srv/acc/workspaces/hello-app")).toBeVisible();  // where it goes, before it's made
  await expect(dialog.getByRole("checkbox", { name: /Trust it for agent sessions/ })).toBeChecked();
  await expectAxeClean(page, "[role=dialog]");
  await dialog.screenshot({ path: wikiShotPath(testInfo, "new-project.png") });
  await dialog.getByRole("button", { name: "Create project" }).dblclick();
  await expect(dialog).toHaveCount(0);
  // The new project's page opens, with New session as the next step (first-run review).
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  await expect(page.getByRole("heading", { name: "Hello app", level: 1 })).toBeVisible();
  await expect(page.getByRole("main").getByRole("button", { name: "New session", exact: true })).toBeVisible();
  await expect(page.getByText("not trusted", { exact: true })).toHaveCount(0);  // trusted is the quiet default (PJ-2)
  expect(existsSync(`${workspaces()}/hello-app/.git`)).toBe(true);
});

test("Clone starts untrusted, explains a repository it can't reach in words, and creates nothing", async ({ page }) => {
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  const trust = dialog.getByRole("checkbox", { name: /Trust it for agent sessions/ });
  await dialog.getByRole("radio", { name: "Clone repository" }).click();
  await expect(trust).not.toBeChecked();  // a clone brings its own config and hooks: trust is a choice, not a default
  await expect(dialog.getByText("trust lets agents run with the repository's own config and hooks")).toBeVisible();
  await dialog.getByRole("radio", { name: "Empty folder" }).click();
  await expect(trust).toBeChecked();
  await dialog.getByRole("radio", { name: "Clone repository" }).click();
  await dialog.getByRole("textbox", { name: "Repository" }).fill("file:///etc");
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("etc");  // filled from the URL
  await dialog.getByRole("button", { name: "Clone and add" }).click();
  await expect(dialog.getByRole("alert")).toContainText("HTTPS or SSH repository URL");
  expect(existsSync(`${workspaces()}/etc`)).toBe(false);
});

test("New session trusts an untrusted project in place and keeps the draft", async ({ page }) => {
  await page.goto("/sessions");
  const headers = await csrfHeaders(page);
  const made = await page.request.post("/api/projects/new", { headers, data: { name: "Untrusted app", trust: false } });
  const project = await made.json();
  expect(project.trusted).toBe(false);
  // This fixture bypasses the UI mutation that invalidates the projects query. Load it before opening the picker.
  await page.reload();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
  await dialog.getByRole("button", { name: "Rename", exact: true }).click();
  await dialog.getByLabel("Name", { exact: true }).fill("first-run");
  await expect(dialog.getByText("Not trusted for agents yet.")).toBeVisible();
  await dialog.getByRole("button", { name: "Trust and launch" }).click();
  const ask = page.getByRole("dialog", { name: "Trust Untrusted app?" });
  await expect(ask).toContainText(`${workspaces()}/untrusted-app`);
  await ask.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("first-run");  // nothing lost
  await dialog.getByRole("button", { name: "Trust and launch" }).click();
  await ask.getByRole("button", { name: "Trust and launch" }).click();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  const after = await (await page.request.get("/api/projects")).json();
  expect(after.find((p: { id: number }) => p.id === project.id).trusted).toBe(true);
});

test("New session without projects offers New project on top, and Esc closes only that", async ({ page }) => {
  await page.route("**/api/projects", (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await expect(dialog.getByText("No project yet.")).toBeVisible();
  await dialog.getByRole("button", { name: "Rename", exact: true }).click();
  await dialog.getByLabel("Name", { exact: true }).fill("kept");
  await dialog.getByRole("button", { name: "New project" }).click();
  const inner = page.getByRole("dialog", { name: "New project" });
  await expect(inner.getByLabel("Name", { exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(inner).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("kept");
});

// Owner request: New project on another machine. lab-box (tests/demo_machine.py) relays to the test broker, so the
// folder is made by "its" broker, under the root that broker reports.
test("New project on another machine makes the folder there and registers it on that machine", async ({ page }) => {
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByRole("combobox", { name: "Machine" }).selectOption("lab-box");
  await expect(dialog.getByText("A folder for agent sessions, made on lab-box for you.")).toBeVisible();
  await expect(dialog.getByRole("radio", { name: "New GitHub repo" })).toHaveCount(0);  // Rynhult's token clones here only
  await dialog.getByLabel("Name", { exact: true }).fill("Lab app");
  await expect(dialog.getByText(`${workspaces()}/lab-app`)).toBeVisible();  // lab-box's broker's root, before it's made
  await expectAxeClean(page, "[role=dialog]");
  await dialog.getByRole("button", { name: "Create project" }).dblclick();
  await expect(dialog).toHaveCount(0);
  const projects = await (await page.request.get("/api/projects")).json();
  const made = projects.find((p: { slug: string }) => p.slug === "lab-app");
  expect(made).toMatchObject({ host: "lab-box", trusted: true, workspace_path: `${workspaces()}/lab-app` });
  expect(existsSync(`${workspaces()}/lab-app/.git`)).toBe(true);
});


test("Empty folder immediately launches a session With helpers", async ({ page }, testInfo) => {
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project", exact: true }).first().click();
  const create = page.getByRole("dialog", { name: "New project", exact: true });
  await expect(create.getByRole("radio", { name: "Empty folder", exact: true })).toHaveAttribute("aria-checked", "true");
  await create.getByLabel("Name", { exact: true }).fill("First team checkout");
  await create.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  const projectId = Number(new URL(page.url()).pathname.split("/").pop());
  await page.getByRole("main").getByRole("button", { name: "New session", exact: true }).click();
  const launch = page.getByRole("dialog", { name: "New session", exact: true });
  await expect(launch.getByRole("combobox", { name: "Project", exact: true })).toHaveValue(String(projectId));
  await openAdvanced(launch);
  await launch.getByRole("radio", { name: /^With helpers/ }).check();
  await expect(launch.getByRole("group", { name: "Team limits", exact: true })).toBeVisible();
  const responsePromise = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/sessions");
  await launch.getByRole("button", { name: "Launch", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(201);
  const session = await response.json();
  try {
    await expect(page).toHaveURL(new RegExp(`/sessions/${session.id}$`));
    expect(session.project_id).toBe(projectId);
    expect(session.worktree).toBeTruthy();
    expect(session.team_id).toBe(session.id);
    await waitForSessionState(page, session.id, "idle", "claude_hook");
    await expect(page.getByRole("dialog", { name: "New session", exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("empty-project-first-team.png") });
  } finally {
    expect((await page.request.post(`/api/sessions/${session.id}/team/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
});
