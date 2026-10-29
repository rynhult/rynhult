import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, routeDemoSessions, shot } from "./helpers";

test.use({ storageState: AUTH_FILE });

let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

test("mobile: overview, session terminal with key bar, inbox", async ({ page }, testInfo) => {
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const headers = { "x-acc-csrf": csrf };
  // This project can run alone; it creates the session it exercises.
  const project = await page.request.post("/api/projects", { headers, data: {
    slug: `mobile-demo-${randomUUID().slice(0, 8)}`, name: "Mobile demo", workspace_path: `${info().workspace}/../mobile-demo`,
  } });
  expect(project.ok()).toBeTruthy();
  const pid = (await project.json()).id;
  expect((await page.request.post(`/api/projects/${pid}/trust`, { headers })).ok()).toBeTruthy();
  const launched = await page.request.post("/api/sessions", { headers, data: {
    project_id: pid, name: "mobile-demo", provider: "claude", initial_prompt: "Mobile demo",
  } });
  expect(launched.ok()).toBeTruthy();
  const sid = (await launched.json()).id;
  sessionId = sid;
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await shot(page, testInfo.project.name, "overview");
  await page.goto(`/sessions/${sid}`);
  await expect(page.getByRole("button", { name: "Esc" })).toBeVisible();  // touch key bar
  await page.getByRole("button", { name: "More keys" }).click();
  await expect(page.getByRole("button", { name: "Ctrl" })).toHaveAttribute("aria-pressed", "false");
  await shot(page, testInfo.project.name, "terminal");
  await page.goto("/inbox");
  await shot(page, testInfo.project.name, "inbox");
});

test("mobile: the session that needs you is the first row on Sessions", async ({ page }) => {
  await routeDemoSessions(page);
  await page.goto("/sessions");
  await expect(page.getByRole("main").getByRole("region").first()).toHaveAccessibleName("Needs you");
  const first = page.getByRole("main").getByRole("region").first()
    .getByRole("link", { name: /^api-refactor\b/ });
  await expect(first).toBeInViewport();
});

test("mobile: Overview puts the sessions that need you before health and machines", async ({ page }) => {
  await routeDemoSessions(page);
  await page.route("**/api/nodes", (route) => route.fulfill({ json: [{ id: "a", name: "demo-cloud", role: "always_on", status: "active",
    connected: false, offline_alert: true, last_seen: new Date().toISOString(), info: { os: "linux" }, accounts: [] }] }));
  await page.goto("/");
  const main = page.getByRole("main");
  const top = async (x: import("@playwright/test").Locator) => (await x.boundingBox())!.y;
  const needs = main.getByRole("link", { name: /^Needs you/ });
  const sessions = main.getByRole("heading", { name: "Live sessions" });
  const cpu = main.getByText("Host CPU", { exact: true });
  const machines = main.getByRole("heading", { name: "Machines", exact: true });
  await expect(machines).toBeVisible();
  const [n, s, c, m] = [await top(needs), await top(sessions), await top(cpu), await top(machines)];
  expect(n).toBeLessThan(s);  // attention, then the sessions to answer
  expect(s).toBeLessThan(c);  // then health
  expect(c).toBeLessThan(m);  // then machine administration, whose alert stays visible
  await expect(main.getByText("Always-on machine offline")).toBeVisible();
  await expect(main.getByRole("button", { name: "Check machines now" })).toHaveCount(0);
  // Keyboard order follows the same reading order: no control above the sessions comes after them.
  const links = await main.getByRole("link").evaluateAll((els) => els.map((e) => e.textContent?.trim() ?? ""));
  expect(links.indexOf("All sessions")).toBeLessThan(links.indexOf("All machines"));
});

