import { expect, test, type Page } from './fixtures';
import { mkdirSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, BASE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain } from './helpers';

test.use({ presentationChanges: ['admin_features', 'ui_shell', 'layouts'], storageState: AUTH_FILE });
let sessionId: string | undefined;
test.afterEach(async ({ page }) => {
  // Finish proxy fetch/fulfill callbacks before this page's API context closes.
  // Otherwise a polling response can fail in the next test instead of its owner.
  await page.unrouteAll({ behavior: 'wait' });
  if (sessionId) {
    expect((await page.request.post(`/api/sessions/${sessionId}/stop`, { headers: await csrfHeaders(page) })).ok()).toBeTruthy();
    sessionId = undefined;
  }
});
async function shell(page: Page, enabled: boolean) {
  const h = await csrfHeaders(page);
  const state = await (await page.request.get('/api/admin-features')).json();
  let r = await page.request.put('/api/admin-features/new_shell', { headers: h, data: { enabled: true, revision: state.revision } });
  if (!r.ok()) {
    await verifyAgain(page);
    const fresh = await (await page.request.get('/api/admin-features')).json();
    r = await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: fresh.revision } });
  }
  expect(r.ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled } })).ok()).toBeTruthy();
}
async function screenshot(page: Page, name: string) {
  const dir = `${BASE}/project-pages`;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: true, animations: 'disabled' });
}

for (const theme of ['dark', 'light'] as const) {
  test(`owner directory and overview: filters, keyboard and axe (${theme})`, async ({ page }) => {
    test.setTimeout(90_000);
    await shell(page, true);
    const p = await gitProject(page, `cedar-${theme}`, { name: 'Cedar archive', notes: 'Private workshop notes and restore tools. Build a clear restore preview while keeping the original notes private.' });
    mkdirSync(`${p.folder}/docs/handoffs`, { recursive: true });
    writeFileSync(`${p.folder}/docs/handoffs/2026-11-01-restore-preview.md`, '# Restore preview\nReview the saved notes before restoring.\n');
    const made = await page.request.post('/api/sessions', { headers: p.headers, data: { project_id: p.id, provider: 'claude', name: 'Restore preview' } });
    expect(made.ok()).toBeTruthy(); sessionId = (await made.json()).id;
    await page.emulateMedia({ colorScheme: theme });
    await page.goto('/projects');
    const main = page.getByRole('main');
    await expect(main.getByRole('heading', { name: 'All projects', exact: true })).toBeVisible();
    await expect(main.getByRole('combobox', { name: 'Source owner', exact: true })).toBeVisible();
    for (const name of ['Project', 'Source repositories', 'Working locations', 'Activity']) await expect(main.getByRole('columnheader', { name, exact: true })).toBeVisible();
    const filter = main.getByRole('textbox', { name: 'Find a project or repository' });
    await filter.fill(p.slug);
    const row = main.getByRole('row').filter({ has: page.getByRole('link', { name: p.name, exact: true }) });
    await expect(row).toBeVisible();
    await main.getByRole('combobox', { name: /^Working location/ }).selectOption({ index: 1 });
    await expect(row).toBeVisible();
    await filter.fill('no-project-matches-this');
    await expect(main.getByText('No projects match these filters.')).toBeVisible();
    await filter.fill(p.slug);
    await screenshot(page, `directory-owner-${theme}`);
    await expectAxeClean(page);
    const link = row.getByRole('link', { name: p.name, exact: true });
    await link.focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/projects/${p.id}$`));
    await expect(main.getByRole('tablist', { name: 'Project views' })).toBeVisible();
    await expect(main.getByRole('region', { name: 'Project overview', exact: true })).toBeVisible();
    await expect(main.getByRole('region', { name: 'Sessions', exact: true })).toBeVisible();
    await expect(main.getByRole('region', { name: 'Sessions', exact: true }).getByRole('button', { name: /New session/ })).toHaveCount(0);
    await expect(main.getByRole('button', { name: 'New session', exact: true })).toHaveCount(0);
    await expect(page.locator('#shell-new-session')).toContainText(p.name);
    await expect(main.getByRole('link', { name: 'Continue Restore preview', exact: true })).toBeVisible();
    await main.getByRole('tab', { name: 'Working locations', exact: true }).click();
    await expect(main.getByRole('region', { name: 'Sources', exact: true })).toBeVisible();
    await expect(main.getByRole('region', { name: 'Working locations', exact: true })).toBeVisible();
    await main.getByRole('tab', { name: 'Recent results', exact: true }).click();
    await screenshot(page, `overview-owner-${theme}`);
    await expectAxeClean(page);
    await main.getByRole('region', { name: 'Recent results', exact: true }).getByRole('button', { name: '2026-11-01-restore-preview.md' }).click();
    await expect(page.getByRole('dialog')).toContainText('Review the saved notes before restoring.');
    await page.keyboard.press('Escape');
  });
}

test('classic test preference retains the original projects controls and project overview', async ({ page }) => {
  await shell(page, true);
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: false } })).ok()).toBeTruthy();
  const p = await gitProject(page, 'legacy-project');
  await page.goto('/projects');
  const main = page.getByRole('main');
  await expect(main.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
  await expect(main.getByRole('radiogroup', { name: 'View' })).toBeVisible();
  await expect(main.getByRole('searchbox', { name: 'Filter projects' })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'All projects' })).toHaveCount(0);
  await page.goto(`/projects/${p.id}`);
  await expect(main.getByRole('tablist', { name: 'Project sections' })).toBeVisible();
  await expect(main.getByRole('region', { name: 'Sessions' })).toHaveCount(0);
  await expect(main.getByRole('region', { name: 'Project overview', exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${p.id}#project-backup`);
  const strip = main.getByRole('tablist', { name: 'Project sections' });
  const backup = strip.getByRole('tab', { name: 'Backup', exact: true });
  await expect(backup).toHaveAttribute('aria-selected', 'true');
  const selectedIsRevealed = () => strip.evaluate(el => {
    const selected = el.querySelector('[aria-selected=true]')!;
    const tab = selected.getBoundingClientRect(), box = el.getBoundingClientRect();
    return tab.left >= box.left - 1 && tab.right <= box.right + 1;
  });
  await expect.poll(selectedIsRevealed).toBe(true);
  await backup.focus();
  await page.keyboard.press('Home');
  await expect(strip.getByRole('tab', { name: 'Sessions', exact: true })).toBeFocused();
  await expect.poll(selectedIsRevealed).toBe(true);
  await page.keyboard.press('End');
  await expect(strip.getByRole('tab', { name: 'Runtime', exact: true })).toBeFocused();
  await expect.poll(selectedIsRevealed).toBe(true);
});

