import { expect, test } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

// Break-it pass 9: careless use of tonight's UX waves (focused Settings, automatic session names, Overview links).
test.use({ storageState: AUTH_FILE });

test("an emoji topic names the session with whole characters, and the launch preview answers", async ({ page }) => {
  const previews: number[] = [];
  page.on("response", response => { if (response.url().endsWith("/api/sessions/preview")) previews.push(response.status()); });
  await page.goto("/");
  await page.getByRole("button", { name: "New session", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "New session" });
  await dialog.getByRole("radio", { name: "Local workspace" }).click();
  // 41 characters: cutting the name at 40 UTF-16 units used to leave half of the last emoji.
  await dialog.getByRole("textbox", { name: "Topic", exact: true }).fill("a" + "😀".repeat(40));
  await expect(dialog.getByTestId("automatic-name")).toHaveText("a" + "😀".repeat(39));
  await expect(dialog.getByText("Ready to launch.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeEnabled();
  expect(previews.length).toBeGreaterThan(0);
  expect(previews.every(status => status === 200)).toBe(true);
  await dialog.getByRole("button", { name: "Discard draft" }).click();
  await page.getByRole("dialog", { name: "Discard this draft?" }).getByRole("button", { name: "Discard draft" }).click();
});

test("Back while a Settings dialog is open keeps that dialog visible, with its draft and focus", async ({ page }) => {
  await page.goto("/settings#passkeys");
  await page.locator("#settings-section").selectOption("dns");
  await page.getByRole("button", { name: "Add infrastructure credential" }).click();
  const dialog = page.getByRole("dialog", { name: "Add infrastructure credential" });
  await dialog.getByLabel("Credential name").fill("break-it-9 draft");
  await page.goBack();
  await expect(page).toHaveURL(/#passkeys$/);
  // Its section stays shown until the dialog closes, so the modal's focus trap never holds an invisible page.
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Credential name")).toHaveValue("break-it-9 draft");
  await dialog.getByLabel("Credential name").focus();
  await page.keyboard.press("Tab");
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#dns")).toBeHidden();
  await expect(page.locator("#passkeys")).toBeVisible();
});

test("Overview's budget link opens Settings at Budgets", async ({ page }) => {
  await page.route("**/api/observations", route => route.fulfill({ json: [{ key: "claude.rate_limits", observed_at: Date.now() / 1000,
    value: { five_hour: { used_percentage: 38, resets_at: Date.now() / 1000 + 3600 } } }] }));
  await page.route("**/api/budgets", async route => {
    if (route.request().method() !== "GET") return route.continue();
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, status: { ...real.status,
      "claude.five_hour": { allowed: 50, used: 38, over: false, action: "warn" } } } });
  });
  await page.goto("/");
  await page.getByRole("link", { name: /Budget details/ }).click();
  await expect(page).toHaveURL(/\/settings#budgets$/);
  await expect(page.locator("#budgets")).toBeVisible();
  await expect(page.locator("#passkeys")).toBeHidden();
});
