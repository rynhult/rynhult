import { expect, test } from "./fixtures";
import { AUTH_FILE, gitProject } from "./helpers";

test.use({ storageState: AUTH_FILE });

test("Changes commits multiline messages and displays push results even with no diff", async ({ page }) => {
  const { id, headers } = await gitProject(page, "git-actions");
  const session = await (await page.request.post("/api/sessions", { headers,
    data: { project_id: id, provider: "claude", name: "Git actions", worktree: true } })).json();
  let message = "";
  await page.route(`**/api/sessions/${session.id}/git/commit`, route => {
    message = route.request().postDataJSON().message;
    return route.fulfill({ json: { output: "Commit created" } });
  });
  await page.route(`**/api/sessions/${session.id}/git/push`, route => route.fulfill({ json: { ok: false, output: "remote rejected" } }));
  await page.goto(`/sessions/${session.id}?tab=changes`);
  const commit = page.getByRole("button", { name: "Commit all", exact: true });
  await expect(commit).toBeDisabled();
  const text = 'Fix "quotes"\n\nSecond paragraph';
  await page.getByRole("textbox", { name: "Commit message" }).fill(text);
  await commit.click();
  await expect(page.getByRole("status").filter({ hasText: "Commit created" })).toBeVisible();
  expect(message).toBe(text);
  await expect(page.getByRole("textbox", { name: "Commit message" })).toHaveValue("");
  await page.getByRole("button", { name: "Push branch", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Push failed. remote rejected" })).toBeVisible();
});
