import { expect, type Page, test } from "@playwright/test";
import type { AuthState, BoardView } from "../src/lib/types";
import { AUTH_FILE, demoSessions, expectAxeClean, wikiShotPath } from "./helpers";

/* The status board: one read-only screen for a large display (docs/STATUS-BOARD.md). The e2e server's machines are
   acc-host, lab-box and studio-mac; sessions are the demo's shapes, served through a route so a test
   can change them and watch the board follow without a reload. */
test.use({ storageState: AUTH_FILE });

/** The demo sessions, each in its own project (the shared helper puts them all in project 1). */
const IDS: Record<string, number> = { "demo-api": 1, "demo-web": 2, "sam-notes": 3, local: 9 };
const boardSessions = () => demoSessions().map((s) => ({ ...s, project_id: IDS[s.project] ?? 1 }));

const PANELS = ["Machines", "Projects", "Running sessions", "Plan usage", "Inbox", "Recent alerts"];

async function serveSessions(page: Page, sessions: () => unknown[]) {
  await page.route("**/api/sessions", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: sessions() }) : route.continue());
}

/** Inbox items to go with the demo sessions (artificial): the session that needs you, an approval and an alert. */
async function serveInbox(page: Page) {
  const at = (minutes: number) => new Date(Date.now() - minutes * 60e3).toISOString();
  const item = (id: number, kind: string, severity: string, title: string, minutes: number, extra = {}) => ({
    id, kind, severity, source: "acc", verified: true, title, detail: null, session_id: null, session_name: null, project: null,
    created_at: at(minutes), updated_at: at(minutes), acked_at: null, resolved_at: null, publish_id: null, ...extra });
  await page.route("**/api/attention", (route) => route.fulfill({ json: [
    item(1, "needs_input", "warn", "api-refactor needs you: permission to run tests", 30, { session_id: "aaaaaaaaaaa6", session_name: "api-refactor", project: "demo-api" }),
    item(2, "tool_request", "info", "demo-api asks to install ripgrep", 12, { project: "demo-api" }),
    item(3, "auth", "warn", "Claude login expires in 5 days", 50),
  ] }));
}

/** Projects to go with the demo sessions (all in project 1), artificial: one backed up, one failing, one off. */
async function serveProjects(page: Page) {
  const at = (hours: number) => new Date(Date.now() - hours * 3600e3).toISOString();
  await page.route("**/api/board", async (route) => {
    const response = await route.fetch();
    const board = await response.json();
    board.projects = [
      { id: 1, slug: "demo-api", name: "demo-api", host: "local", git_backup: true, backup: { last_at: at(3) } },
      { id: 2, slug: "demo-web", name: "demo-web", host: "lab-box", git_backup: true, backup: { last_at: at(30), failed_at: at(1), error: "lab-box is not connected" } },
      { id: 3, slug: "sam-notes", name: "sam-notes", host: "local", git_backup: false, backup: null },
    ];
    await route.fulfill({ response, json: board });
  });
}

async function expectFitsTheScreen(page: Page) {
  const { width, height } = page.viewportSize()!;
  const overflow = await page.evaluate(() => ({ x: document.documentElement.scrollWidth, y: document.documentElement.scrollHeight }));
  expect(overflow.x).toBeLessThanOrEqual(width);  // one screen: nothing scrolls the page
  expect(overflow.y).toBeLessThanOrEqual(height);
  for (const name of PANELS) {
    const box = (await page.getByRole("region", { name, exact: true }).boundingBox())!;
    expect(box.width).toBeGreaterThan(200);
    expect(box.height).toBeGreaterThan(100);
    expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
    expect(box.y + box.height).toBeLessThanOrEqual(height + 1);
  }
}

