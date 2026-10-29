import { expect, test } from "@playwright/test";
import { build } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { navigationIndex } from "../src/lib/keyboard";

for (const [key, at, count, orientation, loop, expected] of [
  ["ArrowRight", 2, 3, "horizontal", true, 0],
  ["ArrowLeft", 0, 3, "horizontal", true, 2],
  ["ArrowDown", 2, 3, "vertical", false, 2],
  ["ArrowUp", 0, 3, "vertical", false, 0],
  ["Home", 2, 3, "horizontal", true, 0],
  ["End", 0, 3, "vertical", true, 2],
  ["ArrowDown", -1, 3, "both", true, 0],
  ["ArrowUp", -1, 3, "both", true, 2],
  ["ArrowDown", 0, 3, "horizontal", true, null],
  ["ArrowRight", 0, 3, "vertical", true, null],
  ["Enter", 0, 3, "both", true, null],
  ["ArrowDown", 0, 0, "vertical", true, null],
] as const) {
  test(`navigation ${key} at ${at}/${count}, ${orientation}, ${loop ? "wrap" : "clamp"}`, () => {
    expect(navigationIndex(key, at, count, { orientation, loop })).toBe(expected);
  });
}

test("editable comboboxes retain native Home and End editing", () => {
  expect(navigationIndex("Home", 2, 3, { orientation: "vertical", homeEnd: false })).toBeNull();
  expect(navigationIndex("End", 0, 3, { orientation: "vertical", homeEnd: false })).toBeNull();
});

