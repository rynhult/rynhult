import { expect, test, type BrowserContext, type Page } from './fixtures';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, sessionWithChanges, verifyAgain } from './helpers';
test.use({ presentationChanges: ['admin_features', 'layouts'], storageState: AUTH_FILE, viewport: { width: 1440, height: 900 } });

const layouts = (id: number) => `/api/projects/${id}/layouts/desktop/wide`;
const record = async (page: Page, id: number) => (await page.request.get(layouts(id))).json();
const revision = (page: Page, id: number, value: number) => expect.poll(async () => (await record(page, id)).revision).toBe(value);
async function project(page: Page, name: string, files = ['one.txt', 'two.txt']) {
   const made = await gitProject(page, name);
  for (const path of files) expect((await page.request.post(`/api/files/project/${made.id}`, { headers: await csrfHeaders(page), data: { action: 'create', path, content: `Saved ${path}` } })).ok()).toBeTruthy();
  return made;
}
const open = async (page: Page, id: number, file: string) => {
  await page.goto(`/projects/${id}?tab=files&file=${file}`);
  await expect(page.getByRole('tab', { name: new RegExp(file) })).toBeVisible();
};
/** A saved two-view arrangement. */
async function two(page: Page, id: number) { await open(page, id, 'one.txt'); await revision(page, id, 1); await open(page, id, 'two.txt'); await revision(page, id, 2); }
const move = (page: Page, column: string) => page.getByRole('combobox', { name: 'Move view to column' }).selectOption(column);
const notice = (page: Page) => page.locator('[data-layout-sync]');
const columns = (page: Page) => page.locator('[data-working-columns]');

test('an arrangement follows the person to another window, with no file name in what is saved', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-sync');
  await two(page, id); await move(page, '1'); await revision(page, id, 3);
  const saved = await record(page, id);
  expect(saved.layout.columns.map((column: { tabs: string[] }) => column.tabs.length)).toEqual([1, 1]);
  expect(JSON.stringify(saved.layout)).not.toContain('.txt');
  expect(JSON.stringify(await page.evaluate(() => ({ ...localStorage })))).not.toContain('two.txt');
  const other = await context.newPage();
  await other.goto(`/projects/${id}?tab=files&file=one.txt`);
  await expect(columns(other)).toHaveAttribute('data-working-columns', '2');
  await expect(other.getByRole('tablist', { name: 'Column 2 views' })).toContainText('two.txt');
  await other.waitForTimeout(800);
  expect((await record(other, id)).revision).toBe(3); // restoring wrote nothing
  await other.close();
});

test('two windows never overwrite silently: this window wins only by choice', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-conflict-a');
  await two(page, id);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=two.txt`);
  await expect(other.getByRole('tab', { name: /two.txt/ })).toBeVisible();
  await move(page, '1'); await revision(page, id, 3);
  await other.getByRole('button', { name: 'Fill working panel' }).click();
  await expect(notice(other)).toContainText('changed in another window');
  expect((await record(page, id)).revision).toBe(3);
  await expect(notice(other).getByRole('button', { name: "Use this window's layout" })).toBeVisible();
  for (const colorScheme of ['light', 'dark'] as const) { await other.emulateMedia({ colorScheme }); await expectAxeClean(other); }
  await expect(other.getByRole('textbox', { name: 'File content' })).toBeVisible(); // the notice is not modal
  await notice(other).getByRole('button', { name: "Use this window's layout" }).click();
  await revision(page, id, 4);
  expect((await record(page, id)).layout.columns).toHaveLength(1);
  await expect(notice(other)).toHaveCount(0);
  await other.close();
});

test('loading the newer layout applies it, writes nothing and keeps an unsaved draft', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-conflict-b', ['one.txt', 'two.txt', 'three.txt']);
  await two(page, id);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=two.txt`);
  const editor = other.getByRole('textbox', { name: 'File content' }); await expect(editor).toBeVisible();
  await editor.fill('Unsaved in this window');
  await page.getByRole('button', { name: 'Close view' }).click(); await revision(page, id, 3);
  await move(other, '1');
  await expect(notice(other)).toContainText('changed in another window');
  await notice(other).getByRole('button', { name: 'Load the newer one' }).click();
  await expect(notice(other)).toHaveCount(0);
  await expect(other.getByRole('tab', { name: /two.txt/ })).toBeVisible(); // the arrangement dropped it; the draft keeps it placed
  await other.waitForTimeout(800);
  expect((await record(page, id)).revision).toBe(3);
  await other.getByRole('tab', { name: /two.txt/ }).click();
  await expect(editor).toHaveText('Unsaved in this window');
  other.on('dialog', dialog => void dialog.accept()); await other.close({ runBeforeUnload: false });
});

