import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, AXE_PAGES, csrfHeaders, expectAxeCleanPage, SESSION_TABS, sessionWithChanges, waitForSessionState } from "./helpers";

test.use({ storageState: AUTH_FILE });

let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

// UX plan WP5: zero axe violations (WCAG 2.2 A/AA, no exclusions but xterm's own markup) on every page, every session
// tab, the palette, New session and the empty Overview, dark and light. 27-ux-a11y-mobile does the same on a phone.
for (const scheme of ["dark", "light"] as const) {
  test(`axe: every page and session tab, ${scheme}`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: scheme });
    const id = sessionId = await sessionWithChanges(page, `a11y-demo-${randomUUID().slice(0, 8)}`);
    for (const url of AXE_PAGES) await expectAxeCleanPage(page, url);
    for (const tab of SESSION_TABS) {
      await expectAxeCleanPage(page, `/sessions/${id}${tab}`, () => expect(page.getByRole("tablist")).toBeVisible());
    }
    // Whole diff lines are semantic pointer targets; keyboard navigation stays on the named region.
    await page.goto(`/sessions/${id}?tab=changes`);
    await page.getByRole("region", { name: /^Changes in app\.py/ }).focus();
    await page.keyboard.press("ArrowDown");
    const comment = page.getByRole("button", { name: /^Comment on line / });
    expect(await comment.count()).toBeGreaterThan(0);
    for (const line of await comment.all()) await expect(line).toHaveAttribute("tabindex", "-1");
    const box = (await comment.first().boundingBox())!;
    expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(24);
  });

  test(`axe: palette, New session and the empty Overview, ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await expectAxeCleanPage(page, "/", async () => {
      await page.keyboard.press("Control+k");
      await page.keyboard.type("se");
      await expect(page.getByRole("option").first()).toBeVisible();
    });
    await expectAxeCleanPage(page, "/", async () => {
      await page.getByRole("button", { name: "New session" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
    });
    for (const path of ["**/api/sessions", "**/api/projects", "**/api/attention"]) {
      await page.route(path, (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
    }
    await expectAxeCleanPage(page, "/", () => expect(page.getByText("No projects yet.")).toBeVisible());
  });
}

// SE-8: one Tab stop per tab list; Arrow keys, Home and End move and select; tabs control a labelled panel.
test("tabs: arrow keys on Catalog and the session page", async ({ page }) => {
  await page.goto("/catalog");
  const profiles = page.getByRole("tab", { name: /Profiles/ });
  await expect(profiles).toHaveAttribute("tabindex", "0");
  await expect(page.getByRole("tab", { name: /Skills/ })).toHaveAttribute("tabindex", "-1");
  await profiles.focus();
  await page.keyboard.press("ArrowRight");
  const skills = page.getByRole("tab", { name: /Skills/ });
  await expect(skills).toBeFocused();
  await expect(skills).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toHaveAccessibleName(/Skills/);
  await page.keyboard.press("End");
  await expect(page.getByRole("tab", { name: /Not loaded by default/ })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");  // wraps
  await expect(profiles).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByRole("tab", { name: /Not loaded by default/ })).toBeFocused();

  const id = sessionId = await sessionWithChanges(page, `a11y-tabs-${randomUUID().slice(0, 8)}`);
  await waitForSessionState(page, id, "idle", "claude_hook");
  const terminal = page.getByRole("tab", { name: "Terminal" });
  let release!: () => void;
  const terminalChunk = new Promise<void>(resolve => { release = resolve; });
  await page.route(/\/assets\/Terminal-[^/]+\.js(?:\?.*)?$/, async route => {
    await terminalChunk;
    await route.continue();
  });
  try {
    await page.goto(`/sessions/${id}`);
    await terminal.focus();
    release();
    await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
    await expect(terminal).toBeFocused();
  } finally {
    release();
  }
  await page.keyboard.press("ArrowRight");
  await expect(page).toHaveURL(/\?tab=files$/);
  await expect(page.getByRole("tab", { name: "Files" })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "session-tabs-tab-Files");
  await page.keyboard.press("End");
  await expect(page).toHaveURL(/\?tab=spec$/);
  await expect(page.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "session-tabs-tab-Setup");
  await page.keyboard.press("Home");
  await expect(terminal).toHaveAttribute("aria-selected", "true");
});

test("a tab the session can't show falls back to Terminal (SE-13)", async ({ page }) => {
  const id = sessionId = await sessionWithChanges(page, `a11y-fallback-${randomUUID().slice(0, 8)}`);
  await page.goto(`/sessions/${id}?tab=team`);
  await expect(page.getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toBeVisible();
});

test("names match what is shown: the Jump to button and Files rows (NAV-7)", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Jump to…" }).first()).toHaveAttribute("aria-keyshortcuts", "Control+K");
  const id = sessionId = await sessionWithChanges(page, `a11y-files-${randomUUID().slice(0, 8)}`);
  await page.goto(`/sessions/${id}?tab=files`);
  await expect(page.getByRole("button", { name: "Open app.py, modified", exact: true })).toBeVisible();
});
