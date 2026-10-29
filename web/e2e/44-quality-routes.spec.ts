import { expect, test } from "@playwright/test";
import { AUTH_FILE, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("a slow route download shows a loading state inside the layout", async ({ page }, testInfo) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/assets/Audit-*.js", async (route) => { await gate; await route.continue(); });
  try {
    await page.goto("/audit");
    await expect(page.getByRole("main", { name: "Content", exact: true }).getByText("Loading page…", { exact: true })).toBeVisible();
    await page.screenshot({ path: wikiShotPath(testInfo, "troubleshooting-route-loading.png") });
    release();
    await expect(page.getByRole("heading", { name: "Audit", exact: true })).toBeVisible();
  } finally { release(); }
});

test("a failed route download offers reload and recovers", async ({ page }, testInfo) => {
  let fail = true;
  let attempts = 0;
  let navigations = 0;
  page.on("request", (request) => { if (request.isNavigationRequest()) navigations++; });
  await page.route("**/assets/Audit-*.js", (route) => {
    attempts++;
    return fail ? route.abort("failed") : route.continue();
  });
  await page.goto("/audit");
  const banner = page.getByRole("region", { name: "Update required", exact: true });
  await expect(banner).toContainText("Rynhult was updated, reload to continue");
  const reload = banner.getByRole("button", { name: "Reload", exact: true });
  await expect(reload).toBeVisible();
  expect(attempts).toBe(2);
  expect(navigations).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("route-download-failed.png") });
  await reload.click();
  const confirmation = page.getByRole("dialog", { name: "Reload Rynhult?", exact: true });
  await expect(confirmation).toContainText("unsaved");
  await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(confirmation).toBeHidden();
  await expect(reload).toBeVisible();
  expect(navigations).toBe(1);
  fail = false;
  await reload.click();
  await confirmation.getByRole("button", { name: "Reload", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Audit", exact: true })).toBeVisible();
  await expect(banner).toHaveCount(0);
  expect(navigations).toBe(2);
});