test('offline saves honestly, and the change arrives when the server returns', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-offline');
  await two(page, id);
  await context.setOffline(true);
  await move(page, '1');
  await expect(notice(page)).toContainText('Offline. Layout changes are kept on this device');
  await context.setOffline(false);
  await revision(page, id, 3);
  await expect(notice(page)).toHaveCount(0);
  expect((await record(page, id)).layout.columns).toHaveLength(2);
});

test('a change made offline survives a reload, without paths in the retained copy', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-offline-reload');
  await two(page, id);
  await context.setOffline(true);
  await move(page, '1');
  await expect(notice(page)).toContainText('Offline.');
  const retained = await page.evaluate(() => Object.entries(localStorage).filter(([key]) => key.startsWith('ryn-layout-pending:')));
  expect(retained).toHaveLength(1); expect(JSON.stringify(retained)).not.toContain('.txt');
  await context.setOffline(false);
  await page.reload();
  await expect(columns(page)).toHaveAttribute('data-working-columns', '2');
  await revision(page, id, 3);
});

test('reset is explicit, audited, keeps the open views and the next window starts fresh', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-reset');
  await two(page, id); await move(page, '1'); await revision(page, id, 3);
  await page.getByRole('button', { name: 'Reset columns' }).click();
  await revision(page, id, 4);
  expect((await record(page, id)).layout).toBeNull();
  await expect(page.getByRole('tablist', { name: 'Column 1 views' }).getByRole('tab')).toHaveCount(2);
  const audit = await (await page.request.get('/api/audit?action=layout.reset')).json();
  expect(audit.some((entry: { target: string }) => entry.target === String(id))).toBe(true);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=one.txt`);
  await expect(other.locator('[data-view-tab]')).toHaveCount(1);
  await other.close();
});

test('unavailable references stay neutral while active and are pruned once hidden', async ({ page }) => {
  const { id } = await project(page, 'cedar-unavailable');
  await open(page, id, 'one.txt'); await revision(page, id, 1);
  const current = (await record(page, id)).layout;
  const views = [...current.views, { id: 'f-gone', kind: 'file', file_ref: 'unknown-reference' }, { id: 't-lost', kind: 'terminal', resource: { kind: 'session', id: 'not-a-session' } }];
  const layout = { ...current, views, columns: [{ tabs: views.map((view: { id: string }) => view.id), active: 'f-gone', width: 1 }], focused: 0 };
  expect((await page.request.put(layouts(id), { headers: await csrfHeaders(page), data: { expected_revision: 1, layout } })).ok()).toBeTruthy();
  const reads: string[] = []; page.on('request', request => { if (/not-a-session|unknown-reference/.test(request.url())) reads.push(request.url()); });
  await page.goto(`/projects/${id}?tab=files&file=one.txt`);
  await expect(page.getByRole('tab', { name: 'View unavailable' })).toHaveCount(1);
  await page.getByRole('tab', { name: 'View unavailable' }).click();
  await expect(page.locator('[data-working-view="unavailable:f-gone"]').getByText('View unavailable. Access may have ended')).toBeVisible();
  await page.getByRole('tab', { name: /one.txt/ }).click();
  await expect(page.getByRole('tab', { name: 'View unavailable' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'File content' })).toHaveText('Saved one.txt');
  await expect(page.getByText('Tabs removed', { exact: true })).toBeVisible();
  await expect.poll(async () => (await record(page, id)).layout.views.filter((view: { id: string }) => ['f-gone', 't-lost'].includes(view.id)).length).toBe(0);
  expect(reads).toEqual([]);
  await move(page, '1');
  await expect.poll(async () => (await record(page, id)).layout.columns.length).toBe(2);
  const savedIds = (await record(page, id)).layout.views.map((view: { id: string }) => view.id);
  expect(savedIds).not.toContain('f-gone'); expect(savedIds).not.toContain('t-lost');
});

test('F1: a full newer layout that cannot keep a draft stays a conflict and is never overwritten', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-full');
  await two(page, id);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=two.txt`);
  const editor = other.getByRole('textbox', { name: 'File content' }); await expect(editor).toBeVisible(); await editor.fill('Unsaved in this window');
  const views = Array.from({ length: 32 }, (_, n) => ({ id: `pp-${n}`, kind: 'page', resource: { kind: 'project', id }, tab: `view-${String.fromCharCode(97 + n % 26)}${String.fromCharCode(97 + Math.floor(n / 26))}` }));
  const full = { schema_version: 2, views, columns: [{ tabs: views.map(view => view.id), active: 'pp-0', width: 1 }], focused: 0, filled: null };
  expect((await page.request.put(layouts(id), { headers: await csrfHeaders(page), data: { expected_revision: 2, layout: full } })).ok()).toBeTruthy();
  await move(other, '1');
  await expect(notice(other)).toContainText('changed in another window');
  await notice(other).getByRole('button', { name: 'Load the newer one' }).click();
  await expect(notice(other)).toContainText('changed in another window');
  await expect(other.getByText('Close a view')).toBeVisible();
  await move(other, '0'); await other.waitForTimeout(900);
  const saved = await record(page, id);
  expect(saved.revision).toBe(3); expect(saved.layout.views).toHaveLength(32);
  await expect(editor).toHaveText('Unsaved in this window');
  other.on('dialog', dialog => void dialog.accept()); await other.close({ runBeforeUnload: false });
});

