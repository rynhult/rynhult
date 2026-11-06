import { expect, type Page } from '@playwright/test';
import { test } from './shell-state';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain, freshStep, loadSecret } from './helpers';

test.use({ presentationChanges: ['layouts', 'ui_shell'], storageState: AUTH_FILE, viewport: { width: 1440, height: 1000 } });
async function enableShell(page: Page) {
  const feature = await (await page.request.get('/api/admin-features')).json();
  let enabled = await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: feature.revision } });
  if (enabled.status() === 403) { await verifyAgain(page); enabled = await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: feature.revision } }); }
  expect(enabled.ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
}
async function openInventory(page: Page, id: number, section: 'sources' | 'locations') {
  const target = new URL(`/projects/${id}`, info().origin);
  const current = new URL(page.url());
  // Hash-only navigation commits the existing view and does not fetch again.
  const fullNavigation = current.origin !== target.origin || current.pathname !== target.pathname || current.search !== target.search;
  const read = fullNavigation ? page.waitForResponse(r => new URL(r.url()).pathname === `/api/source-views/projects/${id}` && r.status() === 200) : undefined;
  await page.goto(`/projects/${id}#project-${section}`);
  if (read) await (await read).finished();
}
test('read-only sources and locations: owner/member, themes, isolation and failure', async ({ page, context }) => {
  test.slow();
  await enableShell(page);
  const { slug } = await gitProject(page, 'cedar-sources');
  const projects = await (await page.request.get('/api/projects')).json();
  const project = projects.find((p: { slug: string }) => p.slug === slug);
  const recorded = await page.request.post(`/api/projects/${project.id}/issue-sources`, { headers: await csrfHeaders(page), data: { remote_url: 'https://example.com/cedar/web-app.git' } });
  expect(recorded.ok(), await recorded.text()).toBeTruthy();
  const dir = '../docs/handoffs/mount-p4-views'; mkdirSync(dir, { recursive: true });
  for (const person of ['owner', 'member']) {
    let id = project.id;
    if (person === 'member') {
      const account = 'p4-view-agent'; mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
      const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
      expect(invited.ok(), await invited.text()).toBeTruthy(); const token = (await invited.json()).link.split('/').pop();
      await context.clearCookies();
      const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
      const joined = await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: 'blair-p4', password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
      expect(joined.ok(), await joined.text()).toBeTruthy();
      expect((await page.request.get(`/api/source-views/projects/${project.id}`)).status()).toBe(404);
      expect(JSON.stringify(await (await page.request.get('/api/source-views/directory')).json())).not.toContain(slug);
      expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
      const created = await page.request.post('/api/projects', { headers: await csrfHeaders(page), data: { slug: 'blair-local-notes', name: 'Blair notes', workspace_path: info().workspace } });
      expect(created.ok(), await created.text()).toBeTruthy(); id = (await created.json()).id;
    }
    for (const theme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
      await openInventory(page, id, 'sources');
      const sources = page.getByRole('region', { name: 'Sources', exact: true });
      await expect(sources).toBeVisible();
      await expect(page.locator('#project-sources')).toBeFocused();
      await expect(sources).toContainText(person === 'owner' ? 'https://example.com/cedar/web-app.git' : 'No repository source is visible');
      if (person === 'owner') await expect(sources).toContainText('Unresolved locator');
      await expect(sources.getByRole('button', { name: 'Add repository', exact: true })).toBeDisabled();
      await expect(sources).toContainText('Connections are not available');
      await sources.scrollIntoViewIfNeeded();
      await expectAxeClean(page);
      await page.screenshot({ path: `${dir}/${person}-${theme}-sources.png`, fullPage: true });
      await openInventory(page, id, 'locations');
      const locations = page.getByRole('region', { name: 'Working locations', exact: true });
      await expect(locations).toBeVisible();
      await expect(locations).toContainText('Recorded location');
      await expect(page.locator('#project-locations')).toBeFocused();
      await expect(locations.getByRole('button', { name: 'Change launch target' })).toBeDisabled();
      await locations.scrollIntoViewIfNeeded();
      await expectAxeClean(page);
      await page.screenshot({ path: `${dir}/${person}-${theme}-locations.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 900 });
      await expect(locations).toBeVisible();
      await locations.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: `${dir}/${person}-${theme}-390.png`, fullPage: true });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    await page.route(`**/api/source-views/projects/${id}`, route => route.fulfill({ status: 503, json: { detail: 'View temporarily unavailable' } }));
    const refused = page.waitForResponse(r => new URL(r.url()).pathname === `/api/source-views/projects/${id}` && r.status() === 503);
    await page.reload();
    await refused;
    await expect(page.getByRole('region', { name: 'Working locations', exact: true }).getByRole('alert')).toContainText("Couldn't read sources and working locations");
    await page.unroute(`**/api/source-views/projects/${id}`);
    await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: false } });
    await page.reload();
    await expect(page.getByRole('region', { name: 'Working locations', exact: true })).toHaveCount(0);
    await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } });
  }
});

test('group view preserves viewer empty state and loses revoked membership', async ({ page, browser }) => {
  test.slow();
  await enableShell(page);
  const account = 'p4-group-agent'; mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invited.ok(), await invited.text()).toBeTruthy(); const token = (await invited.json()).link.split('/').pop();
  const guest = await browser.newContext({ baseURL: info().origin });
  try {
    const begin = await guest.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    expect((await guest.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: 'rowan-p4', password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();
    const users = (await (await page.request.get('/api/users')).json()).users;
    const member = users.find((u: { username: string }) => u.username === 'rowan-p4');
    const owner = users.find((u: { role: string }) => u.role === 'owner');
    const groupResponse = await page.request.post('/api/groups', { headers: await csrfHeaders(page), data: { name: 'Cedar collaborators', maintainer_id: member.id } });
    expect(groupResponse.ok(), await groupResponse.text()).toBeTruthy(); const group = await groupResponse.json();
    const projectResponse = await page.request.post(`/api/groups/${group.id}/projects`, { headers: await csrfHeaders(page), data: { name: 'Cedar group app', slug: 'p4-group-app', owners: [member.id], revision: group.revision } });
    expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy(); const project = await projectResponse.json();
    const change = async (id: number, role: string) => {
      const current = await (await page.request.get(`/api/groups/${group.id}`)).json();
      const result = await page.request.post(`/api/groups/${group.id}/members/${id}`, { headers: await csrfHeaders(page), data: { role, revision: current.revision } });
      expect(result.ok(), await result.text()).toBeTruthy();
    };
    await change(owner.id, 'maintainer');
    const current = await (await page.request.get(`/api/groups/${group.id}`)).json();
    const owners = await page.request.post(`/api/groups/${group.id}/projects/${project.id}/owners`, { headers: await csrfHeaders(page), data: { owners: [owner.id], revision: current.revision } });
    expect(owners.ok(), await owners.text()).toBeTruthy();
    await change(member.id, 'viewer');
    const memberPage = await guest.newPage();
    expect((await memberPage.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(memberPage), data: { enabled: true } })).ok()).toBeTruthy();
    for (const theme of ['dark', 'light'] as const) {
      await memberPage.setViewportSize({ width: 1440, height: 1000 }); await memberPage.emulateMedia({ colorScheme: theme });
      await memberPage.goto(`/groups?group=${group.id}&tab=projects`);
      const dialog = memberPage.getByRole('dialog', { name: 'Cedar collaborators · Projects' });
      await dialog.getByRole('button', { name: 'Sources and locations', exact: true }).click();
      const locations = dialog.getByRole('region', { name: 'Working locations', exact: true });
      await expect(locations).toContainText('Group viewers cannot see private checkouts');
      await locations.scrollIntoViewIfNeeded(); await expectAxeClean(memberPage);
      await memberPage.screenshot({ path: `../docs/handoffs/mount-p4-views/group-viewer-${theme}.png`, fullPage: true });
    }
    await change(member.id, 'remove');
    expect((await memberPage.request.get(`/api/source-views/groups/${group.id}/projects/${project.id}`)).status()).toBe(404);
    await memberPage.reload(); await expect(memberPage.getByText('Cedar group app', { exact: true })).toHaveCount(0);
  } finally { await guest.close(); }
});

test('mounted directory drops cached rows and facets when revalidation is refused', async ({ page }) => {
  await enableShell(page);
  const { slug } = await gitProject(page, 'directory-revalidation');
  let denied = false;
  await page.route('**/api/source-views/directory?*', route => denied
    ? route.fulfill({ status: 403, json: { detail: 'Access changed. Reload before continuing.' } })
    : route.continue());
  const initial = page.waitForResponse(r => new URL(r.url()).pathname === '/api/source-views/directory' && new URL(r.url()).searchParams.get('offset') === '0' && r.status() === 200);
  await page.goto(`/projects?q=${slug}&offset=20`);
  await (await initial).finished();
  const main = page.getByRole('main');
  await expect(main.getByRole('link', { name: slug, exact: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`q=${slug}.*offset=0`));
  await expect(main.getByRole('combobox', { name: 'Working location machine' }).locator('option')).toHaveCount(2);
  denied = true;
  const refused = page.waitForResponse(r => new URL(r.url()).pathname === '/api/source-views/directory' && r.status() === 403);
  // Drive the browser visibility contract: returning to a visible page revalidates
  // stale inventory, without sleeping or controlling React Query's internals.
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    Reflect.deleteProperty(document, 'visibilityState');
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await (await refused).finished();
  await expect(main.getByRole('alert')).toContainText("Couldn't load project sources and locations");
  await expect(main.getByRole('table', { name: 'Project directory' })).toHaveCount(0);
  await expect(main.getByRole('link', { name: slug, exact: true })).toHaveCount(0);
  await expect(main.getByRole('combobox', { name: 'Working location machine' }).locator('option')).toHaveCount(1);
  await page.unrouteAll({ behavior: 'wait' });
});

test('V3: another-audience records show a neutral notice instead of a local-only claim', async ({ page }) => {
  await enableShell(page);
  const p = await gitProject(page, 'transferred-source');
  await page.route(`**/api/source-views/projects/${p.id}`, route => route.fulfill({ json: {
    project_id: p.id, repositories: [], locations: [], counts: { repositories: 0, locations: 0 },
    membership_count: 0, registration_count: 0, counts_scope: 'visible', read_only: true,
    truncated: { repositories: false, locations: false }, audience_mismatch: true,
  } }));
  try {
    await page.goto(`/projects/${p.id}#project-sources`);
    const sources = page.getByRole('region', { name: 'Sources', exact: true });
    await expect(sources).toContainText('recorded under another audience');
    await expect(sources).not.toContainText('Local-only projects can work without one');
    await page.goto(`/projects/${p.id}#project-locations`);
    const locations = page.getByRole('region', { name: 'Working locations', exact: true });
    await expect(locations).toContainText('recorded under another audience');
    await expect(locations).not.toContainText('Group viewers cannot see private checkouts');
    await expectAxeClean(page);
  } finally { await page.unrouteAll({ behavior: 'wait' }); }
});

test('V2: a redacted verified source is not labelled an unresolved locator', async ({ page }) => {
  await enableShell(page);
  const p = await gitProject(page, 'redacted-verified');
  await page.route(`**/api/source-views/projects/${p.id}`, route => route.fulfill({ json: {
    project_id: p.id, repositories: [{ membership_id: 'redacted', membership_ids: ['redacted'], repository_id: 'opaque',
      locator_id: null, locator: null, verification: 'verified', name: null, owner: null, instance: null, compatibility_primary: false }],
    locations: [], counts: { repositories: 1, locations: 0 }, membership_count: 1, registration_count: 0,
    counts_scope: 'visible', read_only: true, truncated: { repositories: false, locations: false }, audience_mismatch: false,
  } }));
  try {
    await page.goto(`/projects/${p.id}#project-sources`);
    const sources = page.getByRole('region', { name: 'Sources', exact: true });
    await expect(sources).toContainText('Repository details are not visible to you');
    await expect(sources).not.toContainText('Unresolved locator');
    await expectAxeClean(page);
  } finally { await page.unrouteAll({ behavior: 'wait' }); }
});

test('reduced-motion section navigation focuses the newly visible working view', async ({ page }) => {
  await enableShell(page);
  const { id } = await gitProject(page, 'section-focus');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openInventory(page, id, 'sources');
  await expect(page.locator('#project-sources')).toBeFocused();
  await openInventory(page, id, 'locations');
  await expect(page.locator('#project-locations')).toBeFocused();
});

test('V4: signing out and Back never retain a private project title', async ({ browser }) => {
  // This journey signs out for real. Never revoke the setup login used by later specs.
  const context = await browser.newContext({ baseURL: info().origin });
  const page = await context.newPage();
  try {
    await freshStep(page);
    const login = await page.request.post('/api/auth/login', { headers: { Origin: info().origin },
      data: { username: 'alex', password: PASSWORD, code: totp(loadSecret()) } });
    expect(login.ok(), await login.text()).toBeTruthy();
    await enableShell(page);
    const p = await gitProject(page, 'private-title');
    let signingOut = false;
    let release!: () => void;
    const drained = new Promise<void>(resolve => { release = resolve; });
    // A racing data-read 401 forces a full document load, which masks the SPA's stale
    // title. Hold those unrelated reads until the sign-in view has committed.
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() !== 'GET' || (path.startsWith('/api/auth/') || path === '/api/health')) return route.continue();
      const response = await route.fetch();
      if (signingOut) { await drained; await route.abort(); }
      else await route.fulfill({ response });
    });
    try {
      await page.goto(`/projects/${p.id}`);
      await expect(page).toHaveTitle(`${p.slug} · Rynhult`);
      await page.evaluate(() => { document.documentElement.dataset.titleProbe = 'same-document'; });
      await page.getByRole('button', { name: /^Account:/ }).click();
      signingOut = true;
      await page.getByRole('button', { name: 'Sign out', exact: true }).click();
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
      await expect(page.locator('html')).toHaveAttribute('data-title-probe', 'same-document');
      await expect(page).toHaveTitle('Rynhult');
      await page.goBack();
      await expect(page).toHaveURL(/\/login$/);
      await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
      await expect(page).toHaveTitle('Rynhult');
      await expect(page.getByRole('heading', { name: p.slug })).toHaveCount(0);
    } finally { release(); await page.unrouteAll({ behavior: 'wait' }); }
  } finally { await context.close(); }
});