test('member uses authorized project data only in both themes', async ({ page, context }) => {
  test.setTimeout(120_000);
  await shell(page, true);
  const ownerProject = await gitProject(page, 'owner-private');
  const account = `pages-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invite.ok()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop();
  await context.clearCookies();
  const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
  expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } })).ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
  const p = await gitProject(page, 'member-notes', { name: 'Field notes', notes: 'Product development and research notes.' });
  const requests: string[] = [];
  page.on('request', request => requests.push(new URL(request.url()).pathname));
  for (const theme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto('/projects');
    const main = page.getByRole('main');
    await expect(main.getByRole('heading', { name: 'All projects', exact: true })).toBeVisible();
    await expect(main.getByRole('link', { name: p.name, exact: true })).toBeVisible();
    await expect(main.getByRole('link', { name: ownerProject.slug, exact: true })).toHaveCount(0);
    await main.getByRole('textbox', { name: 'Find a project or repository' }).fill(p.slug);
    await screenshot(page, `directory-member-${theme}`); await expectAxeClean(page);
    await main.getByRole('link', { name: p.name, exact: true }).click();
    await expect(main.getByRole('region', { name: 'Sessions' })).toBeVisible();
    await main.getByRole('tab', { name: 'Working locations', exact: true }).click();
    await expect(main.getByRole('region', { name: 'Sources', exact: true })).toBeVisible();
    await expect(main.getByRole('region', { name: 'Working locations', exact: true })).toContainText(p.folder);
    await main.getByRole('tab', { name: 'Recent results', exact: true }).click();
    await expect(main.getByRole('region', { name: 'Recent results' })).toContainText('Nothing saved here yet.');
    await screenshot(page, `overview-member-${theme}`); await expectAxeClean(page);
  }
  expect(requests).not.toContain('/api/security/policy');
  expect(requests).not.toContain('/api/security/effective');
  expect(requests).not.toContain('/api/nodes');
  expect(requests).not.toContain('/api/board');
});

test('directory paginates, filters repository owner, and reports unavailable activity', async ({ page }) => {
  await shell(page, true);
  const p = await gitProject(page, 'directory-states');
  const original = (await (await page.request.get('/api/projects')).json()).find((project: { id: number }) => project.id === p.id);
  const rows = Array.from({ length: 25 }, (_, i) => ({ ...original, id: 10000 + i, name: `Directory ${String(i).padStart(2, '0')}`, slug: `directory-${i}`, repo: `${i === 24 ? 'birch' : 'cedar'}/app-${i}`, notes: 'Product development', host: 'acc-host', archived: i === 23, machine_offline: i === 0 }));
  await page.route('**/api/projects', route => route.fulfill({ json: rows }));
  const template = await (await page.request.get(`/api/source-views/projects/${p.id}`)).json();
  await page.route('**/api/source-views/directory?*', route => {
    const params = new URL(route.request().url()).searchParams;
    const owner = params.get('source_owner');
    const query = (params.get('q') ?? '').toLowerCase();
    const matching = rows.filter(row => (params.get('include_archived') === 'true' || !row.archived) && (!owner || row.repo.startsWith(owner + '/')) && `${row.name} ${row.slug} ${row.repo} ${row.notes}`.toLowerCase().includes(query));
    const offset = Number(params.get('offset') ?? 0); const limit = Number(params.get('limit') ?? 20);
    return route.fulfill({ json: { items: matching.slice(offset, offset + limit).map(row => ({ ...template, id: row.id, project_id: row.id, name: row.name, slug: row.slug,
      counts: { repositories: 1, locations: 1 }, membership_count: 1, registration_count: 1,
      repositories: [{ membership_id: `source-${row.id}`, membership_ids: [`source-${row.id}`], repository_id: `repo-${row.id}`, locator_id: null, locator: null, verification: 'verified', name: row.repo.split('/')[1], owner: { id: row.repo.split('/')[0], login: row.repo.split('/')[0] }, instance: { id: 'github', provider: 'github', api_origin: 'https://api.github.com' }, compatibility_primary: true }],
    })), total: matching.length, offset, limit, next_offset: offset + limit < matching.length ? offset + limit : null,
      facets: { source_owners: [...new Set(matching.slice(offset, offset + limit).map(row => row.repo.split('/')[0]))].map(id => ({ id, login: id, instance: { id: 'github', provider: 'github', api_origin: 'https://api.github.com' } })), hosts: [], scope: 'visible_page' } } });
  });
  await page.route('**/api/sessions', route => route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } }));
  await page.goto('/projects'); const main = page.getByRole('main');
  await expect(main.getByText('Page 1 of 2')).toBeVisible();
  await expect(main.getByText('Host offline', { exact: true })).toBeVisible();
  await expect(main.getByText('Activity unavailable', { exact: true }).first()).toBeVisible();
  await main.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(main.getByText('Page 2 of 2')).toBeVisible();
  await main.getByRole('combobox', { name: /^Source owner/ }).selectOption('birch');
  await expect(main.getByText('Page 1 of 1')).toBeVisible();
  await expect(main.getByRole('link', { name: 'Directory 24', exact: true })).toBeVisible();
  await expect(main.getByRole('link', { name: 'Directory 00', exact: true })).toHaveCount(0);
  await main.getByRole('combobox', { name: /^Source owner/ }).selectOption('');
  await expect(main.getByRole('link', { name: 'Directory 00', exact: true })).toBeVisible();
  await main.getByRole('combobox', { name: /^Source owner/ }).selectOption('cedar');
  await main.getByRole('textbox', { name: 'Find a project or repository' }).fill('Directory 23');
  await expect(main.getByText('No projects match these filters.')).toBeVisible();
  await main.getByRole('checkbox', { name: 'Show archived' }).check();
  await expect(main.getByRole('link', { name: 'Directory 23', exact: true })).toBeVisible();
  await page.reload();
  await expect(main.getByRole('checkbox', { name: 'Show archived' })).toBeChecked();
  await expect(main.getByRole('link', { name: 'Directory 23', exact: true })).toBeVisible();
});

test('PP-1: shell overview retains failing CI and repository counts in both themes', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'overview-health');
  await page.route('**/api/projects', async route => {
    const response = await route.fetch(); await route.fulfill({ json: (await response.json()).map((project: { id: number }) => project.id === p.id ? { ...project, repo: 'cedar-labs/archive', remote_url: 'https://github.com/cedar-labs/archive' } : project) });
  });
  await page.route(`**/api/projects/${p.id}/github`, route => route.fulfill({ json: {
    repo: { full_name: 'cedar-labs/archive', default_branch: 'main', html_url: 'https://github.com/cedar-labs/archive' },
    pulls: [{ number: 1, title: 'Restore preview', author: 'alex', head: 'restore', updated_at: new Date().toISOString(), url: 'https://github.com/cedar-labs/archive/pull/1' }],
    issues: [{ number: 2, title: 'Review restore', author: 'alex', labels: [], updated_at: new Date().toISOString(), url: 'https://github.com/cedar-labs/archive/issues/2' }],
    runs: [{ id: 1, name: 'ci', branch: 'main', event: 'push', status: 'completed', conclusion: 'failure', created_at: new Date().toISOString(), url: 'https://github.com/cedar-labs/archive/actions/runs/1' }],
  } }));
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto(`/projects/${p.id}`);
    const overview = page.getByRole('region', { name: 'Project overview', exact: true });
    await expect(overview.getByText(/^CI failing/)).toBeVisible();
    await expect(overview.getByText(/^CI failing/)).toHaveClass(/text-bad/);
    await expect(overview.getByText('1 open pull request', { exact: true })).toBeVisible();
    await expect(overview.getByText('1 open issue', { exact: true })).toBeVisible();
    await expectAxeClean(page);
  }
});

test('PP-2: single-row menu is not clipped and keyboard focus returns; narrow tables still scroll', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'row-menu');
  await page.goto(`/projects?q=${p.slug}`);
  const trigger = page.getByRole('main').getByRole('button', { name: `Project actions for ${p.name}`, exact: true });
  await trigger.focus(); await page.keyboard.press('ArrowDown');
  const menu = page.getByRole('menu', { name: `Project actions for ${p.name}` });
  await expect(menu.getByRole('menuitem', { name: 'Archive', exact: true })).toBeFocused();
  const deletion = menu.getByRole('menuitem', { name: 'Delete', exact: true });
  expect(await deletion.evaluate(el => { const box = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)); })).toBe(true);
  const table = page.getByRole('region', { name: 'Projects table, scroll horizontally' });
  expect(await table.evaluate(el => el.scrollTop)).toBe(0);
  await expectAxeClean(page);
  await page.keyboard.press('Escape'); await expect(trigger).toBeFocused();
  await page.setViewportSize({ width: 900, height: 900 });
  await expect(table).toHaveCSS('overflow-x', 'auto');
  expect(await table.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
});

test('PP-3: read-only project keeps the warning badge tone', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'readonly-tone');
  await page.route('**/api/projects', async route => {
    const response = await route.fetch(); await route.fulfill({ json: (await response.json()).map((project: { id: number }) => project.id === p.id ? { ...project, import_mode: 'readonly' } : project) });
  });
  await page.goto(`/projects?q=${p.slug}`);
  await expect(page.getByRole('main').getByText('Read-only', { exact: true })).toHaveClass(/text-wait/);
});

test('PP-4: directory pluralizes attention and overview names the main host', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'attention-wording');
  const made = await page.request.post('/api/sessions', { headers: p.headers, data: { project_id: p.id, provider: 'claude', name: 'Attention fixture' } });
  expect(made.ok()).toBeTruthy(); const session = await made.json();
  const detail = await (await page.request.get(`/api/sessions/${session.id}`)).json();
  await page.request.post(`/api/sessions/${session.id}/stop`, { headers: p.headers });
  await page.route('**/api/sessions', route => route.fulfill({ json: [0, 1].map(i => ({ ...detail, id: `attention-${i}`, observed_state: 'waiting_input', desired_state: 'running', status_stale_since: null, status_at: new Date().toISOString() })) }));
  await page.route('**/api/auth/state', async route => { const response = await route.fetch(); const auth = await response.json(); await route.fulfill({ json: { ...auth, instance: { ...auth.instance, host: 'cedar-main' } } }); });
  await page.goto(`/projects?q=${p.slug}`);
  await expect(page.getByRole('main').getByText('2 need you', { exact: true })).toBeVisible();
  await page.goto(`/projects/${p.id}`);
  const overview = page.getByRole('region', { name: 'Project overview', exact: true });
  await expect(overview).toContainText('cedar-main');
  await expect(overview).not.toContainText('Main host');
});


test('PP-6: direct and directory navigation keep the overview hero in view', async ({ page }) => {
  await shell(page, true);
  const p = await gitProject(page, 'overview-scroll');
  const main = page.getByRole('main');
  for (const navigation of ['direct', 'directory']) {
    if (navigation === 'direct') await page.goto(`/projects/${p.id}`);
    else {
      await page.goto('/projects');
      await main.getByRole('textbox', { name: 'Find a project or repository' }).fill(p.slug);
      await main.getByRole('link', { name: p.name, exact: true }).click();
    }
    await expect(main.getByRole('tablist', { name: 'Project sections' })).toBeAttached();
    await expect(main.getByText('Checking backup…', { exact: true })).toHaveCount(0);
    await expect.poll(() => main.evaluate(el => el.scrollTop)).toBe(0);
    await expect(main.getByRole('tablist', { name: 'Project sections' })).toBeInViewport();
  }
});

test('PP-7: loading backup status is replaced once without duplicate project keys', async ({ page }) => {
  await shell(page, true);
  const first = await gitProject(page, 'status-before');
  const p = await gitProject(page, 'status-transition');
  const warnings: string[] = [];
  page.on('console', message => {
    if (message.text().includes('Encountered two children with the same key')) warnings.push(message.text());
  });
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/projects/${first.id}/backup`, async route => {
    await held;
    await route.fulfill({ json: { configured: false, enabled: false, repo: null } });
  });
  await page.route(`**/api/projects/${p.id}/backup`, route =>
    route.fulfill({ json: { configured: false, enabled: false, repo: null } }));
  try {
    await page.goto(`/projects/${first.id}`);
    const status = page.getByRole('region', { name: 'Project status', exact: true });
    await expect(status).toHaveCount(1);
    await expect(status).toContainText('Checking backup…');
    await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole('main').getByRole('heading', { name: 'All projects', exact: true, level: 1 })).toBeVisible();
    await page.getByRole('textbox', { name: 'Search projects', exact: true }).fill(p.slug);
    await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${p.name}`) }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${p.id}$`));
    release();
    await expect(status).toHaveCount(1);
    await expect(status).toContainText('Backups need');
    await expect(page.getByText('Checking backup…', { exact: true })).toHaveCount(0);
    expect(warnings).toEqual([]);
  } finally {
    release();
    await page.unrouteAll({ behavior: 'wait' });
  }
});