test('F2 and F3: an unchanged tab writes nothing, and checked restored tabs show names', async ({ page }) => {
  const { id } = await project(page, 'cedar-quiet');
  await two(page, id);
  await page.reload();
  await expect(page.getByRole('tab', { name: /two.txt/ })).toBeVisible();
  await expect(page.getByRole('tab', { name: /one.txt/ })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Saved view', exact: true })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'View unavailable' })).toHaveCount(0);
  await page.getByRole('tab', { name: /two.txt/ }).click(); await page.waitForTimeout(900);
  expect((await record(page, id)).revision).toBe(2);
});

test('F4: a page tab outside the saved vocabulary keeps later saves working', async ({ page }) => {
  const { id } = await project(page, 'cedar-weird');
  await two(page, id);
  // Opening the omitted tab selects a different serializable fallback active
  // view. Let that save finish, so moving it cannot coalesce into the same PUT.
  const opened = page.waitForResponse(response => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === layouts(id));
  await page.goto(`/projects/${id}?tab=Weird`);
  await expect(page.getByText("Some views stay in this window")).toBeVisible();
  await expect(page.getByText('Invalid tab')).toHaveCount(0);
  const response = await opened;
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).revision).toBe(3);
  await move(page, '1'); await revision(page, id, 4);
  const saved = (await record(page, id)).layout;
  expect(saved.columns).toHaveLength(2);
  expect(saved.focused).toBe(1);
  expect(JSON.stringify(saved)).not.toContain('Weird');
});

