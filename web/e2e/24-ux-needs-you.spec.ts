import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, routeDemoSessions } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review SL-1 and NAV-1 (owner decision 0008, Q1): what needs you comes first, and the counts agree.
test("sessions list: the session that needs you is first, under the same headings as the Overview", async ({ page }) => {
  await routeDemoSessions(page);
  await page.goto("/sessions");
  const sections = page.getByRole("main").getByRole("region");
  await expect(sections.first()).toHaveAccessibleName("Needs you");
  await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveText([/^Needs you/, /^Working/, /^Idle/]);
  await expect(page.getByRole("main").getByRole("row").filter({ hasText: /\S/ }).nth(1)).toContainText("api-refactor");
  await expectAxeClean(page, "main");
  await page.getByRole("radio", { name: "Recent" }).click();
  await expect(page.getByRole("main").getByRole("row").nth(1)).toContainText("notes-cleanup");
  await page.getByRole("radio", { name: "All" }).click();
  await page.getByRole("radio", { name: "Needs you first" }).click();
  await expect(page.getByRole("main").getByRole("heading", { level: 2 }).last()).toHaveText(/^Stopped/);
});

test("needs-you counts agree: sidebar, Overview stat and the filtered list", async ({ page }) => {
  await routeDemoSessions(page);
  await page.goto("/");
  const sidebarGroup = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /^Needs you/ });
  await expect(sidebarGroup).toContainText("1");
  // The Overview stat is a count and a link; the names are in the list right below it (UX declutter D03).
  const stat = page.getByRole("main").getByRole("link", { name: /^Needs you/ });
  await expect(stat).not.toContainText("api-refactor");
  await expect(stat).toContainText("1");
  await stat.click();
  await expect(page).toHaveURL(/\/sessions\?group=needs_you$/);
  await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveText([/^Needs you\s*1/]);
  await expect(page.getByRole("main").getByRole("region", { name: "Needs you", exact: true })
    .getByRole("link", { name: /^api-refactor\b/ })).toBeVisible();
  await page.getByRole("button", { name: /show all/ }).click();
  await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveCount(3);
});

test("inbox badge counts every open item, and the Inbox groups them by type", async ({ page }) => {
  const now = new Date().toISOString();
  const item = (id: number, kind: string, severity: string, title: string) => ({ id, kind, severity, title, source: "agent", verified: true, detail: null,
    session_id: null, session_name: null, project: null, created_at: now, updated_at: now, acked_at: null, resolved_at: null, publish_id: null });
  const items = [item(1, "guardrail_blocked", "info", "Guardrail note"), item(2, "ci_failed", "critical", "CI failed on main"),
    { ...item(3, "needs_input", "warn", "api-refactor needs your input"), session_id: "aaaaaaaaaaa6" }];
  await page.route("**/api/attention?scope=open", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(items) }));
  await page.route("**/api/attention", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(items) }));
  await page.goto("/inbox");
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /Inbox/ })).toContainText("3");
  await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveText([/^Waiting sessions/, /^Alerts/, /^Notes/]);
  await expectAxeClean(page, "main");
});
