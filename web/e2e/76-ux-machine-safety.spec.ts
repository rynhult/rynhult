import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, gotoAppPage, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px machine safety`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390, hasTouch: width === 390 });
    for (const theme of ["dark", "light"] as const) {
      test(`silent hooks are Unknown, never Working or Thinking, in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        const old = new Date(Date.now() - 7 * 86400000).toISOString();
        const session = { ...demoSessions()[3], name: "Demo admin", observed_state: "active",
          status_source: "claude_hook", status_at: old, activity: "Thinking", activity_at: old,
          status_stale_since: old, host: "local" };
        await page.route("**/api/sessions", route => route.fulfill({ json: [session] }));
        for (const path of ["/", "/sessions", "/board"]) {
          await gotoAppPage(page, path);
          const main = page.getByRole("main");
          await expect(main.getByText(/Unknown since/).filter({ visible: true }).first()).toBeVisible();
          await expect(main.getByText("Thinking", { exact: true }).filter({ visible: true })).toHaveCount(0);
          if (path === "/board") await expect(page.getByRole("banner").getByText("Working", { exact: true })
            .filter({ visible: true }).locator("..").getByText("0", { exact: true })).toBeVisible();
          await expectAxeClean(page);
          if (theme === "dark" && path === "/sessions") await page.screenshot({ fullPage: true, animations: "disabled",
            path: wikiShotPath(testInfo, `sessions-unknown${width === 390 ? "-phone" : ""}.png`) });
        }
      });

      test(`main machine names and account confirmation precede any login in ${theme}`, async ({ page }, testInfo) => {
        await page.emulateMedia({ colorScheme: theme });
        await page.route("**/api/auth/state", async route => {
          const data = await (await route.fetch()).json();
          await route.fulfill({ json: { ...data, instance: { ...data.instance, host: "acc-host", agent_user: "agent" } } });
        });
        await page.route("**/api/nodes", route => route.fulfill({ json: [{
          id: "demo-node", name: "lab-box", status: "active", role: "workstation", connected: true,
          last_seen: new Date().toISOString(), offline_threshold_seconds: 600, accounts: ["agent"], info: { os: "linux" },
        }] }));
        let logins = 0;
        let loginMachine: string | null | undefined;
        await page.route("**/api/accounts**", route => {
          if (route.request().method() === "POST") {
            if (new URL(route.request().url()).pathname.endsWith("/login")) {
              logins++;
              loginMachine = new URL(route.request().url()).searchParams.get("machine");
            }
            return route.fulfill({ json: { flow_id: "demo-login", done: false, url: "https://example.com/authorize" } });
          }
          return route.fulfill({ json: { account: "agent", default_account: "agent", accounts: ["agent"],
            checked_at: Date.now() / 1000, claude: { logged_in: true }, codex: { logged_in: true }, github: { logged_in: false } } });
        });
        for (const machine of ["local", "lab-box"]) {
          await gotoAppPage(page, machine === "local" ? "/providers" : `/providers?machine=${machine}`);
          const host = machine === "local" ? "acc-host" : "lab-box";
          if (width === 1440) await expect(page.getByRole("radio", { name: /acc-host.*main/ })).toBeVisible();
          else await expect(page.getByRole("combobox", { name: "Machine", exact: true })
            .getByRole("option", { name: "acc-host · main", exact: true })).toHaveCount(1);
          await page.getByRole("button", { name: "Re-login", exact: true }).first().click();
          const dialog = page.getByRole("dialog", { name: `Sign in again as agent on ${host}?`, exact: true });
          await expect(dialog).toBeVisible();
          expect(logins).toBe(0);
          await expectAxeClean(page);
          if (machine === "local" && theme === "dark") await page.screenshot({ fullPage: true, animations: "disabled",
            path: wikiShotPath(testInfo, `providers-confirm-machine${width === 390 ? "-phone" : ""}.png`) });
          await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
          expect(logins).toBe(0);
        }
        await page.getByRole("button", { name: "Re-login", exact: true }).first().click();
        const confirmation = page.getByRole("dialog", { name: "Sign in again as agent on lab-box?", exact: true });
        await expect(confirmation).toBeVisible();
        await page.evaluate(() => {
          history.pushState(null, "", "/providers");
          dispatchEvent(new PopStateEvent("popstate"));
        });
        await confirmation.getByRole("button", { name: "Start re-login", exact: true }).click();
        await expect.poll(() => loginMachine).toBe("lab-box");
        await expect(page.getByRole("dialog", { name: /as agent on lab-box/ })).toBeVisible();
        await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
        await gotoAppPage(page, "/projects");
        await page.getByRole("button", { name: "New project", exact: true }).first().click();
        await expect(page.getByRole("dialog").getByRole("combobox", { name: "Machine", exact: true })
          .getByRole("option", { name: "acc-host · main", exact: true })).toHaveCount(1);
        if (theme === "dark") await page.screenshot({ fullPage: true, animations: "disabled",
          path: wikiShotPath(testInfo, `projects-machine-picker${width === 390 ? "-phone" : ""}.png`) });
        await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
        await page.getByRole("button", { name: "Register folder", exact: true }).click();
        await expect(page.getByRole("dialog").getByRole("combobox", { name: "Machine", exact: true })
          .getByRole("option", { name: "acc-host · main", exact: true })).toHaveCount(1);
      });
    }
  });
}
