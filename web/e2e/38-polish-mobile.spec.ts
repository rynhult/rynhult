import { expect, test } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, sessionWithChanges } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review top-ten #10: phone polish on secondary paths.
test("mobile: icon buttons are 44px on touch (PH-2, phone pass)", async ({ page }) => {
  await page.goto("/sessions");
  for (const name of ["Menu", "Jump to…"]) {
    const box = (await page.getByRole("button", { name }).boundingBox())!;
    expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(44);
  }
});

test("mobile: the Guide shows the article first, the page tree under Contents (GU-1)", async ({ page }) => {
  await page.goto("/guide");
  await expect(page.locator("article")).toBeVisible();
  await expect(page.locator("article")).toBeInViewport();
  await expect(page.getByRole("navigation", { name: "Wiki pages" })).toBeHidden();
  await page.getByRole("button", { name: "Contents" }).click();
  const sheet = page.getByRole("dialog", { name: "Contents" });
  await expect(sheet.getByRole("navigation", { name: "Wiki pages" })).toBeVisible();
  await expectAxeClean(page, "[role=dialog]");
  await sheet.getByRole("link", { name: "Keyboard shortcuts" }).click();
  await expect(sheet).toHaveCount(0);
  await expect(page).toHaveURL(/Keyboard-Shortcuts$/);
});

test("mobile: Grid shows one terminal with a switcher (GR-1)", async ({ page }) => {
  const a = await sessionWithChanges(page, "grid-phone-a");
  const b = await sessionWithChanges(page, "grid-phone-b");
  const first = await (await page.request.get(`/api/sessions/${a}`)).json();
  const second = await (await page.request.get(`/api/sessions/${b}`)).json();
  await page.goto(`/grid?ids=${a},${b}`);
  const switcher = page.getByRole("tablist", { name: "Sessions in the grid" });
  await expect(switcher.getByRole("tab")).toHaveCount(2);
  await expect(page.locator(".xterm")).toHaveCount(1);
  await expect(page.getByRole("toolbar", { name: "Terminal keys" })).toHaveCount(1);
  await expect(page.getByRole("region", { name: first.name, exact: true })).toBeVisible();
  await switcher.getByRole("tab", { name: second.name }).click();
  await expect(page.getByRole("region", { name: second.name, exact: true })).toBeVisible();
  await expect(page.locator(".xterm")).toHaveCount(1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

// UX review R10: the phone header keeps the project name on one line.
test("mobile: the session header never wraps the project name", async ({ page }) => {
  const id = await sessionWithChanges(page, "wrap-check-project");
  const session = await (await page.request.get(`/api/sessions/${id}`)).json();
  const projects = await (await page.request.get("/api/projects")).json();
  const project = projects.find((p: { id: number }) => p.id === session.project_id);
  await page.goto(`/sessions/${id}`);
  const link = page.locator("header").getByRole("link", { name: project.name, exact: true });
  await expect(link).toBeVisible();
  // One line of text (the link itself is a 44 px tap target on touch).
  const lines = await link.evaluate((a) => {
    const range = document.createRange();
    range.selectNodeContents(a);
    return new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top))).size;
  });
  expect(lines).toBe(1);
  expect((await link.boundingBox())!.height).toBeLessThanOrEqual(46);
});

// Owner, phone testing: a key-bar tap sends the key without opening the on-screen keyboard (it focused xterm's input).
test("mobile: tapping a terminal key never opens the keyboard, and an open one stays open", async ({ page }) => {
  const id = await sessionWithChanges(page, "keybar-focus");
  await page.goto(`/grid?ids=${id}`);
  const keys = page.getByRole("toolbar", { name: "Terminal keys" });
  const input = page.locator(".xterm-helper-textarea");
  await expect(input).toHaveCount(1);
  const focused = () => page.evaluate(() => document.activeElement?.classList.contains("xterm-helper-textarea") ?? false);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  for (const name of ["Up", "Down", "Esc", "Tab"]) {
    await keys.getByRole("button", { name, exact: true }).tap();
    expect(await focused(), name).toBe(false);
  }
  await keys.getByRole("button", { name: "More keys" }).tap();
  await keys.getByRole("button", { name: "Left" }).tap();
  expect(await focused()).toBe(false);
  // typing already (keyboard open): a key tap keeps the terminal's input focused
  await input.focus();
  await keys.getByRole("button", { name: "Up", exact: true }).tap();
  expect(await focused()).toBe(true);
});

// Owner, phone testing: the installed app is the phone connected; the banner pointing at it never shows there.
test("mobile: no Connect your phone banner inside the installed app", async ({ page }) => {
  await page.goto("/");
  await page.request.put("/api/preferences/phone-banner", { headers: await csrfHeaders(page), data: { dismissed: false } });
  await page.addInitScript(() => localStorage.removeItem("acc.getStarted.phone"));
  await page.reload();
  await expect(page.getByRole("complementary", { name: "Connect your phone" })).toBeVisible();  // in the browser
  // opened from the home screen (iOS reports navigator.standalone; others the display-mode media query)
  await page.addInitScript(() => Object.defineProperty(navigator, "standalone", { get: () => true }));
  await page.reload();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Connect your phone" })).toHaveCount(0);
});