test.describe("mounted keyboard hooks", () => {
  let code: string;
  test.beforeAll(async () => {
    const bundle = await build({ configFile: false, plugins: [react()], logLevel: "error",
      define: { "process.env.NODE_ENV": JSON.stringify("development") },
      build: { write: false, minify: false,
        lib: { entry: fileURLToPath(new URL("./keyboard-harness.tsx", import.meta.url)), name: "KeyboardHarness", formats: ["iife"] } } });
    const output = Array.isArray(bundle) ? bundle[0] : bundle;
    if (!("output" in output)) throw new Error("Keyboard harness did not produce a bundle");
    const chunk = output.output.find((item) => item.type === "chunk");
    if (!chunk || chunk.type !== "chunk") throw new Error("Keyboard harness has no JavaScript");
    code = chunk.code;
  });
  test.beforeEach(async ({ page }) => {
    await page.setContent('<html lang="en"><title>Keyboard hook tests</title><div id="root"></div></html>');
    await page.addScriptTag({ content: code });
  });

  test("roving navigation skips hidden, disabled and inert controls without consuming perpendicular arrows", async ({ page }) => {
    const alpha = page.getByRole("button", { name: "Alpha", exact: true });
    const charlie = page.getByRole("button", { name: "Charlie", exact: true });
    await alpha.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("button", { name: "Apricot" })).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("button", { name: "Bravo" })).toBeFocused();
    await expect(alpha).toHaveAttribute("tabindex", "-1");
    await page.keyboard.press("End");
    await expect(charlie).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(alpha).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(alpha).toBeFocused();
    await expect(page.getByLabel("Prevented")).toHaveText("false");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "After navigation" })).toBeFocused();
  });

  test("typeahead cycles repeated letters, extends prefixes and expires", async ({ page }) => {
    await page.clock.install();
    await page.getByRole("button", { name: "Alpha", exact: true }).focus();
    await page.keyboard.type("b");
    await expect(page.getByRole("button", { name: "Bravo" })).toBeFocused();
    await page.keyboard.type("b");
    await expect(page.getByRole("button", { name: "Basil" })).toBeFocused();
    await page.keyboard.type("a");
    await expect(page.getByRole("button", { name: "Basil" })).toBeFocused();
    await page.clock.fastForward(1001);
    await page.keyboard.type("ap");
    await expect(page.getByRole("button", { name: "Apricot" })).toBeFocused();
  });

  test("modified, composing and already handled keys do not move roving focus", async ({ page }) => {
    const alpha = page.getByRole("button", { name: "Alpha", exact: true });
    await alpha.focus();
    await page.keyboard.press("Control+ArrowRight");
    await expect(alpha).toBeFocused();
    await alpha.evaluate((element) => element.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, isComposing: true })));
    await expect(alpha).toBeFocused();
    await alpha.evaluate((element) => element.addEventListener("keydown", (event) => event.preventDefault(), { once: true }));
    await page.keyboard.press("ArrowRight");
    await expect(alpha).toBeFocused();
  });

  test("typeahead owns unmatched letters without moving focus or leaking to global shortcuts", async ({ page }) => {
    const alpha = page.getByRole("button", { name: "Alpha", exact: true });
    await alpha.focus(); await page.keyboard.type("z");
    await expect(alpha).toBeFocused();
    await expect(page.getByLabel("Prevented")).toHaveText("true");
  });

  test("a reopened navigation surface starts a fresh typeahead prefix", async ({ page }) => {
    const alpha = page.getByRole("button", { name: "Alpha", exact: true });
    await alpha.focus(); await page.keyboard.type("b");
    await expect(page.getByRole("button", { name: "Bravo" })).toBeFocused();
    await page.getByRole("button", { name: "Remount navigation" }).click();
    await alpha.focus(); await page.keyboard.type("a");
    await expect(page.getByRole("button", { name: "Apricot" })).toBeFocused();
  });

  test("Space activates an action-menu link without removing its native href", async ({ page }) => {
    await page.getByRole("button", { name: "Link actions", exact: true }).focus();
    await page.keyboard.press("Enter");
    const link = page.getByRole("menuitem", { name: "Go to notes", exact: true });
    await expect(link).toBeFocused(); await expect(link).toHaveAttribute("href", "#chosen");
    await page.keyboard.press("Space");
    await expect(page).toHaveURL(/#chosen$/);
    await expect(page.getByRole("menu", { name: "Link actions", exact: true })).toHaveCount(0);
  });

  test("a menu returns to its trigger even when invocation did not focus that button", async ({ page }) => {
    await page.getByRole("button", { name: "Alpha", exact: true }).focus();
    const trigger = page.getByRole("button", { name: "Link actions", exact: true });
    await trigger.evaluate((element: HTMLButtonElement) => element.click());
    await expect(page.getByRole("menuitem", { name: "Go to notes", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
  });

  test("modal scope skips excluded controls, wraps both ways and restores the autofocus opener", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "Open primary", exact: true });
    await trigger.focus(); await page.keyboard.press("Enter");
    const first = page.getByRole("textbox", { name: "First field", exact: true });
    await expect(first).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByRole("button", { name: "Close primary", exact: true })).toBeFocused();
    await page.keyboard.press("Tab"); await expect(first).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("textbox", { name: "Own Escape", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Primary", exact: true })).toBeVisible();
    await first.focus(); await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Primary", exact: true })).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("nested modal Escape closes only the top scope and restores its opener", async ({ page }) => {
    await page.getByRole("button", { name: "Open primary", exact: true }).click();
    const nested = page.getByRole("button", { name: "Open secondary", exact: true });
    await nested.focus(); await page.keyboard.press("Enter");
    await expect(page.getByRole("textbox", { name: "Second field", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Secondary", exact: true })).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: "Primary", exact: true })).toBeVisible();
    await expect(nested).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Open primary", exact: true })).toBeFocused();
  });

  test("focus on a modal's own boundary wraps to its last control on Shift+Tab", async ({ page }) => {
    await page.getByRole("button", { name: "Open primary", exact: true }).click();
    await page.getByRole("dialog", { name: "Primary", exact: true }).focus();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByRole("button", { name: "Close primary", exact: true })).toBeFocused();
  });

  test("popup dismissal does not dismiss its modal or steal outside-click focus", async ({ page }) => {
    await page.getByRole("button", { name: "Open primary", exact: true }).click();
    const trigger = page.getByRole("button", { name: "Open popup", exact: true });
    await trigger.click();
    await expect(page.getByRole("button", { name: "Popup item", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("group", { name: "Popup", exact: true })).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await expect(page.getByRole("dialog", { name: "Primary", exact: true })).toBeVisible();
    await trigger.click();
    await page.getByRole("textbox", { name: "First field", exact: true }).click();
    await expect(page.getByRole("group", { name: "Popup", exact: true })).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "First field", exact: true })).toBeFocused();
  });

  test("an empty modal retains focus and Escape still returns to its trigger", async ({ page }) => {
    const trigger = page.getByRole("button", { name: "Open empty", exact: true });
    await trigger.focus(); await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Empty", exact: true });
    await expect(dialog).toBeFocused();
    await page.keyboard.press("Tab"); await expect(dialog).toBeFocused();
    await page.keyboard.press("Shift+Tab"); await expect(dialog).toBeFocused();
    await page.keyboard.press("Escape"); await expect(trigger).toBeFocused();
  });

  test("Tab and Shift+Tab from a tail menu keep focus inside its modal", async ({ page }) => {
    await page.getByRole("button", { name: "Open menu modal", exact: true }).click();
    const first = page.getByRole("textbox", { name: "Menu first field", exact: true });
    await expect(first).toBeFocused();
    await page.keyboard.press("Tab"); await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("menuitem", { name: "Final action", exact: true })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("menu", { name: "Tail actions", exact: true })).toHaveCount(0);
    await expect(first).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    const trigger = page.getByRole("button", { name: "Tail actions", exact: true });
    await expect(trigger).toBeFocused(); await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("menuitem", { name: "Final action", exact: true })).toBeFocused();
    await page.keyboard.press("Shift+Tab"); await expect(trigger).toBeFocused();
    await expect(page.getByRole("menu", { name: "Tail actions", exact: true })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Open menu modal", exact: true })).toBeFocused();
  });

  test("rerenders preserve typed focus and use the latest dismissal callback", async ({ page }) => {
    await page.getByRole("button", { name: "Open primary", exact: true }).click();
    const field = page.getByRole("textbox", { name: "First field", exact: true });
    await field.fill("Keep typing");
    await page.getByRole("button", { name: "Rerender", exact: true }).evaluate((element) => element.click());
    await expect(field).toBeFocused(); await expect(field).toHaveValue("Keep typing");
    await page.keyboard.press("Escape");
    await expect(page.getByLabel("Closed version")).toHaveText("1");
  });
});
