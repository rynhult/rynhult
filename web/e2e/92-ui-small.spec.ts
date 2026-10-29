import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';
test.use({ storageState: AUTH_FILE });

test('shown notices: confirm, skip decisions, cancel, dismiss and Undo', async ({ page }) => {
  const at = new Date().toISOString();
  let dismissed = false;
  let calls = 0;
  const notice = { id: 801, kind: 'state', title: 'Demo notice', severity: 'info', source: 'test', verified: true,
    created_at: at, updated_at: at, acked_at: null, resolved_at: null, session_id: null, bulk_dismissible: true };
  await page.route('**/api/attention?*', route => route.fulfill({ json: [
    ...(!dismissed ? [notice] : []), { ...notice, id: 802, title: 'Answer needed', kind: 'needs_input', bulk_dismissible: false },
  ] }));
  await page.route('**/api/attention/dismiss', route => {
    expect(route.request().postDataJSON().items).toEqual([{ id: 801, updated_at: at }, { id: 802, updated_at: at }]);
    calls++; dismissed = true; return route.fulfill({ json: { batch: 33, dismissed: 1, skipped: 1 } });
  });
  await page.route('**/api/attention/dismiss/undo', route => {
    expect(route.request().postDataJSON()).toEqual({ batch: 33 });
    dismissed = false; return route.fulfill({ json: { restored: 1 } });
  });
  await page.goto('/inbox');
  await page.getByRole('button', { name: 'Dismiss all', exact: true }).click();
  await expect(page.getByText('Dismiss 1 items?', { exact: true })).toBeVisible();
  await expect(page.getByText(/1 skipped/)).toBeVisible();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(calls).toBe(0);
  await page.getByRole('button', { name: 'Dismiss all', exact: true }).click();
  await page.getByRole('button', { name: 'Dismiss 1 items', exact: true }).click();
  await expect(page.getByText('Demo notice', { exact: true })).toBeHidden();
  await expect(page.getByText('Answer needed', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(page.getByText('Demo notice', { exact: true })).toBeVisible();
  await page.clock.install();
  await page.getByRole('button', { name: 'Dismiss all', exact: true }).click();
  await page.getByRole('button', { name: 'Dismiss 1 items', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeVisible();
  await page.clock.runFor(8000);
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeHidden();
  await expectAxeClean(page);
});

for (const path of ['projects', 'workspaces']) test(`${path}: shared view control, keyboard and persistence`, async ({ page }) => {
  await page.goto(`/${path}`);
  const view = page.getByRole('radiogroup', { name: 'View' });
  await expect(view.getByRole('radio', { name: 'Cards', exact: true })).toBeChecked();
  await view.getByRole('radio', { name: 'List', exact: true }).click();
  await page.reload();
  await expect(view.getByRole('radio', { name: 'List', exact: true })).toBeChecked();
  await view.getByRole('radio', { name: 'List', exact: true }).focus();
  await page.keyboard.press('Home');
  await expect(view.getByRole('radio', { name: 'Cards', exact: true })).toBeFocused();
  await expect(view.getByRole('radio', { name: 'Cards', exact: true })).toBeChecked();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
});

test('provider sections: current, keyboard, deep link and wrapping', async ({ page }) => {
  await page.goto('/providers#provider-models');
  const group = page.getByRole('group', { name: 'Provider sections' });
  const models = group.getByRole('link', { name: 'Available models', exact: true });
  await expect(models).toHaveAttribute('aria-current', 'location');
  await models.focus();
  await page.keyboard.press('Home');
  await expect(group.getByRole('link', { name: 'Sign-ins and usage', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#provider-sign-ins$/);
  await expect(group.getByRole('link', { name: 'Sign-ins and usage', exact: true })).toHaveAttribute('aria-current', 'location');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
});

test('Audit search is debounced and shows bounded page counts', async ({ page }) => {
  const queries: string[] = [];
  await page.route('**/api/audit?*', route => {
    const params = new URL(route.request().url()).searchParams;
    queries.push(params.get('q') ?? '');
    return route.fulfill({ json: { items: params.get('q') ? [] : [{ id: 7, ts: '2026-10-29T12:00:00Z', actor: 'alex', action: 'demo.audit', target: null, detail: {} }], has_more: false, next_before: null } });
  });
  await page.goto('/audit');
  await expect(page.getByText('1 entry on this page')).toBeVisible();
  await page.clock.install();
  const search = page.getByRole('searchbox', { name: 'Search audit log' });
  await search.fill('needle_%');
  await page.clock.runFor(299);
  expect(queries).not.toContain('needle_%');
  await page.clock.runFor(1);
  await expect(page.getByText('0 entries on this page')).toBeVisible();
  await expect(page.getByText('No entries match.')).toBeVisible();
  expect(queries).toContain('needle_%');
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const path of ['projects', 'workspaces']) test(`${path}: populated views keep fields/actions and isolate viewers`, async ({ page }, testInfo) => {
  const project = { id: 88001, slug: 'demo-ui', name: 'Demo app', repo: 'example/demo', workspace_path: '/tmp/demo-ui',
    archived: false, trusted: true, import_mode: 'readonly', private_workspace: false };
  await page.route('**/api/projects', route => route.fulfill({ json: [project] }));
  await page.route('**/api/workspaces', route => route.fulfill({ json: [{ folder: 'demo-ui', path: '/tmp/demo-ui',
    created_at: new Date().toISOString(), project: null, sessions: [], docs: { research: 2, council: 1 } }] }));
  await page.goto(`/${path}`);
  const content = page.locator('[data-view]');
  await expect(content).toHaveAttribute('data-view', 'cards');
  const controls = await content.locator('a,button').evaluateAll(elements => elements.map(el => [el.textContent?.trim(), el.getAttribute('aria-label'), el.getAttribute('href')]));
  await page.getByRole('radio', { name: 'List', exact: true }).click();
  await expect(content).toHaveAttribute('data-view', 'list');
  await expect(content.getByRole('table', { name: path === 'projects' ? 'Projects' : 'Workspaces', exact: true })).toBeVisible();
  await expect(content.getByText(path === 'projects' ? 'read-only' : '2 research', { exact: true })).toBeVisible();
  const tableControls = await content.locator('a,button').evaluateAll(elements => elements.map(el => [el.textContent?.trim(), el.getAttribute('aria-label'), el.getAttribute('href')]));
  for (const control of controls) expect(tableControls).toContainEqual(control);
  await page.reload();
  await expect(content).toHaveAttribute('data-view', 'list');
  await page.screenshot({ path: testInfo.outputPath(`${path}-list.png`) });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    return route.fulfill({ json: { ...await response.json(), username: 'different-demo-viewer' } });
  });
  await page.reload();
  await expect(page.getByRole('radio', { name: 'Cards', exact: true })).toBeChecked();
  await page.unroute('**/api/auth/state');
  await page.reload();
  await expect(content).toHaveAttribute('data-view', 'list');
});


test('Audit pages use stable cursors, Previous and filter reset', async ({ page }) => {
  const requests: URLSearchParams[] = [];
  await page.route('**/api/audit?*', route => {
    const params = new URL(route.request().url()).searchParams;
    requests.push(params);
    expect(params.has('offset')).toBe(false);
    const older = params.get('before_id') === '20';
    const row = { id: older ? 19 : 20, ts: '2026-10-29T12:00:00Z', actor: 'alex', action: 'demo.audit', target: null, detail: {} };
    return route.fulfill({ json: { items: [row], has_more: !older, next_before: older ? null : 20 } });
  });
  await page.goto('/audit');
  const next = page.getByRole('button', { name: 'Next', exact: true });
  const previous = page.getByRole('button', { name: 'Previous', exact: true });
  await expect(previous).toBeDisabled();
  await next.click();
  await expect(next).toBeDisabled();
  expect(requests.at(-1)!.get('before_id')).toBe('20');
  await expect(previous).toBeEnabled();
  await previous.click();
  await expect(previous).toBeDisabled();
  await next.click();
  await expect(previous).toBeEnabled();
  await page.getByRole('combobox', { name: 'Type', exact: true }).selectOption('session.');
  await expect(previous).toBeDisabled();
  await expect.poll(() => requests.at(-1)!.get('action')).toBe('session.');
  expect(requests.at(-1)!.has('before_id')).toBe(false);
  await expectAxeClean(page);
});

test('Audit budget errors invite narrower filters and never claim no matches', async ({ page }) => {
  await page.route('**/api/audit?*', route => {
    const filtered = new URL(route.request().url()).searchParams.has('action');
    return route.fulfill(filtered ? { json: { items: [], has_more: false, next_before: null } } : {
      status: 503, json: { detail: 'Audit search reached its time or work limit. Narrow Type, Person or Since, then retry.' },
    });
  });
  await page.goto('/audit');
  await expect(page.getByRole('alert')).toContainText('narrow Type, Person or Since');
  await expect(page.getByText('No entries match.', { exact: true })).toBeHidden();
  await page.getByRole('combobox', { name: 'Type', exact: true }).selectOption('session.');
  await expect(page.getByText('0 entries on this page')).toBeVisible();
  await expect(page.getByText('No entries match.', { exact: true })).toBeVisible();
  await expectAxeClean(page);
});
