import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, csrfHeaders, expectAxeClean, resetTerminalThrottle, sessionWithChanges, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });
test.beforeEach(async ({ page }) => {
  await resetTerminalThrottle(page);
});

let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

test("mobile: accessible output fits beside a usable terminal key bar without grabbing focus", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const id = sessionId = await sessionWithChanges(page, `phone-output-demo-${randomUUID().slice(0, 8)}`);
  await page.goto(`/sessions/${id}`);
  const input = page.locator(".xterm-helper-textarea");
  await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
  await expect(input).not.toBeFocused();
  await expect(page.getByText("leaves the terminal")).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Screen reader output" })).toBeInViewport();
  const enter = page.getByRole("button", { name: "Enter", exact: true });
  await expect(enter).toBeInViewport();
  const y = (await page.locator(".xterm").boundingBox())!.y;
  expect(y).toBeLessThan(250);  // the header's 44 px touch targets (phone pass) cost ~20 px
  await enter.tap();
  await expect(input).not.toBeFocused();
  await page.getByRole("button", { name: "Esc", exact: true }).tap();
  await expect(input).not.toBeFocused();
  await page.getByRole("button", { name: "More keys" }).tap();
  await expect(page.getByRole("button", { name: "Ctrl", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await expectAxeClean(page);
  await page.getByRole("button", { name: "More keys" }).tap();
  await page.screenshot({ path: wikiShotPath(testInfo, "phone-terminal.png"), fullPage: true });
  await page.getByRole("button", { name: "Open as text", exact: true }).tap();
  const text = page.getByRole("textbox", { name: "Recent terminal output" });
  await expect(text).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await expectAxeClean(page);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open as text", exact: true })).toBeFocused();
  await page.emulateMedia({ colorScheme: "light" });
  await expectAxeClean(page);
});
