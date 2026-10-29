import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review NAV-9: while Rynhult isn't answering, the Inbox keeps the last items with a stale note, and buttons that would
// change something are disabled with a reason, instead of failing after a click.
test("offline: Inbox actions are disabled and the cards say the data is old", async ({ page }) => {
  const at = new Date().toISOString();
  const base = { verified: true, detail: null, session_id: null, session_name: null, project: null, acked_at: null, resolved_at: null,
    created_at: at, updated_at: at };
  let down = false;
  await page.route("**/api/attention*", (route) => down ? route.abort() : route.fulfill({ json: [
    { ...base, id: 3, kind: "publish_request", severity: "warn", title: "ui-polish asks to publish preview.example.com", source: "session ui-polish",
      publish_id: "pub-000000000010", publish: { host: "preview.example.com", port: 8010, access: "internal", action: "publish",
        requested_by: "session ui-polish", reason: "Review", project: "demo-web", replaces: null, port_owner: null } },
    { ...base, id: 4, kind: "ci_failed", severity: "warn", title: "CI failed on main", source: "github actions" },
  ] }));
  await page.route(/\/api\/(sessions|health)$/, (route) => down ? route.abort() : route.fallback());
  await page.goto("/inbox");
  const approve = page.getByRole("button", { name: "Review and approve" });
  await expect(approve).toBeEnabled();
  down = true;
  await expect(approve).toBeDisabled({ timeout: 30_000 });
  await expect(approve).toHaveAttribute("title", /Offline/);
  await expect(page.getByRole("button", { name: "Dismiss notice" })).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "Showing the last data received" })).toBeVisible({ timeout: 30_000 });
  down = false;
  await expect(approve).toBeEnabled({ timeout: 30_000 });
});

// UX review NAV-2: with many live sessions, the pinned list scrolls on its own and Configure stays on screen.
test("sidebar: Settings and Guide stay in view with many live sessions", async ({ page }) => {
  const [first] = demoSessions();
  const many = Array.from({ length: 18 }, (_, i) => ({ ...first, id: `bbbbbbbbbb${String(i).padStart(2, "0")}`, name: `busy-${i}`,
    observed_state: i % 3 === 0 ? "waiting_input" : i % 3 === 1 ? "active" : "idle" }));
  await page.route("**/api/sessions", (route) => route.request().method() === "GET" ? route.fulfill({ json: many }) : route.continue());
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: /^busy-0 / })).toBeVisible();
  for (const name of ["Settings", "Guide", "Audit"]) {
    const link = nav.getByRole("link", { name, exact: true });
    await expect(link).toBeInViewport({ ratio: 1 });
    const box = (await link.boundingBox())!;
    const footer = (await page.getByRole("button", { name: "Sign out" }).first().boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(footer.y);
  }
  const pinned = nav.getByRole("group", { name: "Live sessions" });
  expect(await pinned.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
});

// UX review R11: no CSP error for an inlined font (fonts are files, never data: URLs).
test("no console errors from the content security policy", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
  expect(errors.filter((e) => /Content Security Policy|font/i.test(e))).toEqual([]);
});
