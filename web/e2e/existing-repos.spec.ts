import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { expect, test } from './fixtures';
import { AUTH_FILE, csrfHeaders, info } from './helpers';

test.use({ storageState: AUTH_FILE });

test('Register outside roots keeps the draft and links to Allowed folders', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('button', { name: 'Register folder', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Register a folder', exact: true });
  const outside = `${info().state}/existing-${randomUUID().slice(0, 8)}`;
  mkdirSync(outside);
  await dialog.getByLabel('Name', { exact: true }).fill('Existing outside');
  await dialog.getByLabel('Workspace path', { exact: true }).fill(outside);
  await dialog.getByRole('button', { name: 'Register', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Allow this folder in Security → Allowed folders');
  await expect(dialog.getByLabel('Workspace path', { exact: true })).toHaveValue(outside);
  await dialog.getByRole('link', { name: 'Open Allowed folders in Security', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Allowed folders', exact: true })).toBeVisible();
});

// Helper responses are simulated. API/worker tests cover approval and policy
// publication; this browser fixture never connects to an installed root worker.
test('Trust repair opens the folder editor, applies reviewed roots, and retries Trust', async ({ page }) => {
  const slug = `policy-${randomUUID().slice(0, 8)}`;
  const folder = `${info().workspace}/${slug}`;
  mkdirSync(folder);
  const headers = await csrfHeaders(page);
  const response = await page.request.post('/api/projects', { headers, data: { slug, name: slug, workspace_path: folder } });
  expect(response.ok()).toBe(true);
  const project = await response.json();
  let applied = false;
  const operation = 'e'.repeat(32), digest = 'f'.repeat(64);
  const roots = [info().workspace, '/home/alex/src/existing'];
  await page.route(`**/api/projects/${project.id}/trust`, route => applied ? route.continue() : route.fulfill({
    status: 409, json: { detail: 'Allow this folder in Security → Allowed folders, then retry.' },
  }));
  await page.route('**/api/security/folders', route => route.fulfill({ json: { revision: applied ? 1 : 0, accounts: [{
    scope: { kind: 'account', machine: 'local', machine_id: 'local', account: 'alex-agent' },
    state: applied ? 'applied' : 'legacy', stored_state: applied ? 'applied' : 'legacy',
    roots: applied ? roots : [roots[0]], revision: applied ? 1 : 0, applied_revision: applied ? 1 : 0,
    allowed_folders: true, retry_available: false, detail: {}, projects: [],
  }] } }));
  await page.route('**/api/security/folders/check', route => route.fulfill({ json: { state: 'access_checked', message: 'Account access checked.' } }));
  await page.route('**/api/security/folders/preview', async route => {
    expect(route.request().postDataJSON().roots).toEqual(roots);
    await route.fulfill({ json: { id: operation, digest, active_sessions: [], active_gates: [], warning: 'No OS access grant.', preview: { roots, legacy_roots: [roots[0]] } } });
  });
  await page.route(`**/api/security/folders/${operation}/apply`, async route => {
    expect(route.request().postDataJSON()).toEqual({ digest });
    applied = true;
    await route.fulfill({ json: { state: 'applied', revision: 1 } });
  });
  try {
    await page.goto('/projects');
    const card = page.locator('section').filter({ has: page.getByRole('link', { name: slug, exact: true }) });
    await card.getByRole('button', { name: 'Trust', exact: true }).click();
    await page.getByRole('button', { name: 'Trust this workspace', exact: true }).click();
    await page.getByRole('link', { name: 'Open Allowed folders in Security', exact: true }).click();
    await page.getByLabel('Folder to add', { exact: true }).fill(roots[1]);
    await page.getByRole('button', { name: 'Check folder', exact: true }).click();
    await page.getByRole('button', { name: 'Add to review', exact: true }).click();
    await page.getByRole('button', { name: 'Preview folders', exact: true }).click();
    await page.getByRole('dialog', { name: 'Review allowed folders', exact: true }).getByRole('button', { name: 'Apply folder policy', exact: true }).click();
    await page.getByRole('dialog', { name: 'Apply account folder policy?', exact: true }).getByRole('button', { name: 'Apply folder policy', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Review allowed folders', exact: true })).toHaveCount(0);
    await page.goto('/projects');
    await card.getByRole('button', { name: 'Trust', exact: true }).click();
    await page.getByRole('button', { name: 'Trust this workspace', exact: true }).click();
    await expect(card.getByText('not trusted', { exact: true })).toHaveCount(0);
  } finally {
    await page.unrouteAll({ behavior: 'wait' });
    const removed = await page.request.post(`/api/projects/${project.id}/delete`, { headers, data: { confirm: slug } });
    expect(removed.ok()).toBe(true);
  }
});
