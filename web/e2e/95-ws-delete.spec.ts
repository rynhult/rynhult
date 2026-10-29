import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';
test.use({ storageState: AUTH_FILE });

for (const missing of [false, true]) test(`${missing ? 'missing folder' : 'workspace'}: confirmation, safe refusal, retry and removal`, async ({ page }, testInfo) => {
  const folder = 'old-research-20261019';
  let removed = false;
  let attempts = 0;
  await page.route('**/api/workspaces', route => route.fulfill({ json: removed ? [] : [{ folder, path: `/tmp/local/${folder}`,
    created_at: '2026-10-19T12:00:00Z', project: null, folder_removed: missing, sessions: [], docs: missing ? null : {} }] }));
  await page.route(`**/api/workspaces/${folder}/delete`, route => {
    expect(route.request().postDataJSON()).toEqual({ confirm: folder });
    attempts++;
    if (attempts === 1) return route.fulfill({ status: 409, json: { detail: 'Ask the owner to repair the agent service, then retry.' } });
    removed = true;
    return route.fulfill({ json: { deleted: folder, sessions_archived: 0, already_removed: missing } });
  });
  await page.goto('/workspaces');
  if (missing) await expect(page.getByText('Folder already removed', { exact: true })).toBeVisible();
  const action = missing ? 'Remove from list' : 'Delete workspace';
  const open = async () => {
    await page.getByRole('button', { name: 'Workspace actions for Old research', exact: true }).click();
    await page.getByRole('menuitem', { name: `${action} Old research`, exact: true }).click();
  };
  await open();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(missing ? 'already removed' : "can't be undone");
  const confirm = dialog.getByRole('button', { name: action, exact: true });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Type the folder name to confirm').fill('wrong-name');
  await expect(confirm).toBeDisabled();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(attempts).toBe(0);
  await open();
  await dialog.getByLabel('Type the folder name to confirm').fill(folder);
  await confirm.click();
  await expect(dialog.getByRole('alert')).toContainText('Ask the owner to repair the agent service, then retry.');
  await expect(confirm).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.screenshot({ path: testInfo.outputPath(`delete-${missing ? 'missing' : 'existing'}.png`) });
  await confirm.click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('No workspaces yet.', { exact: false })).toBeVisible();
  expect(attempts).toBe(2);
});

test('an internal session-service failure is not described as an outside service outage', async ({ page }) => {
  const folder = 'old-research-20261019';
  await page.route('**/api/workspaces', route => route.fulfill({ json: [{ folder, path: `/tmp/local/${folder}`,
    created_at: '2026-10-19T12:00:00Z', project: null, sessions: [], docs: {} }] }));
  await page.route(`**/api/workspaces/${folder}/delete`, route => route.fulfill({ status: 500,
    json: { detail: 'The session service has an internal configuration or version mismatch. Ask the owner to update or repair it, then try again.' } }));
  await page.goto('/workspaces');
  await page.getByRole('button', { name: 'Workspace actions for Old research', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Delete workspace Old research', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Type the folder name to confirm').fill(folder);
  await dialog.getByRole('button', { name: 'Delete workspace', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Something went wrong inside Rynhult.');
  await expect(dialog.getByRole('alert')).not.toContainText("A service Rynhult relies on didn't answer");
  await expect(dialog).not.toContainText('unknown method');
  await expectAxeClean(page);
});
