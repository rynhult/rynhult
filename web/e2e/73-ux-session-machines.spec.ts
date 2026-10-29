import { expect, test } from "@playwright/test";
import { AUTH_FILE, demoSessions, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

test.describe("session machine labels", () => {
  for (const width of [1440, 390, 320]) for (const theme of ["dark", "light"]) {
    test(`${width}px ${theme}: main and remote machines are visible on every session list`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width < 768 ? 844 : 900 });
      await page.emulateMedia({ colorScheme: theme });
      await page.route("**/api/auth/state", async (route) => {
        const response = await route.fetch();
        const data = await response.json();
        await route.fulfill({ json: { ...data, instance: { ...data.instance, host: "acc-host" } } });
      });
      const sessions = demoSessions().slice(3, 5).map((session, i) => ({
        ...session, name: i ? "Remote task with a long descriptive name" : "Main task with a long descriptive name",
        host: i ? "lab-box" : "local",
      }));
      await page.route("**/api/sessions", (route) => route.fulfill({ json: sessions }));
      for (const [path, screenshot] of [["/", "overview"], ["/sessions", "sessions"], ["/board", "board"]]) {
        await page.goto(path);
        if (path === "/" && width === 1440) {
          const host = page.getByRole("columnheader", { name: "Host", exact: true }).first();
          await expect(host).toBeVisible();
          await host.getByRole("button").click();
          await expect(host).toHaveAttribute("aria-sort", "ascending");
          const rows = page.getByRole("main").getByRole("row").filter({ has: page.getByRole("link", { name: /task with a long/ }) });
          await expect(rows.first()).toContainText("Main task");
          await host.getByRole("button").click();
          await expect(host).toHaveAttribute("aria-sort", "descending");
          await expect(rows.first()).toContainText("Remote task");
        }
        for (const [i, machine] of ["acc-host", "lab-box"].entries()) {
          const row = path === "/board"
            ? page.locator(`li[data-session="${sessions[i].name}"]`)
            : page.getByRole("main").getByRole("row").filter({ hasText: sessions[i].name });
          const label = row.getByText(machine, { exact: true }).filter({ visible: true });
          await expect(label).toBeVisible();
          const bounds = await label.boundingBox();
          expect(bounds!.x).toBeGreaterThanOrEqual(0);
          expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
        await expectAxeClean(page);
        if (theme === "dark" && width !== 320) {
          const target = path === "/board"
            ? page.getByRole("region", { name: "Running sessions", exact: true })
            : page.getByRole("table", { name: "Sessions", exact: true }).first();
          await target.screenshot({ path: wikiShotPath(testInfo, `${screenshot}-machines${width === 390 ? "-phone" : ""}.png`),
            animations: "disabled" });
        }
      }
    });
  }
});
