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

for (const width of [390, 1440]) test(`empty repository has an explicit local first-commit button at ${width}px`, async ({ page }) => {
  const { mkdirSync, readFileSync } = await import('node:fs');
  const { dirname } = await import('node:path');
  const { execFileSync } = await import('node:child_process');
  const { info } = await import('./helpers');
  const name = `empty-${randomUUID()}`;
  const folder = `${dirname(info().workspace)}/${name}`;
  mkdirSync(folder);
  execFileSync('git', ['init', '-q', '-b', 'main', folder]);
  const headers = await csrfHeaders(page);
  const response = await page.request.post('/api/projects', { headers, data: {
    name, slug: name, workspace_path: folder, import_mode: 'managed',
  } });
  expect(response.ok(), await response.text()).toBe(true);
  const project = await response.json();
  await page.setViewportSize({ width, height: 900 });
  // Stand in for the network clone only; state inspection and the commit use the real broker.
  await page.route('**/api/projects/new', route => route.fulfill({ status: 201, json: project }));
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New project', exact: true });
  await dialog.getByRole('radio', { name: 'Clone repository' }).click();
  await dialog.getByRole('textbox', { name: 'Repository', exact: true }).fill('https://example.com/empty.git');
  await dialog.getByRole('button', { name: 'Clone and add', exact: true }).click();
  const imported = page.getByRole('dialog', { name: 'Project imported', exact: true });
  await expect(imported.getByRole('button', { name: 'Make the first commit', exact: true })).toBeVisible();
  await imported.getByRole('button', { name: 'Later — open project', exact: true }).click();
  await page.goto(`/projects/${project.id}`);
  const notice = page.getByRole('region', { name: 'First Git commit' });
  await expect(notice).toContainText('nothing is pushed');
  await notice.getByRole('button', { name: 'Make the first commit', exact: true }).click();
  await expect(notice).toHaveCount(0);
  expect(readFileSync(`${folder}/README.md`, 'utf8')).toContain(`# ${name}`);
  expect(execFileSync('git', ['-C', folder, 'log', '-1', '--format=%s'], { encoding: 'utf8' }).trim()).toBe('Initialize project');
  await page.reload();
  await expect(notice).toHaveCount(0);
});
