import { wikiShotPath } from "./helpers";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, info } from "./helpers";

test.use({ storageState: AUTH_FILE });

let sessionId: string;
test.beforeEach(async ({ page }) => {
  await page.goto("/");
  const csrf = (await page.context().cookies()).find((c) => c.name === "acc_csrf")!.value;
  const headers = { "X-ACC-CSRF": csrf, Origin: info().origin };
  const project = await page.request.post("/api/projects", { headers,
    data: { slug: `activity-${Date.now()}`, name: "Activity demo", workspace_path: info().workspace } });
  const pid = (await project.json()).id;
  await page.request.post(`/api/projects/${pid}/trust`, { headers });
  const session = await page.request.post("/api/sessions", { headers, data: { project_id: pid, provider: "claude", name: "Activity demo" } });
  expect(session.ok()).toBe(true);
  sessionId = (await session.json()).id;
});

test("Q16: long event content fits 320px and 375px phones, including enlarged text", async ({ page }, testInfo) => {
  const message = `Failed to read ${"very-long-path/".repeat(12)}config.json: permission denied`;
  await page.route("**/events?*", (route) => route.fulfill({ json: [
    { id: 1, ts: "2026-10-22T12:00:00Z", source: "supervisor", kind: "recovery_failed", verified: true, payload: { error: message } },
  ] }));
  for (const width of [320, 375]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(`/sessions/${sessionId}?tab=activity`);
    for (const fontSize of ["16px", "20px"]) {
      await page.evaluate((size) => { document.documentElement.style.fontSize = size; }, fontSize);
      const content = page.getByText(message, { exact: true });
      await expect(content).toBeVisible();
      const rect = await content.boundingBox();
      expect(rect!.width).toBeGreaterThan(150);
      expect(rect!.x).toBeGreaterThanOrEqual(0);
      expect(rect!.x + rect!.width).toBeLessThanOrEqual(width);
      expect(await content.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    }
  }
  await page.screenshot({ path: testInfo.outputPath("activity-phone.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { document.documentElement.style.fontSize = "16px"; });
  const row = page.getByText("Recovery failed", { exact: true }).locator("..").locator("..");
  await row.screenshot({ path: wikiShotPath(testInfo, "session-activity-phone.png") });
  await page.setViewportSize({ width: 1440, height: 900 });
  await row.screenshot({ path: wikiShotPath(testInfo, "session-activity.png") });
});

test("Q17: distinguishes loading, initial failure, empty and stale results", async ({ page }) => {
  let state: "loading" | "failed" | "empty" | "data" = "loading";
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/events?*", async (route) => {
    if (state === "loading") await gate;
    if (state === "failed") return route.fulfill({ status: 503, json: { detail: "events unavailable" } });
    await route.fulfill({ json: state === "empty" ? [] : [
      { id: 1, ts: "2026-10-22T12:00:00Z", source: "supervisor", kind: "Saved event", verified: true, payload: {} },
    ] });
  });
  await page.goto(`/sessions/${sessionId}?tab=activity`);
  try {
    await expect(page.getByRole("status", { name: "Loading", exact: true })).toBeVisible();
    await expect(page.getByText("No events yet.")).toHaveCount(0);
  } finally { state = "failed"; release(); }
  await expect(page.getByRole("alert")).toContainText("events unavailable", { timeout: 15000 });
  await expect(page.getByText("No events yet.")).toHaveCount(0);
  state = "empty";
  await expect(page.getByText("No events yet.")).toBeVisible({ timeout: 15000 });
  state = "data";
  await expect(page.getByText("Saved event", { exact: true })).toBeVisible();
  state = "failed";
  await expect(page.getByText(/Showing the last data received; refresh failed/)).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("Saved event", { exact: true })).toBeVisible();
});