test('F5: a closed file, reopened, restores by name after its reference was pruned', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-prune');
  await two(page, id);
  await page.getByRole('button', { name: 'Close view' }).click(); await revision(page, id, 3);
  await open(page, id, 'two.txt'); await revision(page, id, 4);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=two.txt`);
  await expect(other.getByRole('tab', { name: /two.txt/ })).toBeVisible();
  await expect(other.getByRole('tab', { name: 'View unavailable' })).toHaveCount(0);
  await other.close();
});

test('F5: a window holding a reference that was pruned elsewhere recovers it on its next save', async ({ page, context }) => {
  const { id } = await project(page, 'cedar-stale-ref');
  await two(page, id);
  const other = await context.newPage(); await other.goto(`/projects/${id}?tab=files&file=two.txt`); await expect(other.getByRole('tab', { name: /two.txt/ })).toBeVisible();
  await page.getByRole('button', { name: 'Reset columns' }).click(); await revision(page, id, 3); // deletes both references
  await move(other, '1'); // the window still holds both, unreloaded
  await expect(notice(other)).toContainText('changed in another window');
  await notice(other).getByRole('button', { name: "Use this window's layout" }).click();
  await expect.poll(async () => { const saved = await record(other, id); const views = Object.values(saved.resources as Record<string, { available: boolean; file?: { path: string } }>); return views.length === 2 && views.every(item => item.available) ? views.map(item => item.file?.path).sort().join() : ''; }).toBe('one.txt,two.txt');
  await other.close();
});

async function frames(context: BrowserContext, page: Page) {
  const sent: { t: string; take?: boolean }[] = [];
  const cdp = await context.newCDPSession(page); await cdp.send('Network.enable');
  cdp.on('Network.webSocketFrameSent', event => { try { sent.push(JSON.parse(event.response.payloadData)); } catch { /* binary */ } });
  return sent;
}

test('restoring and choosing a layout never takes input, changes the writer or starts work', async ({ page, context }) => {
   const session = await sessionWithChanges(page, 'cedar-observer');
  const detail = await (await page.request.get(`/api/sessions/${session}`)).json(); const id = detail.project_id;
  await page.goto(`/sessions/${session}`);
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  await revision(page, id, 1);
  const effects: string[] = []; const urls: string[] = [];
  const watch = (target: Page) => { target.on('request', request => { const path = new URL(request.url()).pathname; if (request.method() !== 'GET' && !path.includes('/layouts/') && !path.startsWith('/api/auth')) effects.push(path); }); target.on('websocket', socket => urls.push(socket.url())); };
  const other = await context.newPage(); watch(other); watch(page);
  const sent = await frames(context, other);
  await other.goto(`/sessions/${session}`);
  await expect(other.getByText('Read-only terminal', { exact: true })).toBeVisible();
  await other.reload();
  await expect(other.getByText('Read-only terminal', { exact: true })).toBeVisible();
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  // A conflict choice in the observing window changes placement only.
  await page.getByRole('button', { name: 'Fill working panel' }).click(); await revision(page, id, 2);
  await other.getByRole('button', { name: 'Reset columns' }).click(); // differs from the newer layout, so a real conflict
  await expect(notice(other)).toContainText('changed in another window');
  await notice(other).getByRole('button', { name: 'Load the newer one' }).click();
  await expect(other.getByText('Read-only terminal', { exact: true })).toBeVisible();
  await expect(page.getByText('You control this terminal', { exact: true })).toBeVisible();
  expect(sent.filter(frame => ['control', 'release', 'i', 'd'].includes(frame.t))).toEqual([]);
  expect(urls.every(url => url.includes('mode=read') && !url.includes('take'))).toBe(true);
  expect(effects).toEqual([]);
  await page.getByRole('button', { name: 'Release control', exact: true }).click();
  await other.close();
});

test('a layout never crosses people', async ({ page }) => {
  const { id } = await project(page, 'cedar-people');
  await open(page, id, 'one.txt'); await revision(page, id, 1);
  const scope = (await record(page, id)).scope;
  expect(scope.person).not.toContain('alex');
  expect((await page.request.get(`${layouts(id)}?person=someone-else`)).ok()).toBeTruthy();
  expect((await (await page.request.get(`${layouts(id)}?person=someone-else`)).json()).scope.person).toBe(scope.person);
  expect((await page.request.put(layouts(id), { data: { expected_revision: 1, layout: null } })).status()).toBe(403); // no CSRF header
});

test('saved session tabs stay neutral while checking cached names, then name or refuse without opening', async ({ page, _drainRoutes }) => {
  const { id } = await project(page, 'cedar-access-label');
  const sessions = [
    { id: 'saved-label-allowed', name: 'Cached allowed session', project_id: id },
    { id: 'saved-label-refused', name: 'Cached refused session', project_id: id },
  ];
  let listed = false;
  await page.route('**/api/sessions', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: [...await response.json(), ...sessions] });
    listed = true;
  });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const checks = new Set<string>();
  await page.route('**/api/sessions/saved-label-*', async route => {
    const session = sessions.find(item => route.request().url().endsWith(item.id))!;
    checks.add(session.id);
    await gate;
    await route.fulfill({ status: session.id.endsWith('refused') ? 403 : 200, json: session });
  });
  const saved = await record(page, id);
  const views = sessions.map(session => ({ id: session.id, kind: 'page', resource: { kind: 'session', id: session.id }, tab: 'setup' }));
  await page.route(`**${layouts(id)}`, route => route.fulfill({ json: {
    ...saved, revision: 1, layout: { schema_version: 2, views, columns: [{ tabs: views.map(view => view.id), active: views[0].id, width: 1 }], focused: 0, filled: null },
    resources: Object.fromEntries(views.map(view => [view.id, { available: true }])),
  } }));
  try {
    await page.goto(`/projects/${id}`);
    await expect.poll(() => listed && checks.size === 2).toBe(true);
    await expect(page.getByRole('tab', { name: 'Saved view', exact: true })).toHaveCount(2);
    await expect(page.getByRole('tab', { name: /Cached/ })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: 'View unavailable' })).toHaveCount(0);
    release();
    await expect(page.getByRole('tab', { name: 'Cached allowed session · Setup', exact: true })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'View unavailable', exact: true })).toHaveCount(0);
    await expect(page.getByText('Tabs removed', { exact: true })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Saved view', exact: true })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: /Cached refused/ })).toHaveCount(0);
  } finally { release(); }
});


test('P2 polish caps restored strips and drops a deleted file with an explanation', async ({ page }) => {
  test.setTimeout(90000);
  const files = Array.from({ length: 14 }, (_, i) => `polish-${i}.txt`);
  const { id, folder } = await project(page, 'cedar-polish-strip', files);
  for (const [index, file] of files.entries()) {
    await open(page, id, file);
    await expect(page.getByRole('textbox', { name: 'File content' })).toBeVisible();
    await expect.poll(async () => (await record(page, id)).layout?.views.length).toBe(Math.min(index + 1, 12));
  }
  await expect(page.locator('[data-view-tab]')).toHaveCount(12);
  await expect(page.getByRole('tab', { name: /polish-0.txt/ })).toHaveCount(0);
  await expect(page.getByText('Tabs removed', { exact: true })).toHaveAttribute('title', /oldest first/);
  // Validate a hidden saved file without mounting its editor.
  const { unlinkSync } = await import('node:fs');
  unlinkSync(`${folder}/polish-12.txt`);
  await page.reload();
  await expect(page.getByRole('tab', { name: /polish-12.txt/ })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: /polish-13.txt/ })).toBeVisible();
  await expect.poll(async () => (await record(page, id)).layout?.views.length).toBe(11);
  await expect(page.getByText('Tabs removed', { exact: true })).toHaveAttribute('title', /unavailable views/);
});

test('P2 polish duplicate registration offers the existing project', async ({ page }) => {
  const made = await gitProject(page, 'cedar-polish-duplicate');
  await page.goto('/projects');
  await page.getByRole('button', { name: 'Register folder' }).click();
  const dialog = page.getByRole('dialog', { name: 'Register a folder' });
  await dialog.getByLabel('Name', { exact: true }).fill('Duplicate registration');
  await dialog.getByLabel('Workspace path', { exact: true }).fill(made.folder);
  await dialog.getByRole('button', { name: 'Register', exact: true }).click();
  const existing = dialog.getByRole('link', { name: 'Open the existing project' });
  await expect(existing).toHaveAttribute('href', `/projects/${made.id}`);
  await existing.click();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/projects/${made.id}$`));
});

