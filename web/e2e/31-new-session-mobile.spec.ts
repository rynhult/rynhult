import { expect, test } from "./fixtures";
import { AUTH_FILE, gitProject, openAdvanced, expectAxeClean } from "./helpers";

/* UX review 2, R2 and WP7 on a phone: no sideways scrolling with a long project path, and the task in the first screen. */
test.use({ storageState: AUTH_FILE });

test("New session fits a phone with a 60-character project path, task first", async ({ page }) => {
  const project = await gitProject(page, 'a-rather-long-example-project-name-here');
  const path = project.folder;
  expect(path.length).toBeGreaterThanOrEqual(60);
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("region", { name: "New session", exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole("combobox", { name: "Project", exact: true }).selectOption(String(project.id));
  await expect(dialog.getByRole("region", { name: "Working location", exact: true })).toContainText(path);
  const sideways = await dialog.evaluate((panel) => [...panel.querySelectorAll<HTMLElement>("*")]
    .filter((el) => el.scrollWidth > el.clientWidth + 1 && ["auto", "scroll"].includes(getComputedStyle(el).overflowX) && !el.closest("pre, details"))
    .map((el) => `${el.tagName}.${el.className}`.slice(0, 80)));
  expect(sideways).toEqual([]);  // nothing scrolls sideways (the path truncates with an ellipsis instead)
  for (const field of [dialog.getByLabel("Task (optional)", { exact: true }), dialog.locator("summary").filter({ hasText: "Model and working location" })]) {
    const box = (await field.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);  // nothing cut off at the right
  }
  const task = (await dialog.getByLabel("Task (optional)", { exact: true }).boundingBox())!;
  expect(task.y + task.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await expectAxeClean(page, 'main');
});
