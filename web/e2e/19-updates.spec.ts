import { wikiShotPath } from "./helpers";
import { readFileSync, writeFileSync } from 'node:fs';
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, info } from './helpers';

test.use({ storageState: AUTH_FILE });
const state = {
  version: '0.15.0-rc.1', build: 'a'.repeat(64), latest: '0.15.0', notes: '<b>Release notes stay plain text</b>',
  available: true, checked_at: Date.now()/1000, repository: 'example/acc', automatic: false, time: '03:00',
  signature: { status: 'verified', primary: '0C2CE12657BD833FB5B563B2B555F6E127990DB5' },
  timezone: 'UTC', helper: true, git_checkout: false, progress: {}, pending: false,
  session_notice: 'Agents keep running in tmux. Terminals disconnect briefly while the dashboard restarts.',
};
test('owners check, confirm and schedule updates; notes stay text', async ({ page }, testInfo) => {
  let requested = false;
  await page.route('**/api/updates', r => r.fulfill({ json: state }));
  await page.route('**/api/updates/check', r => r.fulfill({ json: state }));
  await page.route('**/api/updates/config', r => r.fulfill({ json: { ...state, ...r.request().postDataJSON() } }));
  await page.route('**/api/updates/apply', r => { requested = true; return r.fulfill({ json: { queued: true } }); });
  await page.goto('/settings#updates');
  await expect(page.getByRole('heading', { name: 'Updates', exact: true })).toBeVisible();
  await expect(page.getByText(state.notes, { exact: true })).toBeVisible();
  await expect(page.locator('b').filter({ hasText: 'Release notes stay plain text' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Check now', exact: true }).dblclick();
  await page.getByLabel('Install updates automatically').check();
  await page.getByLabel('Daily time').fill('04:30');
  await page.getByRole('button', { name: 'Save update settings' }).click();
  await page.getByRole('button', { name: 'Back up and update', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('backup');
  expect(requested).toBe(false);
  await page.getByRole('dialog').getByRole('button', { name: 'Back up and update', exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const a11y = await new AxeBuilder({page}).include('#updates').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await page.locator('#updates').screenshot({ path: wikiShotPath(testInfo, 'updates-settings.png') });
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});

test('machines show different and unknown builds and confirmed updates', async ({ page }, testInfo) => {
  let requested = false, followed = false, all = false;
  await page.route('**/api/nodes', r => r.fulfill({ json: [
    { id:'one', name:'lab-pc', status:'active', connected:true, info:{os:'linux',version:'0.12.0',build:'b'.repeat(64)}, accounts:[], update_available:true, follow:followed },
    { id:'two', name:'studio-mac', status:'active', connected:true, info:{os:'darwin',version:'0.12.0'}, installer:'https://example.com/Rynhult-1.0.0.pkg', accounts:[], update_available:true, follow:false },
  ] }));
  await page.route('**/api/updates/machines/lab-pc', r => { requested=true; return r.fulfill({json:{'lab-pc':{queued:true}}}); });
  await page.route('**/api/updates/machines/all', r => { all=true; return r.fulfill({json:{'lab-pc':{queued:true}}}); });
  await page.route('**/api/updates/machines/lab-pc/follow', r => { followed=r.request().postDataJSON().enabled; return r.fulfill({json:{enabled:followed}}); });
  await page.goto('/host');
  await expect(page.getByText('unknown build', { exact: false })).toBeVisible();
  await page.getByRole('button', { name:'Update lab-pc', exact:true }).click();
  expect(requested).toBe(false);
  await page.getByRole('dialog').getByRole('button', {name:'Update',exact:true}).click();
  await expect.poll(() => requested).toBe(true);
  await expect(page.getByRole('link', {name:'Update this Mac with the new installer'})).toBeVisible();
  await page.getByLabel('Follow the main Rynhult automatically on lab-pc').click();
  await expect(page.getByLabel('Follow the main Rynhult automatically on lab-pc')).toBeChecked();
  await expect.poll(() => followed).toBe(true);
  await page.getByRole('button',{name:'Update all',exact:true}).click();
  expect(all).toBe(false);
  await page.getByRole('dialog').getByRole('button',{name:'Update',exact:true}).click();
  await expect.poll(() => all).toBe(true);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.locator('section').filter({has:page.getByRole('heading',{name:'Machines',exact:true})}).screenshot({ path: wikiShotPath(testInfo, 'updates-machines.png') });
});

test('members see no updater or machine controls', async ({ page }) => {
  let updateRequests = 0;
  await page.route('**/api/auth/state', r => r.fulfill({json:{authenticated:true,setup_required:false,username:'demo-member',role:'member',account:'demo-agent',instance:{host:'acc-host'}}}));
  await page.route('**/api/updates', r => {updateRequests++; return r.fulfill({status:403,json:{detail:'owners only'}});});
  await page.goto('/settings#updates');
  await expect(page.getByRole('heading',{name:'Settings',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Updates',exact:true})).toHaveCount(0);
  await expect(page.getByText('Update available',{exact:true})).toHaveCount(0);
  await page.goto('/host');
  await expect(page.getByRole('heading',{name:'Host',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Machines',exact:true})).toHaveCount(0);
  expect(updateRequests).toBe(0);
});

test('corrupt saved updater configuration is visible and an explicit save repairs it', async ({ page }, testInfo) => {
  const path = `${info().state}/data/updates-config.json`;
  const before = readFileSync(path, 'utf8');
  try {
    writeFileSync(path, '{"automatic":"false"');
    await page.goto('/settings#updates');
    const updates = page.locator('#updates');
    await expect(updates.getByRole('status')).toContainText('Updater configuration is corrupt or unreadable');
    await expect(updates.getByRole('button', { name: 'Check now', exact: true })).toBeDisabled();
    await expect(updates.getByRole('button', { name: 'Back up and update', exact: true })).toHaveCount(0);
    await updates.screenshot({ path: testInfo.outputPath('updates-corrupt-configuration.png') });
    await updates.getByLabel('GitHub repository').fill('');
    await updates.getByLabel('Daily time').fill('03:00');
    await updates.getByRole('button', { name: 'Save update settings' }).click();
    await expect(updates.getByText('Update settings saved.', { exact: true })).toBeVisible();
    await expect(updates.getByText(/Updater configuration is corrupt or unreadable/)).toHaveCount(0);
    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ repository: '', automatic: false, time: '03:00', follow: {} });
  } finally {
    writeFileSync(path, before);
  }
});


test('an ignored update shows recovery steps on its machine', async ({page}, testInfo) => {
  const script = "set -eu; work=$(mktemp -d /tmp/rynhult-update.XXXXXX); cleanup() { rm -rf -- $work; }; trap cleanup EXIT; "
    + "curl -fsS --proto =https --max-filesize 268435456 https://acc.example.lan/api/nodes/bundle -o $work/bundle.tar.gz; "
    + "mkdir $work/source; tar -xzf $work/bundle.tar.gz -C $work/source --strip-components=1 --no-same-owner --no-same-permissions; "
    + "bash $work/source/deploy/node/install.sh --update";
  const command = `wsl -d 'Ubuntu-24.04' -u root --exec sh -c '${script}'`;
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.route('**/api/nodes', r => r.fulfill({json: [{id:'old', name:'old-pc', status:'active', connected:true,
    info:{os:'linux',wsl:'2',wsl_distro:'Ubuntu-24.04'}, system:{kind:'wsl',label:'Linux (WSL2) · Windows 11 (build 22631)',wsl:'2'}, accounts:[], update_available:true,
    update:{running:false, ok:false, installer_required:true,
      detail:"This machine didn't respond to the update. Its Rynhult may be too old to update itself.",
      recovery:{command,root_command:`sh -c '${script}'`,
        detail:'Run in PowerShell on this Windows PC. Keeps the existing enrollment, agent accounts and workspace folders.'}}}]}));
  await page.goto('/host');
  await expect(page.getByRole('status').filter({hasText:"This machine didn't respond"})).toBeVisible();
  await expect(page.getByText('Run in PowerShell', {exact:false})).toBeVisible();
  await page.getByRole('button', {name:'Copy the update command for old-pc',exact:true}).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(command);
  await page.getByRole('button', {name:'Copy the in-Ubuntu root command for old-pc',exact:true}).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(`sh -c '${script}'`);
  const machines = page.locator('section').filter({has:page.getByRole('heading',{name:'Machines',exact:true})});
  await machines.screenshot({path:wikiShotPath(testInfo, 'nodes-old-machine-update.png')});
  const a11y = await new AxeBuilder({page}).include('section').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByRole('button', {name:'Copy the update command for old-pc',exact:true})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.getByRole('button', {name:'Copy the update command for old-pc',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:wikiShotPath(testInfo, 'nodes-old-machine-update-phone.png')});
  await page.getByRole('link', {name:'Recovery steps'}).click();
  await expect(page.getByRole('heading', {name:'Update an older machine'})).toBeVisible();
});

test('Mac without an installer gets an update command from this main, keeping its enrollment', async ({page}, testInfo) => {
  // A real Mac row from the e2e server (pre-v1, private CA): enrolled as a Mac would, then read back from Host.
  await page.goto('/host');
  const csrf = (await page.context().cookies()).find((c) => /acc_csrf$/.test(c.name))!.value;
  const added = await (await page.request.post('/api/nodes', {data:{name:'spare-mac'}, headers:{'x-acc-csrf':csrf}})).json();
  const key = Buffer.alloc(32, 9).toString('base64');
  const enrolled = await page.request.post('/api/nodes/enroll', {data:{name:'spare-mac', code:added.code, public_key:key,
    info:{os:'darwin', arch:'arm64', version:'0.13.0'}}});
  expect(enrolled.ok(), `${JSON.stringify(added)} ${await enrolled.text()}`).toBe(true);
  await page.reload();
  const command = page.getByRole('region', {name:'the update command for spare-mac'});
  await expect(command).toContainText(/^sh -c 'set -eu; /);
  await expect(command).toContainText('https://acc.example.lan/api/nodes/bundle');
  await expect(command).toContainText(/--cacert \$work\/ca\.pem .*bash \$work\/source\/deploy\/node\/macos\/install\.sh'$/);
  await expect(command).not.toContainText('enroll');
  await expect(command).not.toContainText(added.code);
  const row = page.locator('.divide-y > div').filter({has: command});
  await expect(row.getByText('Keeps the existing enrollment; no new code is needed.', {exact:false})).toBeVisible();
  await expect(page.getByText('next installer release', {exact:false})).toHaveCount(0);
  await expect(page.getByRole('link',{name:'Update this Mac with the new installer'})).toHaveCount(0);
  await expect(row.getByText('Update failed; check the machine helper log')).toHaveCount(0);
  await row.screenshot({path: wikiShotPath(testInfo, 'nodes-mac-update.png')});
  expect((await page.request.post('/api/nodes/spare-mac/remove', {headers:{'x-acc-csrf':csrf}})).ok()).toBe(true);
});

test('pre-v1 enrollment tabs offer from-source commands, or the steps by hand without them', async ({page}) => {
  let fromSource: Record<string, string | null> = {
    windows: "sudo sh -c 'set -eu; echo windows'", mac: "sh -c 'set -eu; echo mac'"};
  await page.route('**/api/nodes', r => r.request().method() === 'POST'
    ? r.fulfill({json:{id:'new',name:'build-box',code:'000000-000000-000000-000000',expires_in:600,
      command:'rynhult-node enroll --primary https://acc.example.lan --trust system --code 000000-000000-000000-000000 --name build-box',
      linux:'curl -fsSL https://acc.example.lan/api/nodes/install.sh | sudo sh',version:'0.14.0',
      trust:'system',verified:true,installers:{},from_source:fromSource}})
    : r.fulfill({json:[]}));
  await page.goto('/host');
  const dialog = page.getByRole('dialog',{name:'Add a machine'});
  const open = async () => {
    await page.getByRole('button',{name:'Add machine',exact:true}).click();
    await dialog.getByPlaceholder('gaming-pc').fill('build-box');
    await dialog.getByRole('button',{name:'Create enrollment code'}).click();
  };
  await open();
  for (const [system, label, tab] of [['Windows','Ubuntu','Windows (WSL)'],['Mac','Mac','Mac']]) {
    await dialog.getByRole('tab',{name:tab,exact:true}).click();
    await expect(dialog.getByText(`This build has no ${system} installer`,{exact:false})).toBeVisible();
    await expect(dialog.getByRole('button',{name:`Copy the ${label} command`,exact:true})).toBeVisible();
    await expect(dialog.getByRole('region',{name:`the ${label} command`})).toHaveText(fromSource[system === 'Mac' ? 'mac' : 'windows']!);
    await expect(dialog.getByRole('button',{name:'Copy the enroll command',exact:true})).toHaveCount(0);
    await expect(dialog.getByRole('link',{name:/Rynhult.*\.(exe|pkg)/})).toHaveCount(0);
  }
  await dialog.getByRole('button',{name:'Done'}).click();
  // An origin the server can't build a command for: the steps by hand and the enroll line.
  fromSource = {windows: null, mac: null};
  await open();
  await dialog.getByRole('tab',{name:'Windows (WSL)',exact:true}).click();
  await expect(dialog.getByText('Install the node by hand',{exact:false})).toBeVisible();
  await expect(dialog.getByRole('region',{name:'the enroll command'})).toHaveText(/^echo 000000-000000-000000-000000 \| sudo rynhult-node enroll --primary https:\/\/acc\.example\.lan --trust system --code - --name build-box$/);
  await dialog.getByRole('tab',{name:'Mac',exact:true}).click();
  await expect(dialog.getByRole('region',{name:'the enroll command'})).toHaveText(/^echo 000000-000000-000000-000000 \| rynhult-node enroll .* --code - --name build-box$/);
});

for (const [kind, message] of [
  ['private repository', 'Repository not found. Private repositories need a token in data/secrets/update_token.'],
  ['rate limit', 'GitHub rate limited update checks. Try again later.'],
] as const) {
  test(`update check explains ${kind} separately`, async ({ page }) => {
    let error: string | null = null;
    await page.route('**/api/updates', route => route.fulfill({ json: { ...state, error, available: false } }));
    await page.route('**/api/updates/check', route => {
      error = message;
      return route.fulfill({ json: { ...state, error, available: false } });
    });
    await page.goto('/settings#updates');
    await page.getByRole('button', { name: 'Check now', exact: true }).click();
    await expect(page.getByText(message, { exact: true })).toBeVisible();
    await expect(page.getByText('Release check failed or rate limited.', { exact: false })).toHaveCount(0);
  });
}

test('machine helper failure detail is visible at desktop and phone widths', async ({ page }) => {
  await page.route('**/api/nodes', route => route.fulfill({ json: [{
    id: 'update-error-node', name: 'build-box', status: 'active', connected: true,
    info: { os: 'linux', version: '0.14.0', build: 'a'.repeat(64), capabilities: ['update'] },
    accounts: [], update_available: true, follow: false,
    update: { ok: false, running: false, detail: 'node bundle version or build mismatch' },
  }] }));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/host');
    await expect(page.getByRole('status').filter({ hasText: 'node bundle version or build mismatch' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  }
});

for (const width of [1440, 390]) test(`release signature and explicit unsigned exception at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  let missing = false;
  let submitted: unknown;
  const primary = '0C2CE12657BD833FB5B563B2B555F6E127990DB5';
  await page.route('**/api/updates', route => route.fulfill({ json: { ...state,
    signature: missing ? { status: 'missing', detail: 'Release signature is missing.' }
      : { status: 'verified', primary, name: 'Rynhult Releases' } } }));
  await page.route('**/api/updates/apply', route => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { queued: true } });
  });
  await page.goto('/settings#updates');
  await expect(page.getByText('Signature verified (Rynhult Releases, key …0DB5)', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Update without signature…', exact: true })).toHaveCount(0);
  missing = true;
  await page.reload();
  await expect(page.getByText('Release signature is missing.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back up and update', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Update without signature…', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('cannot authenticate');
  expect(submitted).toBeUndefined();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(submitted).toBeUndefined();
  await page.getByRole('button', { name: 'Update without signature…', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Update without signature', exact: true }).click();
  await expect.poll(() => submitted).toEqual({ allow_unsigned: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
});
