import { environmentValue } from "../../src/acc/env.mjs";
import { execFileSync } from 'node:child_process';
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owners see capture age, protection limits and confirm recovery kit export', async ({ page }, testInfo) => {
  let exported = false;
  const capturedNow = new Date();
  await page.clock.setFixedTime(capturedNow);
  const now = capturedNow.getTime() / 1000;
  await page.route('**/api/host/backups', r => r.fulfill({ json: {
    targets: [{id:'a'.repeat(32),repository:'/mnt/backup/acc',enabled:false,captured_at:now-1800,
      verified_at:now-60,restore_tested_at:now-60,backup_id:'b'.repeat(32),stale:false,
      status:'backed up, not protected',last_job:null}], protected:false,quarterly_due:true,drill:{},
    coverage:'Rynhult database, secrets, catalog overlay, project bare git repositories. Main agent state needs separate backups.'
  }}));
  await page.route('**/api/host/backups/kit', r => {
    exported = true;
    return r.fulfill({json:{format:1,instructions:'Example kit'}, headers:{'Content-Disposition':'attachment; filename="acc-recovery-kit.json"'}});
  });
  await page.goto('/host');
  await expect(page.getByRole('heading',{name:'Backups',exact:true})).toBeVisible();
  await expect(page.getByTestId('backup-status')).toHaveText('No off-machine backup yet · Last local backup 30m ago · scheduled backups off · restore test due');
  await expect(page.getByText('backed up, not protected',{exact:true})).toHaveCount(0);  // details on request
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await expect(page.getByText('backed up, not protected',{exact:true})).toBeVisible();
  await expect(page.getByText('Quarterly restore test due',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Export recovery kit',exact:true}).click();
  await expect(page.getByRole('dialog')).toContainText('password manager');
  expect(exported).toBe(false);
  const download = page.waitForEvent('download');
  await page.getByRole('dialog').getByRole('button',{name:'Export recovery kit',exact:true}).click();
  expect((await download).suggestedFilename()).toBe('acc-recovery-kit.json');
  expect(exported).toBe(true);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const a11y = await new AxeBuilder({page}).include('#host-backups').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await page.locator('#host-backups').screenshot({path:wikiShotPath(testInfo, 'host-backups.png')});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
});

test('members neither see nor request host backups', async ({page}) => {
  let requests = 0;
  await page.route('**/api/auth/state', r => r.fulfill({json:{authenticated:true,setup_required:false,
    username:'demo-member',role:'member',account:'demo-agent',instance:{host:'acc-host'}}}));
  await page.route('**/api/host/backups**', r => {requests++; return r.fulfill({status:403,json:{detail:'owners only'}});});
  await page.goto('/host');
  await expect(page.getByRole('heading',{name:'Host',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Backups',exact:true})).toHaveCount(0);
  expect(requests).toBe(0);
});

test('without a target, Backups is one amber line that says how to set it up', async ({ page }) => {
  await page.route('**/api/host/backups', r => r.fulfill({ json: { targets: [], quarterly_due: false, drill: {}, coverage: 'Rynhult database and secrets.' } }));
  await page.goto('/host');
  await expect(page.getByTestId('backup-status')).toHaveText('Not set up · no backup target yet');
  await expect(page.getByTestId('backup-status')).toHaveClass(/text-wait/);
  await expect(page.locator('#host-backups').getByRole('link', { name: 'Set up' })).toHaveAttribute('href', '/host/backups/setup');
  await expect(page.getByText('Ask your local administrator')).toHaveCount(0);
});

test('owner prepares a Linux target, reads exact commands and checks setup', async ({ page }, testInfo) => {
  const id = 'c'.repeat(32);
  let checked = false;
  await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{id,kind:'node',repository:'enrolled:archive-node',enabled:false,setup_verified_at:null,status:'no verified backup',stale:true}],coverage:'Rynhult backup',drill:{}}}));
  await page.route(`**/api/host/backups/${id}/setup`, r => r.fulfill({json:{max_size_bytes:32212254720,commands:execFileSync('python3', ['-c', "from acc.backup_targets import setup_commands; print(setup_commands({'id':'c'*32,'max_size_bytes':32212254720},'d'*32,'{SHA}EXAMPLE'))"], {cwd:'..',env:{...process.env,PYTHONPATH:'src'}}).toString()}}));
  await page.route(`**/api/host/backups/${id}/check`, r => { checked = true; return r.fulfill({json:{id,setup_verified_at:Date.now()/1000}}); });
  await page.goto('/host');
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await page.getByRole('link',{name:'Target setup',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Target setup',exact:true})).toBeVisible();
  await expect(page.getByRole('region',{name:'Target commands'})).toContainText('--append-only');
  await page.getByRole('button',{name:'Check setup',exact:true}).click();
  await expect(page.getByText('Setup verified. Export a new recovery kit, then run a test backup.')).toBeVisible();
  expect(checked).toBe(true);
  const a11y = await new AxeBuilder({page}).include('main').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await page.screenshot({path:testInfo.outputPath('target-setup.png')});
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.screenshot({path:'../docs/wiki/.attachments/backup-target-setup.png'});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
});

