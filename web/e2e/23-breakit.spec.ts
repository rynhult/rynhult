import { expect, test } from '@playwright/test';
import { mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, fileRow, info, openAdvanced, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`impatient registration and hostile Files inputs at ${width}px`, async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const root = `${info().workspace}/breakit-${width}`;
    mkdirSync(root, { recursive: true });
    writeFileSync(`${root}/space . Ω 😀 שלום.txt`, 'original');
    writeFileSync(`${root}/other.txt`, 'second file');
    writeFileSync(`${root}/huge.txt`, 'x'.repeat(1024 * 1024 + 1));
    writeFileSync(`${root}/binary.bin`, Buffer.from([0, 255, 1]));
    symlinkSync('other.txt', `${root}/link.txt`);
    await page.goto('/projects');
    await page.getByRole('button', { name: 'Register folder', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Slug', { exact: true }).fill(`breakit-${width}`);
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
    await dialog.getByRole('button', { name: 'Register', exact: true }).dblclick();
    await expect.poll(() => posts).toBe(1);
    await expect(dialog.getByRole('button', { name: 'Register', exact: true })).toBeDisabled();
    release();
    await expect(dialog).not.toBeVisible();
    const projects = await (await page.request.get('/api/projects')).json();
    const project = projects.find((p: { slug: string }) => p.slug === `breakit-${width}`);
    expect(project.name).toBe(`Ω 😀 שלום ${width}`);
    await page.goto(`/projects/${project.id}?tab=files`);
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
    await expect(other.getByRole('heading', { name: 'File changed on disk' })).toBeVisible();
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
    await selectFile('other.txt');
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    try { await expect(page.getByRole('button', { name: 'Saving…', exact: true })).toBeDisabled(); }
    finally { finishSave(); }
    await expect.poll(() => readFileSync(`${root}/space . Ω 😀 שלום.txt`, 'utf8')).toBe('save still in flight');
    await page.unroute(`**/api/files/project/${project.id}`);
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
    await page.goBack();
    await expect(page.getByRole('dialog', { name: 'Discard unsaved edits?' })).toBeVisible();
    if (width === 1440) await page.screenshot({ path: wikiShotPath(testInfo, "files-unsaved-navigation.png"), fullPage: true });
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(editor).toHaveText('unsaved draft before Back');
    await page.goBack();
    await page.getByRole('dialog').getByRole('button', { name: 'Discard draft', exact: true }).click();
    await expect(editor).toHaveText('retry after offline');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  });
}

for (const flow of ['session', 'multirun', 'schedule']) {
  test(`double-click ${flow} creates one durable launch`, async ({ page }) => {
    const { execFileSync } = await import('node:child_process');
    const root = `${info().workspace}/breakit-${flow}`;
    mkdirSync(root, { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', root]);
    writeFileSync(`${root}/README.md`, 'Artificial test project');
    execFileSync('git', ['-C', root, 'add', '.']);
    execFileSync('git', ['-C', root, '-c', 'user.name=Alex Example', '-c', 'user.email=alex@example.com', 'commit', '-qm', 'Start']);
    await page.goto('/');
    const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
    const headers = { 'X-ACC-CSRF': csrf };
    const made = await page.request.post('/api/projects', { headers, data: { slug: `breakit-${flow}`, name: `Breakit ${flow}`, workspace_path: root } });
    const pid = (await made.json()).id;
    await page.request.post(`/api/projects/${pid}/trust`, { headers });
    await page.goto(flow === 'schedule' ? '/schedules' : '/sessions');
    await page.getByRole('button', { name: flow === 'schedule' ? 'New schedule' : 'New session', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Rename', exact: true }).click();
    await dialog.getByLabel('Name', { exact: true }).fill(`Breakit ${flow}`);
    await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(pid));
    if (flow === 'multirun') {
      await openAdvanced(dialog);
      await dialog.getByRole('radio', { name: /Several agents/ }).check();
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
    await submit.dblclick();
    try { await expect.poll(() => requests).toBe(1); } finally { release(); }
    await expect(dialog).not.toBeVisible();
    const rows = await (await page.request.get(endpoint)).json();
    expect(rows.filter((r: { name?: string; launch?: { name: string } }) => (r.launch?.name ?? r.name) === `Breakit ${flow}`)).toHaveLength(1);
    await page.reload();
    await page.goBack();
    await page.goForward();
  });
}

test('invite survives reload, two people conflict safely, and an ended sign-in cannot submit', async ({ page, browser }) => {
  const { PASSWORD, totp, verifyAgain } = await import('./helpers');
  await page.goto('/');
  await verifyAgain(page);  // inviting an owner needs fresh verification
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const invite = await page.request.post('/api/users/invites', { headers: { 'X-ACC-CSRF': csrf }, data: { role: 'owner' } });
  expect(invite.status()).toBe(201);
  const link = (await invite.json()).link;
  const guest = await browser.newContext({ viewport: { width: 390, height: 844 } });
  try {
    const second = await guest.newPage();
    await second.goto(info().origin + link);
    const secret = await second.getByTitle('Manual entry key').innerText();
    await second.reload();
    await expect(second.getByTitle('Manual entry key')).toHaveText(secret);
    await second.getByLabel('Username', { exact: true }).fill('breakit-owner');
    await second.getByLabel('Password', { exact: true }).fill(PASSWORD);
    await second.getByLabel('Confirm password', { exact: true }).fill(PASSWORD);
    await second.getByLabel('Code from your authenticator').fill(totp(secret));
    await second.getByRole('button', { name: 'Join', exact: true }).dblclick();
    await expect(second.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
    const root = `${info().workspace}/breakit-people`;
    mkdirSync(root, { recursive: true });
    writeFileSync(`${root}/shared.txt`, 'before either person');
    const made = await page.request.post('/api/projects', { headers: { 'X-ACC-CSRF': csrf }, data: {
      slug: 'breakit-people', name: 'Shared demo', workspace_path: root,
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
    await page.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect.poll(() => readFileSync(`${root}/shared.txt`, 'utf8')).toBe('Alex saved');
    await secondEditor.fill('Riley draft');
    await second.getByRole('button', { name: 'Save', exact: true }).dblclick();
    await expect(second.getByRole('heading', { name: 'File changed on disk' })).toBeVisible();
    await expect(secondEditor).toHaveText('Riley draft');
    expect(readFileSync(`${root}/shared.txt`, 'utf8')).toBe('Alex saved');
    const ended = await guest.newPage();
    await ended.goto(info().origin + '/projects');
    await ended.getByRole('button', { name: 'Register folder', exact: true }).click();
    await ended.getByLabel('Name', { exact: true }).fill('Must not be created');
    await ended.getByLabel('Slug', { exact: true }).fill('ended-signin-probe');
    await ended.getByLabel('Workspace path').fill(root);
    // An unknown server-side session has the same lookup result as an expired one.
    await guest.addCookies([{ name: 'acc_session', value: 'expired-demo-session', url: info().origin }]);
    await ended.getByRole('button', { name: 'Register', exact: true }).dblclick();
    await expect(ended).toHaveURL(/\/login$/);
    const rows = await (await page.request.get('/api/projects')).json();
    expect(rows.some((p: { slug: string }) => p.slug === 'ended-signin-probe')).toBe(false);
  } finally { await guest.close(); }
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