for (const viewport of [{ width: 1920, height: 1080 }, { width: 1440, height: 900 }]) {
  test(`the board fits one ${viewport.width}x${viewport.height} screen, with every machine, project and session`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await serveSessions(page, boardSessions);
    await serveProjects(page);
    await serveInbox(page);
    await page.goto("/board");
    await expect(page.getByRole("heading", { name: "Status board", level: 1 })).toBeVisible();
    await expect(page.getByRole("button", { name: "Details", exact: true })).toBeHidden();
    await expect(page.getByRole("link", { name: "Back to Rynhult" })).toBeVisible();
    await expect(page.locator("header")).toContainText("Machine alerts");
    await expect(page.getByRole("navigation")).toHaveCount(0);  // full screen: no sidebar
    const machines = page.getByRole("region", { name: "Machines", exact: true });
    await expect(machines.locator("[data-machine=local]")).toContainText("acc-host");
    await expect(machines.locator("[data-machine=lab-box]")).toContainText("lab-box");
    await expect(machines.locator("[data-machine=studio-mac]")).toContainText("studio-mac");
    await expect(machines.locator("[data-machine]")).toHaveCount(3);
    await expect(machines.getByRole("meter", { name: "Disk /srv used" }).last()).toHaveAttribute("aria-valuenow", "88");
    await expect(machines).toContainText("3 of 3 connected");
    const sessions = page.getByRole("region", { name: "Running sessions", exact: true });
    await expect(sessions.getByRole("heading", { name: "Needs you · 1" })).toBeVisible();
    await expect(sessions.locator("[data-session=api-refactor]")).toContainText("Needs you");
    await expect(sessions.locator("[data-session=old-spike]")).toHaveCount(0);  // stopped: not running
    const projects = page.getByRole("region", { name: "Projects", exact: true });
    await expect(projects.locator("[data-project=demo-api]")).toContainText("1 live · 1 needs you");
    await expect(projects.locator("[data-project=demo-web]")).toContainText("2 live");
    await expect(projects.locator("[data-project=demo-api]")).toContainText("Backup 3h ago");
    await expect(projects.locator("[data-project=demo-web]")).toContainText("Backup failed 1h ago");
    await expect(projects.locator("[data-project=sam-notes]")).toContainText("Backup off");
    const alerts = page.getByRole("region", { name: "Recent alerts", exact: true });
    await expect(alerts).toContainText("Disk /srv is 88% full");  // lab-box's, from its summary
    await expect(alerts).toContainText("Claude login expires in 5 days");
    await expect(page.getByRole("region", { name: "Inbox", exact: true })).toContainText(/Waiting sessions\s*1/);
    await expectFitsTheScreen(page);
    await expectAxeClean(page);
    await page.screenshot({ path: wikiShotPath(testInfo, `status-board-${viewport.width}.png`) });
  });
}

