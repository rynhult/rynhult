import { expect, test, type Page } from '@playwright/test';
import { AUTH_FILE, demoSessions, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
const project = { id: 91, name: 'Demo API', slug: 'demo-api', account: 'alex-agent', host: 'local',
  workspace_path: '/srv/acc-demo/demo-api', repo: 'example-org/demo-api', remote_url: 'https://github.com/example-org/demo-api',
  import_mode: 'managed', trusted: true, archived: false, notes: null, compose_autostart: false,
  compose_approved: true, git_backup: true };
const backup = { enabled: true, configured: true, last_at: new Date().toISOString(), checked_at: Date.now() / 1000,
  repo: { branches: [{ name: 'main', sha: '123', date: null }], tags: 0, size: 100 } };
async function fixture(page: Page, overrides = {}) {
  // This fixture covers the baseline tab layout; v2 Gate queue tabs are exercised in spec 77.
  await page.route('**/api/auth/state', async r => {
    const response = await r.fetch();
    const state = await response.json();
    await r.fulfill({ response, json: { ...state, instance: { ...state.instance, orchestrator: false } } });
  });
  await page.route('**/api/projects', r => r.fulfill({ json: [{ ...project, ...overrides }] }));
  await page.route('**/api/sessions', r => r.fulfill({ json: [{ ...demoSessions()[0], project_id: 91 }] }));
  await page.route('**/api/projects/91/github*', r => r.fulfill({ json: { fetched_at: Date.now()/1000,
    repo: { full_name: project.repo, default_branch: 'main' }, pulls: [{ number: 42, title: 'Demo change', url: 'https://example.com/pr/42', head: 'demo', author: 'alex', updated_at: new Date().toISOString() }], issues: [{ number: 17, title: 'Demo issue', url: 'https://example.com/issues/17', labels: [], updated_at: new Date().toISOString() }], commits: [], branches: [],
    runs: [{ id: 1, name: 'Checks', branch: 'main', status: 'completed', conclusion: 'failure', event: 'push',
      created_at: new Date().toISOString(), url: 'https://example.com/run' }] } }));
  await page.route('**/api/projects/91/keys', r => r.fulfill({ json: { keys: [], available: [] } }));
  await page.route('**/api/projects/91/backup', r => r.fulfill({ json: backup }));
  await page.route('**/api/projects/91/runtime', r => r.fulfill({ json: { containers: [{ Service: 'web', Name: 'demo-web', State: 'running', Status: 'Up', Health: 'healthy' }], compose: { approved: true, digest: 'demo' } } }));
  await page.route('**/api/ports', r => r.fulfill({ json: [] }));
  await page.route('**/api/containers', r => r.fulfill({ json: { enabled: false, accounts: [], member_defaults: {},
    qualification: { passed: false, reason: 'this example host has no qualification record' } } }));
  await page.route('**/api/projects/91/knowledge', r => r.fulfill({ json: { groups: [], files: [] } }));
  await page.route('**/api/projects/91/issue-sources', r => r.fulfill({ json: {} }));
}

// The Overview's sections are tabs nested under Overview/Files/Preview (UX follow-up to W7): one panel at a time.
const sections = (page: Page) => page.getByRole('tablist', { name: 'Project sections', exact: true });
const sectionTab = (page: Page, name: string) => sections(page).getByRole('tab', { name, exact: true });

for (const width of [390, 1440]) for (const theme of ['dark', 'light'] as const) {
  test(`Project sections are nested tabs, one panel at a time, at ${width}px in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript(t => localStorage.setItem('acc-theme', t), theme);
    await fixture(page);
    await page.goto('/projects/91');
    await expect(page.getByRole('region', { name: 'Project summary', exact: true })).toContainText('CI failing');
    await expect(sectionTab(page, 'Sessions')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Pull requests', exact: true })).toBeHidden();
    for (const section of ['repository', 'knowledge', 'keys', 'backup', 'ports', 'stack']) await expect(page.locator(`#project-${section}`)).toBeHidden();
    expect(await sections(page).getByRole('tab').allTextContents()).toEqual(
      ['Sessions', 'Issues and pull requests', 'Knowledge', 'Keys', 'Backup', 'Ports', 'Runtime']);
    // Keyboard: the arrow keys move along the tab list and show that section; the address follows.
    await sectionTab(page, 'Sessions').focus();
    await page.keyboard.press('ArrowRight');
    await expect(sectionTab(page, 'Issues and pull requests')).toBeFocused();
    await expect(sectionTab(page, 'Issues and pull requests')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('heading', { name: 'Pull requests', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeHidden();
    await expect(page).toHaveURL(/#project-repository$/);
    await expect(sectionTab(page, 'Issues and pull requests')).toHaveCSS('outline-style', 'solid');
    await expectAxeClean(page);
    await sectionTab(page, 'Ports').click();
    await expect(page.getByRole('button', { name: '+ Dev port', exact: true })).toBeVisible();
    await expect(page).toHaveURL(/#project-ports$/);
    // A link to a section (a bookmark, the status line) opens that tab and puts focus on it, even from Files.
    await page.goto('/projects/91?tab=files#project-ports');
    await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(sectionTab(page, 'Ports')).toHaveAttribute('aria-selected', 'true');
    await expect(sectionTab(page, 'Ports')).toBeFocused();
    await expect(page.getByRole('button', { name: '+ Dev port', exact: true })).toBeVisible();
    await page.reload();
    await expect(sectionTab(page, 'Ports')).toHaveAttribute('aria-selected', 'true');
    await expect(sectionTab(page, 'Ports')).toBeFocused();
    await page.goto('/projects/91#project-settings');  // the old Project settings heading opens Keys
    await expect(sectionTab(page, 'Keys')).toHaveAttribute('aria-selected', 'true');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    for (const name of ['Files', 'Preview']) await expect(page.getByRole('tab', { name, exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Wiki', exact: true })).toBeVisible();
    await expectAxeClean(page);
    await page.getByRole('tab', { name: 'Files', exact: true }).click();
    await expect(page.getByRole('tab', { name: 'Files', exact: true })).toHaveAttribute('aria-selected', 'true');
  });
}

test('Project warnings stay visible while their sections are on other tabs', async ({ page }) => {
  await fixture(page, { containers: true, compose_autostart: true });
  await page.route('**/api/projects/91/backup', r => r.fulfill({ json: { ...backup, error: 'Example backup failed', last_at: null } }));
  await page.route('**/api/projects/91/runtime', r => r.fulfill({ json: { containers: [], note: 'Example runtime unavailable', compose: { approved: false, digest: 'changed' } } }));
  await page.goto('/projects/91');
  const attention = page.getByRole('region', { name: 'Project status', exact: true });
  for (const text of ['Example backup failed', 'Example runtime unavailable', 'Autostart', 'Containers are off']) await expect(attention).toContainText(text);
  for (const section of ['backup', 'stack']) await expect(page.locator(`#project-${section}`)).toBeHidden();
  await expectAxeClean(page);
  // Its status links open the section's tab.
  await attention.getByRole('link', { name: 'Backup', exact: true }).first().click();
  await expect(sectionTab(page, 'Backup')).toHaveAttribute('aria-selected', 'true');
  await expect(sectionTab(page, 'Backup')).toBeFocused();
  await expect(page.locator('#project-backup')).toBeVisible();
});

test('Project configuration unavailable is never an empty or healthy result', async ({ page }) => {
  await fixture(page);
  let recovered = false;
  for (const path of ['backup', 'runtime', 'keys']) await page.route(`**/api/projects/91/${path}`, r => path === 'backup' && recovered ? r.fulfill({ json: backup }) : r.fulfill({ status: 503, json: { detail: 'Example unavailable' } }));
  await page.goto('/projects/91');
  const attention = page.getByRole('region', { name: 'Project status', exact: true });
  await expect(attention.getByRole('alert')).toHaveCount(3);
  await expect(attention).toContainText("Couldn't read");
  await expect(page.locator('#project-backup')).toBeHidden();
  recovered = true;
  await attention.getByRole('alert').filter({ hasText: "Couldn't read the backup" }).getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(attention.getByRole('alert')).toHaveCount(2);
  await expect(attention).toContainText('Last backup');
});

test('Readonly and offline projects retain scope and safe launch boundaries', async ({ page }) => {
  await fixture(page, { import_mode: 'readonly', trusted: false, machine_offline: true });
  await page.goto('/projects/91');
  await expect(page.getByRole('button', { name: 'New session', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^Review pull request/ })).toHaveCount(0);
  for (const section of ['ports', 'stack']) await expect(page.locator(`#project-${section}`)).toHaveCount(0);
  await expect(page.getByText('Machine offline', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Wiki', exact: true })).toBeVisible();
});


test('Project settings retain key drafts and send grants only for this project', async ({ page }) => {
  await fixture(page);
  const key = { id: 'demo-key', name: 'Example key', variable: 'OPENAI_API_KEY', owner: 'alex', mine: true,
    shared: false, remote: false, note: null, projects: [], mcp: [], created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_used_at: null };
  let given = false;
  await page.route('**/api/projects/91/keys', r => r.fulfill({ json: { keys: given ? [key] : [], available: given ? [] : [key] } }));
  await page.route('**/api/vault/keys/demo-key/give', r => {
    expect(r.request().postDataJSON()).toEqual({ project_id: 91 });
    given = true;
    return r.fulfill({ json: {} });
  });
  await page.goto('/projects/91#project-keys');
  const keys = page.locator('#project-keys');
  await page.getByRole('combobox', { name: 'Key to give', exact: true }).selectOption('demo-key');
  await sectionTab(page, 'Ports').click();  // another tab and back: the draft is kept
  await expect(keys).toBeHidden();
  await sectionTab(page, 'Keys').click();
  await expect(page.getByRole('combobox', { name: 'Key to give', exact: true })).toHaveValue('demo-key');
  expect(given).toBe(false);
  await keys.getByRole('button', { name: 'Give', exact: true }).click();
  await expect(keys.getByRole('list', { name: 'Keys given to this project' })).toContainText('Example key');
});

test('Members do not acquire runtime controls or owner runtime queries', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/auth/state', async r => {
    const response = await r.fetch();
    return r.fulfill({ response, json: { ...await response.json(), role: 'member' } });
  });
  let calls = 0;
  await page.route('**/api/projects/91/runtime', r => { calls++; return r.fulfill({ json: { containers: [] } }); });
  await page.goto('/projects/91');
  await expect(page.getByRole('heading', { name: 'Demo API', exact: true })).toBeVisible();
  await expect(page.locator('#project-stack')).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: 'Autostart', exact: true })).toHaveCount(0);
  await expect(sectionTab(page, 'Runtime')).toHaveCount(0);
  expect(calls).toBe(0);
});

for (const enabled of [false, true]) test(`Empty backup and stopped runtime remain visible (backup ${enabled})`, async ({ page }) => {
  await fixture(page);
  await page.route('**/api/projects/91/backup', r => r.fulfill({ json: { ...backup, enabled, repo: null } }));
  await page.route('**/api/projects/91/runtime', r => r.fulfill({ json: { containers: [] } }));
  await page.goto('/projects/91');
  const attention = page.getByRole('region', { name: 'Project status', exact: true });
  await expect(attention).toContainText(enabled ? 'No branches recorded' : 'Backup off. No copy recorded');
  await expect(attention).toContainText('Stack not running');
  for (const section of ['backup', 'stack']) await expect(page.locator(`#project-${section}`)).toBeHidden();
});

test('Runtime still requires compose review and the exact project digest', async ({ page }) => {
  await fixture(page);
  const digest = 'a'.repeat(64);
  let posted: unknown, enabled = false;
  await page.route('**/api/projects', r => r.fulfill({ json: [{ ...project, compose_autostart: enabled }] }));
  await page.route('**/api/projects/91/runtime', r => {
    if (r.request().method() === 'POST') {
      posted = r.request().postDataJSON(); enabled = true;
      return r.fulfill({ json: {} });
    }
    return r.fulfill({ json: { containers: [{ Service: 'web', Name: 'demo-web', State: 'running', Status: 'Up' }],
      compose: { approved: enabled, digest, diff: '--- /dev/null\n+++ now/compose.yaml\n+services:\n+  web:\n+    image: nginx\n' } } });
  });
  await page.goto('/projects/91#project-stack');
  await page.getByRole('checkbox', { name: 'Autostart', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: "Approve Demo API's compose files?", exact: true });
  await expect(dialog.getByRole('region', { name: 'Compose approval', exact: true })).toContainText('image: nginx');
  await expect(dialog).toContainText("can reach that person's own files");
  expect(posted).toBeUndefined();
  await dialog.getByRole('button', { name: 'Approve and turn on autostart', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(posted).toEqual({ compose_autostart: true, digest });
});

test('Changing projects with the same section hash opens the new project section tab', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/projects', r => r.fulfill({ json: [project, { ...project, id: 92, name: 'Demo Web', slug: 'demo-web' }] }));
  await page.goto('/projects/91#project-keys');
  await expect(sectionTab(page, 'Keys')).toHaveAttribute('aria-selected', 'true');
  // A history transition keeps the ProjectPage component mounted, unlike a reload.
  await page.evaluate(() => {
    history.pushState(history.state, '', '/projects/92#project-keys');
    dispatchEvent(new PopStateEvent('popstate', { state: history.state }));
  });
  await expect(page.getByRole('heading', { name: 'Demo Web', exact: true })).toBeVisible();
  await expect(page.locator('#project-keys')).toBeVisible();
  await expect(sectionTab(page, 'Keys')).toHaveAttribute('aria-selected', 'true');
  await expect(sectionTab(page, 'Keys')).toBeFocused();
});

for (const theme of ['dark', 'light'] as const) test(`Repeated project tab selection cannot suppress section-link focus in ${theme}`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: theme });
  await fixture(page);
  await page.goto('/projects/91#project-sessions');
  await expect(sectionTab(page, 'Sessions')).toBeFocused();
  await sectionTab(page, 'Sessions').click(); // same hash: must not poison the next navigation
  const backupLink = page.getByRole('region', { name: 'Project status' }).getByRole('link', { name: 'Backup', exact: true }).first();
  await backupLink.click();
  await expect(sectionTab(page, 'Backup')).toBeFocused();
  await backupLink.click(); // link to the already selected section must restore focus too
  await expect(sectionTab(page, 'Backup')).toBeFocused();
  await page.keyboard.press('Home');
  await expect(sectionTab(page, 'Sessions')).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(sectionTab(page, 'Runtime')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(sectionTab(page, 'Sessions')).toBeFocused();
  await page.keyboard.press('End');
  await expect(sectionTab(page, 'Runtime')).toHaveAttribute('aria-selected', 'true');
  await expect(sectionTab(page, 'Runtime')).toBeFocused();
  await expect(page.getByRole('tabpanel', { name: 'Runtime', exact: true })).toBeVisible();
  await page.screenshot({ path: `../.acc/artifacts/ux-followups-review/project-${theme}-390.png` });
  await expectAxeClean(page);
});

for (const width of [390, 1440]) test(`Git author is editable in Repository at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  let identity = { supported: true, name: 'Casey Example', email: 'casey@example.com' };
  await page.route('**/api/projects/91/git-identity', async route => {
    if (route.request().method() === 'PUT') identity = { supported: true, ...route.request().postDataJSON() };
    await route.fulfill({ json: identity });
  });
  await page.goto('/projects/91#project-repository');
  const name = page.getByRole('textbox', { name: 'Git author name', exact: true });
  await expect(name).toHaveValue('Casey Example');
  await name.fill('Project author');
  await page.getByRole('textbox', { name: 'Git author email', exact: true }).fill('project@example.com');
  await page.getByRole('button', { name: 'Save Git author' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('status').filter({ hasText: 'Git author saved.' })).toBeVisible();
  await expect(name).toHaveValue('Project author');
  expect(identity).toEqual({ supported: true, name: 'Project author', email: 'project@example.com' });
  await expectAxeClean(page);
});
