import { expect, test } from './fixtures';
import { AUTH_FILE, csrfHeaders, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE, signedInLayout: 'new', presentationChanges: ['setup_level', 'admin_features'] });

async function seed(page: import('@playwright/test').Page) {
  const headers = await csrfHeaders(page);
  const value = await (await page.request.get('/api/preferences/setup-level')).json();
  const response = await page.request.put('/api/preferences/setup-level', { headers, data: {
    level: 1, dismissed: true, wizard: { revision: value.wizard.revision, step: 'recovery', state: 'completed' },
  } });
  expect(response.ok()).toBeTruthy();
}

test('resumes across browsers; level and unselected/skipped previews preserve policy and features', async ({ page, context }) => {
  await seed(page);
  const security = await (await page.request.get('/api/security/registry')).json();
  const features = await (await page.request.get('/api/admin-features')).json();
  const resourceWrites: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET' && /\/api\/(security|admin-features|projects|nodes|accounts|updates|host\/backups)/.test(request.url())) resourceWrites.push(request.url()); });
  await page.goto('/setup/wizard');
  await expect(page.getByRole('heading', { name: 'Choose level', exact: true })).toBeFocused();
  await page.getByRole('combobox', { name: 'Setup level', exact: true }).selectOption('6');
  await page.getByRole('button', { name: 'Confirm reviewed · Continue', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review protections', exact: true })).toBeFocused();
  await expect(page.getByText('No changes selected.', { exact: false })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Review protections', exact: true })).toBeVisible();
  // Another tab resumes the same record rather than recreating resources.
  const other = await context.newPage();
  await other.goto('/setup/wizard');
  await expect(other.getByRole('heading', { name: 'Review protections', exact: true })).toBeVisible();
  await other.close();
  for (const title of ['Review protections', 'First machine', 'Provider and keys', 'First project and people', 'Backups', 'Updates']) {
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Finish later', exact: true }).click();
  }
  await expect(page.getByRole('heading', { name: 'Choose preview features', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Review selected feature changes', exact: true })).toBeDisabled();
  expect(await page.getByRole('checkbox').count()).toBeGreaterThan(0);
  for (const checkbox of await page.getByRole('checkbox').all()) await expect(checkbox).not.toBeChecked();
  await expect(page.getByText('new_shell', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Finish later', exact: true }).click();
  await expect(page.getByText('V1 supports small teams.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Finish setup', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Setup checklist' })).toContainText('Unfinished:');
  expect(resourceWrites).toEqual([]);
  expect(await (await page.request.get('/api/security/registry')).json()).toEqual(security);
  expect(await (await page.request.get('/api/admin-features')).json()).toEqual(features);
});

for (const width of [320, 390]) for (const theme of ['light', 'dark'] as const) {
  test(`keyboard, announcements and readable steps at ${width} ${theme}`, async ({ page }) => {
    await seed(page); await page.setViewportSize({ width, height: 850 }); await page.emulateMedia({ colorScheme: theme });
    await page.goto('/setup/wizard');
    await expect(page.getByRole('heading', { name: 'Choose level', exact: true })).toBeFocused();
    await expectAxeClean(page);
    await page.getByRole('combobox', { name: 'Setup level', exact: true }).focus();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeFocused();
    await page.keyboard.press('Tab'); await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: 'Review protections', exact: true })).toBeFocused();
    await expect(page.getByRole('list', { name: 'Setup steps' })).toContainText('completed');
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // 200% text zoom reflows without hiding actions or errors.
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await expect(page.getByRole('button', { name: 'Finish later', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('stale progress reloads for review and retains current level', async ({ page }) => {
  await seed(page); await page.goto('/setup/wizard');
  await expect(page.getByRole('heading', { name: 'Choose level', exact: true })).toBeVisible();
  const current = await (await page.request.get('/api/preferences/setup-level')).json();
  expect((await page.request.put('/api/preferences/setup-level', { headers: await csrfHeaders(page), data: { level: 3, dismissed: true, wizard: { revision: current.wizard.revision, step: 'level', state: 'completed' } } })).ok()).toBeTruthy();
  await page.getByRole('button', { name: 'Confirm reviewed · Continue', exact: true }).click();
  await expect(page.getByText('Setup changed in another browser.', { exact: false })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toHaveValue('3');
  expect((await (await page.request.get('/api/preferences/setup-level')).json()).level).toBe(3);
});

test('explicit feature choice confirms one change and preserves every other feature', async ({ page }) => {
  await seed(page); await page.goto('/setup/wizard');
  await page.getByRole('button', { name: /^9\. Choose preview features/ }).click();
  const before = await (await page.request.get('/api/admin-features')).json();
  const remote = before.features.find((row: { id: string }) => row.id === 'remote_access');
  expect(remote.locked).toBe(false);
  const writes: string[] = [];
  page.on('request', request => { if (request.method() === 'PUT' && request.url().includes('/api/admin-features/')) writes.push(request.url()); });
  await page.getByRole('checkbox', { name: /^Direct public remote access/ }).check();
  await page.getByRole('button', { name: 'Review selected feature changes', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Turn off Direct public remote access?' })).toBeVisible();
  expect(writes).toEqual([]);
  await page.getByRole('button', { name: 'Confirm this change', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api/admin-features')).json()).features.find((row: { id: string }) => row.id === 'remote_access').enabled).toBe(false);
  const after = await (await page.request.get('/api/admin-features')).json();
  expect(writes).toHaveLength(1);
  expect(after.features.filter((row: { id: string }) => row.id !== 'remote_access')).toEqual(before.features.filter((row: { id: string }) => row.id !== 'remote_access'));
});

test('locked previews stay unavailable with an explanation', async ({ page }) => {
  await seed(page);
  await page.route('**/api/admin-features', async route => {
    const response = await route.fetch(); const registry = await response.json();
    registry.features = registry.features.map((row: { id: string }) => row.id === 'organizations' ? { ...row, locked: true } : row);
    await route.fulfill({ response, json: registry });
  });
  await page.goto('/setup/wizard');
  await page.getByRole('button', { name: /^9\. Choose preview features/ }).click();
  await expect(page.getByRole('checkbox', { name: /^Company sign-in/ })).toBeDisabled();
  await expect(page.getByText('Locked by server configuration.', { exact: true }).first()).toBeVisible();
  await page.unrouteAll({ behavior: 'wait' });
});

test('existing member gets personal steps and owner explanations without owner registry requests', async ({ page, browser }) => {
  const { info, PASSWORD, totp } = await import('./helpers'); const { mkdirSync } = await import('node:fs');
  const account = `wizard-${Date.now()}-agent`; mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invited.ok()).toBeTruthy(); const token = (await invited.json()).link.split('/').pop();
  const context = await browser.newContext({ baseURL: info().origin });
  try {
    const begin = await context.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    const joined = await context.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `wizard-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
    expect(joined.ok()).toBeTruthy(); const member = await context.newPage(); const ownerRequests: string[] = [];
    member.on('request', request => { if (/\/api\/(admin-features|security\/scopes|nodes|updates|host\/backups)/.test(request.url())) ownerRequests.push(request.url()); });
    await member.goto('/setup/wizard');
    await member.getByRole('button', { name: 'Finish later', exact: true }).click();
    await expect(member.getByRole('heading', { name: 'Choose level', exact: true })).toBeVisible();
    await member.getByRole('button', { name: /^9\. Choose preview features/ }).click();
    await expect(member.getByText('An owner can choose installation preview features.', { exact: false })).toBeVisible();
    await expect(member.getByRole('checkbox')).toHaveCount(0);
    await member.getByRole('button', { name: /^7\. Backups/ }).click();
    await expect(member.getByText('An owner must configure installation backups.', { exact: true })).toBeVisible();
    expect(ownerRequests).toEqual([]); await expectAxeClean(member);
  } finally { await context.close(); }
});

test('selected resources survive resume; deleted projects cannot start a first session', async ({ page }) => {
  const { gitProject } = await import('./helpers'); const project = await gitProject(page, 'wizard-resume');
  await seed(page); await page.goto('/setup/wizard');
  await page.getByRole('button', { name: /^4\. First machine/ }).click();
  await expect(page.getByRole('combobox', { name: 'Machine and account', exact: true })).not.toHaveValue('');
  await page.getByRole('button', { name: 'Confirm reviewed · Continue', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Provider and keys', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Finish later', exact: true }).click();
  await page.getByRole('combobox', { name: 'First project', exact: true }).selectOption(String(project.id));
  await page.getByRole('button', { name: 'Confirm reviewed · Continue', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api/preferences/setup-level')).json()).wizard.project_id).toBe(project.id);
  await page.reload();
  await page.getByRole('button', { name: /^6\. First project/ }).click();
  await expect(page.getByRole('combobox', { name: 'First project', exact: true })).toHaveValue(String(project.id));
  const creations: string[] = [];
  page.on('request', request => { if (request.method() === 'POST' && /\/api\/(projects|sessions|nodes)/.test(request.url())) creations.push(request.url()); });
  // Simulate a resource disappearing between saves without deleting unrelated fixture work.
  await page.route('**/api/projects', route => route.fulfill({ json: [] }));
  await page.reload();
  await page.getByRole('button', { name: /^10\. Review and finish/ }).click();
  await expect(page.getByRole('button', { name: 'Start first session', exact: true })).toBeDisabled();
  await expect(page.getByText('unavailable or binding changed; review project selection', { exact: false })).toBeVisible();
  expect(creations).toEqual([]);
  await page.unrouteAll({ behavior: 'wait' });
});
