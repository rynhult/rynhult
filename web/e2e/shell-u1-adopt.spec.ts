import { expect, test, type Page } from './fixtures';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain } from './helpers';
test.use({ presentationChanges: ['admin_features', 'layouts'], storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });

async function member(page: Page) {
  const account = `adopt-${Date.now()}-agent`; mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invite.ok()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop();
  await page.context().clearCookies();
  const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
  expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-adopt-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();

}
function inbox(page: Page) {
  return page.route('**/api/attention**', route => route.fulfill({ json: [{ id: 912, kind: 'needs_input', severity: 'warn', title: 'Choose the restore scope', source: 'claude_hook', verified: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), session_id: 'restore-preview', session_name: 'Restore preview', project: 'cedar', host: 'acc-host', acked_at: null, resolved_at: null }] }));
}
test('U1: Settings section wrappers adopt surfaces', async ({ page }) => {
   await page.goto('/settings#passkeys');
  await expect(page.locator('[data-settings-section="Passkeys"] .shell-surface')).toBeVisible();
});
test('U2: Providers adopts page chrome and surfaces', async ({ page }) => {
   await page.goto('/providers');
  await expect(page.locator('[data-shell-page] .shell-workbar h1')).toHaveText('Providers');
  await expect(page.locator('main .shell-surface').filter({ visible: true }).first()).toBeVisible();
});
test('U3: workbar never invents a product breadcrumb', async ({ page }) => {
   await page.goto('/sessions');
  await expect(page.locator('.shell-workbar h1')).toBeVisible();
  await expect(page.locator('.shell-workbar .shell-page-meta').getByText('Rynhult', { exact: true })).toHaveCount(0);
});
test('U4: batch Hosts and categorized Inbox adopt primitives', async ({ page }) => {
   await inbox(page);
  for (const route of ['/hosts', '/inbox']) {
    await page.goto(route); await expect(page.locator('[data-shell-page] .shell-workbar h1')).toBeVisible();
    await expect(page.locator('main .shell-surface').filter({ visible: true }).first()).toBeVisible();
  }
});
for (const role of ['owner', 'member'] as const) test(`${role}: adoption light/dark, keyboard, axe and reload checks`, async ({ page }) => {
  test.setTimeout(300_000);  if (role === 'member') await member(page);
  await inbox(page); await page.addInitScript(() => localStorage.removeItem('acc-theme'));
  const sections = ['passkeys', 'access-devices', 'password', 'sign-ins', 'keys', 'security', 'notifications', ...(role === 'owner' ? ['tool-approvals', 'budgets', 'updates', 'admin-features'] : [])];
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme });
    for (const section of sections) {
      await page.goto(`/settings#${section}`);
      const content = page.locator(`#${section}[data-settings-section]`);
      await expect(content).toBeVisible();
      await expect(content.locator('.shell-surface').first()).toBeVisible();
      expect(await content.locator('.shell-surface').first().evaluate(el => getComputedStyle(el).borderRadius)).toBe('0px');
      await expectAxeClean(page);
    }
    for (const route of ['/providers', '/hosts', '/inbox']) {
      await page.goto(route); await expect(page.locator('[data-shell-page] .shell-workbar h1')).toBeVisible();
      await expect(page.locator('main .shell-surface').filter({ visible: true }).first()).toBeVisible(); await expectAxeClean(page);
      const control = page.getByRole('main').locator('button:not(:disabled), a[href], select:not(:disabled), input:not(:disabled)').filter({ visible: true }).first();
      await control.focus(); await expect(control).toBeFocused(); await page.keyboard.press('Tab');
      expect(await control.evaluate(el => el !== document.activeElement)).toBe(true);
    }

    for (const route of ['/settings#passkeys', '/settings#access-devices', '/providers', '/hosts', '/inbox']) {
      await page.goto(route); await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
      await expect(page.locator('main .shell-surface').filter({ visible: true }).first()).toBeVisible();
      expect(await page.getByRole('main').getByRole('heading', { level: 1 }).evaluate(el => getComputedStyle(el).fontSize)).toBe('20px');
      if (route.startsWith('/settings')) expect(await page.locator(`${route.includes('passkeys') ? '#passkeys' : '#access-devices'} > section`).first().evaluate(el => getComputedStyle(el).borderRadius)).toBe('0px');
    }
  }
});