test('owner lists historical generations and requests an explicit isolated restore test', async ({page}, testInfo) => {
  const id = 'a'.repeat(32), backup = 'b'.repeat(32), genuine = 'c'.repeat(64), impostor = 'd'.repeat(64);
  let requested = {backup_id:'', snapshot_id:''};
  await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{id,kind:'node',setup_verified_at:1,repository:'enrolled:archive-node',status:'append-only receiver · independent offline copy still required',enabled:false}],drill:{}}}));
  await page.route(`**/api/host/backups/${id}/snapshots`, r => r.fulfill({json:{verified_missing:false,items:[
    // A compromised main can append a snapshot reusing an old backup ID: both rows stay selectable by snapshot ID.
    {backup_id:backup,snapshot_id:genuine,time:'2026-10-23T00:00:00Z',duplicate:true},
    {backup_id:backup,snapshot_id:impostor,time:'2026-10-23T05:00:00Z',duplicate:true}]}}));
  await page.route(`**/api/host/backups/${id}/verify`, r => { requested=r.request().postDataJSON(); return r.fulfill({json:{state:'queued'}}); });
  await page.goto('/host');
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await page.getByRole('button',{name:'List backups',exact:true}).click();
  await expect(page.getByText(backup,{exact:true})).toHaveCount(2);
  await expect(page.getByText('Only the snapshot ID you recorded identifies the genuine backup.',{exact:false})).toBeVisible();
  await page.locator('#host-backups').screenshot({path:wikiShotPath(testInfo, 'backup-history.png')});
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.locator('#host-backups').screenshot({path:'../docs/wiki/.attachments/backup-history.png'});
  await page.getByRole('button',{name:`Test restore of snapshot ${genuine.slice(0, 12)}`,exact:true}).click();
  await expect.poll(() => requested).toEqual({confirmed:true,backup_id:backup,snapshot_id:genuine});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
});

test('owner records a restore drill of an older generation, not only the latest', async ({page}) => {
  const id = 'a'.repeat(32), older = 'e'.repeat(32), latest = 'f'.repeat(32);
  let recorded: {target_id?: string; backup_id?: string} = {};
  await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{id,kind:'node',setup_verified_at:1,backup_id:latest,repository:'enrolled:archive-node',status:'append-only receiver · independent offline copy still required',enabled:false}],drill:{}}}));
  await page.route(`**/api/host/backups/${id}/snapshots`, r => r.fulfill({json:{verified_missing:false,items:[
    {backup_id:older,snapshot_id:'1'.repeat(64),time:'2026-07-01T00:00:00Z',duplicate:true},
    {backup_id:older,snapshot_id:'3'.repeat(64),time:'2026-07-02T00:00:00Z',duplicate:true},
    {backup_id:latest,snapshot_id:'2'.repeat(64),time:'2026-10-23T00:00:00Z',duplicate:false}]}}));
  await page.route('**/api/host/backups/drill', r => { recorded=r.request().postDataJSON(); return r.fulfill({json:{ok:true}}); });
  await page.goto('/host');
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await page.getByRole('button',{name:'List backups',exact:true}).click();
  await page.getByRole('button',{name:`Record restore drill of snapshot ${'3'.repeat(12)}`,exact:true}).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(older);
  await dialog.getByRole('button',{name:'Record restore drill',exact:true}).click();
  await expect.poll(() => recorded).toEqual({confirmed:true,target_id:id,backup_id:older,snapshot_id:'3'.repeat(64)});
});

