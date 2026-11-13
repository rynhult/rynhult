import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { AUTH_FILE, csrfHeaders, expectAxeClean, fileRow, info, PASSWORD, selectSessionView, sessionWithChanges, totp } from "./helpers";

const payload = JSON.parse(execFileSync(process.execPath, ["scripts/check-bundle-size.mjs", "--report"], { encoding: "utf8" })) as {
  pages: Record<string, { js: { files: string[] }; css: { files: string[] } }>;
};
const initialAssets = (name: string) => [...payload.pages[name].js.files, ...payload.pages[name].css.files].map((file) => `/${file}`).sort();

for (const phone of [false, true]) {
  test.describe(phone ? "phone payload" : "desktop payload", () => {
    test.use({ storageState: AUTH_FILE, viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 },
      isMobile: phone, hasTouch: phone });
    // The manifest roots and runtime asset set both cover the sole retained frame.

    test("sign-in loads only its budgeted assets and still signs in", async ({ browser, page: owner }, testInfo) => {
      const context = await browser.newContext({ storageState: { cookies: [], origins: [] },
        viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 }, isMobile: phone, hasTouch: phone, colorScheme: "dark" });
      const page = await context.newPage();
      const assets = new Set<string>();
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (/^\/assets\/.*\.(js|css)$/.test(path)) assets.add(path);
      });
      try {
        // TOTP codes are single-use per person. Parallel repeats must not all
        // consume alex's next code, even when each waits for a fresh time step.
        // A member fixture exercises this generic sign-in path without adding
        // an unrelated owner-promotion/reverification requirement.
        const identity = randomUUID().replaceAll('-', '').slice(0, 12);
        const username = `payload-${identity}`;
        const account = `${username}-agent`;
        mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
        const invite = await owner.request.post('/api/users/invites', {
          headers: await csrfHeaders(owner), data: { role: 'member', account },
        });
        expect(invite.ok(), await invite.text()).toBe(true);
        const token = (await invite.json()).link.split('/').pop();
        const headers = { Origin: info().origin };
        const begin = await context.request.post('/api/auth/invite/begin', { headers, data: { token } });
        expect(begin.ok(), await begin.text()).toBe(true);
        const secret = (await begin.json()).totp_secret;
        const joined = await context.request.post('/api/auth/invite/complete', {
          headers, data: { token, username, password: PASSWORD, code: totp(secret) },
        });
        expect(joined.ok(), await joined.text()).toBe(true);
        await context.clearCookies();
        await page.goto("/login");
        await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
        expect([...assets].filter(path => /\/(?:ThemeAppearance|SetupLevelSetting|SettingsSurfaces|SessionChat|Chat|SourceViews|Shell)-/.test(path))).toEqual([]);
        expect([...assets].sort()).toEqual(initialAssets("sign-in"));
        await expectAxeClean(page);
        await page.screenshot({ path: testInfo.outputPath("sign-in.png"), fullPage: true });
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
        // Enrollment used the current step. Use the next, still-valid skew
        // window once, so payload coverage need not sleep at a wall-clock edge.
        await page.getByLabel("Authenticator code", { exact: true }).fill(totp(secret, Date.now() / 1000 + 30));
        // The URL changes before the lazy working page finishes loading. Wait
        // for that actual chunk, rather than spending the heading assertion's
        // render budget on a cold download under concurrent browser load.
        const [overview] = await Promise.all([
          page.waitForResponse(response => /\/assets\/Overview-[^/]+\.js$/.test(new URL(response.url()).pathname)),
          page.getByRole("button", { name: "Sign in", exact: true }).click(),
        ]);
        expect(overview.status()).toBe(200);
        expect(await overview.finished()).toBeNull();
        await expect(page).toHaveURL(/\/$/);
        await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
        const signedIn = await (await page.request.get('/api/auth/state')).json();
        expect(signedIn.username).toBe(username);
        expect(signedIn.role).toBe('member');
      } finally { await context.close(); }
    });

    for (const newShell of [true]) test(`Board loads only its budgeted assets without the authenticated shell (new layout ${newShell})`, async ({ page }, testInfo) => {
      await page.route('**/api/auth/state', async route => {
        const response = await route.fetch(); const state = await response.json();
        await route.fulfill({ response, json: { ...state, instance: { ...state.instance,
          features: { ...state.instance.features } } } });
      });

      const assets = new Set<string>();
      page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (/^\/assets\/.*\.(js|css)$/.test(path)) assets.add(path);
      });
      await page.goto("/board");
      await expect(page.getByRole("heading", { name: "Status board", exact: true })).toBeVisible();
      expect([...assets].filter(path => /\/(?:ThemeAppearance|SetupLevelSetting|SettingsSurfaces|SessionChat|Chat|SourceViews|Shell)-/.test(path))).toEqual([]);
      expect([...assets].filter(path => /\/useMutation-/.test(path))).toEqual([]);
      expect([...assets].sort()).toEqual(initialAssets("board"));
      expect([...assets].filter((path) => /\/(?:Layout|NewSessionDialog|Terminal|FileEditor)-/.test(path))).toEqual([]);
      await expectAxeClean(page);
      await page.screenshot({ path: testInfo.outputPath("board.png"), fullPage: true });
    });

    test("session defers Files and Changes until selected, keeping terminal and editor usable", async ({ page, request }, testInfo) => {
      const id = await sessionWithChanges(page, `payload-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      // The setup helper visits /sessions. Cancel that document's pending lazy
      // requests before measuring a fresh session document under load.
      await page.goto('about:blank');
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
        if (!phone) {
          // Explicit desktop session navigation focuses the real input.
          await expect(terminal).toBeFocused();
          // A browser-tab round trip must leave the person's chosen focus alone.
          const tab = page.getByRole('tab', { name: 'Terminal', exact: true });
          await tab.focus();
          const other = await page.context().newPage();
          await other.goto('about:blank'); await other.bringToFront();
          await page.bringToFront(); await other.close();
          await expect(tab).toBeFocused();
        } else await expect(terminal).not.toBeFocused();
        const phoneKeys = [...assets].filter(path => /\/TerminalPhoneKeys-[^/]+\.js$/.test(path));
        expect(phoneKeys).toHaveLength(phone ? 1 : 0);
        expect([...assets].filter(path => !phoneKeys.includes(path)).sort()).toEqual(initialAssets("session"));
        expect([...scripts].filter((path) => /\/(?:Files|Changes|FileEditor|Preview|PullRequest|TeamPanel|SessionSetup|SessionAutomation|SessionActivity|Artifacts|InboxLinks)-[^/]+\.js$/.test(path))).toEqual([]);
        const views = page.getByRole('tablist', { name: 'Session views', exact: true });
        await expect(views).toBeVisible();
        await expect(views.getByRole('tab', { name: 'Terminal', exact: true })).toBeInViewport();
        await selectSessionView(page, "Files");
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
        // Opening a file creates a retained working tab. Return to its Files
        // parent before using the session's secondary-view menu.
        await page.getByRole('tab', { name: / · Files$/, exact: false }).click();
        await selectSessionView(page, "Changes");
        await expect.poll(() => [...scripts].some((path) => /\/Changes-[^/]+\.js$/.test(path))).toBe(true);
        await page.getByRole("tab", { name: "Terminal", exact: true }).click();
        await expect(terminal).toBeVisible();
        await expectAxeClean(page);
      } finally {
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
    });

    if (!phone) test('a pending terminal never steals focus from an open dialog', async ({ page, request }) => {
      const id = await sessionWithChanges(page, 'payload-dialog-focus');
      const headers = await csrfHeaders(page);
      await page.goto('about:blank');
      let release!: () => void;
      const ready = new Promise<void>(resolve => { release = resolve; });
      await page.route(`**/api/sessions/${id}/terminal-reference`, async route => { await ready; await route.continue(); });
      try {
        await page.goto(`/sessions/${id}`);
        await page.getByRole('button', { name: 'Go to…', exact: true }).click();
        const input = page.getByRole('dialog', { name: 'Jump to…', exact: true }).getByRole('combobox');
        await expect(input).toBeFocused();
        release();
        await expect(page.locator('.xterm-helper-textarea')).toBeVisible();
        await expect(input).toBeFocused();
        await page.keyboard.press('Escape');
      } finally {
        release(); await page.unrouteAll({ behavior: 'wait' });
        await request.post(`/api/sessions/${id}/stop`, { headers });
      }
    });

    test("a Files deep link loads no terminal host or xterm and defers Markdown until needed", async ({ page, request }) => {
      const id = await sessionWithChanges(page, `payload-files-${phone ? "phone" : "desktop"}`);
      const headers = await csrfHeaders(page);
      // The setup helper visits /sessions. Cancel that document's pending lazy
      // requests before measuring a fresh session document under load.
      await page.goto('about:blank');
      const scripts = new Set<string>();
      page.on("request", (request) => { if (request.resourceType() === "script") scripts.add(new URL(request.url()).pathname); });
      try {
        await page.goto(`/sessions/${id}?tab=files&file=app.py`);
        const editor = page.getByRole("textbox", { name: "File content" });
        await expect(editor).toContainText("changed");
        expect([...scripts].filter((path) => /\/(?:Terminal|terminalHostLayer|terminalRuntime|xterm|FileMarkdownPreview)-/.test(path))).toEqual([]);
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
