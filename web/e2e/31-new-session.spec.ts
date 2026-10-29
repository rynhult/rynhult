import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean, gitProject, openAdvanced, wikiShotPath } from "./helpers";

// Fields are found by role and name: a reopened draft's textarea text would leak into getByLabel's label-text match.

/* UX review 2, beta item 3: New session asks for the task first (WP7: What / Who / Advanced, one Run choice). */
test.use({ storageState: AUTH_FILE });
test.describe.configure({ mode: "serial" });
let projectId: number;

test("the task is in the first screen, a closed Advanced says what it holds, and the run is one choice", async ({ page }, testInfo) => {
  const { id } = await gitProject(page, "task-first");
  projectId = id;
  // The wiki's screenshot shows the usual workspaces folder, not this run's temporary one.
  await page.route("**/api/projects", async (route) => route.request().method() !== "GET" ? route.continue() : route.fulfill({
    json: (await (await route.fetch()).json()).map((p: { slug: string }) => ({ ...p, workspace_path: `/srv/acc/workspaces/${p.slug}` })) }));
  await page.reload();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
  const task = dialog.getByRole("textbox", { name: "Task (optional)", exact: true });
  const box = (await task.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(900);  // no scrolling to reach it at 1440×900
  await expect(task).toBeFocused();
  const summary = dialog.getByTestId("advanced-summary");
  await expect(summary).toHaveText(/^Once · default model · .*shared checkout · 1 skill$/);
  await expect(dialog.getByLabel("Permissions")).toHaveCount(0);  // engine choices wait under Advanced
  await expectAxeClean(page, "[role=dialog]");
  await task.fill("Tidy the greeting in app.py and add a test.");
  await dialog.screenshot({ path: wikiShotPath(testInfo, "new-session-dialog.png") });
  await task.fill("");

  await openAdvanced(dialog);
  await expect(dialog.getByRole("group", { name: "Run", exact: true })).toBeVisible();
  await dialog.getByRole("checkbox", { name: /Own worktree/ }).check();
  await dialog.getByLabel("Effort").selectOption("high");
  await expect(summary).toContainText("high effort");
  await expect(summary).toContainText("own worktree");
  await dialog.getByRole("radio", { name: /Several agents/ }).check();
  await expect(summary).toHaveText("2 agents, each in its own worktree");
  await dialog.getByRole("radio", { name: /On a schedule/ }).check();
  await expect(dialog.getByRole("combobox", { name: "Timing" })).toBeVisible();  // timing follows the task
  await expect(summary).toContainText("On a schedule (daily at 09:00, paused)");
  await expect(dialog.getByRole("button", { name: "Create paused schedule" })).toBeVisible();
  await dialog.getByRole("radio", { name: /^Once/ }).check();
  await expectAxeClean(page, "[role=dialog]");
});

test("the draft survives closing, Discard draft clears it, and project + task + Launch is enough", async ({ page }) => {
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Tidy the greeting in app.py");
  await expect(dialog.getByTestId("automatic-name")).toHaveText("tidy-the-greeting-in");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await expect(dialog.getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("Tidy the greeting in app.py");  // kept

  await dialog.getByRole("button", { name: "Discard draft" }).click();
  await page.getByRole("dialog", { name: "Discard this draft?" }).getByRole("button", { name: "Discard draft" }).click();
  await expect(dialog.getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("");

  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Tidy the greeting in app.py");
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  const session = await (await page.request.get(`/api/sessions/${page.url().split("/").pop()}`)).json();
  expect(session.name).toBe("tidy-the-greeting-in");
  expect(session.worktree).toBeNull();  // defaults unchanged: the project's own checkout

  await page.getByRole("button", { name: "New session", exact: true }).click();  // after a launch, a clean form
  await expect(page.getByRole("dialog", { name: "New session", exact: true }).getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("");
  await page.keyboard.press("Escape");
});

test("a second session in the same checkout is warned, and can take its own worktree", async ({ page }) => {
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  const warning = dialog.getByText("tidy-the-greeting-in is already working in this checkout.", { exact: false });
  await expect(warning).toBeVisible({ timeout: 15_000 });
  await dialog.getByRole("button", { name: "Use its own worktree" }).click();
  await expect(warning).toHaveCount(0);
  await expect(dialog.getByTestId("advanced-summary")).toContainText("own worktree");
  await page.keyboard.press("Escape");
});
