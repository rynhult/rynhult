import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`Allowed folders retains a draft after verification failure at ${width}px`, async ({ page }) => {
    // A different spec can have freshly verified this shared owner session.
    // Exercise this failure explicitly; Python tests cover the real auth check.
    await page.route('**/api/security/folders/check', route => route.fulfill({
      status: 403, json: { detail: 'Verify your sign-in again in Settings → Passkeys, then try again within five minutes.' },
    }));
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/settings/security/folders');
    await expect(page.getByRole('heading', { name: 'Allowed folders', exact: true })).toBeVisible();
    await expect(page.getByText('It does not sandbox native agent processes.', { exact: false })).toBeVisible();
    await page.getByLabel('Folder to add', { exact: true }).fill('/home/alex/src/demo-api');
    await page.getByRole('button', { name: 'Check folder', exact: true }).click();
    await expect(page.getByText('Verify your sign-in again', { exact: false })).toBeVisible();
    await expect(page.getByLabel('Folder to add', { exact: true })).toHaveValue('/home/alex/src/demo-api');
    await expect(page.getByRole('link', { name: 'Verify your sign-in in Passkeys', exact: true })).toBeVisible();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('Existing folder opens the in-place importer instead of making a checkout', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await page.getByRole('button', { name: 'Use existing folder', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Register a folder', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Name', { exact: true }).fill('Existing demo');
  await dialog.getByLabel('Workspace path', { exact: true }).fill('/home/alex/src/demo-api');
  await expect(dialog.getByText('Use where it is', { exact: false })).toBeVisible();
  await expect(dialog.getByRole('link', { name: 'Allowed folders in Security', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('button', { name: 'Register folder', exact: true }).click();
  await expect(page.getByLabel('Workspace path', { exact: true })).toHaveValue('/home/alex/src/demo-api');
});

// UI fault simulation only; real approval/CAS/worker persistence is covered in
// test_security_folders.py. This is not privileged OS-grant qualification.
for(const outcome of ['applied','pending_policy'] as const) test(`A pending reviewed policy retries with running sessions and gates: ${outcome}`, async ({ page }) => {
  const id = 'a'.repeat(32), digest = 'b'.repeat(64);
  let state = 'legacy', submissions = 0;
  let finishApply: (() => void) | undefined;
  const roots = ['/srv/acc/workspaces/alex'];
  const reviewed = { id, digest, active_sessions: ['running-session'], active_gates: ['running-gate'], warning: 'Policy only. No OS access grant.', preview: { roots, legacy_roots: roots } };
  await page.route('**/api/security/folders', route => route.fulfill({ json: { revision: state === 'legacy' ? 0 : 1, accounts: [{
    scope: { kind: 'account', machine: 'local', machine_id: 'local', account: 'alex-agent' },
    state:state==='pending_policy'?'pending':state, stored_state:state, retry_available:state==='pending', allowed_folders:state==='pending_policy'?false:true, roots, revision: state === 'legacy' ? 0 : 1, applied_revision: state === 'applied' ? 1 : 0,
    detail: state === 'legacy' ? {} : state === 'pending_policy' ? {allowed_folders:true,message:'Unstarted delivery recovered from target status. Review folders again, or restore the acknowledged switch in Security.'} : { operation_id: id, message: 'Waiting for target acknowledgement.' }, projects: [],
  }] } }));
  await page.route('**/api/security/folders/preview', route => route.fulfill({ json: reviewed }));
  await page.route(`**/api/security/folders/${id}/apply`, async route => {
    submissions++;
    await new Promise<void>(resolve => { finishApply = resolve; });
    state = 'pending';
    await route.fulfill({ status: 503, json: { detail: 'The folder service is unavailable.' } });
  });
  await page.route(`**/api/security/folders/operations/${id}`, route => route.fulfill({ json: { ...reviewed, approved: true } }));
  await page.route(`**/api/security/folders/${id}/retry`, route => {
    state = outcome;
    return route.fulfill({ json: { state, revision: 1 } });
  });
  await page.goto('/settings/security/folders');
  await page.getByRole('button', { name: 'Preview folders', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Review allowed folders', exact: true })).toContainText('Running sessions and gates do not prevent this policy reload');
  await page.getByRole('dialog', { name: 'Review allowed folders', exact: true }).getByRole('button', { name: 'Apply folder policy', exact: true }).click();
  await page.getByRole('dialog', { name: 'Apply account folder policy?', exact: true }).getByRole('button', { name: 'Apply folder policy', exact: true }).dblclick();
  await expect.poll(() => submissions).toBe(1);
  finishApply!();
  await expect(page.getByRole('button', { name: 'Retry reviewed application', exact: true })).toBeVisible();
  await page.getByRole('dialog', { name: 'Review allowed folders', exact: true }).getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('button', { name: 'Review pending application', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Review allowed folders', exact: true })).toContainText(roots[0]);
  await page.getByRole('button', { name: 'Retry reviewed application', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Review allowed folders', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Review pending application', exact: true })).toHaveCount(0);
  if(outcome==='pending_policy') {
    await expect(page.getByText('Unstarted delivery recovered from target status.',{exact:false})).toBeVisible();
    await expect(page.getByRole('button',{name:'Preview folders',exact:true})).toBeEnabled();
    await expect(page.getByText('Last acknowledged folder check: On.',{exact:false})).toBeVisible();
    await expect(page.getByRole('link',{name:'Open Security to restore this setting',exact:true})).toHaveAttribute('href',/security_scope=account.*alex-agent#security$/);
  }
  expect(submissions).toBe(1);
});

test('An offline folder check is unavailable rather than an OS permission diagnosis', async ({ page }) => {
  await page.route('**/api/security/folders/check', route => route.fulfill({ json: {
    state: 'unavailable', message: 'The disposable account service is offline. No OS access result is known.',
  } }));
  await page.goto('/settings/security/folders');
  await page.getByLabel('Folder to add', { exact: true }).fill('/home/alex/src/demo-api');
  await page.getByRole('button', { name: 'Check folder', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Unavailable.' })).toBeVisible();
  await expect(page.getByLabel('Folder to add', { exact: true })).toHaveValue('/home/alex/src/demo-api');
  await expect(page.getByText('Access checked.', { exact: false })).toHaveCount(0);
});

test('A re-enabled folder policy needs a fresh review rather than an old retry', async ({ page }) => {
  await page.route('**/api/security/folders', route => route.fulfill({ json: { revision: 4, accounts: [{
    scope: { kind: 'account', machine: 'local', machine_id: 'local', account: 'alex-agent' },
    state: 'pending', stored_state: 'pending_policy', retry_available:false, allowed_folders: true, roots: ['/home/alex/src/demo'], revision: 1, applied_revision: 1,
    detail: { allowed_folders: false, policy_revision: 4, operation_id: 'a'.repeat(32) }, projects: [],
  }] } }));
  await page.goto('/settings/security/folders');
  await expect(page.getByText('Pending application', { exact: true })).toBeVisible();
  await expect(page.getByText('Applied', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Preview folders', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Review pending application', exact: true })).toHaveCount(0);
});

for(const width of [1440,390]) test(`W4 real derived pending preserves recovery after reload at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:900});
  await page.route('**/api/security/folders',route=>route.fulfill({json:{revision:8,accounts:[{
    scope:{kind:'account',machine:'local',machine_id:'local',account:'alex-agent'},
    state:'pending',stored_state:'pending_policy',retry_available:false,allowed_folders:true,
    roots:['/home/alex-agent/workspaces'],revision:2,applied_revision:2,
    detail:{allowed_folders:false,operation_id:'a'.repeat(32),recovered_from:'b'.repeat(32),message:'Review folders again, or restore the acknowledged switch in Security.'},projects:[],
  }]}}));
  await page.goto('/settings/security/folders');
  for(let view=0;view<2;view++) {
    await expect(page.getByText('Last acknowledged folder check: Off.',{exact:false})).toBeVisible();
    await expect(page.getByRole('link',{name:'Open Security to restore this setting',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'Review pending application',exact:true})).toHaveCount(0);
    if(view===0)await page.reload();
  }
});

test('W1 owner reviews acknowledged policy before closing a stuck delivery',async({page})=>{
  const id='c'.repeat(32),digest='d'.repeat(64),recoveryDigest='e'.repeat(64);
  let recovered=false;
  await page.route('**/api/security/folders',route=>route.fulfill({json:{revision:3,accounts:[{
    scope:{kind:'account',machine:'local',machine_id:'local',account:'alex-agent'},
    state:'pending',stored_state:recovered?'pending_policy':'pending',retry_available:!recovered,allowed_folders:false,
    roots:['/home/alex-agent/workspaces'],revision:recovered?2:1,applied_revision:recovered?2:0,
    detail:recovered?{allowed_folders:true,message:'Delivery closed; acknowledged folder policy reloaded at a newer revision.'}:{operation_id:id},projects:[],
  }]}}));
  await page.route(`**/api/security/folders/operations/${id}`,route=>route.fulfill({json:{
    id,digest,approved:true,retry_available:false,active_sessions:['running'],active_gates:[],warning:'Recovery changes later service access.',
    preview:{roots:['/home/alex-agent/workspaces','/home/alex/src/deleted'],legacy_roots:[]},
    recovery:{digest:recoveryDigest,roots:['/home/alex-agent/workspaces'],allowed_folders:true,revision:0},
  }}));
  await page.route(`**/api/security/folders/${id}/recover`,route=>{
    expect(route.request().postDataJSON()).toEqual({digest,recovery_digest:recoveryDigest});
    recovered=true;
    return route.fulfill({json:{state:'pending_policy',revision:2}});
  });
  await page.goto('/settings/security/folders');
  await page.getByRole('button',{name:'Review pending application',exact:true}).click();
  const review=page.getByRole('dialog',{name:'Review allowed folders',exact:true});
  await expect(review).toContainText('Acknowledged folder check: On');
  await review.getByRole('button',{name:'Recover acknowledged policy',exact:true}).click();
  const confirmation=page.getByRole('dialog',{name:'Recover acknowledged folder policy?',exact:true});
  await expect(confirmation).toContainText('running sessions');
  await confirmation.getByRole('button',{name:'Recover acknowledged policy',exact:true}).click();
  await expect(review).toHaveCount(0);
  await expect(page.getByText('Last acknowledged folder check: On.',{exact:false})).toBeVisible();
  await page.reload();
  await expect(page.getByText('Last acknowledged folder check: On.',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'Review pending application',exact:true})).toHaveCount(0);
});
