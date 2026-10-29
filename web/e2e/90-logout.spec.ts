import { expect, test } from "@playwright/test";
import { PASSWORD, freshStep, loadSecret, totp } from "./helpers";

test("logout, then log in with password and a fresh code", async ({ page }) => {
  test.setTimeout(90_000);
  await freshStep(page);
  await page.goto("/login");
  await page.getByLabel("Username").fill("alex");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByLabel("Authenticator code").fill("000000");
  await page.getByRole("button", { name: "Sign in", exact: true }).dblclick();
  await expect(page.getByText("That sign-in didn't work. Check the username, password and code.")).toBeVisible();
  await page.getByLabel("Authenticator code").fill(totp(loadSecret()));
  await page.getByRole("button", { name: "Sign in", exact: true }).dblclick();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await page.getByTitle("Sign out").click();
  await expect(page).toHaveURL(/\/login$/);
  // Let the logout navigation finish before testing a fresh protected-page visit. Authentication
  // can redirect before the document's load event: observe the committed request, then assert the URL.
  await page.getByRole("button", { name: "Sign in", exact: true }).waitFor();
  await page.waitForLoadState("networkidle");
  await page.goto("/", { waitUntil: "commit" });
  await expect(page).toHaveURL(/\/login$/);
});
