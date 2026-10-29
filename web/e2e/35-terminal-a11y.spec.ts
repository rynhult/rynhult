import { expect, test } from "@playwright/test";
import { AUTH_FILE, control, csrfHeaders, sessionWithChanges } from "./helpers";

test.use({ storageState: AUTH_FILE });

let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

// UX review SE-9 (WP6): the terminal has an accessibility tree, a documented key leaves it, and state changes are
// announced once through the app's one polite live region.
test("terminal: screen-reader rows, F6 leaves it, state changes are announced", async ({ page }) => {
  const id = sessionId = await sessionWithChanges(page, "terminal-a11y");
  const session = await (await page.request.get(`/api/sessions/${id}`)).json();
  await page.goto(`/sessions/${id}`);
  const input = page.locator(".xterm-helper-textarea");
  await expect(input).toBeFocused();
  await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(1);
  await expect(page.locator(".xterm .live-region")).toHaveAttribute("aria-live", "off");  // the stream is never read aloud
  await expect(page.getByText("leaves the terminal")).toBeVisible();
  await expect(input).toHaveAttribute("aria-describedby", `leave-${id}`);

  await page.keyboard.press("F6");
  await expect(input).not.toBeFocused();
  const outside = await page.evaluate(() => !document.activeElement?.closest(".xterm") && document.activeElement !== document.body);
  expect(outside).toBe(true);
  await page.locator(".xterm").click();
  await expect(input).toBeFocused();
  await page.keyboard.press("Shift+F6");
  await expect(input).not.toBeFocused();

  // One live region on the page; the risk label is no longer one.
  const announcer = page.getByRole("status", { name: "Announcements" });
  await expect(announcer).toHaveCount(1);
  control(id, "permission");
  await expect(announcer).toContainText(`${session.name}: Needs you`, { timeout: 15_000 });
  await expect(page.locator('[role="note"][aria-live]')).toHaveCount(0);
});
