import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

test.use({ storageState: AUTH_FILE });

// Code-quality review UI-1: a sign-out the server didn't confirm is never presented as one. (90-logout signs out for
// real and checks the sign-in is gone; here the request fails, so the shared test sign-in stays valid.)
for (const failure of ["aborted", "503"] as const) {
  test(`sign out: a ${failure} request says you're still signed in`, async ({ page }) => {
    let calls = 0;
    await page.route("**/api/auth/logout", (route) => {
      calls += 1;
      return failure === "aborted" ? route.abort("connectionreset")
        : route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "database is busy" }) });
    });
    await page.goto("/settings#sign-ins");
    await page.getByRole("button", { name: "Sign out" }).click();
    const dialog = page.getByRole("dialog", { name: "You're still signed in" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/didn't confirm the sign-out/)).toBeVisible();
    if (failure === "503") await expect(dialog.getByRole("alert")).toContainText("database is busy");
    await expect(page).toHaveURL(/\/settings#sign-ins$/);  // nothing cleared, no trip to the login page
    await expectAxeClean(page, '[role="dialog"]');
    await dialog.getByRole("button", { name: "Try again" }).click();
    await expect.poll(() => calls).toBe(2);
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Close" }).last().click();
    await expect(dialog).toBeHidden();
    await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  });
}
