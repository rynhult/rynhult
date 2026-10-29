import { expect, test } from "@playwright/test";
import { AUTH_FILE, expectAxeClean } from "./helpers";

/* UX review 2, R2 and WP7 on a phone: no sideways scrolling with a long project path, and the task in the first screen. */
test.use({ storageState: AUTH_FILE });

test("New session fits a phone with a 60-character project path, task first", async ({ page }) => {
  const path = "/srv/acc/workspaces/a-rather-long-example-project-name-here";  // with "host: " below, over 60 characters
  await page.route("**/api/projects", (route) => route.request().method() !== "GET" ? route.continue() : route.fulfill({ json: [{
    id: 4242, slug: "long-example", name: "Long example", host: "build-box", workspace_path: path, repo: null, remote_url: null,
    import_mode: "managed", trusted: true, archived: false, notes: null, compose_autostart: false, account: "alex-agent", owner: "alex",
  }] }));
  await page.goto("/sessions");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New session", exact: true });
  await expect(dialog.getByText(`build-box: ${path}`)).toBeAttached();
  const sideways = await dialog.evaluate((panel) => [...panel.querySelectorAll<HTMLElement>("*")]
    .filter((el) => el.scrollWidth > el.clientWidth + 1 && ["auto", "scroll"].includes(getComputedStyle(el).overflowX) && !el.closest("pre, details"))
    .map((el) => `${el.tagName}.${el.className}`.slice(0, 80)));
  expect(sideways).toEqual([]);  // nothing scrolls sideways (the path truncates with an ellipsis instead)
  for (const field of [dialog.getByLabel("Task (optional)", { exact: true }), dialog.getByRole("button", { name: /^Advanced/ })]) {
    const box = (await field.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width);  // nothing cut off at the right
  }
  const task = (await dialog.getByLabel("Task (optional)", { exact: true }).boundingBox())!;
  expect(task.y + task.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await expectAxeClean(page, "[role=dialog]");
});