test('P2 polish sign-in help is public and software repair links to the Guide', async ({ page, browser }) => {
  const signedOut = await browser.newContext({ baseURL: info().origin, storageState: { cookies: [], origins: [] } });
  try {
    const login = await signedOut.newPage();
    await login.goto('/login');
    await expect(login.getByRole('link', { name: 'Trouble signing in?' })).toHaveAttribute('href',
      'https://rynhult.dev/docs/User-Guide/Troubleshooting/#sign-in-and-launch-policy');
  } finally { await signedOut.close(); }
  await page.route('**/api/packages?*', route => route.fulfill({ json: { packages: {}, pending: [], helper: false, reported: true } }));
  await page.goto('/providers');
  await expect(page.getByRole('link', { name: 'Installer re-run steps' })).toHaveAttribute('href', '/guide/User-Guide/Updating#repair-agent-software');
});

for (const action of ['rename', 'delete'] as const) {
  test(`P2 recovery keeps a dirty file after agent ${action}`, async ({ page }) => {
    test.setTimeout(90000);
    const { id, folder } = await project(page, `cedar-draft-${action}`);
    await open(page, id, 'one.txt');
    const editor = page.getByRole('textbox', { name: 'File content' });
    await editor.fill('Precious unsaved edits');
    const fs = await import('node:fs');
    if (action === 'rename') fs.renameSync(`${folder}/one.txt`, `${folder}/moved.txt`);
    else fs.unlinkSync(`${folder}/one.txt`);
    await expect(page.getByText('File moved or removed - your changes are kept', { exact: true })).toBeVisible({ timeout: 45000 });
    await expect(page.getByRole('textbox', { name: 'Retained file draft' })).toHaveValue('Precious unsaved edits');
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Save as', exact: true }).click();
    const saved = await download;
    expect(fs.readFileSync((await saved.path())!, 'utf8')).toBe('Precious unsaved edits');
    await expect(page.getByRole('button', { name: 'Copy', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Discard', exact: true })).toBeVisible();
  });
}
