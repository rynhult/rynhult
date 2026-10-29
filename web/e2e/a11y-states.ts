import { expect, test, type Page } from "@playwright/test";
import { AUTH_FILE, AXE_PAGES, expectAxeClean } from "./helpers";

// WCAG 2.2 AA beyond the default view (roadmap status 2026-10-25, item 6): 27-ux-a11y scans every page as it first
// renders. These scans cover the states that view hides: every request failing, everything still loading, every
// disclosure and details element open, and the add/new dialogs. Both themes; 70-a11y-states-mobile repeats on a phone.

// Dialogs that only open a form: nothing is sent until their own submit button, which these scans never press.
const OPENERS: [path: string, button: string, dialog: string][] = [
  ["/projects", "Register folder", "Register a folder"],
  ["/projects", "New project", "New project"],
  ["/workspaces", "New workspace", "New workspace"],
  ["/catalog", "Restore built-ins", "Restore built-ins"],
  ["/catalog", "Add skill or agent", "Add a skill or agent"],
  ["/catalog", "Add MCP server", "Add an MCP server"],
  ["/schedules", "New schedule", "New schedule"],
  ["/host", "Add machine", "Add a machine"],
  ["/settings#keys", "Add key", "Add a key"],
  ["/settings#keys", "Paste .env", "Paste a .env"],
];

async function settle(page: Page) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

/** Every GET under /api fails, except what keeps the person signed in. */
async function failRequests(page: Page) {
  await page.route("**/api/**", (route) => {
    const { pathname } = new URL(route.request().url());
    if (route.request().method() !== "GET" || pathname.startsWith("/api/auth/") || pathname === "/api/health") {
      return route.fallback();
    }
    return route.fulfill({ status: 500, json: { detail: "Example failure for the accessibility check" } });
  });
}

/** Every GET under /api stays pending until the page is closed, except what the app shell needs to render. */
async function holdRequests(page: Page) {
  await page.route("**/api/**", (route) => {
    const { pathname } = new URL(route.request().url());
    if (route.request().method() !== "GET" || pathname.startsWith("/api/auth/") || pathname === "/api/health") {
      return route.fallback();
    }
    return new Promise<void>(() => undefined);
  });
}

/** Open every details element and press every collapsed disclosure that is not a dialog or menu opener. */
async function expandEverything(page: Page) {
  await page.evaluate(() => document.querySelectorAll("details").forEach((details) => { details.open = true; }));
  const collapsed = page.locator('button[aria-expanded="false"]:not([aria-haspopup]):visible');
  for (let pass = 0; pass < 3; pass++) {
    const count = await collapsed.count();
    if (!count) break;
    for (let index = count - 1; index >= 0; index--) {
      await collapsed.nth(index).click({ timeout: 2000 }).catch(() => undefined);
    }
    await page.evaluate(() => document.querySelectorAll("details").forEach((details) => { details.open = true; }));
  }
}

export function a11yStateTests(device: "desktop" | "phone") {
  test.use({ storageState: AUTH_FILE });

  for (const scheme of ["dark", "light"] as const) {
    test(`axe on a ${device}: every page with its requests failing, ${scheme}`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: scheme });
      await failRequests(page);
      // Search sends nothing without a query.
      for (const url of AXE_PAGES.map((path) => path === "/search" ? "/search?q=example" : path)) {
        await page.goto(url);
        await settle(page);
        await expect(page.getByRole("alert").first(), `${url} shows its failure`).toBeVisible();
        await expandEverything(page);
        await expectAxeClean(page);
      }
    });

    test(`axe on a ${device}: every page while it is still loading, ${scheme}`, async ({ page }) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ colorScheme: scheme });
      await holdRequests(page);
      for (const url of AXE_PAGES) {
        await page.goto(url, { waitUntil: "domcontentloaded" });
        await expect(page.locator("main")).toBeVisible();
        await expectAxeClean(page);
      }
    });

    test(`axe on a ${device}: every page with disclosures and details expanded, ${scheme}`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.emulateMedia({ colorScheme: scheme });
      for (const url of AXE_PAGES) {
        await page.goto(url);
        await settle(page);
        await expandEverything(page);
        await expectAxeClean(page);
      }
    });

    test(`axe on a ${device}: add and new dialogs, ${scheme}`, async ({ page }) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ colorScheme: scheme });
      for (const [path, button, dialog] of OPENERS) {
        await page.goto(path);
        await settle(page);
        await page.getByRole("button", { name: button, exact: true }).first().click();
        const opened = page.getByRole("dialog", { name: dialog });
        await expect(opened, `${button} on ${path}`).toBeVisible();
        await expandEverything(page);
        await expectAxeClean(page);
        // Fragment-only navigation keeps Settings mounted; dismiss before opening the next form.
        await opened.getByRole("button", { name: /^(Close|Cancel)$/ }).first().click();
        await expect(opened).toBeHidden();
      }
      if (device === "phone") {
        await page.goto("/");
        await page.getByRole("button", { name: "Menu" }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await expectAxeClean(page);
      }
    });
  }
}
