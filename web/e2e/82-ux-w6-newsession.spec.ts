import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, gitProject, openAdvanced } from "./helpers";

// UX declutter wave 6: New session shows the automatic name with an explicit Rename, after the task and Agent/Profile;
// a chosen name survives closing the dialog, and Use automatic name returns focus to Rename.
test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });

    test("New session names the session automatically, and Rename is explicit and kept in the draft", async ({ page }) => {
      const project = await gitProject(page, `w6-${width}`);
      await page.goto("/");
      await page.getByRole("button", { name: "New session", exact: true }).first().click();
      let dialog = page.getByRole("dialog", { name: "New session" });
      await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
      await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Fix the login form error");
      const rename = dialog.getByRole("button", { name: "Rename", exact: true });
      await expect(dialog.getByTestId("automatic-name")).toHaveText("fix-the-login-form");
      await expect(dialog.getByRole("textbox", { name: "Name", exact: true })).toHaveCount(0);  // no field until Rename
      const top = async (name: string) => (await dialog.getByRole("combobox", { name, exact: true }).boundingBox())!.y;
      expect(await top("Agent")).toBeLessThan((await rename.boundingBox())!.y);
      expect(await top("Profile")).toBeLessThanOrEqual((await rename.boundingBox())!.y);
      await expectAxeClean(page, '[role="dialog"]');

      await rename.click();
      const name = dialog.getByRole("textbox", { name: "Name", exact: true });
      await expect(name).toBeFocused();
      await expect(name).toHaveAttribute("placeholder", "fix-the-login-form");
      await dialog.getByRole("button", { name: "Use automatic name", exact: true }).click();
      await expect(name).toHaveCount(0);
      await expect(rename).toBeFocused();

      await rename.click();
      await name.fill("login-fix");
      await expectAxeClean(page, '[role="dialog"]');
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await page.getByRole("button", { name: "New session", exact: true }).first().click();
      dialog = page.getByRole("dialog", { name: "New session" });
      await expect(dialog.getByRole("textbox", { name: "Name", exact: true })).toHaveValue("login-fix");  // the draft keeps it
      await dialog.getByRole("button", { name: "Discard draft" }).click();
      await page.getByRole("dialog", { name: "Discard this draft?" }).getByRole("button", { name: "Discard draft" }).click();
    });

    test("a local workspace uses the same Rename row, named from the topic", async ({ page }) => {
      await page.goto("/");
      await page.getByRole("button", { name: "New session", exact: true }).first().click();
      const dialog = page.getByRole("dialog", { name: "New session" });
      await dialog.getByRole("radio", { name: "Local workspace" }).click();
      await dialog.getByRole("textbox", { name: "Topic", exact: true }).fill("Tournament apps");
      await expect(dialog.getByTestId("automatic-name")).toHaveText("Tournament apps");
      await expect(dialog.getByRole("textbox", { name: "Session name", exact: true })).toHaveCount(0);
      await dialog.getByRole("button", { name: "Rename", exact: true }).click();
      await expect(dialog.getByRole("textbox", { name: "Name", exact: true })).toBeFocused();
      await dialog.getByRole("button", { name: "Close" }).click();
    });
  });
}

for (const width of [1440, 390]) for (const theme of ["dark", "light"]) {
  test(`launch warnings stay visible when Advanced is closed, ${width}px ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.addInitScript(t => localStorage.setItem("acc-theme", t), theme);
    const project = await gitProject(page, `launch-warnings-${width}-${theme}`);
    await page.route("**/api/sessions/preview", route => {
      const body = route.request().postDataJSON();
      return body.provider === "codex"
        ? route.fulfill({ status: 409, json: { detail: "Example launch policy refused this run." } })
        : route.fulfill({ json: { command: ["claude"], cwd: project.workspace_path, files: {}, digest: "example",
          keys: { keys: [], held_back: [{ id: "example", name: "Example remote key", variable: "EXAMPLE_KEY" }] },
          not_delivered: [{ kind: "instructions", id: "example-rules", reason: "Example instructions cannot be delivered." }] } });
    });
    await page.goto("/");
    await page.getByRole("button", { name: "New session", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "New session", exact: true });
    await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
    const warning = dialog.getByRole("status").filter({ hasText: "Example instructions cannot be delivered." });
    await expect(warning).toBeVisible();
    await openAdvanced(dialog);
    await expect(warning).toBeVisible();
    await dialog.getByRole("radio", { name: /Several agents/ }).check();
    await dialog.getByRole("textbox", { name: "Task", exact: true }).fill("Review this example");
    await dialog.getByRole("combobox", { name: "Run 1: Agent", exact: true }).selectOption("claude");
    await dialog.getByRole("combobox", { name: "Run 2: Agent", exact: true }).selectOption("codex");
    const error = dialog.getByRole("alert").filter({ hasText: "Example launch policy refused this run." });
    await expect(error).toBeVisible();
    await expect(dialog.getByText(/Example remote key: not sent to other machines/)).toBeVisible();
    const advanced = dialog.getByRole("button", { name: /^Advanced/ });
    await advanced.focus();
    await page.keyboard.press("Enter");
    await expect(advanced).toHaveAttribute("aria-expanded", "false");
    await expect(error).toBeVisible();
    await expect(warning).toBeVisible();
    await expect(dialog.getByText(/Example remote key: not sent to other machines/)).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeDisabled();
    await expectAxeClean(page, '[role="dialog"]');
  });
}