test.describe("compact phone header", () => {
  test.use({ isMobile: true, hasTouch: true });

  for (const colorScheme of ["dark", "light"] as const) {
    test(`phone board keeps counts and the first machine above the fold with accessible details (${colorScheme})`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.emulateMedia({ colorScheme });
      await serveSessions(page, boardSessions);
      await serveProjects(page);
      await serveInbox(page);
      const [boardResponse, authResponse] = await Promise.all([
        page.waitForResponse((response) => new URL(response.url()).pathname === "/api/board"),
        page.waitForResponse((response) => new URL(response.url()).pathname === "/api/auth/state"),
        page.goto("/board"),
      ]);
      expect(boardResponse.ok()).toBe(true);
      expect(authResponse.ok()).toBe(true);
      const board: BoardView = await boardResponse.json();
      const auth: AuthState = await authResponse.json();
      const connected = board.machines.filter((machine) => machine.connected).length;
      const machineAlerts = board.machines.reduce((count, machine) =>
        count + machine.alerts.filter((alert) => alert.severity !== "info").length, 0);
      const header = page.locator("header");
      const machine = page.getByRole("region", { name: "Machines", exact: true }).locator("[data-machine]").first();
      await expect(machine).toHaveAttribute("data-machine", board.machines[0].name);
      await expect(machine).toContainText(board.machines[0].label);
      await expect(header.getByText("Needs you", { exact: true }).locator("..")).toContainText("1");
      await expect(header.getByText("Working", { exact: true }).locator("..")).toContainText("2");
      const headerBox = (await header.boundingBox())!;
      expect(headerBox.height).toBeLessThanOrEqual(144);
      const machineBox = (await machine.boundingBox())!;
      expect(machineBox.y).toBeLessThan(200);
      expect(machineBox.y + machineBox.height).toBeLessThanOrEqual(page.viewportSize()!.height);
      expect((await header.getByText("Needs you", { exact: true }).boundingBox())!.y)
        .toBeLessThan((await header.getByRole("heading", { name: "Status board" }).boundingBox())!.y);
      const details = header.getByRole("button", { name: "Details", exact: true });
      await expect(details).toHaveAttribute("aria-expanded", "false");
      await expect(header.getByText("Machine alerts", { exact: true })).toBeHidden();
      await expect(header.getByRole("link", { name: "Back to Rynhult" })).toBeHidden();
      await expectAxeClean(page);
      if (colorScheme === "dark") {
        await page.screenshot({ path: wikiShotPath(testInfo, "status-board-phone.png") });
      }

      await page.keyboard.press("Tab");
      await expect(details).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(details).toHaveAttribute("aria-expanded", "true");
      const disclosure = header.getByRole("group", { name: "Board details" });
      await expect(disclosure.getByText("Machines", { exact: true }).locator(".."))
        .toContainText(`${connected}/${board.machines.length}`);
      await expect(disclosure).toContainText(connected === board.machines.length
        ? "connected" : `${board.machines.length - connected} disconnected`);
      await expect(disclosure.getByText("Machine alerts", { exact: true }).locator("..")).toContainText(String(machineAlerts));
      await expect(disclosure.getByText("Inbox", { exact: true }).locator("..")).toContainText("3");
      expect(auth.instance?.host).toBeTruthy();
      await expect(disclosure).toContainText(`${auth.instance!.host} · machines/projects updated`);
      await expect(disclosure).toContainText(/machines\/projects updated \d+s ago/);
      const back = disclosure.getByRole("link", { name: "Back to Rynhult" });
      await expect(back).toBeVisible();
      const fullScreen = disclosure.getByRole("button", { name: "Full screen", exact: true });
      await expect(fullScreen).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(back).toBeFocused();
      for (const control of [details, fullScreen, back]) {
        const box = (await control.boundingBox())!;
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.width).toBeGreaterThanOrEqual(44);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
      await expectAxeClean(page);
      if (colorScheme === "dark") {
        await page.screenshot({ path: wikiShotPath(testInfo, "status-board-phone-details.png") });
      }
      await page.keyboard.press("Escape");
      await expect(details).toBeFocused();
      await expect(details).toHaveAttribute("aria-expanded", "false");
      await expect(disclosure).toBeHidden();
      await details.tap();
      await back.tap();
      await expect(page).toHaveURL(/\/$/);
    });
  }

  test("offline and source failure stay visible with Details closed and retain the counts", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    let fail = false;
    await page.route("**/api/sessions", (route) => fail
      ? route.fulfill({ status: 503, json: { detail: "Demo sessions unavailable" } })
      : route.fulfill({ json: boardSessions() }));
    await page.goto("/board");
    const header = page.locator("header");
    const needs = header.getByText("Needs you", { exact: true }).locator("..");
    await expect(needs).toContainText("1");
    fail = true;
    await expect(header.getByRole("status")).toContainText("Offline: showing data from", { timeout: 15000 });
    const warning = page.getByRole("status").filter({ hasText: "Sessions: refresh failed" });
    await expect(warning).toContainText("last successful update");
    await expect(header.getByRole("button", { name: "Details", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(needs).toContainText("1");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await expectAxeClean(page);
    await warning.getByRole("button", { name: "Retry Sessions", exact: true }).tap();
    fail = false;
    await expect(warning).toBeHidden();
    await expect(header.getByRole("status")).toHaveCount(0);
  });
});

test("the board follows changes live, without a reload", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  let sessions = demoSessions();
  await serveSessions(page, () => sessions);
  let labBoxDown = false;
  await page.route("**/api/board", async (route) => {
    const response = await route.fetch();
    const board = await response.json();
    if (labBoxDown) board.machines = board.machines.map((m: { name: string }) => m.name === "lab-box"
      ? { ...m, connected: false, light: null, alerts: [], last_seen: new Date(Date.now() - 120e3).toISOString() } : m);
    await route.fulfill({ response, json: board });
  });
  await page.goto("/board");
  const needs = page.getByText("Needs you", { exact: true }).first().locator("..");
  await expect(needs).toContainText("1");
  sessions = sessions.map((s) => s.name === "checkout-flow" ? { ...s, observed_state: "waiting_input" } : s);
  await expect(needs).toContainText("2");  // the shared sessions query refreshes every 3 s
  await expect(page.locator("[data-session=checkout-flow]")).toContainText("Needs you");
  labBoxDown = true;
  const labBox = page.locator("[data-machine=lab-box]");
  await expect(labBox).toContainText("Disconnected · last seen 2m ago", { timeout: 15000 });  // the board every 10 s
  await expect(page.getByRole("region", { name: "Machines", exact: true })).toContainText("2 of 3 connected");
  await expectAxeClean(page);
});

test("the board follows the light theme too, and stays readable", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await serveSessions(page, demoSessions);
  await page.goto("/board");
  await expect(page.locator("[data-machine=lab-box]")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(false);
  await expectAxeClean(page);
});

test("Status board is in the owners' navigation; members get an explanation", async ({ page }) => {
  await page.goto("/host");
  await page.getByRole("link", { name: "Status board" }).first().click();
  await expect(page).toHaveURL(/\/board$/);
  await page.getByRole("link", { name: "Back to Rynhult" }).click();
  await expect(page).toHaveURL(/\/$/);
  let asked = 0;
  await page.route("**/api/auth/state", (r) => r.fulfill({ json: { authenticated: true, setup_required: false,
    username: "demo-member", role: "member", account: "demo-agent", instance: { host: "acc-host" } } }));
  await page.route("**/api/board", (r) => { asked++; return r.fulfill({ status: 403, json: { detail: "owners only" } }); });
  await page.goto("/board");
  await expect(page.getByText("The status board is for Rynhult's owners.")).toBeVisible();
  expect(asked).toBe(0);
  await expectAxeClean(page);
});
