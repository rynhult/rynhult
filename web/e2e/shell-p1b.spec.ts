import { test } from './shell-state';
import { expect, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, expectAxeClean, info, PASSWORD, totp, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE });
async function rollout(page: Page, enabled = true) {
  const put = async () => {
    const state = await (await page.request.get('/api/admin-features')).json();
    return page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled, revision: state.revision } });
  };
  let result = await put();
  if (enabled && result.status() === 403 && (await result.json()).detail?.startsWith('Verify your sign-in')) {
    await verifyAgain(page); result = await put();
  }
  expect(result.ok()).toBeTruthy();
  await preference(page, enabled);
}
async function preference(page: Page, enabled: boolean) {
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled } })).ok()).toBeTruthy();
}
async function project(page: Page, prefix: string) {
  const p = await gitProject(page, prefix);
  mkdirSync(`${p.folder}/src`, { recursive: true });
  writeFileSync(`${p.folder}/src/alpha notes #1.ts`, 'export const cedar = 1;\n');
  writeFileSync(`${p.folder}/src/beta notes.ts`, 'export const cedar = 2;\n');
  writeFileSync(`${p.folder}/content-only.txt`, 'UNIQUE_CONTENT_NEEDLE\n');
  return p;
}
const dialog = (page: Page) => page.getByRole('dialog', { name: 'Find project files', exact: true });
const input = (page: Page) => dialog(page).getByRole('searchbox', { name: 'Filename', exact: true });
async function open(page: Page) {
  await expect(page.locator('#shell-header')).toBeVisible();
  const overflow = page.getByRole('button', { name: 'Header tools', exact: true });
  if (await overflow.isVisible() && !await page.getByRole('button', { name: /^Find project files/ }).isVisible()) await overflow.click();
  await page.getByRole('button', { name: /^Find project files/ }).click();
  await expect(dialog(page)).toBeVisible();
}
async function shot(page: Page, name: string) {
  if (process.env.ACC_UPDATE_SHELL_SCREENSHOTS) await page.screenshot({ path: `../docs/wiki/.attachments/shell-file-search-${name}.png`, animations: 'disabled' });
}

test('project filename search stays scoped while browsing; keyboard opens encoded Files deep links and preserves dirty guards', async ({ page }) => {
  await rollout(page);
  const a = await project(page, 'cedar-search'); const b = await project(page, 'birch-search');
  await page.goto(`/projects/${a.id}`);
  await expect(page.getByRole('button', { name: 'Find project files', exact: true })).toBeVisible();
  await page.keyboard.press('Control+Shift+f'); await expect(dialog(page)).toBeVisible(); await page.keyboard.press('Escape');
  await page.getByRole('link', { name: 'Back to all projects', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search projects', exact: true }).fill(b.slug);
  await page.getByRole('region', { name: 'Available projects' }).getByRole('link', { name: new RegExp(`^${b.slug}`) }).click();
  await expect(page.getByRole('main').getByRole('heading', { name: b.slug, exact: true, level: 1 })).toBeVisible();
  await page.keyboard.press('Control+Shift+f');
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByRole('combobox', { name: 'Search project', exact: true })).toHaveValue(String(a.id));
  await expect(dialog(page)).toContainText(a.folder);
  await input(page).fill('notes');
  await expect(dialog(page).getByRole('link', { name: 'src/alpha notes #1.ts', exact: true })).toBeVisible();
  await input(page).focus(); await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown');
  await expect(dialog(page).getByRole('link', { name: 'src/beta notes.ts', exact: true })).toBeFocused();
  await page.keyboard.press('ArrowUp'); await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp(`/projects/${a.id}\\?tab=files&file=src%2Falpha%20notes%20%231.ts$`));
  const editor = page.getByRole('textbox', { name: 'File content' });
  await expect(editor).toContainText('export const cedar = 1;');
  await open(page);
  await dialog(page).getByRole('combobox', { name: 'Search project', exact: true }).selectOption(String(b.id));
  await expect(dialog(page)).toContainText(b.folder);
  await input(page).fill('alpha');
  await dialog(page).getByRole('link', { name: 'src/alpha notes #1.ts', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${b.id}\\?tab=files&file=`));
  await page.goBack(); await expect(editor).toContainText('export const cedar = 1;');
  await open(page);
  await expect(dialog(page).getByRole('combobox', { name: 'Search project', exact: true })).toHaveValue(String(b.id));
  await dialog(page).getByRole('combobox', { name: 'Search project', exact: true }).selectOption(String(a.id));
  await page.keyboard.press('Escape');
  await editor.fill('keep this unsaved edit');
  await open(page); await input(page).fill('beta');
  await dialog(page).getByRole('link', { name: 'src/beta notes.ts', exact: true }).click();
  const guard = page.getByRole('dialog', { name: 'Discard unsaved edits?' });
  await expect(guard).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/projects/${a.id}\\?tab=files&file=src%2Fbeta`));
  await page.goBack();
  await expect(editor).toContainText('keep this unsaved edit');
  await page.getByRole('button', { name: 'Close view', exact: true }).click();
  await expect(guard).toBeVisible(); await guard.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(editor).toContainText('keep this unsaved edit');
  await open(page); await input(page).fill('UNIQUE_CONTENT_NEEDLE');
  await expect(dialog(page)).toContainText('No filenames match');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: /^Find project files/ })).toBeFocused();
});