test("mobile: the Overview keeps the Inbox count in a compact line above the page", async ({ page }) => {
  await routeDemoSessions(page);
  const now = new Date().toISOString();
  const items = [{ id: 9, kind: "needs_input", severity: "warn", title: "api-refactor needs your input", source: "claude_hook", verified: true,
    detail: "Claude needs your permission", session_id: "aaaaaaaaaaa6", session_name: "api-refactor", project: "demo-api", created_at: now,
    updated_at: now, acked_at: null, resolved_at: null, publish_id: null }];
  await page.route("**/api/attention", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(items) }));
  await page.goto("/");
  const status = page.getByRole("region", { name: "Inbox status" });
  await expect(status.getByRole("link", { name: "Inbox (1)" })).toBeInViewport();
  expect((await status.boundingBox())!.height).toBeLessThanOrEqual(64);
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview", exact: true })).toBeInViewport();
});

// WP3, the phone answer loop (UX review PH-1, SE-6, SE-7, NAV-4, OV-4).
async function liveSession(page: import("@playwright/test").Page) {
  const { id, headers } = await gitProject(page, `mobile-terminal-${randomUUID().slice(0, 8)}`);
  const launched = await page.request.post("/api/sessions", { headers,
    data: { project_id: id, provider: "claude", name: "Mobile terminal" } });
  expect(launched.ok()).toBe(true);
  sessionId = (await launched.json()).id;
  return sessionId!;
}

test("mobile: Enter is on screen, and the terminal starts near the top", async ({ page }) => {
  await page.goto(`/sessions/${await liveSession(page)}`);
  const enter = page.getByRole("button", { name: "Enter" });
  await expect(enter).toBeVisible();
  const box = (await enter.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  const terminal = (await page.locator(".xterm").boundingBox())!;
  expect(terminal.y).toBeLessThan(250);  // the header's 44 px touch targets (phone pass) cost ~20 px
  // Interrupt and Stop are behind "⋯" on touch, never next to each other in the header
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeHidden();
  await page.getByRole("button", { name: /^Actions for / }).click();
  await expect(page.getByRole("menuitem", { name: "Stop session…" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Session details" }).click();
  await expect(page.getByRole("group", { name: "Session details" }).getByRole("link", { name: "Export" })).toBeVisible();
});

test("mobile: a deep-linked tab is on screen, and the others are under More", async ({ page }) => {
  const id = await liveSession(page);
  await page.goto(`/sessions/${id}?tab=activity`);
  const tab = page.getByRole("tab", { name: "Activity" });
  await expect(tab).toHaveAttribute("aria-selected", "true");
  const inside = async (name: string) => {
    const box = (await page.getByRole("tab", { name }).boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  };
  await inside("Activity");
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menu", { name: "More" }).getByRole("menuitem", { name: /Setup/ }).click();
  await expect(page).toHaveURL(/\?tab=spec$/);
  await inside("Setup");
});

test("mobile: a broker outage shows a banner, but not during the first seconds of start-up", async ({ page }) => {
  await page.route("**/api/health", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, broker: false }) }));
  const health = page.waitForResponse("**/api/health");
  await page.goto("/");
  await health;
  const banner = page.getByRole("status").filter({ hasText: "Sessions unavailable" });
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await expect(banner).toHaveCount(0);  // a start-up moment: the broker isn't reported unreachable yet
  await expect(banner).toBeInViewport({ timeout: 15_000 });  // still unreachable past the grace period (8 s)
  await expect(banner.getByRole("link", { name: "What to do" })).toHaveAttribute("href", "/guide/User-Guide/Troubleshooting");
});

test("mobile: axe on the session header and tabs", async ({ page }) => {
  const id = await liveSession(page);
  for (const tab of ["", "?tab=preview", "?tab=activity", "?tab=handoffs", "?tab=spec", "?tab=changes", "?tab=files"]) {
    await page.goto(`/sessions/${id}${tab}`);
    await expect(page.getByRole("tablist")).toBeVisible();
    await expectAxeClean(page, tab ? "main" : "main header");
  }
});
