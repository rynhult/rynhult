import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { AUTH_FILE, control, info, openAdvanced, shot, totp, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });
test.describe.configure({ mode: "serial" });

let sessionId = "";
// Numeric topic suffixes previously masqueraded as a date/collision suffix (gate regressions).
const run = String(parseInt(randomUUID().slice(0, 8), 16) % 100_000_000).padStart(8, "0");
const fixture = (name: string) => [name, test.info().repeatEachIndex, run].join(name.includes(" ") ? " " : "-");
const workspace = () => path.join(info().workspace, fixture("app"));
const ownedSessions = new Set<string>();
const ownedKeys = new Set<string>();
const ownedInvites = new Set<string>();
const ownedCatalog = new Set<"mcp" | "skill">();
let captures: Promise<void>[] = [];
let installedCopilot = false;
let editedQa = false;

test.beforeAll(() => {
  ownedSessions.clear();
  ownedKeys.clear();
  ownedInvites.clear();
  ownedCatalog.clear();
  installedCopilot = false;
  editedQa = false;
});

test.beforeEach(async ({ page }) => {
  captures = [];
  page.on("response", response => {
    if (response.request().method() !== "POST") return;
    const endpoint = new URL(response.url()).pathname;
    if (response.status() === 200 && ["/api/catalog/mcps", "/api/catalog/skills"].includes(endpoint)) {
      ownedCatalog.add(endpoint === "/api/catalog/mcps" ? "mcp" : "skill");
    }
    if (response.status() !== 201) return;
    if (!["/api/sessions", "/api/vault/keys", "/api/vault/import", "/api/users/invites"].includes(endpoint)) return;
    captures.push(response.json().then(value => {
      if (endpoint === "/api/sessions") ownedSessions.add(value.id);
      if (endpoint === "/api/vault/keys") ownedKeys.add(value.id);
      if (endpoint === "/api/vault/import") value.added.forEach((key: { id: string }) => ownedKeys.add(key.id));
      if (endpoint === "/api/users/invites") ownedInvites.add(value.id);
    }));
  });
});

test.afterEach(async () => {
  await Promise.all(captures);
});

test.afterAll(async ({ request }) => {
  const state = await request.storageState();
  const csrf = state.cookies.find(cookie => cookie.name === "acc_csrf")!.value;
  const headers = { "X-ACC-CSRF": csrf, Origin: info().origin };
  const cleanup = async (endpoint: string, data?: { resources: "archive" }) => {
    const response = await request.post(endpoint, { headers, data });
    expect.soft([200, 404], `cleanup ${endpoint}`).toContain(response.status());
  };
  for (const id of ownedSessions) await cleanup(`/api/sessions/${id}/stop`);
  for (const id of ownedKeys) await cleanup(`/api/vault/keys/${id}/remove`);
  for (const id of ownedInvites) await cleanup(`/api/users/invites/${id}/revoke`);
  const people = await request.get("/api/users");
  expect(people.status()).toBe(200);
  const roster = await people.json();
  const member = roster.users.find((user: { username: string }) => user.username === fixture("blair"));
  if (member) await cleanup(`/api/users/${member.id}/remove`, { resources: "archive" });
  await cleanup(`/api/nodes/${fixture("build-box")}/remove`);
  await cleanup(`/api/nodes/${fixture("release-box")}/remove`);
  for (const kind of ownedCatalog) {
    await cleanup(`/api/catalog/${kind}/${fixture(kind === "mcp" ? "shadcn" : "dense-dashboards")}/remove`);
  }
  if (editedQa) await cleanup("/api/catalog/profiles/qa/reset");
  if (installedCopilot) {
    expect((await request.post("/api/packages/copilot/remove?machine=lab-box", { headers })).status()).toBe(202);
    await expect.poll(async () => {
      const response = await request.get("/api/packages?machine=lab-box");
      expect(response.status()).toBe(200);
      return (await response.json()).packages.copilot.installed ?? null;
    }).toBeNull();
  }
});

test("overview renders and has no horizontal scroll", async ({ page }, testInfo) => {
  await page.route("**/api/sessions", route => route.fulfill({ json: [] }));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await expect(page.getByText("No live sessions")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await shot(page, testInfo.project.name, "overview-empty");
});

test("register and trust a project", async ({ page }, testInfo) => {
  mkdirSync(workspace(), { recursive: true });
  await page.goto("/projects");
  await page.getByRole("button", { name: "Register folder" }).click();
  await page.getByRole("dialog").getByLabel("Name", { exact: true }).fill(fixture("Demo"));
  await page.getByLabel("Workspace path").fill(workspace());
  await page.getByRole("button", { name: "Register", exact: true }).dblclick();
  const card = page.locator("section").filter({ has: page.getByRole("link", { name: fixture("Demo"), exact: true }) });
  await expect(card.getByText("not trusted")).toBeVisible();
  await card.getByRole("button", { name: "Trust", exact: true }).click();
  await expect(page.getByText(/without each agent asking/)).toBeVisible();  // words first (CO-1)
  await expect(page.getByText("hasTrustDialogAccepted")).toBeHidden();
  await page.getByText("Exactly what Rynhult changes").click();
  await expect(page.getByText(/hasTrustDialogAccepted/)).toBeVisible();
  await shot(page, testInfo.project.name, "trust-dialog");
  await page.getByRole("button", { name: "Trust this workspace" }).dblclick();
  await expect(card.getByText("not trusted", { exact: true })).toHaveCount(0);  // other projects can remain untrusted
  await shot(page, testInfo.project.name, "projects");
});

test("new session: preview shows the exact command, launch opens the terminal", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Rename", exact: true }).click();
  await page.getByRole("dialog").getByLabel("Name", { exact: true }).fill(fixture("e2e"));
  await page.getByRole("combobox", { name: "Project", exact: true }).selectOption({ label: fixture("Demo") });
  await openAdvanced(page.getByRole("dialog"));
  const preview = page.locator("text=--session-id");
  await expect(preview).toBeVisible();
  await expect(page.getByText("--permission-mode acceptEdits")).toBeVisible();
  await page.getByLabel("Permissions").selectOption("bypassPermissions");
  await expect(page.getByRole("button", { name: "Launch", exact: true })).toBeDisabled();  // needs the explicit checkbox
  await page.getByLabel("Permissions").selectOption("acceptEdits");
  await shot(page, testInfo.project.name, "new-session");
  await page.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  sessionId = page.url().split("/").pop()!;
  await expect(page.getByText("Read-write")).toBeVisible();
  await expect(page.locator(".xterm-rows")).toContainText("fake-claude ready", { timeout: 15_000 });
  await expect(page.locator(".xterm-rows")).not.toContainText("python3*");  // no tmux status bar
  await expect(page.getByText("Idle").first()).toBeVisible({ timeout: 15_000 });
  await shot(page, testInfo.project.name, "session-terminal");
});

test("second window is read-only until it takes control", async ({ browser }, testInfo) => {
  const ctx1 = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
  const ctx2 = await browser.newContext({ storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });
  const one = await ctx1.newPage();
  const two = await ctx2.newPage();
  await one.goto(`/sessions/${sessionId}`);
  await expect(one.getByText("Read-write")).toBeVisible();
  await two.goto(`/sessions/${sessionId}`);
  await expect(two.getByText(/Read-only · another window/)).toBeVisible();
  await two.getByRole("button", { name: "Take control" }).click();
  await expect(two.getByText("Read-write")).toBeVisible();
  await expect(one.getByText("Another window took control of this terminal.", { exact: true })).toBeVisible();
  await shot(one, testInfo.project.name, "control-taken");
  await ctx1.close();
  await ctx2.close();
});