test('history warns when the target no longer lists the last verified backup', async ({page}) => {
  const id = 'a'.repeat(32);
  await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{id,kind:'node',setup_verified_at:1,repository:'enrolled:archive-node',status:'append-only receiver · independent offline copy still required',enabled:false}],drill:{}}}));
  await page.route(`**/api/host/backups/${id}/snapshots`, r => r.fulfill({json:{verified_missing:true,items:[{backup_id:'b'.repeat(32),snapshot_id:'c'.repeat(64),time:'2026-10-23T00:00:00Z',duplicate:false}]}}));
  await page.goto('/host');
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await page.getByRole('button',{name:'List backups',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('no longer lists the last verified backup');
  await expect(page.getByText('Duplicate ID')).toHaveCount(0);
});


test('owner can select only active Linux machines for target preparation', async ({page}, testInfo) => {
  let prepared = '';
  await page.route('**/api/nodes', r => r.fulfill({json:[{id:'a'.repeat(16),name:'archive-node',status:'active',info:{os:'linux'}},{id:'b'.repeat(16),name:'desktop-node',status:'active',info:{os:'darwin'}}]}));
  await page.route('**/api/host/backups/targets', r => {prepared=r.request().postDataJSON().node_id;return r.fulfill({status:201,json:{id:'c'.repeat(32)}});});
  await page.route('**/api/host/backups/*/setup', r => r.fulfill({json:{commands:'# Example owner setup'}}));
  await page.goto('/host/backups/setup');
  await expect(page.getByRole('option',{name:'desktop-node'})).toHaveCount(0);
  await page.getByLabel('Machine to keep the backups on').selectOption('a'.repeat(16));
  await page.screenshot({path:wikiShotPath(testInfo, 'backup-target-machine.png')});
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.screenshot({path:'../docs/wiki/.attachments/backup-target-machine.png'});
  await page.getByRole('button',{name:'Prepare target',exact:true}).click();
  await expect(page.getByRole('region',{name:'Target commands'})).toBeVisible();
  expect(prepared).toBe('a'.repeat(16));
});

for (const refused of [false, true]) {
  test(`Copy target commands preserves the setup block (${refused ? 'clipboard refused' : 'clipboard allowed'})`, async ({ page }) => {
    const commands = "# Root setup\nset -eu\nprintf '%s\\n' 'example'\n";
    await page.route('**/api/host/backups/*/setup', r => r.fulfill({ json: { commands } }));
    await page.addInitScript(({ refused }) => {
      Object.defineProperty(navigator, 'clipboard', { value: { writeText: async (text: string) => {
        if (refused) throw new Error('Clipboard refused');
        (window as unknown as { copied: string }).copied = text;
      } } });
    }, { refused });
    await page.goto(`/host/backups/setup?target=${'c'.repeat(32)}`);
    await page.getByRole('button', { name: 'Copy target commands', exact: true }).click();
    if (refused) {
      await expect(page.getByRole('main').getByText('Clipboard unavailable. Text selected; copy it manually.')).toBeVisible();
      expect(await page.evaluate(() => getSelection()?.getRangeAt(0).toString())).toBe(commands);
    } else {
      // The page's own result, not the Announcements live region, which also says "Copied" (outside main).
      await expect(page.getByRole('main').getByText('Copied', { exact: true })).toBeVisible();
      expect(await page.evaluate(() => (window as unknown as { copied: string }).copied)).toBe(commands);
    }
  });
}

