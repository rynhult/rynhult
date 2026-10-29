import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { AUTH_FILE, csrfHeaders, gitProject } from "./helpers";
import { initials } from "../src/lib/format";

test.use({ storageState: AUTH_FILE });

test("project initials tell similar names apart", () => {
  expect([initials("Demo API"), initials("Demo Web"), initials("handbook"), initials("x")]).toEqual(["DA", "DW", "HA", "X"]);
});

// UX review PJ-1 and PJ-2: work starts from a project, and its card leads with live state, not its path.
test("New session from a project card and the project page, with the project chosen", async ({ page }) => {
  const { id, slug } = await gitProject(page, "start-here");
  await page.goto("/projects");
  await page.getByPlaceholder("Filter projects").fill(slug);
  await expect(page.getByText("No live sessions")).toBeVisible();
  await expect(page.getByText("trusted", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: slug, exact: true })).toHaveAttribute("title", new RegExp(`${slug}$`));
  await page.getByRole("button", { name: `New session in ${slug}` }).click();
  const dialog = page.getByRole("dialog", { name: "New session" });
  await expect(dialog.getByRole("combobox", { name: "Project", exact: true })).toHaveValue(String(id));
  await dialog.getByRole("button", { name: "Close" }).click();

  await page.goto(`/projects/${id}`);
  await page.getByRole("main").getByRole("button", { name: "New session" }).click();
  await expect(dialog.getByRole("combobox", { name: "Project", exact: true })).toHaveValue(String(id));
});

test('Git author settings persist and let a fresh project commit without identity flags', async ({ page }) => {
  // Create a managed workspace: registering an existing folder deliberately
  // leaves its identity untouched until launch (rf2-fix).
  const created = await page.request.post('/api/projects/new', {
    headers: await csrfHeaders(page),
    data: { name: `git-author-${randomUUID()}`, source: 'empty' },
  });
  expect(created.ok(), await created.text()).toBe(true);
  const { id, workspace_path: folder } = await created.json();
  await page.goto(`/projects/${id}#project-repository`);
  const name = page.getByRole('textbox', { name: 'Git author name', exact: true });
  await expect(name).not.toHaveValue('');
  await name.fill('Casey Example');
  await page.getByRole('textbox', { name: 'Git author email', exact: true }).fill('casey@example.com');
  await page.getByRole('button', { name: 'Save Git author' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Git author saved.' })).toBeVisible();
  await page.reload();
  await expect(name).toHaveValue('Casey Example');
  await expect(page.getByRole('textbox', { name: 'Git author email', exact: true })).toHaveValue('casey@example.com');
  const { execFileSync } = await import('node:child_process');
  const { writeFileSync } = await import('node:fs');
  writeFileSync(`${folder}/identity.txt`, 'A project-local Git identity.\n');
  execFileSync('git', ['-C', folder, 'add', 'identity.txt']);
  execFileSync('git', ['-C', folder, 'commit', '-qm', 'Commit from project settings']);
  expect(execFileSync('git', ['-C', folder, 'log', '-1', '--format=%an <%ae>'], { encoding: 'utf8' }).trim())
    .toBe('Casey Example <casey@example.com>');
});
