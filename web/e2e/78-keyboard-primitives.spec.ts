import { expect, test, type Locator, type Page } from "@playwright/test";
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, sessionWithChanges } from "./helpers";

test.use({ storageState: AUTH_FILE });

async function tabTo(page: Page, target: Locator) {
  await expect(target).toBeVisible();
  await expect(target).toBeEnabled();
  // Tab stops include elements this selector misses (summary, scroll regions), so allow two full cycles.
  const limit = 2 * await page.locator("a[href], button, input, select, textarea, [tabindex]").count() + 10;
  const focused = target.and(page.locator(":focus"));
  for (let i = 0; i < limit && !await focused.count(); i++) {
    await page.keyboard.press("Tab");
  }
  await expect(target).toBeFocused();
}

for (const phone of [false, true]) {
  test.describe(phone ? "phone keyboard" : "desktop keyboard", () => {
    test.use({ viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 }, isMobile: phone, hasTouch: phone });

    test("action menus support arrow entry, typeahead, Escape and a named confirmation", async ({ page }, testInfo) => {
      const { slug: name } = await gitProject(page, `keyboard-menu-${phone ? "phone" : "desktop"}`);
      await page.goto("/projects");
      const filter = page.getByRole("searchbox", { name: "Filter projects", exact: true });
      await tabTo(page, filter);
      await page.keyboard.type(name);
      const trigger = page.getByRole("button", { name: `Project actions for ${name}`, exact: true });
      await expect(page.getByRole("button", { name: /^Project actions for / })).toHaveCount(1);
      await tabTo(page, trigger);
      await expect(trigger).toHaveAttribute("aria-haspopup", "menu");
      await page.keyboard.press("ArrowDown");
      const menu = page.getByRole("menu", { name: `Project actions for ${name}`, exact: true });
      const archive = menu.getByRole("menuitem", { name: `Archive ${name}`, exact: true });
      const remove = menu.getByRole("menuitem", { name: `Delete ${name}`, exact: true });
      await expect(archive).toBeFocused();
      await page.keyboard.press("End"); await expect(remove).toBeFocused();
      await page.keyboard.type("c");
      await expect(page.getByRole("dialog", { name: "New session", exact: true })).toHaveCount(0);
      await expect(menu).toBeVisible();
      await page.keyboard.press("Home");
      await page.keyboard.type("a"); await expect(archive).toBeFocused();
      await expectAxeClean(page);
      await page.screenshot({ path: testInfo.outputPath("keyboard-menu.png") });
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0); await expect(trigger).toBeFocused();
      await page.keyboard.press("ArrowUp"); await expect(remove).toBeFocused();
      await page.keyboard.press("Tab"); await expect(menu).toHaveCount(0);
      await tabTo(page, trigger); await page.keyboard.press("Space");
      await expect(archive).toBeFocused(); await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: `Archive ${name}?`, exact: true });
      await expect(dialog).toBeVisible();
      await expectAxeClean(page, "[role=dialog]");
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0); await expect(trigger).toBeFocused();
      await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
    });

    test("Jump to traps Tab, retains editing keys and hands autofocus to New session", async ({ page }) => {
      await gitProject(page, `keyboard-palette-${phone ? "phone" : "desktop"}`);
      const projects: { id: number; name: string }[] = await (await page.request.get("/api/projects")).json();
      expect(projects.length).toBeGreaterThan(0);
      await page.route("**/api/projects", (route) => route.request().method() === "GET" ? route.fulfill({ json: [
        ...projects, ...Array.from({ length: 24 }, (_, index) => ({ ...projects[0], id: 100000 + index, name: `Keyboard project ${index}` })),
      ] }) : route.continue());
      await page.goto("/sessions");
      const trigger = page.getByRole("button", { name: "Jump to…", exact: true });
      await tabTo(page, trigger); await page.keyboard.press("Enter");
      const palette = page.getByRole("dialog", { name: "Jump to…", exact: true });
      const input = palette.getByRole("combobox", { name: "Jump to…", exact: true });
      await expect(input).toBeFocused();
      await page.keyboard.type("Projects");
      await page.keyboard.press("Home");
      expect(await input.evaluate((element: HTMLInputElement) => element.selectionStart)).toBe(0);
      await page.keyboard.press("End");
      expect(await input.evaluate((element: HTMLInputElement) => element.selectionStart)).toBe(8);
      await input.evaluate((element) => element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, isComposing: true })));
      await expect(palette).toBeVisible();
      await expect.poll(() => palette.getByRole("listbox").evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
      await page.keyboard.press("Tab");
      const search = palette.getByRole("link", { name: "Search conversation content", exact: true });
      await expect(search).toBeFocused();
      await page.keyboard.press("Tab"); await expect(input).toBeFocused();
      await page.keyboard.press("Shift+Tab"); await expect(search).toBeFocused();
      await expectAxeClean(page);
      await page.keyboard.press("Escape"); await expect(trigger).toBeFocused();
      await page.evaluate(() => {
        const capture = (event: FocusEvent) => {
          if (!(event.target instanceof HTMLInputElement) || event.target.getAttribute("aria-controls") !== "palette-list") return;
          document.documentElement.dataset.paletteFocusValue = event.target.value;
          document.removeEventListener("focusin", capture);
        };
        document.addEventListener("focusin", capture);
      });
      await page.keyboard.press("Enter"); await expect(input).toBeFocused();
      await expect(input).toHaveValue("");
      await expect(page.locator("html")).toHaveAttribute("data-palette-focus-value", "");
      await page.keyboard.type("New session");
      // Enter activates the rendered selection, not merely the last keystroke sent to Chromium.
      await expect(input).toHaveValue("New session");
      await expect(palette.getByRole("option", { name: "New session", exact: true })).toHaveAttribute("aria-selected", "true");
      await page.keyboard.press("Enter");
      const launch = page.getByRole("dialog", { name: "New session", exact: true });
      await expect(launch.getByRole("textbox", { name: "Task (optional)", exact: true })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(launch).toHaveCount(0); await expect(trigger).toBeFocused();
    });

    test("nested forms keep their draft, trap Tab in the top dialog and restore each opener", async ({ page }, testInfo) => {
      await page.route("**/api/projects", (route) => route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.continue());
      await page.goto("/sessions");
      const trigger = page.getByRole("button", { name: "New session", exact: true }).first();
      await tabTo(page, trigger); await page.keyboard.press("Enter");
      const launch = page.getByRole("dialog", { name: "New session", exact: true });
      const task = launch.getByRole("textbox", { name: "Task (optional)", exact: true });
      await expect(task).toBeFocused(); await page.keyboard.type("Keep my keyboard draft");
      const nestedTrigger = launch.getByRole("button", { name: "New project", exact: true });
      await tabTo(page, nestedTrigger); await page.keyboard.press("Enter");
      const nested = page.getByRole("dialog", { name: "New project", exact: true });
      await expect(nested).toBeVisible();
      for (let i = 0; i < 20; i++) {
        await page.keyboard.press(i % 2 ? "Tab" : "Shift+Tab");
        expect(await nested.evaluate((element) => element.contains(document.activeElement))).toBe(true);
      }
      await expectAxeClean(page, "[role=dialog][aria-label='New project']");
      await page.screenshot({ path: testInfo.outputPath("keyboard-dialogs.png") });
      await page.keyboard.press("Escape");
      await expect(nested).toHaveCount(0); await expect(nestedTrigger).toBeFocused();
      await expect(task).toHaveValue("Keep my keyboard draft");
      await page.keyboard.press("Escape");
      await expect(launch).toHaveCount(0); await expect(trigger).toBeFocused();
    });

    if (phone) test("More is a keyboard menu and typeahead selects a hidden session tab", async ({ page, request }) => {
      const id = await sessionWithChanges(page, "keyboard-more");
      const headers = await csrfHeaders(page);
      try {
        await page.goto(`/sessions/${id}?tab=files`);
        const trigger = page.getByRole("button", { name: "More", exact: true });
        await tabTo(page, trigger); await page.keyboard.press("ArrowUp");
        const menu = page.getByRole("menu", { name: "More", exact: true });
        await expect(menu.getByRole("menuitem", { name: "Setup", exact: true })).toBeFocused();
        await page.keyboard.press("Home");
        await expect(menu.getByRole("menuitem", { name: "Preview", exact: true })).toBeFocused();
        await page.keyboard.type("s");
        await expect(menu.getByRole("menuitem", { name: "Setup", exact: true })).toBeFocused();
        await page.keyboard.press("Space");
        await expect(menu).toHaveCount(0);
        await expect(page.getByRole("tab", { name: "Setup", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(page).toHaveURL(/\?tab=spec$/);
        await expectAxeClean(page);
      } finally { await request.post(`/api/sessions/${id}/stop`, { headers }); }
    });
  });
}