test("permission prompt reaches the inbox and clears on interrupt", async ({ page }, testInfo) => {
  await page.goto("/inbox");
  const notice = page.getByText(`${fixture("e2e")} needs your input`, { exact: true });
  await expect(notice).toHaveCount(0);
  const inbox = page.getByRole("navigation", { name: "Main", exact: true }).getByRole("link", { name: /^Inbox/ });
  const badge = inbox.locator('[aria-label$=" open"]');
  const response = await page.request.get("/api/attention");
  expect(response.status()).toBe(200);
  const before = (await response.json()).length;
  const expectBadge = async (count: number) => {
    if (count === 0) await expect(badge).toHaveCount(0);
    else await expect(badge).toHaveText(String(count));
  };
  await expectBadge(before);
  control(sessionId, "permission");
  await expect(notice).toBeVisible({ timeout: 10_000 });
  await expectBadge(before + 1);
  await shot(page, testInfo.project.name, "inbox");
  await page.goto(`/sessions/${sessionId}`);
  await expect(page.getByText("Needs you").first()).toBeVisible();
  await page.getByRole("button", { name: "Interrupt" }).click();
  await expect(page.getByText("Idle").first()).toBeVisible();
  await page.goto("/inbox");
  await expect(notice).toHaveCount(0);
  await expectBadge(before);
});

test("activity and launch spec tabs", async ({ page }, testInfo) => {
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole("tab", { name: "Activity" }).click();
  await expect(page).toHaveURL(/\?tab=activity$/);
  await expect(page.getByText("Session started").first()).toBeVisible();
  await page.reload();
  await expect(page.getByText("Session started").first()).toBeVisible();  // tab survives a reload
  await shot(page, testInfo.project.name, "activity");
  await page.getByRole("tab", { name: "Setup" }).click();
  await page.getByRole("tabpanel").locator("summary").filter({ hasText: /^Generated files/ }).click();
  await page.getByRole("combobox", { name: "Generated file", exact: true }).selectOption("settings.json");
  await expect(page.getByRole("region", { name: "settings.json", exact: true })).toBeVisible();
  await shot(page, testInfo.project.name, "launch-spec");
});

