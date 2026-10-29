import { environmentValue } from "../../src/acc/env.mjs";
import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("listed variables save, mark existing names, and reset when reopening", async ({ page }, testInfo) => {
  await page.goto("/settings#keys");
  await page.getByRole("button", { name: "Add key", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Add a key" });
  const variable = dialog.getByRole("combobox", { name: "Variable", exact: true });
  await dialog.getByLabel("Name", { exact: true }).fill("Demo Mistral");
  await variable.selectOption("MISTRAL_API_KEY");
  await expect(variable).toHaveAccessibleDescription("Mistral API access");
  await expect(dialog.getByRole("textbox", { name: "Custom variable" })).toHaveCount(0);
  await dialog.getByLabel("Value", { exact: true }).fill("example-placeholder-mistral");
  await expectAxeClean(page, '[role="dialog"]');
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === "1") {
    await page.screenshot({ path: wikiShotPath(testInfo, "settings-keys-add.png"), fullPage: true });
  }
  await dialog.getByRole("button", { name: "Add key", exact: true }).click();
  const keys = page.getByRole("list", { name: "Keys", exact: true });
  await expect(keys.getByText("Demo Mistral", { exact: true })).toBeVisible();
  await expect(keys.getByText("MISTRAL_API_KEY", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add key", exact: true }).click();
  await expect(variable).toHaveValue("");
  await expect(variable.locator('option[value="MISTRAL_API_KEY"]')).toContainText("you have one");
  await variable.selectOption("MISTRAL_API_KEY");
  await expect(variable).toHaveValue("MISTRAL_API_KEY"); // duplicates are allowed, not disabled
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await keys.getByRole("button", { name: "Edit Demo Mistral", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "Edit Demo Mistral" });
  await expect(edit.getByRole("combobox", { name: "Variable", exact: true })).toHaveValue("MISTRAL_API_KEY");
  await edit.getByRole("button", { name: "Cancel", exact: true }).click();
});

test("Custom preserves normalization and validation, switches names, and fits a phone", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings#keys");
  await page.getByRole("button", { name: "Add key", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Add a key" });
  const variable = dialog.getByRole("combobox", { name: "Variable", exact: true });
  await variable.focus();
  await variable.press("End");
  await variable.press("Enter");
  await expect(variable).toHaveValue("custom");
  const custom = dialog.getByRole("textbox", { name: "Custom variable", exact: true });
  await dialog.getByLabel("Name", { exact: true }).fill("Demo application");
  await dialog.getByLabel("Value", { exact: true }).fill("example-placeholder-app");
  const add = dialog.getByRole("button", { name: "Add key", exact: true });
  await expect(add).toBeDisabled();
  await custom.fill("1bad");
  await expect(custom).toHaveValue("1BAD");
  await expect(custom).toHaveAttribute("aria-invalid", "true");
  await expect(custom).toHaveAccessibleDescription("Capital letters, digits and _, not starting with a digit");
  await expect(add).toBeDisabled();
  await custom.fill("demo-app_token");
  await expect(custom).toHaveValue("DEMO_APP_TOKEN");
  await expect(add).toBeEnabled();
  await variable.selectOption("GOOGLE_APPLICATION_CREDENTIALS");
  await expect(custom).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await variable.selectOption("custom");
  await expect(custom).toHaveValue("DEMO_APP_TOKEN");
  await expectAxeClean(page, '[role="dialog"]');
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === "1") {
    await page.screenshot({ path: wikiShotPath(testInfo, "settings-keys-custom-mobile.png"), fullPage: true });
  }
  await add.click();
  const keys = page.getByRole("list", { name: "Keys", exact: true });
  await expect(keys.getByText("DEMO_APP_TOKEN", { exact: true })).toBeVisible();
  await keys.getByRole("button", { name: "Edit Demo application", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "Edit Demo application" });
  await expect(edit.getByRole("combobox", { name: "Variable", exact: true })).toHaveValue("custom");
  await expect(edit.getByRole("textbox", { name: "Custom variable", exact: true })).toHaveValue("DEMO_APP_TOKEN");
});
