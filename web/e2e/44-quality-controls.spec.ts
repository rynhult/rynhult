import { expect, test } from "@playwright/test";
import { AUTH_FILE } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("segmented filters have one tab stop and move selection with arrows, Home and End", async ({ page }) => {
  await page.goto("/sessions");
  const group = page.getByRole("radiogroup", { name: "Show", exact: true });
  const live = group.getByRole("radio", { name: /^Live/ });
  const stopped = group.getByRole("radio", { name: /^Stopped/ });
  const all = group.getByRole("radio", { name: "All", exact: true });
  await expect(live).toHaveAttribute("tabindex", "0");
  await expect(stopped).toHaveAttribute("tabindex", "-1");
  await live.focus();
  await page.keyboard.press("ArrowRight");
  await expect(stopped).toBeFocused();
  await expect(stopped).toHaveAttribute("aria-checked", "true");
  await expect(live).toHaveAttribute("tabindex", "-1");
  await page.keyboard.press("End");
  await expect(all).toBeFocused();
  await expect(all).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("ArrowDown");
  await expect(live).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(all).toBeFocused();
  await page.keyboard.press("Home");
  await expect(live).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(all).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("searchbox", { name: "Filter by name, project or id" })).toBeFocused();
});

test("catalog source tabs label their panel and chips toggle with Space and Enter", async ({ page }) => {
  await page.goto("/catalog");
  await page.getByRole("tab", { name: /Skills/ }).click();
  await page.getByRole("button", { name: "Add skill or agent" }).click();
  const dialog = page.getByRole("dialog");
  const github = dialog.getByRole("tab", { name: "From GitHub" });
  await github.focus();
  await page.keyboard.press("End");
  const write = dialog.getByRole("tab", { name: "Write one" });
  await expect(write).toBeFocused();
  await expect(dialog.getByRole("tabpanel")).toHaveAccessibleName("Write one");
  const chip = dialog.getByRole("group", { name: "Providers", exact: true }).getByRole("button", { name: "claude", exact: true });
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await chip.focus();
  await page.keyboard.press("Space");
  await expect(chip).toHaveAttribute("aria-pressed", "false");
  await page.keyboard.press("Enter");
  await expect(chip).toHaveAttribute("aria-pressed", "true");
});
