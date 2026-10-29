import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, AXE_PAGES, csrfHeaders, expectAxeCleanPage, SESSION_TABS, sessionWithChanges } from "./helpers";

test.use({ storageState: AUTH_FILE });

let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
    sessionId = undefined;
  }
});

// UX plan WP5 on a phone: the same axe sweep as 27-ux-a11y, and the navigation drawer as a real modal (NAV-3).
for (const scheme of ["dark", "light"] as const) {
  test(`mobile axe: every page and session tab, the palette, the drawer and the empty Overview, ${scheme}`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: scheme });
    const id = sessionId = await sessionWithChanges(page, `a11y-phone-${randomUUID().slice(0, 8)}`);
    for (const url of AXE_PAGES) await expectAxeCleanPage(page, url);
    for (const tab of SESSION_TABS) {
      await expectAxeCleanPage(page, `/sessions/${id}${tab}`, () => expect(page.getByRole("tablist")).toBeVisible());
    }
    await expectAxeCleanPage(page, "/", async () => {
      await page.getByRole("button", { name: "Jump to…" }).click();
      await page.keyboard.type("se");
      await expect(page.getByRole("option").first()).toBeVisible();
    });
    await expectAxeCleanPage(page, "/", async () => {
      await page.getByRole("button", { name: "New session" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
    });
    await expectAxeCleanPage(page, "/", async () => {
      await page.getByRole("button", { name: "Menu" }).click();
      await expect(page.getByRole("dialog", { name: "Menu" })).toBeVisible();
    });
    for (const path of ["**/api/sessions", "**/api/projects", "**/api/attention"]) {
      await page.route(path, (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
    }
    await expectAxeCleanPage(page, "/", () => expect(page.getByText("No projects yet.")).toBeVisible());
  });
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
