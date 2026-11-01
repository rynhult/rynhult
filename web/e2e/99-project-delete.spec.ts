import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject } from './helpers';
test.use({ storageState: AUTH_FILE });

for (const managed of [true, false]) test(`${managed ? 'managed checkout' : 'registered folder'} deletion from page and menu`, async ({ page }, testInfo) => {
  let project: { id: number; slug: string; workspace_path: string };
  if (managed) {
    await page.goto('/projects');
    const response = await page.request.post('/api/projects/new', { headers: await csrfHeaders(page),
      data: { name: `Delete ${randomUUID().slice(0, 8)}`, source: 'empty' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    project = await response.json();
  } else {
    const p = await gitProject(page, 'delete-registered');
    project = { ...p, workspace_path: p.folder };
  }
  if (managed) {
    await page.goto(`/projects/${project.id}`);
    await page.getByRole('button', { name: 'Delete project', exact: true }).click();
  } else {
    await page.goto('/projects');
    await page.getByRole('button', { name: `Project actions for ${project.slug}`, exact: true }).click();
    await expect(page.getByRole('menuitem', { name: `Archive ${project.slug}`, exact: true })).toBeVisible();
    await page.getByRole('menuitem', { name: `Delete ${project.slug}`, exact: true }).click();
  }
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(managed ? 'permanently deletes this managed checkout' : 'Keeps the registered or imported folder');
  await expect(dialog).toContainText('Keeps the GitHub repository untouched');
  const confirm = dialog.getByRole('button', { name: 'Delete project', exact: true });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Type the project slug to confirm').fill('wrong');
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Type the project slug to confirm').fill(project.slug);
  await expect(confirm).toBeEnabled();
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('confirmation.png') });
  const deleted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${project.id}/delete`);
  await confirm.click();
  expect((await deleted).ok()).toBeTruthy();
  await expect(dialog).toBeHidden();
  await expect.poll(() => existsSync(project.workspace_path)).toBe(!managed);
  await expect(page).toHaveURL(/\/projects$/);
});

test('Cancel, live-session refusal, Stop all and retry keep confirmation in page', async ({ page }) => {
  const p = await gitProject(page, 'delete-live');
  const created = await page.request.post('/api/sessions', { headers: p.headers,
    data: { project_id: p.id, provider: 'claude', name: 'Deletion history' } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const session = await created.json();
  await page.goto(`/projects/${p.id}`);
  const open = () => page.getByRole('button', { name: 'Delete project', exact: true }).click();
  await open();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('alert')).toContainText('live sessions or a team');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect((await page.request.get(`/api/sessions/${session.id}`)).ok()).toBeTruthy();
  await open();
  await expect(dialog.getByRole('alert')).toContainText('live sessions or a team');
  await dialog.getByRole('button', { name: 'Stop all first', exact: true }).click();
  await expect(dialog).toContainText('Keeps the registered or imported folder');
  await dialog.getByLabel('Type the project slug to confirm').fill(p.slug);
  let failed = false;
  await page.route(`**/api/projects/${p.id}/delete`, async route => {
    if (route.request().method() === 'POST' && !failed) {
      failed = true;
      return route.fulfill({ status: 409, json: { detail: 'The folder check refused deletion. Check again and retry.' } });
    }
    return route.continue();
  });
  const confirm = dialog.getByRole('button', { name: 'Delete project', exact: true });
  await confirm.click();
  await expect(dialog.getByRole('alert')).toContainText('folder check refused deletion');
  await expect(dialog.getByLabel('Type the project slug to confirm')).toHaveValue(p.slug);
  await expectAxeClean(page);
  await confirm.click();
  await expect(dialog).toBeHidden();
  const kept = await (await page.request.get(`/api/sessions/${session.id}`)).json();
  expect(kept.project_id).toBeNull();
  expect(kept.desired_state).toBe('archived');
});
