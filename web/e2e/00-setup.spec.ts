import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { AUTH_FILE, PASSWORD, expectScannableQr, info, saveSecret, shot, totp } from "./helpers";

test("first run: setup token, password and authenticator enrolment", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByText("sudo cat /srv/acc/data/secrets/setup_token")).toBeVisible();
  await page.getByLabel("Setup token").fill("wrong-token");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("That setup token is wrong. Read it again on the server and paste it exactly.")).toBeVisible();

  let begins = 0;
  await page.route('**/api/auth/setup/begin', async route => {
    begins++;
    await new Promise(resolve => setTimeout(resolve, 150));
    await route.continue();
  });
  await page.getByLabel("Setup token").fill(readFileSync(info().setup_token_path, "utf8").trim());
  await page.getByRole("button", { name: "Continue" }).dblclick();
  const secret = (await page.getByTitle("Manual entry key").textContent())!.trim();
  expect(begins).toBe(1);
  await page.unroute('**/api/auth/setup/begin');
  expect(secret).toMatch(/^[A-Z2-7]{32}$/);
  // Reloading after scanning the QR must not invalidate that authenticator.
  await page.reload();
  await page.getByLabel("Setup token").fill(readFileSync(info().setup_token_path, "utf8").trim());
  await page.getByRole("button", { name: "Continue" }).dblclick();
  await expect(page.getByTitle("Manual entry key")).toHaveText(secret);
  saveSecret(secret);
  await expect(page.locator("svg").first()).toBeVisible();  // QR code
  for (const colorScheme of ["light", "dark"] as const) {  // the authenticator QR scans in either theme
    await page.emulateMedia({ colorScheme });
    await expectScannableQr(page.locator("svg.segno"), `setup QR, ${colorScheme}`);
  }
  await page.emulateMedia({ colorScheme: null });
  await shot(page, testInfo.project.name, "setup-enrol");

  await page.getByLabel("Username").fill("alex");  // no default any more: installations name their own user
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByLabel("Code from your authenticator").fill(totp(secret));
  await page.getByRole("button", { name: "Finish setup" }).dblclick();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  await page.context().storageState({ path: AUTH_FILE });
});
