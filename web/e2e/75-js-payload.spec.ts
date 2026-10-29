import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, freshStep, loadSecret, PASSWORD, sessionWithChanges, totp } from "./helpers";

const payload = JSON.parse(execFileSync(process.execPath, ["scripts/check-bundle-size.mjs", "--report"], { encoding: "utf8" })) as {
  pages: Record<string, { js: { files: string[] }; css: { files: string[] } }>;
};
const initialAssets = (name: string) => [...payload.pages[name].js.files, ...payload.pages[name].css.files].map((file) => `/${file}`).sort();

for (const phone of [false, true]) {
  test.describe(phone ? "phone payload" : "desktop payload", () => {
    test.use({ storageState: AUTH_FILE, viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 },
      isMobile: phone, hasTouch: phone });

    test("sign-in loads only its budgeted assets and still signs in", async ({ browser }, testInfo) => {
      const context = await browser.newContext({ storageState: { cookies: [], origins: [] },
        viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 }, isMobile: phone, hasTouch: phone, colorScheme: "dark" });
      const page = await context.newPage();
      const assets = new Set<string>();
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (/^\/assets\/.*\.(js|css)$/.test(path)) assets.add(path);
      });
      try {
        await page.goto("/login");
        await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
        expect([...assets].sort()).toEqual(initialAssets("sign-in"));
        await expectAxeClean(page);
        await page.screenshot({ path: testInfo.outputPath("sign-in.png"), fullPage: true });
        await page.getByLabel("Username", { exact: true }).fill("alex");
        await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
        await freshStep(page);
        await page.getByLabel("Authenticator code", { exact: true }).fill(totp(loadSecret()));
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await expect(page).toHaveURL(/\/$/);
        await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
      } finally { await context.close(); }
    });

    test("Board loads only its budgeted assets without the authenticated shell", async ({ page }, testInfo) => {
      const assets = new Set<string>();
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (/^\/assets\/.*\.(js|css)$/.test(path)) assets.add(path);
      });
      await page.goto("/board");
      await expect(page.getByRole("heading", { name: "Status board", exact: true })).toBeVisible();
      expect([...assets].sort()).toEqual(initialAssets("board"));
      expect([...assets].filter((path) => /\/(?:Layout|NewSessionDialog|Terminal|FileEditor)-/.test(path))).toEqual([]);
      await expectAxeClean(page);
      await page.screenshot({ path: testInfo.outputPath("board.png"), fullPage: true });
    });

    test("session defers Files and Changes until selected, keeping terminal and editor usable", async ({ page, request }, testInfo) => {
      const id = await sessionWithChanges(page, `payload-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      const scripts = new Set<string>();
      const assets = new Set<string>();
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (request.resourceType() === "script") scripts.add(path);
        if (/^\/assets\/.*\.(js|css)$/.test(path)) assets.add(path);
      });
      try {
        await page.goto(`/sessions/${id}`);
        const terminal = page.locator(".xterm-helper-textarea");
        await expect(terminal).toBeVisible();
        if (!phone) await expect(terminal).toBeFocused();
        expect([...assets].sort()).toEqual(initialAssets("session"));
        expect([...scripts].filter((path) => /\/(?:Files|Changes|FileEditor|Preview|PullRequest|TeamPanel)-[^/]+\.js$/.test(path))).toEqual([]);
        if (phone) {
          await page.getByRole("button", { name: "More", exact: true }).click();
          await page.getByRole("menuitem", { name: "Files", exact: true }).click();
        } else await page.getByRole("tab", { name: "Files", exact: true }).click();
        await expect(page.getByRole("button", { name: "Refresh files", exact: true })).toBeVisible();
        expect([...scripts].some((path) => /\/Files-[^/]+\.js$/.test(path))).toBe(true);
        expect([...scripts].some((path) => /\/FileEditor-[^/]+\.js$/.test(path))).toBe(false);
        const created = await page.request.post(`/api/files/session/${id}`, {
          headers, data: { action: "create", path: "payload-notes.md", content: "# Lazy preview\n\nA demo document.\n" },
        });
        expect(created.ok()).toBe(true);
        await page.getByRole("button", { name: "Refresh files", exact: true }).click();
        await page.getByRole("button", { name: fileRow("payload-notes.md") }).click();
        const editor = page.getByRole("textbox", { name: "File content" });
        await expect(editor).toBeVisible();
        await expect(page.getByRole("heading", { name: "Lazy preview", exact: true })).toBeVisible();
        await editor.fill("# Updated preview\n\nKept after the chunk loads.");
        await expect(page.getByRole("heading", { name: "Updated preview", exact: true })).toBeVisible();
        await page.getByRole("button", { name: "Save", exact: true }).click();
        await expect(page.getByRole("status", { name: "File save status" })).toContainText("Saved");
        await expectAxeClean(page);
        await page.screenshot({ path: testInfo.outputPath("lazy-files.png"), fullPage: true });
        await page.getByRole("tab", { name: /^Changes(?: \d+)?$/ }).click();
        await expect.poll(() => [...scripts].some((path) => /\/Changes-[^/]+\.js$/.test(path))).toBe(true);
        await page.getByRole("tab", { name: "Terminal", exact: true }).click();
        await expect(terminal).toBeVisible();
        await expectAxeClean(page);
      } finally {
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
    });

    test("a Files deep link loads its editor without xterm or the Markdown preview until needed", async ({ page, request }) => {
      const id = await sessionWithChanges(page, `payload-files-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      const scripts = new Set<string>();
      page.on("request", (request) => { if (request.resourceType() === "script") scripts.add(new URL(request.url()).pathname); });
      try {
        await page.goto(`/sessions/${id}?tab=files&file=app.py`);
        const editor = page.getByRole("textbox", { name: "File content" });
        await expect(editor).toContainText("changed");
        expect([...scripts].filter((path) => /\/(?:Terminal|FileMarkdownPreview)-/.test(path))).toEqual([]);
        if (phone) await page.getByRole("button", { name: "Show tree", exact: true }).click();
        await page.getByRole("button", { name: fileRow("README.md") }).click();
        await expect(page.getByRole("heading", { name: "Demo", exact: true })).toBeVisible();
        expect([...scripts].some((path) => /\/FileMarkdownPreview-/.test(path))).toBe(true);
      } finally {
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
    });
  });
}
