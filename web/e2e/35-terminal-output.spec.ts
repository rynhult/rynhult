import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { AUTH_FILE, control, csrfHeaders, expectAxeClean, info, resetTerminalThrottle, sessionWithChanges, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });
test.beforeEach(async ({ page }) => {
  await resetTerminalThrottle(page);
});

let sessionId: string | undefined;
async function outputSession(page: import("@playwright/test").Page, slug: string) {
  sessionId = await sessionWithChanges(page, `${slug}-${randomUUID().slice(0, 8)}`);
  return sessionId;
}
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

test("terminal output is selectable, refreshable and copyable without capturing navigation keys", async ({ page, context }, testInfo) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const id = await outputSession(page, "output-demo");
  await page.goto(`/sessions/${id}`);
  const input = page.locator(".xterm-helper-textarea");
  await expect(input).toBeFocused();
  const messages: string[] = [];
  page.on("websocket", socket => socket.on("framesent", event => messages.push(String(event.payload))));
  // Observe a fresh connection, then prove Tab reaches the CLI while navigation leaves it.
  await page.reload();
  // A reload can reconnect before the previous writer lease is released.
  // Use the existing consent action rather than assuming the lease is gone.
  const writable = page.getByText("Read-write", { exact: true });
  const takeControl = page.getByRole("button", { name: "Take control", exact: true });
  await expect(writable.or(takeControl)).toBeVisible();
  if (await takeControl.isVisible()) await takeControl.click();
  await expect(writable).toBeVisible();
  await input.focus();
  await page.keyboard.press("Tab");
  await expect(input).toBeFocused();
  await expect.poll(() => messages.some(m => m.includes('"d":"\\t"'))).toBe(true);
  await page.keyboard.press("F6");
  const reader = page.getByRole("checkbox", { name: "Screen reader output" });
  await expect(reader).toBeFocused();
  await page.keyboard.press("Shift+Tab"); // back into the terminal, entirely by keyboard
  await expect(input).toBeFocused();
  await page.keyboard.press("Shift+F6");
  const skip = page.getByRole("link", { name: "Skip terminal" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(reader).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Open as text" })).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Recent terminal output" });
  const text = dialog.getByRole("textbox", { name: "Recent terminal output" });
  await expect(text).not.toBeEmpty();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open as text" })).toBeFocused();

  // Real PTY output, including colour and a long wrapped line, goes through xterm's parser.
  const long = "Accessible output ".repeat(30);
  writeFileSync(`${info().run_dir}/${id}/fake.screen`, `\x1b[32mText alternative marker\x1b[0m\r\n${long}\r\n`);
  control(id, "screen");
  await expect(page.locator(".xterm-accessibility-tree")).toContainText("Text alternative marker");
  await page.getByRole("button", { name: "Open as text" }).click();
  await expect(text).toHaveValue(/Text alternative marker/);
  await expect(text).toHaveValue(new RegExp(long.trim()));
  expect(await text.inputValue()).not.toContain("\x1b");
  const snapshot = await text.inputValue();
  writeFileSync(`${info().run_dir}/${id}/fake.screen`, "\r\nLater output marker\r\n");
  control(id, "screen");
  await expect(text).toHaveValue(snapshot); // frozen while reading
  await expect(page.locator(".xterm-accessibility-tree")).toContainText("Later output marker");
  await dialog.getByRole("button", { name: "Refresh output" }).click();
  await expect(text).toHaveValue(/Later output marker/);
  await dialog.getByRole("button", { name: "Copy recent output" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(await text.inputValue());
  await expectAxeClean(page);
  await page.screenshot({ path: wikiShotPath(testInfo, "terminal-output-text.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Copy recent output" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Later output marker");
  await expectAxeClean(page); // includes xterm, no grid exclusions
  await page.screenshot({ path: wikiShotPath(testInfo, "session-page.png"), fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await expectAxeClean(page);
  await page.getByRole("button", { name: "Open as text" }).click();
  await expect(dialog.getByRole("button", { name: "Refresh output" })).toBeEnabled();
  await expectAxeClean(page);
});

test("screen reader output toggles without reconnecting and persists for this browser", async ({ page }) => {
  const id = await outputSession(page, "reader-demo");
  await page.goto(`/sessions/${id}`);
  const reader = page.getByRole("checkbox", { name: "Screen reader output" });
  await expect(reader).toBeChecked();
  await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(1);
  const connections: string[] = [];
  page.on("websocket", ws => { if (ws.url().includes("/tty?")) connections.push(ws.url()); });
  await reader.uncheck();
  await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(0);
  expect(connections).toHaveLength(0);
  await page.reload();
  await expect(reader).not.toBeChecked();
  await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(0);
  await reader.check();
  await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(1);
  await expect(page.locator(".xterm .live-region")).toHaveAttribute("aria-live", "off");
  await page.reload();
  await expect(reader).toBeChecked();
});

test("F6 also leaves a focused screen reader row", async ({ page }) => {
  const id = await outputSession(page, "reader-row-demo");
  await page.goto(`/sessions/${id}`);
  const row = page.locator(".xterm-accessibility-tree [role=listitem]").nth(1);
  await row.focus();
  await expect(row).toBeFocused();
  await page.keyboard.press("F6");
  await expect(page.getByRole("checkbox", { name: "Screen reader output" })).toBeFocused();
});

test("clipboard denial offers selectable text instead of losing the output", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator.clipboard, "writeText", { value: async () => { throw new DOMException("Denied", "NotAllowedError"); } });
  });
  const id = await outputSession(page, "copy-fallback-demo");
  await page.goto(`/sessions/${id}`);
  await page.getByRole("button", { name: "Copy recent output" }).click();
  await expect(page.getByRole("tabpanel", { name: "Terminal" }).getByText("Copy failed. Open as text to select and copy the output.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open as text" }).click();
  const text = page.getByRole("textbox", { name: "Recent terminal output" });
  await expect(text).not.toHaveValue("");
  await text.focus();
  await page.keyboard.press("Control+a");
  expect(await text.evaluate((el: HTMLTextAreaElement) => el.selectionEnd - el.selectionStart)).toBeGreaterThan(0);
});

test("text view includes output above the visible grid", async ({ page }) => {
  const id = await outputSession(page, "scrollback-demo");
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
  writeFileSync(`${info().run_dir}/${id}/fake.screen`, "Scrollback first marker\r\n" + Array.from({ length: 100 }, (_, i) => `Output row ${i}\r\n`).join("") + "Scrollback last marker\r\n");
  control(id, "screen");
  await expect(page.locator(".xterm-accessibility-tree")).toContainText("Scrollback last marker");
  const text = page.getByRole("textbox", { name: "Recent terminal output" });
  await page.getByRole("button", { name: "Open as text" }).click();
  await expect(text).toHaveValue(/Scrollback last marker/);
  await expect(text).toHaveValue(/Scrollback first marker/);
});

test("unavailable scrollback keeps browser text with an explicit limitation", async ({ page }) => {
  const id = await outputSession(page, "output-unavailable-demo");
  await page.route("**/terminal-output", route => route.fulfill({ status: 503, json: { detail: "Broker unavailable" } }));
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
  // Read-write is sent before the first PTY bytes. A frozen fallback can only
  // contain output the browser has actually received and parsed.
  writeFileSync(`${info().run_dir}/${id}/fake.screen`, "\r\nBrowser fallback marker\r\n");
  control(id, "screen");
  await expect(page.locator(".xterm-accessibility-tree")).toContainText("Browser fallback marker");
  await page.getByRole("button", { name: "Open as text" }).click();
  await expect(page.getByRole("tabpanel", { name: "Terminal" }).getByText("Scrollback unavailable. Showing only output held in this browser; older lines may be missing.", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Recent terminal output" })).toHaveValue(/Browser fallback marker/);
});

test("too many snapshots says so and keeps browser text", async ({ page }) => {
  const id = await outputSession(page, "output-limited-demo");
  await page.route("**/terminal-output", route => route.fulfill({ status: 429, json: { detail: "Too many terminal snapshots" } }));
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText("Read-write", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open as text" }).click();
  await expect(page.getByRole("tabpanel", { name: "Terminal" }).getByText("Too many snapshots in the last minute. Showing only output held in this browser; older lines may be missing.", { exact: true })).toBeVisible();
});

test("a scrollback failure does not hide a clipboard failure", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator.clipboard, "writeText", { value: async () => { throw new DOMException("Denied", "NotAllowedError"); } });
  });
  const id = await outputSession(page, "copy-both-fail-demo");
  await page.route("**/terminal-output", route => route.fulfill({ status: 503, json: { detail: "Broker unavailable" } }));
  await page.goto(`/sessions/${id}`);
  await page.getByRole("button", { name: "Copy recent output" }).click();
  await expect(page.getByRole("tabpanel", { name: "Terminal" }).getByText("Copy failed. Open as text to select and copy the output.", { exact: true })).toBeVisible();
});
