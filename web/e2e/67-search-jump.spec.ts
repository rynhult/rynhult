import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, routeDemoSessions, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const scheme of ["dark", "light"] as const) {
      test(`Jump to navigates; Search finds conversation content, ${scheme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: scheme });
        await routeDemoSessions(page);
        await page.route("**/api/projects", (route) => route.fulfill({ json: [] }));
        await page.route("**/api/search?*", (route) => route.fulfill({ json: {
          results: [{ session: demoSessions()[5], matches: 1,
            hits: [{ seq: 1, role: "assistant", snippet: "Review the \x02billing webhook\x03 before release." }] }],
        } }));
        await page.goto("/search");
        await expect(page.getByRole("heading", { name: "Search", exact: true })).toBeVisible();
        await expect(page.getByRole("main")).toContainText("For pages, projects, sessions and commands, use Jump to… in the top bar.");
        await page.getByRole("button", { name: "Jump to…", exact: true }).click();
        const palette = page.getByRole("dialog", { name: "Jump to…" });
        const input = palette.getByRole("combobox", { name: "Jump to…" });
        await expect(input).toBeFocused();
        await expect(palette).toContainText("Pages, projects, sessions and commands");
        await expectAxeClean(page);
        if (scheme === "dark") await page.screenshot({ animations: "disabled",
          path: wikiShotPath(testInfo, width === 390 ? "command-palette-phone.png" : "command-palette.png") });
        await palette.getByRole("link", { name: "Search conversation content" }).focus();
        await page.keyboard.press("Enter");
        await expect(palette).toBeHidden();
        await expect(page.getByRole("dialog")).toHaveCount(0);
        await expect(page).toHaveURL(/\/search$/);

        for (const key of ["Control+k", "Meta+k"]) {
          await page.keyboard.press(key);
          await expect(input).toBeFocused();
          await input.fill("billing webhook");
          await page.keyboard.press("Enter");
          await expect(page).toHaveURL(/\/search\?q=billing%20webhook$/);
          await expect(page.getByRole("main").getByRole("link", { name: /api-refactor/ })).toBeVisible();
          await expect(page.getByRole("main").locator("mark")).toHaveText("billing webhook");
          await expectAxeClean(page);
          if (scheme === "dark" && key === "Control+k") {
            await page.screenshot({ animations: "disabled",
              path: wikiShotPath(testInfo, width === 390 ? "search-phone.png" : "search.png") });
          }
        }

        await page.keyboard.press("Control+k");
        await input.fill("Projects");
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("ArrowUp");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\/projects$/);
        await page.keyboard.press("Control+k");
        await input.fill("api-refactor");
        await page.keyboard.press("Enter");
        await expect(page).toHaveURL(/\/sessions\/aaaaaaaaaaa6$/);
        await page.keyboard.press("Control+k");
        await expect(input).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(palette).toBeHidden();
      });
    }
  });
}
