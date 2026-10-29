import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

/* UX review 2, beta item 2: no raw server errors on default paths. */
test.use({ storageState: AUTH_FILE });

test("an unknown session says so and leads back", async ({ page }) => {
  await page.goto("/sessions/0123456789ab");
  await expect(page.getByText("There's no session here")).toBeVisible();
  await expect(page.getByText("no such session")).toHaveCount(0);
  await expectAxeClean(page);
  await page.getByRole("link", { name: "Back to sessions" }).click();
  await expect(page).toHaveURL(/\/sessions$/);
});

test("an unknown project says so and leads back", async ({ page }) => {
  await page.goto("/projects/987654");
  await expect(page.getByText("There's no project here")).toBeVisible();
  await page.getByRole("link", { name: "Back to projects" }).click();
  await expect(page).toHaveURL(/\/projects$/);
});

test("Publishing without Cloudflare explains what it needs, without an error", async ({ page }) => {
  await page.route("**/api/publish/zones", route => route.fulfill({ json: { zones: [], configured: false } }));
  const zones = page.waitForResponse("**/api/publish/zones");
  await page.goto("/publishing");
  expect((await zones).status()).toBe(200);
  await expect(page.getByText("Publishing needs a Cloudflare zone")).toBeVisible();
  await expect(page.getByRole("link", { name: "How to set one up" })).toHaveAttribute("href", "/guide/User-Guide/Publishing#before-you-start");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("Errno")).toHaveCount(0);
  await expectAxeClean(page);
});

test("a failed page load reads as words, with Retry and the raw text under Details", async ({ page }) => {
  await page.route("**/api/sessions/0123456789ab", (route) => route.fulfill({ status: 502, json: { detail: "GitHub: ConnectError" } }));
  await page.goto("/sessions/0123456789ab");
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Couldn't load this session.", { timeout: 15_000 });
  await expect(alert).toContainText("A service Rynhult relies on didn't answer as expected.");
  await expect(alert.getByText("GitHub: ConnectError")).toBeHidden();  // folded under Details
  await alert.getByText("Details").click();
  await expect(alert.getByText("GitHub: ConnectError")).toBeVisible();
  await expect(alert.getByRole("button", { name: "Retry" })).toBeVisible();
});