test('explicit project selection, bounded debounce, cancellation and truthful partial/unavailable/error states', async ({ page, context }) => {
  await rollout(page);
  const a = await project(page, 'states-search');
  await page.goto('/projects'); await open(page);
  await expect(dialog(page)).toContainText('Choose a project');
  await expect(input(page)).toBeDisabled();
  await dialog(page).getByRole('combobox', { name: 'Search project', exact: true }).selectOption(String(a.id));
  const searches: string[] = [];
  let busy = true;
  await page.route(`**/api/projects/${a.id}/files/search?*`, async route => {
    const url = new URL(route.request().url()); searches.push(url.searchParams.get('q')!);
    expect(url.searchParams.get('limit')).toBe('20');
    const q = url.searchParams.get('q');
    if (q === 'offline') return route.fulfill({ status: 503, json: { state: 'unavailable', detail: 'Machine offline. Wait for Cedar to reconnect.' } });
    if (q === 'invalid') return route.fulfill({ status: 422, json: { detail: 'This query is not supported.' } });
    if (q === 'missing') return route.fulfill({ status: 404, json: { detail: 'Project not found.' } });
    if (q === 'busy' && busy) return route.fulfill({ status: 429, headers: { 'Retry-After': '1' }, json: { state: 'busy', detail: 'File search is busy for this account. Try again shortly.' } });
    if (q === 'slow') await new Promise(resolve => setTimeout(resolve, 700));
    await route.fulfill({ json: { results: q === 'empty' || q === 'partial-empty' ? [] : [{ name: `${q}.ts`, path: `${q}.ts`, kind: 'file' }], visited: 10000, truncated: q === 'partial' || q === 'partial-empty', reason: 'entry_limit' } });
  });
  await input(page).pressSequentially('partial', { delay: 15 });
  await expect(dialog(page)).toContainText('Searching filenames');
  await expect(dialog(page)).toContainText('Partial results');
  expect(searches).toEqual(['partial']);
  await expectAxeClean(page);
  await input(page).fill('partial-empty');
  await expect(dialog(page)).toContainText('No matches in the searched portion');
  await input(page).fill('offline');
  await expect(dialog(page)).toContainText('Machine offline. Wait for Cedar to reconnect.');
  await expect(dialog(page).getByRole('link')).toHaveCount(0);
  await expectAxeClean(page);
  await input(page).fill('invalid'); await expect(dialog(page)).toContainText('This query is not supported.');
  await input(page).fill('missing'); await expect(dialog(page)).toContainText('Project not found.');
  await input(page).fill('busy');
  await expect(dialog(page)).toContainText('File search is busy for this account. Try again shortly.');
  await expect(dialog(page)).not.toContainText('No filenames match');
  await expect(dialog(page)).not.toContainText('Search unavailable');
  await expect(dialog(page).getByRole('link')).toHaveCount(0);
  busy = false;
  await dialog(page).getByRole('button', { name: 'Try search again', exact: true }).click();
  await expect(dialog(page).getByRole('link', { name: 'busy.ts', exact: true })).toBeVisible();
  await input(page).fill('slow'); await expect.poll(() => searches.includes('slow')).toBe(true);
  await input(page).fill('empty'); await expect(dialog(page)).toContainText('No filenames match');
  await expect(dialog(page).getByRole('link')).toHaveCount(0);
  await context.setOffline(true); await input(page).fill('network');
  await expect(dialog(page)).toContainText('Search paused');
  await expect(dialog(page)).not.toContainText('No filenames match');
  await context.setOffline(false);
  await expect(dialog(page).getByRole('link', { name: 'network.ts', exact: true })).toBeVisible();
  await input(page).fill('cancel-before-debounce'); await page.keyboard.press('Escape');
  await page.waitForTimeout(400); // prove closing cancels a pending debounce
  expect(searches).not.toContain('cancel-before-debounce');
});

for (const role of ['owner', 'member']) test(`${role} filename search at 1440/390 light/dark uses the authorized API and is shell-only`, async ({ page, context }) => {
  test.slow(); await rollout(page);
  const foreign = await project(page, 'private-search');
  if (role === 'member') {
    const account = `file-search-${Date.now()}-agent`;
    mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
    const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
    expect(invite.ok()).toBeTruthy(); const token = (await invite.json()).link.split('/').pop();
    await context.clearCookies();
    const begin = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    const joined = await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-search-${Date.now()}`, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
    expect(joined.ok()).toBeTruthy(); await preference(page, true);
    expect((await page.request.get(`/api/projects/${foreign.id}/files/search?q=notes`)).status()).toBe(404);
  }
  const own = await project(page, `filename-${role}`);
  // Opening search before the authorized project list arrives must retain the working route's target.
  if (role === 'owner') await page.route('**/api/projects', async route => { await new Promise(resolve => setTimeout(resolve, 1000)); await route.continue(); });
  for (const width of [1440, 390]) for (const theme of ['light', 'dark'] as const) {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme: theme });
    await page.goto(`/projects/${own.id}`); await open(page);
    await expect(dialog(page)).toContainText(own.folder);
    if (role === 'member') await expect(dialog(page).getByRole('option', { name: foreign.slug, exact: true })).toHaveCount(0);
    await input(page).fill('notes');
    await expect(dialog(page).getByRole('link')).toHaveCount(2);
    await expectAxeClean(page); await shot(page, `${role}-${width}-${theme}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await dialog(page).getByRole('link', { name: 'src/beta notes.ts', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'File content' })).toContainText('export const cedar = 2;');
  }
  try {
    await preference(page, false); await page.reload();
    await expect(page.getByRole('button', { name: /^Find project files/ })).toHaveCount(0);
  } finally {
    // shell-state restores the owner; this newly invited member owns its choice.
    if (role === 'member') await preference(page, true);
  }
});
