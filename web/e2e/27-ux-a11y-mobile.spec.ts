import { expect } from "./fixtures";
import { test as base } from './shell-state';
import { randomUUID } from "node:crypto";
import { AUTH_FILE, AXE_PAGES, csrfHeaders, expectAxeCleanPage, SESSION_TABS, sessionWithChanges } from "./helpers";

// Each scan has its own ordinary assertion/case deadline. A worker owns one
// real session so the page/grid scans still exercise a populated installation.
const test = base.extend<{}, { ownedSession: string }>({
  ownedSession: [async ({ browser }, use) => {
    const owner = await browser.newPage({ storageState: AUTH_FILE });
    let id: string | undefined;
    try {
      id = await sessionWithChanges(owner, `a11y-phone-${randomUUID().slice(0, 8)}`);
      await use(id);
    } finally {
      if (id) expect((await owner.request.post(`/api/sessions/${id}/stop`, {
        headers: await csrfHeaders(owner),
      })).ok()).toBe(true);
      await owner.close();
    }
  }, { scope: 'worker', timeout: 90_000 }],
});
test.use({ storageState: AUTH_FILE, shellLayout: 'standard' });

// UX plan WP5: all original routes, tabs, modal surfaces and empty Overview,
// in both themes. One long sweep previously spent its 180s budget on earlier
// navigations/scans before it could assert the final empty Overview.
for (const scheme of ["dark", "light"] as const) {
  for (const url of AXE_PAGES) test(`mobile axe: page ${url}, ${scheme}`, async ({ page, ownedSession }) => {
    expect(ownedSession).toBeTruthy();
    await page.emulateMedia({ colorScheme: scheme });
    await expectAxeCleanPage(page, url);
  });
  for (const tab of SESSION_TABS) test(`mobile axe: session ${tab || 'terminal'}, ${scheme}`, async ({ page, ownedSession }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await expectAxeCleanPage(page, `/sessions/${ownedSession}${tab}`, () => expect(page.getByRole("tablist")).toBeVisible());
  });
  for (const surface of ['palette', 'new session', 'drawer', 'empty Overview']) {
    test(`mobile axe: ${surface}, ${scheme}`, async ({ page, ownedSession }) => {
      expect(ownedSession).toBeTruthy();
      await page.emulateMedia({ colorScheme: scheme });
      if (surface === 'empty Overview') {
        for (const path of ["**/api/sessions", "**/api/projects", "**/api/attention"]) {
          await page.route(path, route => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
        }
      }
      await expectAxeCleanPage(page, "/", async () => {
        if (surface === 'palette') {
          await page.getByRole("button", { name: "Jump to…" }).click();
          await page.keyboard.type("se");
          await expect(page.getByRole("option").first()).toBeVisible();
        } else if (surface === 'new session') {
          await page.getByRole("button", { name: "New session" }).click();
          await expect(page.getByRole("dialog")).toBeVisible();
        } else if (surface === 'drawer') {
          await page.getByRole("button", { name: "Menu" }).click();
          await expect(page.getByRole("dialog", { name: "Menu" })).toBeVisible();
        } else await expect(page.getByText("No projects yet.")).toBeVisible();
      });
    });
  }
}

test("mobile: the drawer takes focus, keeps it, closes on Esc and gives it back to Menu", async ({ page }) => {
  await page.goto("/");
  const menu = page.getByRole("button", { name: "Menu" });
  await menu.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "Menu" });
  await expect(drawer).toBeVisible();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  expect(await drawer.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  for (let i = 0; i < 40; i++) {  // more Tab presses than the drawer has stops: focus never leaves it
    await page.keyboard.press("Tab");
    expect(await drawer.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Shift+Tab");
  expect(await drawer.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(menu).toBeFocused();
  // A link inside closes it and navigates
  await menu.click();
  await drawer.getByRole("link", { name: "Catalog" }).click();
  await expect(page).toHaveURL(/\/catalog$/);
  await expect(drawer).toBeHidden();
});