test('Files begins below compact title and main tabs', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'density-files');
  await page.goto(`/projects/${p.id}?tab=files`);
  const main = page.getByRole('main');
  await expect(main.getByRole('tablist', { name: 'Project views' })).toBeVisible();
  await expect(main.locator('[id^="project-tabs-"][id$="-panel"]')).toBeVisible();
  const y = (await main.locator('[id^="project-tabs-"][id$="-panel"]').boundingBox())!.y;
  console.log('density Files y', y); expect(y).toBeLessThan(220);
  const sidebar = page.getByRole('complementary', { name: 'Context sidebar', exact: true });
  await sidebar.getByRole('link', { name: 'Changes', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(main.getByRole('heading', { name: 'Session changes' })).toBeVisible();
});

test('project sections start directly below the main tabs', async ({ page }) => {
  await shell(page, true); const p = await gitProject(page, 'trim-density');
  await page.goto(`/projects/${p.id}`);
  const tabs = page.getByRole('tablist', { name: 'Project sections' });
  await expect(tabs).toBeVisible();
  const views = page.getByRole('tablist', { name: 'Project views' });
  await expect(views.getByRole('tab')).toHaveText(['Overview', 'Files', 'Changes', 'Preview', 'Recent results', 'Working locations']);
  await expect(tabs.getByRole('tab')).toHaveText(['Sessions', 'Issues & pull requests', 'Gate queue', 'Project brief', 'Knowledge', 'Keys', 'Backup', 'Ports', 'Runtime']);
  const viewsBox = (await views.boundingBox())!;
  const sectionBox = (await tabs.boundingBox())!;
  console.log('project main tabs y', viewsBox.y, 'height', viewsBox.height, 'section tabs y', sectionBox.y);
  expect(sectionBox.y).toBeGreaterThanOrEqual(viewsBox.y + viewsBox.height);
  expect(sectionBox.y - viewsBox.y - viewsBox.height).toBeLessThanOrEqual(24);
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: '../.acc/artifacts/project-page-trim-r2/project-overview-1440-dark.png', animations: 'disabled' });
  const box = (await tabs.boundingBox())!; console.log('project sections y', box.y); expect(box.y).toBeLessThan(230);
  await expect(page.getByRole('region', { name: 'Continue working' })).toHaveCount(0);
  await page.goto(`/projects/${p.id}?section=results`);
  await expect(page.getByRole('tab', { name: 'Recent results', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('region', { name: 'Recent results' })).toBeVisible();
  await views.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expect(tabs).toBeVisible();
  await page.goto(`/projects/${p.id}#project-sources`);
  await expect(views.getByRole('tab', { name: 'Working locations', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('region', { name: 'Sources', exact: true })).toBeVisible();
  await views.getByRole('tab', { name: 'Overview', exact: true }).click();
  await expect(views.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(tabs).toBeVisible();

});
