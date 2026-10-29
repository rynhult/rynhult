import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { AUTH_FILE, control, expectAxeClean, info, sessionWithChanges, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px grid labels`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`session names keep space ahead of long activity text in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const a = await sessionWithChanges(page, `grid-label-a-${width}-${theme}`);
        const b = await sessionWithChanges(page, `grid-label-b-${width}-${theme}`);
        const sessions = await Promise.all([a, b].map(async id => (await page.request.get(`/api/sessions/${id}`)).json()));
        const rows = sessions.map((session, i) => ({ ...session, name: i ? "review-checkout" : "checkout-flow-auth",
          observed_state: "active", activity: "Checking the checkout workflow and writing a focused browser regression. ".repeat(8),
          activity_at: new Date().toISOString() }));
        await page.route("**/api/sessions", route => route.fulfill({ json: rows }));
        await page.goto(`/grid?ids=${a},${b}`);
        const tile = page.getByRole("region", { name: "checkout-flow-auth", exact: true });
        await expect(tile.locator(".xterm-screen")).toBeVisible();
        await expect.poll(() => tile.locator(".xterm-screen").evaluate(element => element.clientHeight)).toBeGreaterThan(100);
        for (const [index, id] of [a, b].entries()) {
          writeFileSync(`${info().run_dir}/${id}/fake.screen`,
            `\x1b[2J\x1b[H${index ? "Review checkout" : "Checkout workflow"}\n\nFocused browser regression ready.\n`);
          control(id, "screen");
        }
        await expect(tile.locator(".xterm-rows")).toContainText("Checkout workflow");
        const title = tile.locator('[title="checkout-flow-auth"]');
        await expect.poll(() => title.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, `grid-labels${width === 390 ? "-phone" : ""}.png`) });
      });
    }
  });
}
