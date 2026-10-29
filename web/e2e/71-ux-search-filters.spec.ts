import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px conversation filters`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`filters survive typing and reset distinguishes an empty index from no matches in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.route("**/api/projects", r => r.fulfill({ json: [{
          id: 91, name: "Demo API", slug: "demo-api", account: "alex-agent", host: "local",
          workspace_path: "/srv/acc-demo/demo-api", repo: "example-org/demo-api", remote_url: "https://github.com/example-org/demo-api",
          import_mode: "managed", trusted: true, archived: false, notes: null, compose_autostart: false,
          compose_approved: false, git_backup: false,
        }] }));
        let last: URLSearchParams | undefined;
        await page.route("**/api/search?*", r => {
          last = new URL(r.request().url()).searchParams;
          return r.fulfill({ json: { query: last.get("q"), results: [], indexed_sessions: last.has("project_id") ? 0 : 3 } });
        });
        await page.goto("/search?q=billing&project_id=91&provider=claude&since=2026-10-15");
        await expect(page.getByRole("combobox", { name: "Project filter", exact: true })).toHaveValue("91");
        await expect(page.getByRole("combobox", { name: "Agent filter", exact: true })).toHaveValue("claude");
        await expect(page.getByLabel("Sessions started since (UTC)")).toHaveValue("2026-10-15");
        await expect(page.getByRole("main")).toContainText("No indexed conversations in these filters yet.");
        await page.getByRole("searchbox").fill("deployment");
        await expect(page).toHaveURL(/q=deployment/);
        await expect.poll(() => last?.get("project_id")).toBe("91");
        await expect.poll(() => last?.get("provider")).toBe("claude");
        await expect.poll(() => last?.get("since")).toBe("2026-10-15");
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "search-filters-phone.png" : "search-filters.png") });
        await page.getByRole("button", { name: "Clear filters", exact: true }).click();
        await expect(page).toHaveURL(/\/search\?q=deployment$/);
        await expect(page.getByRole("main")).toContainText("No conversation mentions");
        await expect(page.getByRole("main")).not.toContainText("No indexed conversations");
        await expect(page.getByRole("combobox", { name: "Project filter", exact: true })).toHaveValue("");
      });
    }
  });
}
