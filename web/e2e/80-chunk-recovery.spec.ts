import { expect, test as base } from "./fixtures";
import { gotoLoadedPage, loadPage } from "./loaded-page";
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, sessionWithChanges, wikiShotPath } from "./helpers";

const test = base.extend<{ id: string; ownedSessions: string[] }>({
  ownedSessions: [async ({ page }, use) => {
    const sessions: string[] = [];
    await use(sessions);
    for (const id of sessions) {
      expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    }
  }, { timeout: 45_000 }],
  id: [async ({ page, ownedSessions }, use) => {
    const id = await sessionWithChanges(page, 'chunk-recovery');
    ownedSessions.push(id);
    await use(id);
  }, { timeout: 45_000 }],
});
test.use({ storageState: AUTH_FILE });

for (const phone of [false, true]) {
  test.describe(phone ? "phone chunk recovery" : "desktop chunk recovery", () => {
    test.use({ viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 }, isMobile: phone, hasTouch: phone });

    test("a transient route chunk failure retries once without reloading", async ({ page }) => {
      await gotoLoadedPage(page, "/sessions", ["Sessions"], ["/api/sessions"]);
      await expect(page.getByRole("heading", { name: "Sessions", exact: true })).toBeVisible();
      let attempts = 0;
      let navigations = 0;
      page.on("request", (request) => { if (request.isNavigationRequest()) navigations++; });
      await page.route(/\/assets\/Host-[^/]+\.js(?:\?.*)?$/, (route) => {
        attempts++;
        return attempts === 1 ? route.abort("connectionreset") : route.continue();
      });
      await page.evaluate(() => {
        history.pushState(null, "", "/host");
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      await expect(page.getByRole("heading", { name: "Host", exact: true })).toBeVisible();
      expect(attempts).toBe(2);
      expect(navigations).toBe(0);
      await expect(page.getByRole("region", { name: "Update required", exact: true })).toHaveCount(0);
    });

    test("missing preview chunks keep unsaved edits and wait for an explicit reload", async ({ page, id }, testInfo) => {
      // Two cold documents, real reads/write and axe; retain each functional assertion's deadline.
      test.setTimeout(90_000);
      await gotoLoadedPage(page, `/sessions/${id}?tab=files`, ["SessionPage", "Files"], [`/api/files/session/${id}`]);
      const readFile = (path: string) => page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.pathname === `/api/files/session/${id}/read` && url.searchParams.get('path') === path;
      });
      const appRead = readFile('app.py');
      await page.getByRole("button", { name: fileRow("app.py") }).click();
      expect((await appRead).ok()).toBe(true);
      expect(await (await appRead).finished()).toBeNull();
      const editor = page.getByRole("textbox", { name: "File content", exact: true });
      await expect(editor).toBeVisible();
      let attempts = 0;
      let navigations = 0;
      let release!: () => void;
      const gate = new Promise<void>((resolve) => { release = resolve; });
      page.on("request", (req) => { if (req.isNavigationRequest()) navigations++; });
      await page.route(/\/assets\/FileMarkdownPreview-[^/]+\.js(?:\?.*)?$/, async (route) => {
        attempts++;
        await gate;
        await route.fulfill({ status: 404, contentType: "text/plain", body: "Not Found" });
      });
      if (phone) await page.getByRole("button", { name: "Show tree", exact: true }).click();
      const markdownRead = readFile('README.md');
      await page.getByRole("button", { name: fileRow("README.md") }).click();
      expect((await markdownRead).ok()).toBe(true);
      expect(await (await markdownRead).finished()).toBeNull();
      await expect(editor).toBeVisible();
      await editor.fill("# Keep this unsaved draft");
      release();
      const banner = page.getByRole("region", { name: "Update required", exact: true });
      await expect(banner).toContainText("Rynhult was updated, reload to continue");
      await expect(editor).toHaveText("# Keep this unsaved draft");
      expect(attempts).toBe(2);
      expect(navigations).toBe(0);
      await expectAxeClean(page);
      await page.screenshot({ path: wikiShotPath(testInfo, `chunk-recovery-${phone ? "phone" : "desktop"}.png`), fullPage: true });
      await banner.getByRole("button", { name: "Reload", exact: true }).focus();
      await page.keyboard.press("Enter");
      const confirmation = page.getByRole("dialog", { name: "Reload Rynhult?", exact: true });
      await expect(confirmation).toContainText("unsaved");
      await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(editor).toHaveText("# Keep this unsaved draft");
      expect(navigations).toBe(0);
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === `/api/files/session/${id}`
        && response.request().method() === 'POST');
      await page.getByRole("button", { name: "Save", exact: true }).click();
      expect((await saved).ok()).toBe(true);
      expect(await (await saved).finished()).toBeNull();
      await expect(page.getByRole("status", { name: "File save status" })).toContainText("Saved. Replaced README.md (the previous copy is kept as");
      await page.unroute(/\/assets\/FileMarkdownPreview-[^/]+\.js(?:\?.*)?$/);
      await banner.getByRole("button", { name: "Reload", exact: true }).click();
      await loadPage(page, () => confirmation.getByRole("button", { name: "Reload", exact: true }).click(),
        ["SessionPage", "Files", "FileMarkdownPreview"], [`/api/files/session/${id}/read`]);
      await expect(page.getByRole("region", { name: "Markdown preview", exact: true })).toContainText("Keep this unsaved draft");
      await expect(banner).toHaveCount(0);
      expect(navigations).toBe(1);
    });

    test("a failed shell chunk still exposes reload outside Suspense", async ({ page }) => {
      let attempts = 0;
      await page.route(/\/assets\/Layout-[^/]+\.js(?:\?.*)?$/, (route) => {
        attempts++;
        return route.fulfill({ status: 404, contentType: "text/plain", body: "Not Found" });
      });
      await page.goto("/sessions");
      const banner = page.getByRole("region", { name: "Update required", exact: true });
      await expect(banner.getByRole("button", { name: "Reload", exact: true })).toBeVisible();
      expect(attempts).toBe(2);
      await expectAxeClean(page);
    });

    test("a transient terminal stylesheet failure is retried before showing the terminal", async ({ page, id }) => {
      let attempts = 0;
      await page.route(/\/assets\/(?:Terminal|terminalHostLayer)-[^/]+\.css(?:\?.*)?$/, (route) => {
        attempts++;
        return attempts === 1 ? route.abort("connectionreset") : route.continue();
      });
      const stylesheet = page.waitForResponse(response => /\/assets\/(?:Terminal|terminalHostLayer)-[^/]+\.css$/.test(new URL(response.url()).pathname)
        && response.status() === 200);
      await gotoLoadedPage(page, `/sessions/${id}`, ["SessionPage", "Terminal"]);
      expect(await (await stylesheet).finished()).toBeNull();
      await expect(page.locator(".xterm-screen")).toBeVisible();
      expect(attempts).toBe(2);
      await expect(page.getByRole("region", { name: "Update required", exact: true })).toHaveCount(0);
    });
  });
}
