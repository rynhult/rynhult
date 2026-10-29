import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test(`priority-grouped teams retain fresh and stale participant counts at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.emulateMedia({ colorScheme: "dark" });
    const demos = demoSessions();
    const lead = { ...demos[0], name: "Demo lead", observed_state: "waiting_input", team_id: demos[0].id };
    let stale = false;
    const old = new Date(Date.now() - 7 * 86400000).toISOString();
    await page.route("**/api/sessions", route => route.fulfill({ json: [lead, {
      ...demos[3], name: "Demo helper", observed_state: "active", team_id: lead.id, parent_session_id: lead.id,
      status_source: "claude_hook", status_stale_since: stale ? old : null,
      status_at: stale ? old : new Date().toISOString(), activity_at: stale ? old : new Date().toISOString(),
    }] }));
    for (const expired of [false, true]) {
      stale = expired;
      await page.goto("/");
      await expect(page.getByRole("main").getByText(`2 live · 1 ${stale ? "unknown" : "working"}`, { exact: false })).toBeVisible();
      await expectAxeClean(page);
      await page.goto("/board");
      await expect(page.getByRole("banner").getByText("Working", { exact: true }).locator("..")
        .getByText(stale ? "0" : "1", { exact: true })).toBeVisible();
      await expectAxeClean(page);
    }
  });
}