test('repository ceiling defaults from disk capacity and is chosen before commands', async ({ page }) => {
  let prepared: unknown;
  await page.route('**/api/nodes', r => r.fulfill({ json: [
    { id: 'a'.repeat(16), name: 'archive-node', status: 'active', info: { os: 'linux' }, backup_default_max_size_bytes: 50 * 2**30 },
    { id: 'b'.repeat(16), name: 'spare-node', status: 'active', info: { os: 'linux' }, backup_default_max_size_bytes: 20 * 2**30 },
  ] }));
  await page.route('**/api/host/backups/targets', r => {
    prepared = r.request().postDataJSON();
    return r.fulfill({ status: 201, json: { id: 'c'.repeat(32) } });
  });
  await page.route('**/api/host/backups/*/setup', r => r.fulfill({ json: { commands: '# Selected ceiling: 30 GiB' } }));
  await page.goto('/host/backups/setup');
  await page.getByLabel('Machine to keep the backups on').selectOption('a'.repeat(16));
  await expect(page.getByLabel('Maximum backup space (GiB)')).toHaveValue('50');
  await page.getByLabel('Machine to keep the backups on').selectOption('b'.repeat(16));
  await expect(page.getByLabel('Maximum backup space (GiB)')).toHaveValue('20');
  await page.getByLabel('Maximum backup space (GiB)').fill('0');
  await page.getByRole('button', { name: 'Prepare target', exact: true }).click();
  expect(prepared).toBeUndefined();
  await page.getByLabel('Maximum backup space (GiB)').fill('30');
  await page.getByRole('button', { name: 'Prepare target', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Target commands' })).toContainText('30 GiB');
  expect(prepared).toEqual({ node_id: 'b'.repeat(16), max_size_bytes: 30 * 2**30 });
});

test('owner prepares S3 with write-only credentials and sees immutability guidance', async ({ page }, testInfo) => {
  const id = 'e'.repeat(32);
  let submitted: Record<string, unknown> = {};
  await page.route('**/api/host/backups/s3-targets', r => {
    submitted = r.request().postDataJSON();
    return r.fulfill({ status:201, json:{id} });
  });
  await page.route(`**/api/host/backups/${id}/setup`, r => r.fulfill({ json:{
    kind:'s3', id, endpoint:'https://s3.example.com', bucket:'demo-backups', region:'auto',
    prefix:`acc/${'d'.repeat(32)}/${id}`, setup_verified_at:null,
  } }));
  await page.route(`**/api/host/backups/${id}/check`, r => r.fulfill({ json:{kind:'s3',id,setup_verified_at:1,immutability:null,capabilities:{versioning:false,mode:null,default_days:0}} }));
  await page.goto('/host/backups/setup');
  await page.getByLabel('Target type').selectOption('s3');
  await page.getByLabel('Endpoint').fill('https://s3.example.com');
  await page.getByLabel('Bucket', {exact:true}).fill('demo-backups');
  await page.getByLabel('Region', {exact:true}).fill('auto');
  await page.getByLabel('Access key ID').fill('EXAMPLE');
  await page.getByLabel('Secret access key', {exact:true}).fill('example-secret');
  const a11y = await new AxeBuilder({page}).include('main').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(a11y.violations).toEqual([]);
  // Clear artificial secrets before the guide screenshot.
  await page.getByLabel('Secret access key', {exact:true}).fill('');
  await page.getByLabel('Access key ID').fill('');
  await page.screenshot({path:wikiShotPath(testInfo, 'backup-s3-setup.png')});
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.screenshot({path:'../docs/wiki/.attachments/backup-s3-setup.png'});
  await page.setViewportSize({width:390,height:844});
  const mobileA11y = await new AxeBuilder({page}).include('main').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(mobileA11y.violations).toEqual([]);
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.screenshot({path:'../docs/wiki/.attachments/backup-s3-setup-phone.png',fullPage:true});
  await page.setViewportSize({width:1440,height:900});
  await page.getByLabel('Access key ID').fill('EXAMPLE');
  await page.getByLabel('Secret access key', {exact:true}).fill('example-secret');
  await page.getByRole('button', {name:'Prepare S3 target'}).click();
  expect(submitted).toEqual({endpoint:'https://s3.example.com',bucket:'demo-backups',region:'auto',
    credentials:{access_key_id:'EXAMPLE',secret_access_key:'example-secret'}});
  await expect(page.getByText('Separate maintenance credentials', {exact:true})).toBeVisible();
  if (environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === '1') await page.screenshot({path:'../docs/wiki/.attachments/backup-s3-check.png',fullPage:true});
  await expect(page.locator('main')).not.toContainText('example-secret');
  await page.getByRole('button',{name:'Check setup',exact:true}).click();
  await expect(page.locator('main').getByRole('status')).toContainText('not protected');
  await expect(page.getByText('Observed versioning:', {exact:false})).toContainText('Unverified or unsupported');
  let rotated = false;
  await page.route(`**/api/host/backups/${id}/credentials`, r => {rotated = true; return r.fulfill({json:{id,kind:'s3'}});});
  await page.getByText('Replace writer credentials',{exact:true}).click();
  await page.getByLabel('New access key ID').fill('EXAMPLE');
  await page.getByLabel('New secret access key',{exact:true}).fill('example-replacement');
  await page.getByRole('button',{name:'Save writer credentials',exact:true}).click();
  await expect(page.getByText('Writer replaced. Check setup before capturing again.')).toBeVisible();
  await expect(page.getByLabel('New secret access key',{exact:true})).toHaveValue('');
  expect(rotated).toBe(true);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
});


test('owner can pause hourly capture before independent maintenance', async ({page}) => {
  const id = 'a'.repeat(32);
  let paused = false;
  await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{id,kind:'s3',enabled:true,
    setup_verified_at:1,repository:'s3:https://s3.example.com/demo-backups/acc/demo',status:'backed up, not protected'}],drill:{}}}));
  await page.route(`**/api/host/backups/${id}/disable`, r => {paused=true;return r.fulfill({json:{}});});
  await page.goto('/host');
  await page.getByRole('button',{name:'Details',exact:true}).click();
  await page.getByRole('button',{name:'Pause scheduled backups',exact:true}).click();
  await expect.poll(() => paused).toBe(true);
  await expect(page.getByRole('button',{name:'Prune backups',exact:true})).toHaveCount(0);
});

