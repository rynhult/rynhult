import { randomUUID } from 'node:crypto';
import { gotoLoadedPage, loadPage } from './loaded-page';
import { expect, test, type BrowserContext } from './fixtures';
import { mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, fileRow, info, openAdvanced, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`impatient registration and hostile Files inputs at ${width}px`, async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const slug = `breakit-${width}-${randomUUID().slice(0, 8)}`;
    const root = `${info().workspace}/${slug}`;
    mkdirSync(root, { recursive: true });
    writeFileSync(`${root}/space . Ω 😀 שלום.txt`, 'original');
    writeFileSync(`${root}/other.txt`, 'second file');
    writeFileSync(`${root}/huge.txt`, 'x'.repeat(1024 * 1024 + 1));
    writeFileSync(`${root}/binary.bin`, Buffer.from([0, 255, 1]));
    symlinkSync('other.txt', `${root}/link.txt`);
    await gotoLoadedPage(page, '/projects', ['Projects'], ['/api/projects']);
    await page.getByRole('button', { name: 'Register folder', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Slug', { exact: true }).fill(slug);
    await dialog.getByLabel('Name', { exact: true }).fill(' \u2003 ');
    await expect(dialog.getByRole('button', { name: 'Register', exact: true })).toBeDisabled();
    await dialog.getByLabel('Name', { exact: true }).fill(`Ω 😀 שלום ${width}`);
    await dialog.getByLabel('Workspace path').fill(root);
    let posts = 0;
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/projects', async route => {
      if (route.request().method() === 'POST') { posts++; await gate; }
      await route.continue();
    });
    const registered = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/projects');
    await dialog.getByRole('button', { name: 'Register', exact: true }).dblclick();
    await expect.poll(() => posts).toBe(1);
    await expect(dialog.getByRole('button', { name: 'Register', exact: true })).toBeDisabled();
    release();
    expect((await registered).status()).toBe(201);
    expect(await (await registered).finished()).toBeNull();
    await expect(dialog).not.toBeVisible();
    const projects = await (await page.request.get('/api/projects')).json();
    const project = projects.find((p: { slug: string }) => p.slug === slug);
    expect(project.name).toBe(`Ω 😀 שלום ${width}`);
    await gotoLoadedPage(page, `/projects/${project.id}?tab=files`, ["ProjectPage"], ["/api/projects", `/api/files/project/${project.id}`]);
    const selectFile = async (name: string) => {
      const tree = page.getByRole('button', { name: 'Show tree', exact: true });
      if (await tree.isVisible()) await tree.click();
      await page.getByRole('button', { name: fileRow(name) }).click();
    };
    await expect(page.getByRole('button', { name: fileRow('link.txt') })).toBeDisabled();
    await selectFile('huge.txt');
    await expect(page.getByText('Too large to open', { exact: false })).toBeVisible();
    await selectFile('binary.bin');
    await expect(page.getByText('Binary file. Download to view it.')).toBeVisible();
    await selectFile('space . Ω 😀 שלום.txt');
    const editor = page.getByRole('textbox', { name: 'File content' });
    await expect(editor).toHaveText('original');
    const other = await context.newPage();
    await other.goto(page.url());
    await expect(other.getByRole('textbox', { name: 'File content' })).toHaveText('original');
    await editor.fill('first tab');
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(page.getByRole('status', { name: 'File save status' })).toContainText('Saved');
    await other.getByRole('textbox', { name: 'File content' }).fill('second tab draft');
    await other.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(other.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
    await expect(other.getByRole('textbox', { name: 'File content' })).toHaveText('second tab draft');
    expect(readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('first tab');
    await other.close();
    await editor.fill('retry after offline');
    await page.route(`**/api/files/project/${project.id}`, async route => {
      if (route.request().method() === 'POST') await route.abort('failed'); else await route.continue();
    });
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(page.getByText("Rynhult didn't answer. Check your connection and try again.")).toBeVisible();  // the browser's "Failed to fetch" is under Details
    await expect(page.getByRole('status', { name: 'File save status' })).not.toBeVisible();
    await expect(editor).toHaveText('retry after offline');
    await page.unroute(`**/api/files/project/${project.id}`);
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(page.getByRole('status', { name: 'File save status' })).toContainText('Saved');
    await expect.poll(() => readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('retry after offline');
    await page.reload();
    await expect(editor).toHaveText('retry after offline');
    let finishSave!: () => void;
    const saveGate = new Promise<void>(resolve => { finishSave = resolve; });
    await page.route(`**/api/files/project/${project.id}`, async route => {
      if (route.request().method() === 'POST') await saveGate;
      await route.continue();
    });
    await editor.fill('save still in flight');
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    try {
      await selectFile('other.txt');
      await expect(editor).toHaveText('second file');
      await expect(page.getByRole('dialog')).toHaveCount(0);
      expect(readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('retry after offline');
    } finally { finishSave(); }
    await expect.poll(() => readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('save still in flight');
    await page.unroute(`**/api/files/project/${project.id}`);
    await selectFile('space . Ω 😀 שלום.txt');
    await expect(editor).toHaveText('save still in flight');
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
    await editor.fill('retry after offline');
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect.poll(() => readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('retry after offline');
    await selectFile('other.txt');
    await expect(editor).toHaveText('second file');
    await page.goBack();
    await expect(editor).toHaveText('retry after offline');
    await page.goForward();
    await expect(editor).toHaveText('second file');
    await editor.fill('unsaved draft before Back');
    // Working views retain drafts through Back/Forward; explicit Close still confirms discard.
    await page.goBack();
    await expect(editor).toHaveText('retry after offline');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.goForward();
    await expect(editor).toHaveText('unsaved draft before Back');
    await page.getByRole('button', { name: 'Close view', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Discard unsaved edits?' })).toBeVisible();
    if (width === 1440) await page.screenshot({ path: wikiShotPath(testInfo, "files-unsaved-navigation.png"), fullPage: true });
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(editor).toHaveText('unsaved draft before Back');
    await page.getByRole('button', { name: 'Close view', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Discard draft', exact: true }).click();
    await expect(editor).toHaveText('retry after offline');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  });
}

for (const flow of ['session', 'multirun', 'schedule']) {
  test(`double-click ${flow} creates one durable launch`, async ({ page }) => {
    const { execFileSync } = await import('node:child_process');
    const slug = `breakit-${flow}-${randomUUID().slice(0, 8)}`;
    const name = `Breakit ${flow} ${randomUUID().slice(0, 8)}`;
    const root = `${info().workspace}/${slug}`;
    mkdirSync(root, { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', root]);
    writeFileSync(`${root}/README.md`, 'Artificial test project');
    execFileSync('git', ['-C', root, 'add', '.']);
    execFileSync('git', ['-C', root, '-c', 'user.name=Alex Example', '-c', 'user.email=alex@example.com', 'commit', '-qm', 'Start']);
    await page.goto('/');
    const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
    const headers = { 'X-ACC-CSRF': csrf };
    const made = await page.request.post('/api/projects', { headers, data: { slug, name, workspace_path: root } });
    const pid = (await made.json()).id;
    await page.request.post(`/api/projects/${pid}/trust`, { headers });
    await page.goto(flow === 'schedule' ? '/schedules' : '/sessions');
    await page.getByRole('button', { name: flow === 'schedule' ? 'New schedule' : 'New session', exact: true }).click();
    const dialog = page.getByRole(flow === 'schedule' ? 'dialog' : 'region', { name: flow === 'schedule' ? 'New schedule' : 'New session', exact: true });
    await openAdvanced(dialog);
    await dialog.getByRole('button', { name: 'Rename', exact: true }).click();
    await dialog.getByLabel('Name', { exact: true }).fill(name);
    await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(pid));
    if (flow === 'multirun') {
      await openAdvanced(dialog);
      await dialog.getByRole('combobox', { name: 'Run', exact: true }).selectOption('several');
      await dialog.getByLabel('Task', { exact: true }).fill('Review this example project');
    }
    const endpoint = flow === 'schedule' ? '/api/schedules' : flow === 'multirun' ? '/api/multiruns' : '/api/sessions';
    let requests = 0;
    let release!: () => void;
    const gate = new Promise<void>(r => { release = r; });
    await page.route(`**${endpoint}`, async route => {
      if (route.request().method() === 'POST') { requests++; await gate; }
      await route.continue();
    });
    const submit = dialog.getByRole('button', { name: flow === 'schedule' ? 'Create paused schedule' : 'Launch', exact: true });
    await expect(submit).toBeEnabled();
    const committed = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === endpoint);
    await submit.dblclick();
    try { await expect.poll(() => requests).toBe(1); } finally { release(); }
    expect((await committed).status()).toBe(201);
    expect(await (await committed).finished()).toBeNull();
    await expect(dialog).not.toBeVisible();
    const rows = await (await page.request.get(endpoint)).json();
    expect(rows.filter((r: { name?: string; launch?: { name: string } }) => (r.launch?.name ?? r.name) === name)).toHaveLength(1);
    await page.reload();
    await page.goBack();
    await page.goForward();
  });
}

test('invite survives reload, two people conflict safely, and an ended sign-in cannot submit', async ({ page, browser }) => {
  const { PASSWORD, totp, verifyAgain } = await import('./helpers');
  const suffix = randomUUID().slice(0, 8);
  const username = `breakit-owner-${suffix}`;
  const slug = `breakit-people-${suffix}`;
  await page.goto('/');
  await verifyAgain(page);  // inviting an owner needs fresh verification
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const invite = await page.request.post('/api/users/invites', { headers: { 'X-ACC-CSRF': csrf }, data: { role: 'owner' } });
  expect(invite.status()).toBe(201);
  const link = (await invite.json()).link;
  const guest = await browser.newContext({ viewport: { width: 390, height: 844 } });
  let layoutControl: BrowserContext | undefined;
  try {
    const second = await guest.newPage();
    await second.goto(info().origin + link);
    const secret = await second.getByTitle('Manual entry key').innerText();
    await second.reload();
    await expect(second.getByTitle('Manual entry key')).toHaveText(secret);
    await second.getByLabel('Username', { exact: true }).fill(username);
    await second.getByLabel('Password', { exact: true }).fill(PASSWORD);
    await second.getByLabel('Confirm password', { exact: true }).fill(PASSWORD);
    await second.getByLabel('Code from your authenticator').fill(totp(secret));
    const joined = second.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/auth/invite/complete');
    await second.getByRole('button', { name: 'Join', exact: true }).dblclick();
    expect((await joined).ok()).toBe(true);
    // Both people use the retained Files conflict contract. Retain a
    // control context because the journey deliberately replaces guest cookies.
    layoutControl = await browser.newContext({ storageState: await guest.storageState() });

    await loadPage(second, () => second.getByRole('button', { name: 'I saved the codes', exact: true }).click(), ['Overview'], []);
    await expect(second.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
    const root = `${info().workspace}/${slug}`;
    mkdirSync(root, { recursive: true });
    writeFileSync(`${root}/shared.txt`, 'before either person');
    const made = await page.request.post('/api/projects', { headers: { 'X-ACC-CSRF': csrf }, data: {
      slug, name: 'Shared demo', workspace_path: root,
    } });
    const pid = (await made.json()).id;
    const url = `${info().origin}/projects/${pid}?tab=files&file=shared.txt`;
    await page.goto(url);
    await second.goto(url);
    const firstEditor = page.getByRole('textbox', { name: 'File content' });
    const secondEditor = second.getByRole('textbox', { name: 'File content' });
    await expect(firstEditor).toHaveText('before either person');
    await expect(secondEditor).toHaveText('before either person');
    await firstEditor.fill('Alex saved');
    // Safe replacement briefly moves the previous inode to a recovery name.
    // Wait for the completed write before reading: expect.poll does not retry
    // an ENOENT thrown while the replacement is still being published.
    const [saved] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith(`/api/files/project/${pid}`)
        && response.request().method() === 'POST'),
      page.getByRole('button', { name: 'Save', exact: true }).dblclick(),
    ]);
    expect(saved.status()).toBe(200);
    expect((await saved.json()).conflict).toBeFalsy();
    expect(readFileSync(`${root}/shared.txt`, 'utf8')).toBe('Alex saved');
    await secondEditor.fill('Riley draft');
    await second.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(second.getByRole('heading', { name: 'Resolve a save conflict' })).toBeVisible();
    await expect(secondEditor).toHaveText('Riley draft');
    expect(readFileSync(`${root}/shared.txt`, 'utf8')).toBe('Alex saved');
    const ended = await guest.newPage();
    await ended.goto(info().origin + '/projects');
    await ended.getByRole('button', { name: 'Register folder', exact: true }).click();
    await ended.getByLabel('Name', { exact: true }).fill('Must not be created');
    const expiredSlug = `ended-signin-probe-${suffix}`;
    await ended.getByLabel('Slug', { exact: true }).fill(expiredSlug);
    await ended.getByLabel('Workspace path').fill(root);
    // An unknown server-side session has the same lookup result as an expired one.
    await guest.addCookies([{ name: 'acc_session', value: 'expired-demo-session', url: info().origin }]);
    const refused = ended.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
    const warning = ended.waitForEvent('dialog');
    await ended.getByRole('button', { name: 'Register', exact: true }).click({ noWaitAfter: true });
    expect((await refused).status()).toBe(401);
    const prompt = await warning;
    expect(prompt.type()).toBe('beforeunload');
    await prompt.dismiss();
    await expect(ended.getByLabel('Name', { exact: true })).toHaveValue('Must not be created');
    // Expiry must keep the draft guard: explicitly accept leaving after copying.
    const leaving = ended.waitForEvent('dialog');
    const navigation = ended.goto(info().origin + '/login');
    const leave = await leaving;
    expect(leave.type()).toBe('beforeunload');
    await leave.accept();
    await navigation;
    await expect(ended).toHaveURL(/\/login$/);
    const rows = await (await page.request.get('/api/projects')).json();
    expect(rows.some((p: { slug: string }) => p.slug === expiredSlug)).toBe(false);
  } finally {
    await layoutControl?.close(); await guest.close();
  }
});

test('publishing explains a failed zone lookup at desktop and phone sizes', async ({ page }, testInfo) => {
  await page.route('**/api/publish/zones', route => route.fulfill({ status: 502,
    json: { detail: 'Cloudflare: configure a token before publishing' } }));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.goto('/publishing');
    await expect(page.getByRole('alert')).toContainText('Cloudflare: configure a token before publishing');
    if (width === 1440) await page.screenshot({ path: wikiShotPath(testInfo, 'publishing-zone-error.png') });
  }
});