test('A1: provider breadcrumb does not present an unknown URL machine as real context', async ({ page }) => {
   await page.goto('/providers?machine=%3Cfake-node%3E');
  await expect(page.locator('.shell-workbar h1')).toHaveText('Providers');
  await expect(page.locator('.shell-workbar')).not.toContainText('<fake-node>');
});

test('A2: all Settings sections do not claim to be Account settings', async ({ page }) => {
   await page.goto('/settings#all');
  await expect(page.locator('.shell-workbar h1')).toHaveText('Settings');
  await expect(page.locator('.shell-workbar .shell-page-meta')).toHaveCount(0);
});

test('A3: owner provider software and endpoint panels follow shell surfaces only', async ({ page }) => {

  const flags = await (await page.request.get('/api/admin-features')).json();
  expect((await page.request.put('/api/admin-features/local_models', { headers: await csrfHeaders(page), data: { enabled: true, revision: flags.revision } })).ok()).toBeTruthy();
  for (const reload of [false, true]) {
    await page.goto('/providers'); if (reload) await page.reload();
    for (const name of ['Other software', 'Model endpoints']) {
      const panel = page.getByRole('heading', { name, exact: true }).locator('xpath=ancestor::section[1]');
      await expect(panel).toBeVisible();
      await expect(panel).toHaveCSS('border-radius', '0px');
    }
  }
});

test('S-5: project settings call the repository section Issues & PRs', async ({ page }) => {
   const project = await gitProject(page, 'cedar-scopes');
  await page.goto('/settings#project-settings');
  await page.getByRole('main').getByRole('combobox', { name: 'Settings project' }).selectOption(String(project.id));
  await expect(page.getByRole('main').getByRole('link', { name: 'Issues & PRs', exact: true })).toHaveAttribute('href', `/projects/${project.id}#project-repository`);
});

const unchecked = '/providers?machine=%3Cfake-node%3E';
async function expectNeutralBody(page: Page) {
  const main = page.getByRole('main');
  await expect(main.getByText(/on the selected machine, for sessions running as/i).first()).toBeVisible();
  await expect(main.getByText(/no connected machine called <fake-node>/i).first()).toBeVisible();
  await expect.poll(async () => (await main.innerText()).replace(/no connected machine called <fake-node>/gi, '')).not.toContain('<fake-node>');
}
test('PL2: Providers body names an unknown URL machine only in its error', async ({ page }) => {
   await page.goto(unchecked); await expectNeutralBody(page);
});
test('PL2: a member gets the same neutral body', async ({ page }) => {
  test.slow();  await member(page); await page.goto(unchecked); await expectNeutralBody(page);
});
test('PL2: neutral provider text survives reload', async ({ page }) => {
    await page.goto(unchecked);
  await page.reload(); await expectNeutralBody(page);

});
test('PL3: only an active machine counts as known in the breadcrumb', async ({ page }) => {

  await page.route('**/api/nodes', route => route.fulfill({ json: [
    { id: 'n1', name: 'build-2', status: 'active', connected: true, last_seen: null, info: {} },
    { id: 'n2', name: 'gone-1', status: 'revoked', connected: false, last_seen: null, info: {} },
    { id: 'n3', name: 'new-3', status: 'pending', connected: false, last_seen: null, info: {} },
  ] }));
  const crumb = page.locator('.shell-workbar .shell-page-meta');
  await page.goto('/providers?machine=build-2'); await expect(crumb).toContainText('On build-2,');
  for (const name of ['gone-1', 'new-3']) {
    await page.goto(`/providers?machine=${name}`);
    await expect(crumb).toContainText('On the selected machine,');
    await expect.poll(async () => page.getByRole('main').getByText(`On ${name}`).count()).toBe(0);
  }
  await page.unrouteAll({ behavior: 'wait' });
});