for (const width of [1440, 390]) {
  test(`repository lock details and confirmed stale-only recovery (${width})`, async ({page}, testInfo) => {
    const id = 'a'.repeat(32);
    let requested = 0;
    await page.setViewportSize({width, height:900});
    await page.route('**/api/host/backups', r => r.fulfill({json:{targets:[{
      id,kind:'node',setup_verified_at:1,repository:'enrolled:archive-node',enabled:false,status:'append-only receiver',
      last_job:{state:'failed',repository_locked:true,error:'repository locked (restic check exit 11)',
        locks:[{hostname:'archive.example.lan',pid:2718,age_seconds:7200,exclusive:true}]}
    }],drill:{}}}));
    await page.route(`**/api/host/backups/${id}/unlock`, r => {
      expect(r.request().postDataJSON()).toEqual({confirmed:true});
      requested++;
      return r.fulfill({status:202,json:{state:'queued'}});
    });
    await page.goto('/host');
    await page.getByRole('button',{name:'Details',exact:true}).click();
    await expect(page.getByText('archive.example.lan · PID 2718 · age 7200s · exclusive', {exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Remove stale lock',exact:true}).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Active locks are never forced');
    await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
    expect(requested).toBe(0);
    await page.getByRole('button',{name:'Remove stale lock',exact:true}).click();
    await page.getByRole('dialog').screenshot({path:testInfo.outputPath(`backup-lock-${width}.png`)});  // no Guide image uses it
    await dialog.getByRole('button',{name:'Remove stale lock',exact:true}).click();
    await expect.poll(() => requested).toBe(1);
    await expect(page.getByRole('status').filter({hasText:'Stale lock check queued'})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
  });
}

for (const width of [1440, 390]) {
  test(`removed machine targets and retryable capture admission (${width})`, async ({page}) => {
    const id = 'a'.repeat(32);
    let removed = false;
    let unavailable = false;
    await page.setViewportSize({width,height:900});
    await page.route('**/api/host/backups', r => r.fulfill({json:{targets:removed ? [] : [{id,kind:'node',
      machine_removed:!unavailable,enabled:false,setup_verified_at:1,repository:'enrolled:archive-box',
      status:unavailable ? 'backed up, not protected' : 'machine removed',stale:false}],drill:{}}}));
    await page.route(`**/api/host/backups/${id}/remove`, r => {
      expect(r.request().postDataJSON()).toEqual({confirmed:true});
      removed = true;
      return r.fulfill({json:{targets:[],drill:{}}});
    });
    await page.route(`**/api/host/backups/${id}/run`, r => r.fulfill({status:409,json:{detail:'A backup is running; try again shortly.'}}));
    await page.goto('/host');
    await page.getByRole('button',{name:'Details',exact:true}).click();
    const backups = page.locator('#host-backups');
    await expect(backups).toContainText('machine removed');
    await expect(backups).not.toContainText('enrolled:archive-box');
    await expect(backups.getByRole('button',{name:'Enable scheduled backups',exact:true})).toHaveCount(0);
    await expect(backups.getByRole('button',{name:'Back up now',exact:true})).toHaveCount(0);
    await backups.screenshot({path:`../.acc/artifacts/backup-removed-${width}.png`});
    await backups.getByRole('button',{name:'Remove target',exact:true}).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Capture history, recovery keys and audit records are kept');
    await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
    expect(removed).toBe(false);
    await backups.getByRole('button',{name:'Remove target',exact:true}).click();
    await page.getByRole('dialog').getByRole('button',{name:'Remove target',exact:true}).click();
    await expect(backups.getByRole('button',{name:'Remove target',exact:true})).toHaveCount(0);
    unavailable = true; removed = false;
    await page.reload();
    await page.getByRole('button',{name:'Details',exact:true}).click();
    await backups.getByRole('button',{name:'Back up now',exact:true}).click();
    await expect(backups.getByText('A backup is running; try again shortly.',{exact:true})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
  });
}

for (const width of [1440,390]) for (const colorScheme of ['light','dark'] as const) {
  test(`backup cost scenario and schedule at ${width}px ${colorScheme}`, async ({page},testInfo) => {
    await page.setViewportSize({width,height:900});
    await page.emulateMedia({colorScheme});
    const id = 'd'.repeat(32);
    let capture = 86400;
    let saves = 0;
    await page.route('**/api/host/backups',r => r.fulfill({json:{targets:[{
      id,kind:'s3',repository:'s3:https://s3.example.com/backups/example',enabled:true,status:'backed up, not protected',
      setup_verified_at:1,schedule_seconds:capture,verify_interval_seconds:604800,stale:false,
      cost_estimate:{captures:30*86400/capture,full_verifications:30/7,upload_bytes:10000000*30*86400/capture,download_bytes:12000000000,
        s3_policy_requests:409*30*86400/capture,restic_requests:30*30*86400/capture+17*30/7,basis:'30-day scenario from the last incremental capture; repository history changes actual traffic.'}
    }],quarterly_due:false,drill:{},coverage:'Synthetic backup target.'}}));
    await page.route(`**/api/host/backups/${id}/schedule`,async r => {
      expect(r.request().postDataJSON()).toEqual({confirmed:true,schedule_seconds:3600,verify_interval_seconds:604800});
      saves++;capture=3600;await r.fulfill({json:{ok:true}});
    });
    await page.goto('/host#host-backups');
    const region = page.locator('#host-backups');
    await expect(region.getByText('Cloud providers bill egress', {exact:false})).toBeVisible();
    await expect(region.getByTestId('backup-cost-estimate')).toContainText('12,270 requests');
    await expect(region.getByTestId('backup-cost-estimate')).toContainText('973 native restic requests');
    await expect(region.getByRole('combobox',{name:'Capture',exact:true})).toHaveValue('86400');
    await region.screenshot({path:wikiShotPath(testInfo,`backup-cost-${width}-${colorScheme}.png`)});
    await region.getByRole('combobox',{name:'Capture',exact:true}).selectOption('3600');
    await region.getByRole('button',{name:'Save schedule',exact:true}).focus();
    await page.keyboard.press('Enter');
    await expect(region.getByRole('status').filter({hasText:'Backup schedule saved.'})).toBeVisible();
    expect(saves).toBe(1);
    await expect(region.getByTestId('backup-cost-estimate')).toContainText('294,480 requests');
    expect(await page.evaluate(() => document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(0);
    const a11y = await new AxeBuilder({page}).include('#host-backups').withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    expect(a11y.violations).toEqual([]);

  });
}

for (const width of [1440, 390]) {
  test(`cloud target removal keeps bucket data (${width})`, async ({page}) => {
    const id = 'e'.repeat(32);
    let removed = false;
    let pending = true;
    let calls = 0;
    await page.setViewportSize({width,height:900});
    await page.route('**/api/host/backups', r => r.fulfill({json:{targets:removed ? [] : [{
      id,kind:'s3',enabled:false,repository:'s3:https://s3.example.com/backups/example',
      status:'disabled',stale:false,last_job:pending ? {state:'running'} : undefined,
    }],drill:{}}}));
    await page.route(`**/api/host/backups/${id}/remove`, r => {
      expect(r.request().postDataJSON()).toEqual({confirmed:true});
      calls++;
      removed = true;
      return r.fulfill({json:{targets:[],drill:{}}});
    });
    await page.goto('/host');
    await page.getByRole('button',{name:'Details',exact:true}).click();
    const backups = page.locator('#host-backups');
    const remove = backups.getByRole('button',{name:'Remove target',exact:true});
    await expect(remove).toBeDisabled();
    pending = false;
    await page.reload();
    await page.getByRole('button',{name:'Details',exact:true}).click();
    await remove.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Data in the bucket stays until you delete it there.');
    await expect(dialog).toContainText('Object Lock may keep it for the retention period.');
    await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
    expect(calls).toBe(0);
    await remove.click();
    await dialog.getByRole('button',{name:'Remove target',exact:true}).click();
    await expect(remove).toHaveCount(0);
    expect(calls).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(0);
  });
}
