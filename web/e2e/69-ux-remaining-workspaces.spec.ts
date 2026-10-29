import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px workspace actions`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`workspace secondary actions preserve typed deletion and empty-folder links in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const session = demoSessions()[2];
        await page.route("**/api/workspaces", r => r.fulfill({ json: [
          { folder: "market-scan-20261025", path: "/srv/acc-demo/market-scan-20261025", created_at: session.created_at,
            kind: "research", project: null, sessions: [{ ...session, profile: "researcher" }], docs: { research: 2, council: 1 } },
          { folder: "empty-research-20261025", path: "/srv/acc-demo/empty-research-20261025", created_at: session.created_at,
            kind: "research", project: null, sessions: [], docs: {} },
        ] }));
        let deleted = false;
        await page.route("**/api/workspaces/*/delete", r => { deleted = true; return r.fulfill({ json: {} }); });
        await page.goto("/workspaces");
        await expect(page.getByText("Research / council", { exact: true }).first()).toBeVisible();
        await expect(page.locator('a[href*="undefined"]')).toHaveCount(0);
        await expect(page.getByRole("menuitem", { name: "Delete workspace Market scan", exact: true })).toHaveCount(0);
        const actions = page.getByRole("button", { name: "Workspace actions for Market scan", exact: true });
        await actions.click();
        await expect(page.getByRole("menu", { name: "Workspace actions for Market scan" }).getByRole("menuitem", { name: "Files", exact: true })).toBeVisible();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "workspaces-actions-phone.png" : "workspaces.png") });
        await page.getByRole("menuitem", { name: "Delete workspace Market scan", exact: true }).click();
        const confirmation = page.getByRole("dialog", { name: "Delete Market scan?" });
        await expect(confirmation.getByRole("button", { name: "Delete workspace", exact: true })).toBeDisabled();
        await confirmation.getByRole("textbox", { name: "Type the folder name to confirm" }).fill("market-scan-20261025");
        await expect(confirmation.getByRole("button", { name: "Delete workspace", exact: true })).toBeEnabled();
        await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
        expect(deleted).toBe(false);
        await expect(actions).toBeFocused();
      });

      test(`project archive stays secondary and cancellation returns focus in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.route("**/api/projects", r => r.fulfill({ json: [{
          id: 91, name: "Demo API", slug: "demo-api", account: "alex-agent", host: "local",
          workspace_path: "/srv/acc-demo/demo-api", repo: "example-org/demo-api", remote_url: "https://github.com/example-org/demo-api",
          import_mode: "managed", trusted: true, archived: false, notes: null, compose_autostart: false,
          compose_approved: false, git_backup: false,
        }] }));
        await page.route("**/api/projects/91/runtime", r => r.fulfill({ json: { containers: [], note: "no compose file" } }));
        await page.goto("/projects");
        await expect(page.getByRole("button", { name: "New session in Demo API", exact: true })).toBeVisible();
        await expect(page.getByRole("menuitem", { name: "Archive Demo API", exact: true })).toHaveCount(0);
        const actions = page.getByRole("button", { name: "Project actions for Demo API", exact: true });
        await actions.click();
        await expectAxeClean(page);
        if (theme === "dark") await page.screenshot({ animations: "disabled", fullPage: true,
          path: wikiShotPath(testInfo, width === 390 ? "projects-actions-phone.png" : "projects.png") });
        await page.getByRole("menuitem", { name: "Archive Demo API", exact: true }).click();
        const confirmation = page.getByRole("dialog", { name: "Archive Demo API?" });
        await expect(confirmation).toContainText("workspace, containers and repository are left exactly as they are");
        await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
        await expect(actions).toBeFocused();
      });
    }
  });
}
