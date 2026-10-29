import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, sessionWithChanges, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

for (const phone of [false, true]) {
  test.describe(phone ? "phone chunk recovery" : "desktop chunk recovery", () => {
    test.use({ viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 }, isMobile: phone, hasTouch: phone });

    test("a transient route chunk failure retries once without reloading", async ({ page }) => {
      await page.goto("/sessions");
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

    test("missing preview chunks keep unsaved edits and wait for an explicit reload", async ({ page, request }, testInfo) => {
      const id = await sessionWithChanges(page, `chunk-recovery-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      try {
        await page.goto(`/sessions/${id}?tab=files`);
        await page.getByRole("button", { name: fileRow("app.py") }).click();
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
        await page.getByRole("button", { name: fileRow("README.md") }).click();
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
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect(page.getByRole("status", { name: "File save status" })).toHaveText("Saved");
        await page.unroute(/\/assets\/FileMarkdownPreview-[^/]+\.js(?:\?.*)?$/);
        await banner.getByRole("button", { name: "Reload", exact: true }).click();
        await confirmation.getByRole("button", { name: "Reload", exact: true }).click();
        await expect(page.getByRole("region", { name: "Markdown preview", exact: true })).toContainText("Keep this unsaved draft");
        await expect(banner).toHaveCount(0);
        expect(navigations).toBe(1);
      } finally {
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
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

    test("a transient terminal stylesheet failure is retried before showing the terminal", async ({ page, request }) => {
      const id = await sessionWithChanges(page, `chunk-css-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      try {
        let attempts = 0;
        await page.route(/\/assets\/Terminal-[^/]+\.css(?:\?.*)?$/, (route) => {
          attempts++;
          return attempts === 1 ? route.abort("connectionreset") : route.continue();
        });
        await page.goto(`/sessions/${id}`);
        await expect(page.locator(".xterm-screen")).toBeVisible();
        expect(attempts).toBe(2);
        await expect(page.getByRole("region", { name: "Update required", exact: true })).toHaveCount(0);
      } finally {
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
    });
  });
}
