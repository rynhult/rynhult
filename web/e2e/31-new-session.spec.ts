import { expect, test, type Page } from "./fixtures";
import { AUTH_FILE, expectAxeClean, gitProject, openAdvanced, wikiShotPath } from "./helpers";

// Fields are found by role and name: a reopened draft's textarea text would leak into getByLabel's label-text match.

/* UX review 2, beta item 3: New session asks for the task first (WP7: What / Who / Advanced, one Run choice). */
test.use({ storageState: AUTH_FILE });
test.describe.configure({ mode: "serial" });
let projectId: number;
function projectAgents(page: Page, id: number) {
  return page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/providers/registry' && url.searchParams.get('project_id') === String(id)
      && url.searchParams.get('machine') === 'local';
  }).then(async response => {
    expect(response.ok()).toBe(true);
    expect(await response.finished()).toBeNull();
  });
}

test("the task is in the first screen, the model/location disclosure explains its fields, and the run is one choice", async ({ page }, testInfo) => {
  const { id } = await gitProject(page, "task-first");
  projectId = id;
  // The wiki's screenshot shows the usual workspaces folder, not this run's temporary one.
  await page.route("**/api/projects", async (route) => route.request().method() !== "GET" ? route.continue() : route.fulfill({
    json: (await (await route.fetch()).json()).map((p: { slug: string }) => ({ ...p, workspace_path: `/srv/acc/workspaces/${p.slug}` })) }));
  await page.reload();
  const agents = projectAgents(page, id);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("region", { name: "New session", exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(id));
  await agents;
  const task = dialog.getByRole("textbox", { name: "Task (optional)", exact: true });
  const box = (await task.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(900);  // no scrolling to reach it at 1440×900
  await task.focus();
  await expect(task).toBeFocused();
  // Classic closed Advanced aggregate summary is retired (Josh review). Fields live in named disclosures.
  await expect(dialog.getByRole("combobox", { name: "Run", exact: true })).toHaveValue("once");
  await expectAxeClean(page, 'main');
  const preview = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions/preview'
    && response.request().postDataJSON().project_id === id
    && response.request().postDataJSON().initial_prompt === 'Tidy the greeting in app.py and add a test.');
  await task.fill("Tidy the greeting in app.py and add a test.");
  expect((await preview).ok()).toBe(true);
  expect(await (await preview).finished()).toBeNull();
  await expect(dialog.getByRole("button", { name: "Launch", exact: true })).toBeEnabled();
  await dialog.screenshot({ path: wikiShotPath(testInfo, "new-session-dialog.png") });
  await task.fill("");

  await openAdvanced(dialog);
  await expect(dialog.getByRole("combobox", { name: "Run", exact: true })).toBeVisible();
  await dialog.getByRole("checkbox", { name: /Own worktree/ }).check();
  await dialog.getByLabel("Effort").selectOption("high");
  await expect(dialog.getByLabel("Effort")).toHaveValue("high");
  await expect(dialog.getByRole("checkbox", { name: /Own worktree/ })).toBeChecked();
  await dialog.getByRole("combobox", { name: "Run", exact: true }).selectOption("several");
  await expect(dialog.getByRole("combobox", { name: "Run 2: Agent", exact: true })).toBeVisible();
  await dialog.getByRole("combobox", { name: "Run", exact: true }).selectOption("schedule");
  await expect(dialog.getByRole("combobox", { name: "Timing" })).toBeVisible();  // timing follows the task
  await expect(dialog.getByRole("combobox", { name: "Timing" })).toHaveValue("daily");
  await expect(dialog.getByRole("button", { name: "Create paused schedule" })).toBeVisible();
  await dialog.getByRole("combobox", { name: "Run", exact: true }).selectOption("once");
  await expectAxeClean(page, 'main');
});

test("the draft survives closing, Discard draft clears it, and project + task + Launch is enough", async ({ page }) => {
  await page.goto("/sessions");
  const agents = projectAgents(page, projectId);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  let dialog = page.getByRole("region", { name: "New session", exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  await agents;
  await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Tidy the greeting in app.py");
  await expect(dialog.getByTestId("automatic-name")).toHaveText("tidy-the-greeting-in");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  dialog = page.getByRole("region", { name: "New session", exact: true });
  await expect(dialog.getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("Tidy the greeting in app.py");  // kept

  await dialog.getByRole("button", { name: "Discard draft" }).click();
  await page.getByRole("dialog", { name: "Discard this draft?" }).getByRole("button", { name: "Discard draft" }).click();
  await expect(dialog.getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("");

  await openAdvanced(dialog);
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  await dialog.getByRole("textbox", { name: "Task (optional)", exact: true }).fill("Tidy the greeting in app.py");
  const launching = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions');
  await dialog.getByRole("button", { name: "Launch", exact: true }).dblclick();
  expect((await launching).status()).toBe(201);
  expect(await (await launching).finished()).toBeNull();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f]{12}$/);
  const session = await (await page.request.get(`/api/sessions/${page.url().split("/").pop()}`)).json();
  expect(session.name).toBe("tidy-the-greeting-in");
  expect(session.worktree).toBeNull();  // defaults unchanged: the project's own checkout

  await page.getByRole("button", { name: "New session", exact: true }).click();  // after a launch, a clean form
  await expect(page.getByRole("region", { name: "New session", exact: true }).getByRole("textbox", { name: "Task (optional)", exact: true })).toHaveValue("");
  await page.keyboard.press("Escape");
});

test("a second session in the same checkout is warned, and can take its own worktree", async ({ page }) => {
  await page.goto("/sessions");
  const agents = projectAgents(page, projectId);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("region", { name: "New session", exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(projectId));
  await agents;
  const warning = dialog.getByText("Other sessions already work in this checkout.", { exact: false });
  await expect(warning).toBeVisible({ timeout: 15_000 });
  await dialog.getByRole("checkbox", { name: /Own worktree/ }).check();
  await expect(dialog.getByRole("checkbox", { name: /Own worktree/ })).toBeChecked();
  await page.keyboard.press("Escape");
});