test("artifacts tab: screenshots open larger, logs show in place", async ({ page }, testInfo) => {
  await page.goto(`/sessions/${sessionId}`);
  await expect(page.getByRole("tab", { name: /Changes/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Artifacts/ })).toHaveCount(0);  // only once there are some
  const folder = path.join(workspace(), ".acc", "artifacts");
  mkdirSync(folder, { recursive: true });
  copyFileSync(path.resolve("../docs/wiki/.attachments/session-activity.png"), path.join(folder, "activity.png"));
  writeFileSync(path.join(folder, "tests.log"), "pytest -q\n412 passed in 61.2s\n");
  await page.reload();
  const tab = page.getByRole("tab", { name: /Artifacts/ });
  await expect(tab).toContainText("2");
  await tab.click();
  await expect(page).toHaveURL(/\?tab=artifacts$/);
  const thumb = page.getByRole("img", { name: "activity.png" });
  await expect(thumb).toBeVisible();
  await expect.poll(() => thumb.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await expect(page.getByLabel("Contents of tests.log")).toContainText("412 passed");
  await expect(page.getByRole("link", { name: "Download tests.log" })).toBeVisible();
  const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).exclude(".xterm").analyze();
  expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  await shot(page, testInfo.project.name, "session-artifacts");
  await page.getByRole("button", { name: "Open activity.png" }).focus();
  await page.keyboard.press("Enter");  // keyboard opens the larger view
  const dialog = page.getByRole("dialog", { name: "activity.png" });
  await expect(dialog.getByRole("img", { name: "activity.png" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  const served = await page.request.get(`/api/sessions/${sessionId}/artifacts/tests.log`);
  expect(served.headers()["x-content-type-options"]).toBe("nosniff");
  expect(served.headers()["content-disposition"]).toContain("attachment");
});

test("stop keeps the conversation and resume brings it back", async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByRole("dialog", { name: `Stop ${fixture("e2e")}?` })).toBeVisible();  // in-app confirm, not window.confirm
  await page.getByRole("button", { name: "Stop session" }).dblclick();
  await expect(page.getByText("Stopped").first()).toBeVisible();
  await page.getByRole("button", { name: "Resume", exact: true }).dblclick();
  await expect(page.getByText("Idle").first()).toBeVisible({ timeout: 15_000 });
});

test("grid shows live terminals side by side and remembers the choice", async ({ page }, testInfo) => {
  await page.goto(`/grid?ids=${sessionId}`);
  const tile = page.getByRole("region", { name: new RegExp(`^${fixture("e2e")}`) });
  await expect(tile).toBeVisible();
  await expect(tile.locator(".xterm-rows")).toContainText("fake-claude", { timeout: 15_000 });
  await expect(page).toHaveURL(new RegExp(`ids=[^&]*${sessionId}`));
  await page.getByRole("button", { name: "Choose sessions" }).first().click();
  await expect(page.getByRole("dialog", { name: "Choose sessions" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await tile.getByRole("button", { name: /Remove .* from the grid/ }).click();
  await expect(tile).toHaveCount(0);
  await shot(page, testInfo, "grid");
});

test("wizard: a profile pre-fills skills, agents and MCPs and the preview shows them", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Rename", exact: true })).toBeVisible();  // the form starts clean after the earlier launch
  await page.getByRole("dialog").getByRole("button", { name: "Rename", exact: true }).click();
  await expect(page.getByRole("dialog").getByLabel("Name", { exact: true })).toHaveValue("");
  await page.getByRole("dialog").getByLabel("Name", { exact: true }).fill("wizard");
  await page.getByLabel("Profile").selectOption("qa");
  await openAdvanced(page.getByRole("dialog"));
  await page.getByRole("button", { name: /Skills and tools/ }).click();
  await expect(page.getByRole("button", { name: "e2e-runner", pressed: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "playwright", pressed: true })).toBeVisible();
  await expect(page.getByText("--plugin-dir")).toBeVisible();
  await expect(page.getByText("--strict-mcp-config")).toBeVisible();
  await page.getByRole("button", { name: "playwright", pressed: true }).click();  // deselect
  await expect(page.getByRole("button", { name: "playwright", pressed: false })).toBeVisible();
  await page.getByRole("combobox", { name: "Agent", exact: true }).selectOption("codex");
  await expect(page.getByRole("button", { name: "e2e-runner" })).toHaveCount(0);  // Claude-only agents disappear
  await shot(page, testInfo.project.name, "wizard");
  await page.getByRole("button", { name: "Close" }).click();
  await page.goto("/catalog");
  await expect(page.getByRole("tab", { name: /Profiles/ })).toBeVisible();
  await page.getByRole("tab", { name: /Skills/ }).click();
  await expect(page.getByText("superpowers").first()).toBeVisible();
  await shot(page, testInfo.project.name, "catalog");
});

test("catalog: add an MCP server and a skill, assign them, edit a profile", async ({ page }, testInfo) => {
  page.on("dialog", (d) => d.accept());
  const dialog = page.getByRole("dialog");
  await page.goto("/catalog");
  await page.getByRole("button", { name: "Add MCP server" }).click();
  await dialog.getByLabel("ID", { exact: true }).fill(fixture("shadcn"));
  await dialog.getByLabel("Arguments").fill("-y\nshadcn\nmcp");
  await expect(page.getByText("No pinned version")).toBeVisible();
  await dialog.getByLabel("Arguments").fill("-y\nshadcn@4.22.0\nmcp");
  await expect(page.getByText("No pinned version")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Frontend", exact: true }).click();
  await shot(page, testInfo.project.name, "catalog-add-mcp");
  await dialog.getByRole("button", { name: "Add to catalog" }).dblclick();
  await expect(page.getByText(`mcp · ${fixture("shadcn")}`)).toBeVisible();  // Frontend profile now includes it
  await page.getByRole("tab", { name: /MCP servers/ }).click();
  await expect(page.getByText("npx -y shadcn@4.22.0 mcp")).toBeVisible();

  await page.getByRole("tab", { name: /Skills/ }).click();
  await page.getByRole("button", { name: "Add skill or agent" }).click();
  await page.getByRole("tab", { name: "Write one" }).click();
  await dialog.getByLabel("SKILL.md").fill(`---\nname: ${fixture("dense-dashboards")}\ndescription: Dense ops layouts\n---\n\nKeep it dense.`);
  await dialog.getByLabel("ID", { exact: true }).fill(fixture("dense-dashboards"));
  await shot(page, testInfo.project.name, "catalog-add-skill");
  await dialog.getByRole("button", { name: "Add to catalog" }).dblclick();
  await page.getByRole("searchbox", { name: "Filter" }).fill(fixture("dense-dashboards"));
  await expect(page.getByText("Not in any profile")).toBeVisible();
  await page.getByRole("button", { name: `Profiles for ${fixture("dense-dashboards")}` }).click();
  await dialog.getByRole("button", { name: "UX/UI designer" }).click();
  await dialog.getByRole("button", { name: "Save" }).dblclick();
  await expect(page.getByText("UX/UI designer", { exact: true }).first()).toBeVisible();
  await page.getByRole("searchbox", { name: "Filter" }).fill("");
  await page.getByRole("tab", { name: /Profiles/ }).click();

  await page.getByRole("button", { name: "Edit QA" }).click();
  await dialog.getByLabel("Description").fill("Edited from the dashboard");
  await shot(page, testInfo.project.name, "catalog-edit-profile");
  editedQa = true;
  await dialog.getByRole("button", { name: "Save" }).dblclick();
  await expect(page.getByText("Edited from the dashboard")).toBeVisible();
  await expect(page.getByText("edited in Rynhult").first()).toBeVisible();
  await shot(page, testInfo.project.name, "catalog-managed");
  await page.getByRole("button", { name: "Edit QA" }).click();
  await page.getByRole("button", { name: "Reset to repository version" }).dblclick();
  await expect(page.getByText("Edited from the dashboard")).toHaveCount(0);
  editedQa = false;

  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await dialog.getByLabel("Profile").selectOption("frontend");
  await openAdvanced(dialog);
  await dialog.getByRole("button", { name: /Skills and tools/ }).click();
  await expect(page.getByRole("button", { name: fixture("shadcn"), pressed: true })).toBeVisible();
  await shot(page, testInfo.project.name, "new-session-skills");
  await page.getByRole("button", { name: "Close" }).click();
});

async function promoteLocalWorkspace(page: Page, testInfo: TestInfo, topic: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: "Local workspace" }).click();
  await dialog.getByLabel("Topic").fill(topic);
  await expect(dialog.getByRole("combobox", { name: "Profile" })).toHaveValue("researcher");  // standalone profiles only
  await openAdvanced(dialog);
  await expect(dialog.getByText(new RegExp(`local/${topic.toLowerCase().replaceAll(" ", "-")}-\\d{8}`)).first()).toBeVisible();  // the preview's folder
  await shot(page, testInfo.project.name, "new-workspace");
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await expect(page.getByText("local workspace").first()).toBeVisible();
  await page.getByRole("tab", { name: "Findings" }).click();
  await expect(page.getByRole("heading", { name: "Findings" })).toBeVisible();
  await shot(page, testInfo.project.name, "workspace-findings");
  await page.getByRole("button", { name: "Promote to project" }).click();
  await expect(dialog.getByLabel("Project name", { exact: true })).toHaveValue(topic);
  await expect(dialog.getByLabel("Slug", { exact: true })).toHaveValue(topic.toLowerCase().replaceAll(" ", "-"));
  await page.getByRole("dialog").getByRole("checkbox", { name: /Create a private GitHub repository/ }).uncheck();
  await shot(page, testInfo.project.name, "promote-dialog");
  await page.getByRole("dialog").getByRole("button", { name: "Promote" }).dblclick();
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  await expect(page.getByRole("heading", { name: topic, level: 1 })).toBeVisible();
}

test("local workspace: research without a project, then promote it", async ({ page }, testInfo) => {
  await promoteLocalWorkspace(page, testInfo, fixture("Beyblade tournament apps"));
});

test("workspaces page: cards, and a second session in the same folder", async ({ page }, testInfo) => {
  await promoteLocalWorkspace(page, testInfo, fixture("Beyblade tournament apps cards"));
  await page.goto("/workspaces");
  await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
  const card = page.locator("section", { hasText: fixture("Beyblade tournament apps cards") });
  await expect(card.getByText("promoted")).toBeVisible();
  await shot(page, testInfo.project.name, "workspaces");
  await card.getByRole("button", { name: `Workspace actions for ${fixture("Beyblade tournament apps cards")}`, exact: true }).click();
  await shot(page, testInfo.project.name, "workspace-actions");
  await page.getByRole("menuitem", { name: "Add session", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: "Workspace" })).toHaveValue(new RegExp(`^${fixture("beyblade-tournament-apps-cards")}-\\d{8}$`));
  await expect(dialog.getByRole("combobox", { name: "Profile" })).toHaveValue("council");
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await page.goto("/workspaces");
  await expect(card.getByText("council", { exact: true }).first()).toBeVisible();
});

test("wiki: tree from .order, pages, images and links (Azure DevOps layout)", async ({ page }, testInfo) => {
  const root = path.join(workspace(), "docs", "wiki");
  mkdirSync(path.join(root, "User-Guide"), { recursive: true });
  mkdirSync(path.join(root, ".attachments"), { recursive: true });
  writeFileSync(path.join(root, ".order"), "Home\nUser-Guide\n");
  writeFileSync(path.join(root, "Home.md"), "# Demo wiki\n\nStart with [Getting started](/User-Guide/Getting-Started). A [broken link](/%E0%A4%A) must not break the page.\n\n![Overview](/.attachments/dot.png)\n\n| Area | Status |\n|---|---|\n| Sessions | done |\n");
  writeFileSync(path.join(root, "User-Guide.md"), "# User guide");
  writeFileSync(path.join(root, "User-Guide", "Getting-Started.md"), "# Getting started\n\n- [x] Install\n- [ ] Configure");
  writeFileSync(path.join(root, ".attachments", "dot.png"), Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64"));
  await page.goto("/projects");
  await page.getByRole("link", { name: fixture("Demo"), exact: true }).click();
  await page.getByRole("link", { name: "Wiki" }).click();
  await expect(page.locator("article").getByRole("heading", { name: "Demo wiki" })).toBeVisible();
  const tree = page.getByRole("navigation", { name: "Wiki pages" });
  await expect(tree.getByRole("link")).toHaveText(["Home", "User Guide", "Getting Started"]);
  await expect(page.getByRole("cell", { name: "Sessions" })).toBeVisible();  // GFM table
  const img = page.getByRole("img", { name: "Overview" });
  await expect(img).toBeVisible();
  await shot(page, testInfo.project.name, "project-wiki");
  expect(await img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(1);
  await shot(page, testInfo.project.name, "wiki");
  await page.locator("article").getByRole("link", { name: "Getting started" }).click();
  await expect(page).toHaveURL(/\/wiki\/User-Guide\/Getting-Started$/);
  await expect(page.locator("article").getByRole("heading", { name: "Getting started" })).toBeVisible();
  await expect(page.getByRole("checkbox").first()).toBeChecked();
  await shot(page, testInfo.project.name, "project-wiki-page");
});

test("installable app and notifications settings", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/manifest.webmanifest");
  const manifest = await (await page.request.get("/manifest.webmanifest")).json();
  expect(manifest.display).toBe("standalone");
  expect(manifest.name).toBe("Rynhult");
  expect(manifest.short_name).toBe("Rynhult");
  await expect(page).toHaveTitle("Overview · Rynhult");
  expect(manifest.icons.some((i: { purpose?: string }) => i.purpose === "maskable")).toBe(true);
  // the service worker registers (it only shows notifications; no offline cache)
  await expect.poll(() => page.evaluate(async () => !!(await navigator.serviceWorker.getRegistration())?.active)).toBe(true);
  await page.getByRole("link", { name: "Settings" }).first().click();
  await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  await page.getByRole("combobox", { name: "Settings section", exact: true }).selectOption('all');
  await expect(page.getByRole("button", { name: "Turn on notifications" })).toBeVisible();
  await expect(page.getByText("No notification devices yet")).toBeVisible();
  await shot(page, testInfo.project.name, "settings");
});

test("project page lists the project's sessions", async ({ page }, testInfo) => {
  await page.goto("/projects");
  await page.getByRole("link", { name: fixture("Demo"), exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  await expect(page.getByRole("heading", { name: fixture("Demo"), level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: /e2e/ }).first()).toBeVisible();  // Sessions: the first section tab
  await page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Issues and pull requests", exact: true }).click();
  await expect(page.getByText("No GitHub repository linked")).toBeVisible();
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).dblclick();
  await page.getByRole("dialog").getByRole("radio", { name: "New GitHub repo" }).click();
  await page.getByRole("dialog").getByLabel("Name", { exact: true }).fill("x");
  await expect(page.getByRole("button", { name: "Create repository" })).toBeDisabled();  // needs the confirmation box
  await shot(page, testInfo.project.name, "create-repo");
});

test("backup card: back up now, what the backup holds, the bundle, and turning it off", async ({ page }, testInfo) => {
  // A project whose folder is a git repository with two branches (the e2e server pushes backups to itself)
  const folder = path.join(info().state, "workspaces", fixture("backed-up"));
  mkdirSync(folder, { recursive: true });
  const git = (...args: string[]) => execFileSync("git", ["-C", folder, "-c", "user.name=Alex Example", "-c", "user.email=alex@example.com", ...args]);
  git("init", "-q", "-b", "main");
  writeFileSync(path.join(folder, "README.md"), "# Backed up\n");
  git("add", "-A");
  git("commit", "-q", "-m", "first");
  git("branch", "feature-login");
  await page.goto("/projects");
  await page.getByRole("button", { name: "Register folder" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill(fixture("Backed up"));
  await dialog.getByLabel("Workspace path").fill(folder);
  await dialog.getByRole("button", { name: "Register", exact: true }).dblclick();
  await page.getByRole("link", { name: fixture("Backed up"), exact: true }).click();
  await page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Backup", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "Back this project up to the main Rynhult" })).toBeChecked();  // on by default
  await page.getByRole("button", { name: "Back up now" }).dblclick();
  await expect(page.getByText(/2 branches · 0 tags/)).toBeVisible({ timeout: 20_000 });
  const branches = page.getByLabel("Branches in the backup");
  await expect(branches.getByText("main")).toBeVisible();
  await expect(branches.getByText("feature-login")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download bundle" }).click();
  expect((await download).suggestedFilename()).toBe(`${fixture("backed-up")}.bundle`);
  await page.getByText("Every branch and tag, copied to the main Rynhult").scrollIntoViewIfNeeded();
  await shot(page, testInfo.project.name, "project-backup");
  const results = await new AxeBuilder({ page }).include("main").withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations.map((v) => v.id)).toEqual([]);
  await page.getByRole("checkbox", { name: "Back this project up to the main Rynhult" }).uncheck();
  await expect(page.getByText("Off. What was backed up before is kept.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Back up now" })).toHaveCount(0);
  await page.getByRole("checkbox", { name: "Back this project up to the main Rynhult" }).check();
  await page.goto("/host");
  await expect(page.getByText(/Git backups/)).toBeVisible();
});

test("keys: add one, paste a .env, give it to a project, New session shows the variables", async ({ page }, testInfo) => {
  const value = "sk-e2e-never-shown-0123456789";
  await page.goto("/settings#keys");
  await page.getByRole("button", { name: "Add key" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name", { exact: true }).fill(fixture("OpenAI (work)"));
  await dialog.getByRole("combobox", { name: "Variable", exact: true }).selectOption("OPENAI_API_KEY");
  await dialog.getByLabel("Value").fill(value);
  await dialog.getByRole("radio", { name: "Shared" }).click();
  await shot(page, testInfo.project.name, "keys-add");
  await dialog.getByRole("button", { name: "Add key" }).click();
  const keys = page.getByRole("list", { name: "Keys", exact: true });
  const keyRow = keys.getByRole("listitem").filter({ hasText: fixture("OpenAI (work)") });
  await expect(keyRow.getByText(fixture("OpenAI (work)"))).toBeVisible();
  await expect(keyRow.getByText("shared")).toBeVisible();

  await page.getByRole("button", { name: "Paste .env" }).click();
  await page.getByRole("dialog").getByLabel(".env contents").fill(`# tools\nexport SEARCH_API_KEY="srch-e2e-hidden-value"\nPATH=/bin\n`);
  await page.getByRole("dialog").getByRole("button", { name: "Read it" }).click();
  const found = page.getByRole("list", { name: "Keys found", exact: true });
  await expect(found.getByText("SEARCH_API_KEY")).toBeVisible();
  await expect(found.getByText(/shell's or Rynhult's own/)).toBeVisible();  // PATH can't be added
  await expect(page.getByText("srch-e2e-hidden-value")).toHaveCount(0);
  await shot(page, testInfo.project.name, "keys-paste");
  await page.getByRole("dialog").getByRole("button", { name: "Add 1 key" }).dblclick();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(keys.getByText("SEARCH_API_KEY", { exact: true }).first()).toBeVisible();
  await shot(page, testInfo.project.name, "keys");

  await page.goto("/projects");
  await page.getByRole("link", { name: fixture("Demo"), exact: true }).click();
  await page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Keys", exact: true }).click();
  await page.getByLabel("Key to give").selectOption({ label: `${fixture("OpenAI (work)")} (OPENAI_API_KEY)` });
  await page.getByRole("button", { name: "Give", exact: true }).dblclick();
  const given = page.getByRole("list", { name: "Keys given to this project" });
  await expect(given.getByText(fixture("OpenAI (work)"))).toBeVisible();
  await shot(page, testInfo.project.name, "project-keys");

  await page.getByRole("button", { name: "New session" }).first().click();
  await page.getByRole("dialog").getByRole("combobox", { name: "Project" }).selectOption({ label: fixture("Demo") });
  const sessionKeys = page.getByTestId("session-keys");
  await expect(sessionKeys).toContainText("OPENAI_API_KEY");
  await expect(sessionKeys).toContainText(fixture("OpenAI (work)"));
  await expect(sessionKeys).toContainText("OpenAI API access");
  await expect(sessionKeys).not.toContainText("SEARCH_API_KEY");  // not given to this project
  expect(await page.content()).not.toContain(value);
  await shot(page, testInfo.project.name, "new-session-keys");
  await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
});

test("an issue or pull request opens New session pre-filled (nothing launches by itself)", async ({ page }) => {
  // The fixture project has no GitHub repository: stand in a repo and a snapshot with one issue and one PR.
  await page.route("**/api/projects", async (route) => {
    const response = await route.fetch();
    const projects = (await response.json()) as { name: string; repo?: string }[];
    await route.fulfill({ response, json: projects.map((p) => (p.name === fixture("Demo") ? { ...p, repo: "example/demo" } : p)) });
  });
  const updated = new Date().toISOString();
  await page.route("**/api/projects/*/github*", (route) => route.fulfill({ json: {
    repo: { full_name: "example/demo", html_url: "https://github.com/example/demo", private: true, default_branch: "main", pushed_at: updated },
    branches: [], commits: [], runs: [],
    pulls: [{ number: 7, title: "Tidy the parser", author: "sam", draft: false, head: "tidy", updated_at: updated, url: "https://github.com/example/demo/pull/7" }],
    issues: [{ number: 12, title: "Crash on empty input", author: "alex", labels: ["bug"], updated_at: updated, url: "https://github.com/example/demo/issues/12" }],
  } }));
  await page.goto("/projects");
  await page.getByRole("link", { name: fixture("Demo"), exact: true }).click();
  await page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Issues and pull requests", exact: true }).click();
  await page.getByRole("button", { name: "Start session for issue #12" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("issue-12");
  await expect(dialog.getByLabel(/^Task/)).toHaveValue(/gh issue view 12/);
  await expect(dialog.getByTestId("advanced-summary")).toContainText("own worktree");  // said while Advanced is closed
  await openAdvanced(dialog);
  await expect(dialog.getByRole("checkbox", { name: /Own worktree/ })).toBeChecked();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Review pull request #7" }).click();
  await expect(page.getByRole("dialog").getByLabel("Name", { exact: true })).toHaveValue("review-7");
  await page.keyboard.press("Escape");
});

test("autostart: an owner approves the compose files, and a change holds the stack until approved", async ({ page }, testInfo) => {
  // The fixture project has no compose file and no Docker: stand in the runtime answers (tests/test_compose_pin.py covers the server).
  let approved = false, autostart = false, posted: { compose_autostart?: boolean; digest?: string } = {};
  const digest = "a".repeat(64), changed = "b".repeat(64);
  let demo = -1;
  await page.route("**/api/projects", async (route) => {
    const response = await route.fetch();
    const projects = (await response.json()) as { id: number; name: string }[];
    demo = projects.find((p) => p.name === fixture("Demo"))?.id ?? -1;
    await route.fulfill({ response, json: projects.map((p) => (p.name === fixture("Demo") ? { ...p, compose_autostart: autostart, compose_approved: approved } : p)) });
  });
  await page.route("**/api/projects/*/runtime", async (route) => {
    if (!route.request().url().includes(`/api/projects/${demo}/runtime`)) return route.continue();
    if (route.request().method() === "POST") {
      posted = route.request().postDataJSON();
      autostart = !!posted.compose_autostart;
      approved = approved || autostart;
      return route.fulfill({ json: {} });
    }
    const review = !approved
      ? { approved: false, digest, diff: "--- /dev/null\n+++ now/compose.yaml\n@@ -0,0 +1,3 @@\n+services:\n+  web:\n+    image: nginx\n", note: "Never approved: autostart runs this stack only after an owner approves its compose files." }
      : posted.digest === changed ? { approved: true, digest: changed, by: "admin", at: new Date().toISOString() }
      : { approved: false, digest: changed, by: "admin", at: new Date().toISOString(), note: null,
          diff: "--- approved/compose.yaml\n+++ now/compose.yaml\n@@ -1,3 +1,4 @@\n services:\n   web:\n     image: nginx\n+    volumes: [\"/home/sam:/h\"]\n" };
    await route.fulfill({ json: { containers: [{ Service: "web", Name: "demo-web-1", State: "running", Status: "Up 2 minutes" }], compose: review } });
  });
  await page.goto("/projects");
  // Demo's card (other tests may have added projects)
  const card = page.locator("section").filter({ has: page.getByRole("link", { name: fixture("Demo"), exact: true }) });
  const autostartBox = card.getByRole("checkbox", { name: "Autostart" });
  await expect(autostartBox).toBeEnabled();  // once the review has loaded
  await autostartBox.click();
  const dialog = page.getByRole("dialog", { name: `Approve ${fixture("Demo")}'s compose files?` });
  await expect(dialog.getByText("+    image: nginx")).toBeVisible();
  await expect(dialog.getByText(/Never approved/)).toBeVisible();
  await dialog.getByRole("button", { name: "Approve and turn on autostart" }).dblclick();
  await expect(dialog).toBeHidden();
  expect(posted).toEqual({ compose_autostart: true, digest });
  // An agent edits the file: the card says so, and the review shows the added mount
  await expect(card.getByRole("button", { name: /Changed Approve compose files/ })).toBeVisible();
  await card.getByRole("button", { name: /Changed Approve compose files/ }).click();
  const change = page.getByRole("dialog", { name: /compose files changed/ });
  await expect(change.getByText('+    volumes: ["/home/sam:/h"]')).toBeVisible();
  await expect(change.getByText(/Approved by admin/)).toBeVisible();
  await shot(page, testInfo.project.name, "compose-review");
  await change.getByRole("button", { name: "Approve", exact: true }).dblclick();
  await expect(change).toBeHidden();
  expect(posted).toEqual({ compose_autostart: true, digest: changed });
  await expect(card.getByRole("button", { name: /Changed Approve compose files/ })).toBeHidden();
});

test("inbox: a request that retargets a live site shows the change and needs confirming", async ({ page }, testInfo) => {
  // tests/test_publish.py covers the server; here the Inbox shows one stand-in request.
  const now = new Date().toISOString();
  let posted: unknown = "nothing";
  await page.route("**/api/attention?*", (route) => route.fulfill({ json: [{
    id: 9001, kind: "publish_request", severity: "critical", source: "session web", verified: true, session_id: null, session_name: null, project: null,
    title: "Retarget live site shop.example.com: port 8020 (storefront) → port 8004 (public internet)",
    detail: "Port 8004 is reserved for project demo. Asked for project demo. new preview",
    created_at: now, updated_at: now, acked_at: null, resolved_at: null, publish_id: "pub-000000000001",
    publish: { host: "shop.example.com", port: 8004, access: "public", replaces: { id: "pub-000000000000", port: 8020, access: "public", project: "storefront", decided_by: "admin" } },
  }] }));
  await page.route("**/api/publish/pub-000000000001/approve", async (route) => { posted = route.request().postDataJSON(); await route.fulfill({ json: {} }); });
  await page.goto("/inbox");
  await page.getByRole("button", { name: "Review and approve" }).click();
  const dialog = page.getByRole("dialog", { name: "Replace the live site shop.example.com?" });
  await expect(dialog.getByText(/Now: port 8020 \(project storefront\), on the public internet, approved by admin/)).toBeVisible();
  await expect(dialog.getByText(/After: port 8004, on the public internet/)).toBeVisible();
  await shot(page, testInfo.project.name, "publish-retarget");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(posted).toBe("nothing");
  await page.getByRole("button", { name: "Review and approve" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Replace the live site" }).dblclick();
  await expect.poll(() => posted).toEqual({ confirm_replace: true });
});

test("host and providers pages render", async ({ page }, testInfo) => {
  await page.goto("/host");
  await expect(page.getByText("CPU", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Memory", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Agent accounts")).toBeVisible();
  await expect(page.getByText("alex-agent")).toBeVisible();
  await shot(page, testInfo.project.name, "host");
  await page.goto("/providers");
  await page.getByRole("button", { name: "Check now" }).dblclick();
  await page.getByRole("article", { name: "GitHub account", exact: true }).locator("summary").click();
  await expect(page.getByText("fake-agent")).toBeVisible();  // GitHub (gh) login of the agent account
  await expect(page.getByText("prolite", { exact: true })).toBeVisible();
  await expect(page.getByText("Not exposed", { exact: true })).toBeVisible();
  await shot(page, testInfo.project.name, "providers");
});

test("add machine: one thing to copy per system, and a machine on another version is flagged", async ({ page }, testInfo) => {
  await page.goto("/host");
  await page.getByRole("button", { name: "Add machine" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a machine" });
  await dialog.getByPlaceholder("gaming-pc").fill(fixture("build-box"));
  await dialog.getByRole("button", { name: "Create enrollment code" }).dblclick();
  await expect(dialog.getByRole("tab", { name: "Linux", selected: true })).toBeVisible();
  // The e2e server has a private CA: the Linux line checks its fingerprint before fetching anything it trusts.
  const linux = dialog.locator("code").first();
  // the code is piped in and reaches acc-node on stdin (--code -), never on a command line or in sudo's log
  await expect(linux).toContainText(/^echo [0-9a-f-]{27} \| sh -c 'set -e;/);
  await expect(linux).toContainText("/api/nodes/ca");
  await expect(linux).toContainText("--code - --name");
  await expect(linux).toContainText(new RegExp(`' - https://acc\\.example\\.lan [0-9a-f]{64} ${fixture("build-box")}$`));
  await shot(page, testInfo.project.name, "add-machine-linux");
  // Before v1.0 there is no Windows or Mac installer: each tab shows a command that installs from this Rynhult's source.
  // The code is piped in and read from stdin (--code -): it is on no command line, nor in sudo's log.
  const fromSource = new RegExp(`--ca-fingerprint ([0-9a-f]{64}) --code - --name ${fixture("build-box")}'$`);
  await dialog.getByRole("tab", { name: "Windows" }).click();
  await expect(dialog.getByRole("region", { name: "the WSL command" })).toHaveText("wsl --install -d Ubuntu-24.04");
  const ubuntu = dialog.getByRole("region", { name: "the Ubuntu command" });
  await expect(ubuntu).toContainText(/^echo [0-9a-f-]{27} \| sudo sh -c 'set -eu; read code; /);
  await expect(ubuntu).toContainText("https://acc.example.lan/api/nodes/bundle");
  await expect(ubuntu).toContainText("deploy/node/install.sh --install-prereqs");
  await expect(ubuntu).toContainText(fromSource);
  await expect(ubuntu).not.toContainText(" -k ");
  const keepalive = dialog.getByRole("region", { name: "the keep-running task" });
  await expect(keepalive).toContainText("Register-ScheduledTask -TaskName 'Rynhult node (WSL)'");
  await expect(keepalive).toContainText("-d Ubuntu-24.04 --exec /bin/sleep infinity");
  await expect(dialog.getByRole("heading", { name: "Optional: keep Rynhult running while you're signed in" })).toBeVisible();
  const optional = dialog.getByRole("region", { name: "Keep Rynhult running (optional)" });
  // An administrator PowerShell runs the user's profile and modules, which agents can change through WSL interop.
  await expect(optional).toContainText("before any session runs on this PC");
  await optional.scrollIntoViewIfNeeded();
  await optional.screenshot({ path: wikiShotPath(testInfo, "add-machine-windows-keepalive.png") });
  await shot(page, testInfo.project.name, "add-machine-windows");
  await dialog.getByRole("tab", { name: "Mac" }).click();
  const mac = dialog.getByRole("region", { name: "the Mac command" });
  await expect(mac).toContainText(/^echo [0-9a-f-]{27} \| sh -c 'set -eu; read code; /);
  await expect(mac).toContainText("deploy/node/macos/install.sh");
  await expect(mac).toContainText(fromSource);
  await expect(dialog.getByText("A Mac is an additional machine only", { exact: false })).toBeVisible();
  await shot(page, testInfo.project.name, "add-machine-mac");
  // the success screen points at the steps for that system's agent CLIs
  await expect(dialog.getByRole("link", { name: "Guide → Connect an agent" })).toHaveAttribute("href", "/guide/User-Guide/Connect-an-Agent#mac");
  const code = /^echo ([0-9a-f-]{27}) \| /.exec((await mac.textContent()) ?? "")![1];
  await dialog.getByRole("button", { name: "Done" }).click();
  // Enroll it as an older node would (the handshake sends its version).
  const key = Buffer.alloc(32, 7).toString("base64");
  const enrolled = await page.request.post("/api/nodes/enroll", { data: { name: fixture("build-box"), code, public_key: key, info: { os: "linux", arch: "x86_64", version: "0.9.0" } } });
  expect(enrolled.ok()).toBe(true);
  await page.reload();
  await expect(page.getByText("Rynhult 0.9.0 · unknown build")).toBeVisible();
  await expect(page.getByRole("button", { name: `Update ${fixture("build-box")}` })).toBeVisible();
  await shot(page, testInfo.project.name, "machines-version");
  await page.getByRole("button", { name: `Remove ${fixture("build-box")}` }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove" }).dblclick();
  await expect(page.getByText(fixture("build-box"))).toHaveCount(0);
});

test("add machine: a release with installers links to them instead of the from-source commands", async ({ page }) => {
  // v1.0 and later (release_links): mimic one, since this build is pre-v1.
  await page.route("**/api/nodes", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const added = await (await route.fetch()).json();
    const tag = "https://github.com/example-org/demo-app/releases/download/v1.0.0";
    await route.fulfill({ status: 201, json: { ...added, version: "1.0.0", installers: {
      release: "https://github.com/example-org/demo-app/releases/tag/v1.0.0",
      windows: `${tag}/Rynhult-Setup-1.0.0.exe`, mac: `${tag}/Rynhult-1.0.0.pkg` } } });
  });
  await page.goto("/host");
  await page.getByRole("button", { name: "Add machine" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a machine" });
  await dialog.getByPlaceholder("gaming-pc").fill(fixture("release-box"));
  await dialog.getByRole("button", { name: "Create enrollment code" }).dblclick();
  await dialog.getByRole("tab", { name: "Windows" }).click();
  await expect(dialog.getByRole("link", { name: "Rynhult-Setup-1.0.0.exe" })).toHaveAttribute("href", /\/v1\.0\.0\/Rynhult-Setup-1\.0\.0\.exe$/);
  await expect(dialog.locator("code").first()).toHaveText(new RegExp(`^rynhult-node enroll --primary https://acc\\.example\\.lan --ca-fingerprint [0-9a-f]{64} --code [0-9a-f-]{27} --name ${fixture("release-box")}$`));
  await expect(dialog.getByRole("region", { name: "the Ubuntu command" })).toHaveCount(0);
  await dialog.getByRole("tab", { name: "Mac" }).click();
  await expect(dialog.getByRole("link", { name: "Rynhult-1.0.0.pkg" })).toBeVisible();
  await expect(dialog.getByRole("region", { name: "the Mac command" })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Done" }).click();
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const removed = await page.request.post(`/api/nodes/${fixture("release-box")}/remove`, { headers: { "x-acc-csrf": csrf } });
  expect(removed.ok()).toBe(true);
});

test("machine picker: sign-ins and agent CLIs on another machine, from Host to Providers", async ({ page }, testInfo) => {
  // lab-box: the e2e server's connected demo machine (tests/demo_machine.py), relaying to the test broker.
  await page.goto("/host");
  await expect(page.getByText("Codex 0.170.0")).toBeVisible();  // its CLIs at a glance
  await page.getByRole("link", { name: "Sign-ins and CLIs on lab-box" }).click();
  await expect(page).toHaveURL(/\/providers\?machine=lab-box$/);
  await expect(page.getByRole("radio", { name: /lab-box/ })).toHaveAttribute("aria-checked", "true");
  await page.getByRole("button", { name: "Check now" }).dblclick();
  await expect(page.getByText("Logged in").first()).toBeVisible();  // asked of lab-box's broker
  await expect(page.locator("#provider-sign-ins")).toContainText("not this selection");

  // Copilot's install controls are on its own tile (UX follow-up to W11).
  const copilot = page.getByRole("article", { name: "GitHub Copilot CLI account", exact: true });
  await expect(copilot.getByText("Not installed on lab-box yet", { exact: false })).toBeVisible();
  installedCopilot = true;
  await copilot.getByRole("button", { name: "Install" }).dblclick();
  await expect(copilot.getByRole("group", { name: "GitHub Copilot CLI software" }).getByText("1.0.0", { exact: true }))
    .toBeVisible({ timeout: 15_000 });  // reported back by lab-box
  await expect(page.locator("#provider-sign-ins").getByText("Auto-update")).toHaveCount(0);  // set on that machine itself
  const audit = await (await page.request.get("/api/audit?action=package.install")).json();
  expect(audit[0]).toMatchObject({ target: "copilot", detail: { machine: "lab-box" } });
  await shot(page, testInfo.project.name, "providers-machine");

  await page.getByRole("button", { name: "Re-login" }).first().click();  // Claude Code, on lab-box
  await page.getByRole("button", { name: "Start re-login", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: /on lab-box/ });
  await dialog.getByPlaceholder("code#state").fill("good-code");
  await dialog.getByRole("button", { name: "Finish login" }).dblclick();
  await expect(dialog.getByText("Logged in. Running sessions keep their conversations.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).last().click();
  const started = await (await page.request.get("/api/audit?action=account.login_start")).json();
  expect(started[0].detail).toMatchObject({ machine: "lab-box" });

  await page.getByRole("radio", { name: /acc-host.*main/ }).click();
  await expect(page).toHaveURL(/\/providers$/);
  await expect(page.locator("#provider-sign-ins").getByText(/^Agent software: Installed and kept up to date/)).toBeVisible();
});

test("machine picker: Claude Code and Codex cards follow what that machine has installed, and its own plan", async ({ page }) => {
  // A fresh machine (owner report): lab-box's helper reports neither CLI, while the main Rynhult's Codex usage says "prolite".
  let missing = false;
  await page.route("**/api/packages?machine=lab-box*", async (route) => {
    const view = await (await route.fetch()).json();
    if (missing) for (const id of ["claude", "codex"]) view.packages[id] = { ...view.packages[id], installed: null };
    await route.fulfill({ json: view });
  });
  await page.route(/\/api\/providers(\?refresh=1)?$/, async (route) => {
    const view = await (await route.fetch()).json();
    view.observations["codex.rate_limits"] = { value: { plan: "prolite" }, source: "app-server", observed_at: Date.now() / 1000 };
    await route.fulfill({ json: view });
  });
  const card = (name: string) => page.getByRole("article", { name: `${name} account`, exact: true });

  await page.goto("/providers?machine=lab-box");
  await expect(card("Claude Code").getByRole("button", { name: "Re-login" })).toBeVisible();  // installed there
  await expect(card("Codex").getByText("Logged in", { exact: true })).toBeVisible();
  await expect(card("Codex").getByText("prolite")).toHaveCount(0);  // the main Rynhult's plan, never lab-box's
  await page.goto("/providers");
  await expect(card("Codex").getByText("prolite")).toBeVisible();  // here it is this machine's

  missing = true;
  await page.goto("/providers?machine=lab-box");
  for (const name of ["Claude Code", "Codex"]) {
    await expect(card(name).getByText(/^Not installed for /)).toBeVisible();
    await expect(card(name).getByText("Not installed on lab-box yet.")).toBeVisible();
    await expect(card(name).getByRole("button", { name: "Install" })).toBeVisible();
    await expect(card(name).getByRole("button", { name: "Re-login" })).toHaveCount(0);
    await expect(card(name).getByText("Logged in", { exact: true })).toHaveCount(0);
  }
});

test("settings → certificate: Caddy's root, its fingerprint and the device downloads", async ({ page }, testInfo) => {
  await page.goto("/settings#certificate");
  await expect(page.getByText("Caddy's own CA", { exact: true })).toBeVisible();
  await expect(page.getByText("Caddy Local Authority - 2026 ECC Root", { exact: false })).toBeVisible();
  await expect(page.locator("code").filter({ hasText: /^[0-9a-f]{64}$/ })).toBeVisible();
  const crt = await page.request.get("/api/certificate/rynhult-root.crt");
  expect(crt.ok()).toBe(true);
  expect(await crt.text()).toContain("BEGIN CERTIFICATE");
  const profile = await page.request.get("/api/certificate/rynhult-root.mobileconfig");
  expect(profile.headers()["content-type"]).toContain("application/x-apple-aspen-config");
  expect(await profile.text()).toContain("com.apple.security.root");
  await expect(page.getByRole("link", { name: "rynhult-root.mobileconfig" })).toHaveAttribute("href", "/api/certificate/rynhult-root.mobileconfig");
  await expect(page.getByText("Trusted Root Certification Authorities")).toBeVisible();
  await page.getByRole("heading", { name: "Certificate", exact: true }).scrollIntoViewIfNeeded();
  await shot(page, testInfo.project.name, "settings-certificate");
});

test("audit log and knowledge panel", async ({ page }, testInfo) => {
  await page.goto("/audit");
  // Sentences, not action ids (UX review AU-1); the raw action stays in the row's tooltip.
  await expect(page.getByText(/ trusted /).first()).toBeVisible();
  await expect(page.getByText("project.trust")).toHaveCount(0);
  await page.getByRole("combobox", { name: "Type" }).selectOption({ label: "Sessions" });
  await expect(page.getByText(/ started /).first()).toBeVisible();
  await expect(page.getByText(/ trusted /)).toHaveCount(0);
  await shot(page, testInfo.project.name, "audit");
  await page.goto("/projects");
  await page.getByRole("link", { name: fixture("Demo"), exact: true }).click();
  await page.getByRole("tablist", { name: "Project sections" }).getByRole("tab", { name: "Knowledge", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Knowledge" })).toBeVisible();
});

test("providers page shows login health (old /accounts link redirects)", async ({ page }, testInfo) => {
  await page.goto("/accounts");
  await page.getByRole("button", { name: "Check now" }).dblclick();
  await page.getByRole("article", { name: "GitHub account", exact: true }).locator("summary").click();
  await expect(page.getByText("fake-agent")).toBeVisible();  // GitHub (gh) login of the agent account
  await expect(page.getByText("Logged in").first()).toBeVisible();
  await shot(page, testInfo.project.name, "accounts");
});

test("new session dialog keeps focus while the page behind it polls", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Rename", exact: true }).click();
  const name = page.getByRole("dialog").getByLabel("Name", { exact: true });
  await name.click();
  await name.pressSequentially("focus-");
  await page.waitForTimeout(7000);  // two 3 s polls of the overview behind the dialog
  await page.keyboard.type("kept");
  await expect(name).toBeFocused();
  await expect(name).toHaveValue("focus-kept");
  await page.getByRole("button", { name: "Close" }).click();
});

test("delete a local workspace after typing its folder name", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: "Local workspace" }).click();
  await dialog.getByLabel("Topic").fill(fixture("Throwaway idea"));
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await page.goto("/workspaces");
  await page.getByRole("button", { name: `Workspace actions for ${fixture("Throwaway idea")}`, exact: true }).click();
  await page.getByRole("menuitem", { name: `Delete workspace ${fixture("Throwaway idea")}` }).click();
  const confirm = page.getByRole("dialog");
  const remove = confirm.getByRole("button", { name: "Delete workspace" });
  await expect(remove).toBeDisabled();
  const folder = (await confirm.getByText(new RegExp(`^${fixture("throwaway-idea")}-\\d{8}(-\\d+)?$`)).textContent())!.trim();
  await confirm.getByLabel("Type the folder name to confirm").fill(folder);
  await remove.click();
  await expect(confirm).toBeHidden({ timeout: 15_000 });  // stops its session first
  await expect(page.getByRole("link", { name: fixture("Throwaway idea") })).toHaveCount(0);
});


test("owner invites a member, who joins with their own sign-in", async ({ page, browser }, testInfo) => {
  // Two contexts and two deliberately slow password hashes: under parallel test load this ran past the 45 s budget.
  test.slow();
  await page.goto("/settings#people");
  await expect(page.getByRole("heading", { name: "People", exact: true })).toBeVisible();
  // Wait for the async account options: index 1 can still be "New account…" while loading.
  await page.getByRole("combobox", { name: "Agent account" }).selectOption("alex-agent");
  await page.getByRole("button", { name: "Create invite link" }).dblclick();
  const url = (await page.locator("code").filter({ hasText: "/invite/" }).textContent())!.trim();
  await shot(page, testInfo.project.name, "settings-people");
  const guest = await browser.newContext();  // a different person, signed out
  const joiner = await guest.newPage();
  await joiner.goto(url);
  await expect(joiner.getByText("You're invited as a member")).toBeVisible();
  await shot(joiner, testInfo.project.name, "invite");
  const secret = (await joiner.getByTitle("Manual entry key").textContent())!.trim();
  await joiner.getByLabel("Username").fill(fixture("blair"));
  await joiner.getByLabel("Password", { exact: true }).fill("blair's long password");
  await joiner.getByLabel("Confirm password").fill("blair's long password");
  await joiner.getByLabel("Code from your authenticator").fill(totp(secret));
  await joiner.getByRole("button", { name: "Join" }).dblclick();
  await expect(joiner.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  const menu = joiner.getByRole("navigation", { name: "Main" });
  await expect(menu.getByRole("link", { name: "Sessions" })).toBeVisible();
  await expect(menu.getByRole("link", { name: "Host" })).toBeVisible();  // health is for everyone
  await expect(menu.getByRole("link", { name: "Audit" })).toHaveCount(0);  // owners only
  await expect(joiner.getByText("Host CPU")).toBeVisible();
  await expect(joiner.getByText("No live sessions.")).toBeVisible();  // the owner's sessions aren't theirs
  await shot(joiner, testInfo.project.name, "member-overview");
  await joiner.goto("/settings#sign-ins");
  await expect(joiner.getByText("Where you're signed in")).toBeVisible();
  await expect(joiner.getByRole("heading", { name: "People", exact: true })).toHaveCount(0);  // owners only
  await expect(joiner.getByRole("heading", { name: "Infrastructure Keys", exact: true })).toHaveCount(0);
  await expect(joiner.getByRole("heading", { name: "DNS and certificates", exact: true })).toHaveCount(0);
  expect((await guest.request.get("/api/dns/credentials")).status()).toBe(403);
  expect((await guest.request.get("/api/dns/binding")).status()).toBe(403);
  await guest.close();
  await page.reload();
  // Exact: "blair" alone also matches the "Require tool approval for blair" row.
  await expect(page.getByRole("button", { name: `Remove ${fixture("blair")}` })).toBeVisible();
  // Removing them asks what happens to their things; the name is retired either way
  await page.getByRole("button", { name: `Remove ${fixture("blair")}` }).click();
  const dialog = page.getByRole("dialog", { name: `Remove ${fixture("blair")}?` });
  await expect(dialog.getByRole("radio", { name: "Give them to me" })).toHaveAttribute("aria-checked", "true");
  await dialog.getByRole("radio", { name: "Archive them" }).click();
  await expect(dialog.getByRole("radio", { name: "Archive them" })).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByText("their private keys are deleted")).toBeVisible();
  await shot(page, testInfo.project.name, "settings-people-remove");
  await dialog.screenshot({ path: `e2e/screenshots/${testInfo.project.name}-settings-people-remove-dialog.png` });
  await dialog.getByRole("button", { name: "Remove" }).dblclick();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: `Remove ${fixture("blair")}` })).toHaveCount(0);
  const removed = await (await page.request.get("/api/audit?action=user.remove")).json();
  expect(removed[0].detail.resources).toBe("archive");
});

test("new session: OpenCode with its own options, a private server and Rynhult's guard plugin", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New session" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Rename", exact: true }).click();
  await dialog.getByRole("textbox", { name: "Name", exact: true }).fill(fixture("oc"));
  await dialog.getByRole("combobox", { name: "Project" }).selectOption({ label: fixture("Demo") });
  await dialog.getByRole("combobox", { name: "Agent", exact: true }).selectOption("opencode");
  await openAdvanced(dialog);
  await expect(dialog.getByText("opencode --standalone --session ses_<session-id>")).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "Permissions", exact: true })).toHaveValue("commands");
  await expect(dialog.getByRole("combobox", { name: "Primary agent" })).toHaveValue("build");
  await expect(dialog.getByRole("combobox", { name: "Effort" })).toHaveCount(0);  // OpenCode has no effort setting
  await expect(dialog.getByText("checked against OpenCode 2.1.3 with a stand-in model")).toBeVisible();
  await dialog.getByRole("combobox", { name: "Model", exact: true }).selectOption("__custom");
  await expect(dialog.getByPlaceholder("provider/model")).toBeVisible();
  await dialog.getByPlaceholder("provider/model").fill("opencode/big-pickle");
  await dialog.getByRole("combobox", { name: "Permissions", exact: true }).selectOption("auto");
  await expect(dialog.getByText("--standalone --session ses_<session-id> --auto")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeDisabled();  // needs the explicit checkbox
  await dialog.getByRole("combobox", { name: "Permissions", exact: true }).selectOption("commands");
  await dialog.getByRole("combobox", { name: "Primary agent" }).selectOption("plan");
  await dialog.getByText("opencode/opencode.json", { exact: true }).click();  // the session's own config folder, shown in full
  await expect(dialog.getByText('"default_agent": "plan"')).toBeVisible();
  await expect(dialog.getByText('"model": "opencode/big-pickle"')).toBeVisible();
  await expect(dialog.getByText("opencode/plugins/acc/index.js", { exact: true })).toBeVisible();
  await shot(page, testInfo.project.name, "new-session-opencode");
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  await expect(page.locator(".xterm-rows")).toContainText("fake-opencode ready", { timeout: 15_000 });
  await expect(page.locator(".xterm-rows")).toContainText("--standalone · plugin found");
  await expect(page.locator(".xterm-rows")).toContainText("plugin: ready");  // Rynhult's plugin loaded: guard in place
  await expect(page.getByText("Idle").first()).toBeVisible({ timeout: 15_000 });  // verified by the plugin's event
  await shot(page, testInfo.project.name, "session-opencode");
  await page.goto("/providers");
  const card = page.getByRole("article", { name: "OpenCode account", exact: true });
  await expect(card.getByText("Ready")).toBeVisible({ timeout: 15_000 });
  await card.locator("summary").filter({ hasText: /^Account details/ }).click();  // its tile also holds its software line
  await expect(card.getByText("Free models only")).toBeVisible();
});
