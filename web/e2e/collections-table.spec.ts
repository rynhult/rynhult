import { test, expect } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
const projects = [
  { id: 88001, slug: 'alpha', name: 'Alpha app', repo: 'example/alpha', workspace_path: '/srv/projects/alpha', import_mode: 'managed', trusted: true, archived: false, compose_autostart: false },
  { id: 88002, slug: 'zebra', name: 'Zebra handbook', repo: null, workspace_path: '/srv/projects/' + 'long-folder-'.repeat(18), import_mode: 'readonly', trusted: true, archived: false, compose_autostart: false },
  { id: 88003, slug: 'archived', name: 'Archived app', repo: null, workspace_path: '/srv/projects/archived', import_mode: 'managed', trusted: true, archived: true, compose_autostart: false },
];

for (const width of [1440, 390]) for (const theme of ['dark', 'light'] as const) {
  test(`Projects table: columns, sort, filter, actions and containment ${width} ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    let autostart = false;
    await page.route('**/api/projects', route => route.fulfill({ json: projects.map(project => project.id === 88001 ? { ...project, compose_autostart: autostart } : project) }));
    await page.route('**/api/sessions', route => route.fulfill({ json: [] }));
    await page.route('**/api/projects/*/runtime', route => {
      if (route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toEqual({ compose_autostart: true, digest: 'fixture-compose' });
        autostart = true;
        return route.fulfill({ json: { ...projects[0], compose_autostart: true } });
      }
      return route.fulfill({ json: { containers: [{ State: 'running', Service: 'web', Status: 'Up' }],
        compose: { approved: autostart, digest: 'fixture-compose', diff: '+++ now/compose.yml\n+services: {}' } } });
    });
    await page.goto('/projects');
    await page.getByRole('radio', { name: 'List', exact: true }).click();
    const table = page.getByRole('table', { name: 'Projects' });
    await expect(table).toBeVisible();
    for (const name of ['Name', 'Repository / folder', 'Live sessions', 'Containers / compose', 'Autostart', 'Actions']) {
      await expect(table.getByRole('columnheader', { name: new RegExp(name) })).toBeVisible();
    }
    const rows = table.locator('tbody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.first().getByRole('link', { name: 'Alpha app', exact: true })).toBeVisible();
    await table.getByRole('button', { name: 'Sort by Name', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(rows.first()).toContainText('Zebra handbook');
    await expect(table.getByRole('columnheader', { name: /Name/ })).toHaveAttribute('aria-sort', 'descending');
    await expect(rows.first().getByText('read-only', { exact: true })).toBeVisible();
    await expect(rows.first().getByRole('button', { name: /New session/ })).toHaveCount(0);
    const alpha = rows.filter({ hasText: 'Alpha app' });
    await expect(alpha.getByRole('button', { name: 'New session in Alpha app', exact: true })).toBeVisible();
    await alpha.getByRole('button', { name: 'New session in Alpha app', exact: true }).click();
    const session = page.getByRole('dialog', { name: 'New session', exact: true });
    await expect(session.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue('88001');
    await session.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(alpha).toContainText('1/1 containers up');
    await expect(alpha.getByRole('checkbox', { name: 'Autostart', exact: true })).not.toBeChecked();
    await alpha.getByRole('checkbox', { name: 'Autostart', exact: true }).click();
    const approval = page.getByRole('dialog', { name: "Approve Alpha app's compose files?" });
    await approval.getByRole('button', { name: 'Approve and turn on autostart', exact: true }).click();
    await expect(approval).toHaveCount(0);
    await expect(alpha.getByRole('checkbox', { name: 'Autostart', exact: true })).toBeChecked();
    await alpha.getByRole('button', { name: 'Project actions for Alpha app' }).click();
    await expect(page.getByRole('menuitem', { name: 'Archive Alpha app' })).toBeVisible();
    await page.keyboard.press('Escape');
    await page.getByPlaceholder('Filter projects').fill('zebra');
    await expect(rows).toHaveCount(1);
    await page.getByPlaceholder('Filter projects').fill('');
    await page.getByRole('checkbox', { name: 'Show archived' }).check();
    await expect(rows).toHaveCount(3);
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const viewport = page.getByRole('region', { name: /table, scroll horizontally/ });
    await viewport.evaluate(element => { element.scrollLeft = 0; });
    await viewport.focus();
    if (width === 390) {
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => viewport.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
      await viewport.evaluate(element => element.scrollTo({ left: 0, behavior: 'instant' }));
    }
    if (theme === 'dark') await page.screenshot({ path: wikiShotPath(testInfo, `projects-list-${width}.png`), fullPage: true });
  });

  test(`Workspaces table: shared sort, reports and actions ${width} ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme });
    await page.route('**/api/workspaces', route => route.fulfill({ json: [
      { folder: 'alpha-research', path: '/srv/projects/local/alpha-research', created_at: '2026-10-29T12:00:00Z', project: null, sessions: [], docs: { research: 2, council: 1 } },
      { folder: 'zebra-research', path: '/srv/projects/local/' + 'long-folder-'.repeat(18), created_at: '2026-10-29T12:00:00Z', project: null, sessions: [], docs: null, folder_removed: true },
    ] }));
    await page.goto('/workspaces');
    await page.getByRole('radio', { name: 'List', exact: true }).click();
    const table = page.getByRole('table', { name: 'Workspaces' });
    await expect(table).toBeVisible();
    const rows = table.locator('tbody tr');
    await expect(rows).toHaveCount(2);
    await table.getByRole('button', { name: 'Sort by Name', exact: true }).click();
    await expect(rows.first()).toContainText('Zebra research');
    await expect(rows.first()).toContainText('Folder already removed');
    await rows.first().getByRole('button', { name: 'Workspace actions for Zebra research' }).click();
    await expect(page.getByRole('menuitem', { name: /Remove from list Zebra research/ })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Add session', exact: true })).toHaveCount(0);
    await page.keyboard.press('Escape');
    const alpha = rows.filter({ hasText: 'Alpha research' });
    await expect(alpha).toContainText('2 research');
    await expect(alpha).toContainText('1 council');
    await expect(alpha.getByRole('button', { name: 'New session in Alpha research', exact: true })).toBeVisible();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const viewport = page.getByRole('region', { name: /table, scroll horizontally/ });
    await viewport.evaluate(element => { element.scrollLeft = 0; });
    await viewport.focus();
    if (width === 390) {
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => viewport.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
      await viewport.evaluate(element => element.scrollTo({ left: 0, behavior: 'instant' }));
    }
    if (theme === 'dark') await page.screenshot({ path: wikiShotPath(testInfo, `workspaces-list-${width}.png`), fullPage: true });
  });
}

for (const state of ['empty', 'decision-only', 'mixed'] as const) test(`Inbox Dismiss all follows dismissible notices: ${state}`, async ({ page }) => {
  const at = new Date().toISOString();
  const decision = { id: 991, kind: 'needs_input', title: 'Decision needed', severity: 'info', source: 'test', verified: true,
    created_at: at, updated_at: at, bulk_dismissible: false };
  await page.route('**/api/attention?*', route => route.fulfill({ json: state === 'empty' ? [] : state === 'decision-only' ? [decision]
    : [decision, { ...decision, id: 992, kind: 'state', title: 'Finished notice', bulk_dismissible: true }] }));
  await page.goto('/inbox');
  await expect(page.getByText(state === 'empty' ? 'Nothing needs you right now.' : 'Decision needed', { exact: true })).toBeVisible();
  const action = page.getByRole('button', { name: 'Dismiss all', exact: true });
  if (state === 'mixed') await expect(action).toBeEnabled();
  else await expect(action).toHaveCount(0);
  await page.getByRole('radio', { name: 'History', exact: true }).click();
  if (state === 'mixed') await expect(action).toBeEnabled();
  else await expect(action).toHaveCount(0);
});
