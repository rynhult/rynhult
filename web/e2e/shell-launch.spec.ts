import {test, expect, type Page} from './fixtures';
import {mkdirSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {AUTH_FILE, csrfHeaders, gitProject, sessionWithChanges, verifyAgain, expectAxeClean, info, PASSWORD, totp} from './helpers';
test.use({ presentationChanges: ['admin_features', 'ui_shell', 'layouts'],storageState: AUTH_FILE});
async function shell(page: Page, enabled=true) {
  let state=await (await page.request.get('/api/admin-features')).json();
  if (!state.features?.find((item:{id:string})=>item.id==='new_shell')?.enabled) {
    await verifyAgain(page);
    state=await (await page.request.get('/api/admin-features')).json();
    const changed=await page.request.put('/api/admin-features/new_shell',{headers:await csrfHeaders(page),data:{enabled:true,revision:state.revision}});
    expect(changed.ok()).toBe(true);
  }
  expect((await page.request.put('/api/preferences/ui-shell',{headers:await csrfHeaders(page),data:{enabled}})).ok()).toBe(true);
}
for(const role of ['owner','member'] as const) for(const theme of ['light','dark'] as const) test(`task-first workspace launch ${role} ${theme}`,async({page,browser})=>{
  await shell(page);
  let memberAccount: string | undefined;
  let member: Awaited<ReturnType<typeof browser.newContext>> | undefined;
  if(role==='member') {
    const account=`launch-${randomUUID().slice(0,6)}-agent`;memberAccount=account;
    mkdirSync(`${info().state}/fixture-accounts/${account}`,{recursive:true});
    const invite=await page.request.post('/api/users/invites',{headers:await csrfHeaders(page),data:{role:'member',account}});
    expect(invite.ok()).toBe(true); const token=(await invite.json()).link.split('/').pop();
    member=await browser.newContext();
    const begin=await member.request.post('/api/auth/invite/begin',{headers:{Origin:info().origin},data:{token}});
    expect((await member.request.post('/api/auth/invite/complete',{headers:{Origin:info().origin},data:{token,username:`launch-${randomUUID().slice(0,6)}`,password:PASSWORD,code:totp((await begin.json()).totp_secret)}})).ok()).toBe(true);
    page=await member.newPage();
    expect((await page.request.put('/api/preferences/ui-shell',{headers:await csrfHeaders(page),data:{enabled:true}})).ok()).toBe(true);
  }
  try {
  const project=await gitProject(page,'cedar-launch');
  await page.emulateMedia({colorScheme:theme,reducedMotion:'reduce'});await page.setViewportSize({width:1440,height:1000});
  await page.goto(`/projects/${project.id}`);
  await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true});
  await expect(launch).toBeVisible();
  await expect(page.getByRole('dialog',{name:'New session',exact:true})).toHaveCount(0);
  const task=launch.getByRole('textbox',{name:'Task (optional)',exact:true});
  await expect(task).toBeFocused();await task.fill('Keep this task as a draft');
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeEnabled({timeout:20000});
  await expect(launch.locator('summary:visible')).toHaveCount(3);
  const primaryBounds=await launch.getByRole('button',{name:'Launch',exact:true}).boundingBox();
  expect(primaryBounds!.y+primaryBounds!.height).toBeLessThanOrEqual(1000);
  await page.screenshot({path:test.info().outputPath(`task-first-collapsed-${role}-${theme}.png`)});
  for(const name of ['Model and working location','Permissions and limits','More options']){
    await launch.locator('summary').filter({hasText:name}).focus();await page.keyboard.press('Enter');
  }
  await expect(launch.getByRole('combobox',{name:'Machine',exact:true})).toBeVisible();
  if(role==='member'&&theme==='light') {
    await launch.getByRole('radio',{name:'Local workspace',exact:true}).click();
    await expect(launch.getByRole('region',{name:'Working location'})).toContainText(memberAccount!);
    await expect(launch.getByRole('region',{name:'Working location'})).not.toContainText(project.folder);
    await launch.getByRole('radio',{name:'Project',exact:true}).click();
  }
  await expectAxeClean(page);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:test.info().outputPath(`task-first-${role}-${theme}.png`)});
  await launch.getByRole('button',{name:'Back to workspace',exact:true}).click();
  await expect(launch).toHaveCount(0);
  await page.getByRole('button',{name:/^New session/}).first().click();
  await expect(task).toHaveValue('Keep this task as a draft');
  expect((await launch.getByRole('heading',{name:'New session',exact:true}).boundingBox())!.y).toBeGreaterThanOrEqual(0);
  await page.goBack();await expect(launch).toHaveCount(0);
  } finally {await member?.close();}
});

for(const theme of ['light','dark'] as const)test(`offline retained target keeps task, account and observed path; reconnect never launches ${theme}`,async({page})=>{
  await page.setViewportSize({width:1440,height:1000});await page.emulateMedia({colorScheme:theme,reducedMotion:'reduce'});
  const project=await gitProject(page,'offline-cedar'); await shell(page);
  const detail=(await (await page.request.get('/api/projects')).json()).find((item:{id:number})=>item.id===project.id);
  const local=(await (await page.request.get(`/api/nodes?launch=1&project_id=${project.id}`)).json())[0];
  const providers=await (await page.request.get(`/api/providers/registry?project_id=${project.id}&machine=local`)).json();
  let offline=false,reads=0,launches=0;
  await page.route('**/api/nodes?launch=1*',route=>{
    reads++;return route.fulfill({json:[local,{...local,name:'cedar-node',label:'cedar-node',connected:!offline,available:!offline,reason:offline?'Offline. Reconnect this machine.':null,last_seen:'2026-11-01T12:00:00Z'}]});
  });
  await page.route('**/api/providers/registry?*',route=>route.fulfill({json:providers}));
  await page.route('**/api/models?*',route=>route.fulfill({json:{}}));
  await page.route('**/api/sessions/preview',async route=>{
    const body=route.request().postDataJSON();
    if(body.machine==='cedar-node'&&offline)return route.fulfill({status:409,json:{detail:'Offline. Reconnect this machine.'}});
    const response=await page.request.post('/api/sessions/preview',{headers:await csrfHeaders(page),data:{...body,machine:'local',clone_on_machine:false}});
    const value=await response.json();
    return route.fulfill({status:response.status(),json:body.machine==='cedar-node'?{...value,machine:'cedar-node',cwd:'/home/example-agent/cedar',checkout:{requires_clone:false,repository:'/home/example-agent/cedar'}}:value});
  });
  page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/sessions')launches++;});
  await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true});
  await launch.getByRole('textbox',{name:'Task (optional)',exact:true}).fill('Keep the original notes private.');
  await launch.locator('summary').filter({hasText:'Model and working location'}).click();
  await launch.getByRole('combobox',{name:'Machine',exact:true}).selectOption('cedar-node');
  await expect(launch.getByRole('region',{name:'Working location'})).toContainText('/home/example-agent/cedar');
  await launch.getByRole('button',{name:'Back to workspace',exact:true}).click();offline=true;
  await page.getByRole('button',{name:/^New session/}).first().click();
  const before=reads;
  await expect(launch.getByRole('complementary',{name:'Offline working location'})).toBeVisible({timeout:20000});
  const notice=launch.getByRole('complementary',{name:'Offline working location'});
  await expect(launch.getByRole('textbox',{name:'Task (optional)',exact:true})).toHaveValue('Keep the original notes private.');
  await expect(launch.getByRole('region',{name:'Working location'})).toContainText(detail.account);
  await expect(launch.getByRole('region',{name:'Working location'})).toContainText('/home/example-agent/cedar');
  await page.screenshot({path:test.info().outputPath('offline-review.png')});
  await expectAxeClean(page);
  await expect(notice).toContainText('Last seen:');await expect(notice).toContainText('Offline. Reconnect this machine.');
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeDisabled();
  await notice.getByRole('button',{name:'Choose another location'}).click();
  await expect(launch.getByRole('combobox',{name:'Machine',exact:true})).toBeFocused();
  offline=false;await notice.getByRole('button',{name:'Check connection'}).click();
  await expect(notice).toHaveCount(0);expect(reads).toBeGreaterThan(before);expect(launches).toBe(0);
  await expect(launch.getByRole('combobox',{name:'Machine',exact:true})).toHaveValue('cedar-node');
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeEnabled({timeout:20000});
  await launch.getByRole('combobox',{name:'Machine',exact:true}).selectOption('local');
  // A recent preview may be cached, so assert the shown target, never a request.
  await expect(launch.getByRole('region',{name:'Working location'})).toContainText(project.folder);
  await expect(launch.getByRole('region',{name:'Working location'})).not.toContainText('/home/example-agent/cedar');
  expect(launches).toBe(0);
  await page.getByRole('main').evaluate(node=>{node.scrollTop=0;});
  await page.screenshot({path:test.info().outputPath('changed-target.png')});
  await page.unrouteAll({behavior:'wait'});
});

for(const result of ['refused','uncertain'] as const)test(`launch ${result} retains draft without replay`,async({page})=>{
  const project=await gitProject(page,`launch-${result}`);await shell(page);
  let calls=0;
  await page.route('**/api/sessions',route=>{
    if(route.request().method()!=='POST')return route.continue();calls++;
    return result==='uncertain'?route.abort('failed'):route.fulfill({status:409,json:{detail:'Security refused this launch.'}});
  });
  await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true}),task=launch.getByRole('textbox',{name:'Task (optional)',exact:true});
  await task.fill('Retain after failure');await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeEnabled({timeout:20000});
  await launch.getByRole('button',{name:'Launch',exact:true}).click();
  await expect(launch.getByRole('alert')).toBeVisible();
  if(result==='uncertain')await expect(launch.getByText('Launch was not confirmed.',{exact:false})).toBeVisible();
  else await expect(launch.getByRole('alert')).toContainText('Security refused this launch.');
  await expect(task).toHaveValue('Retain after failure');expect(calls).toBe(1);
  await page.screenshot({path:test.info().outputPath(`launch-${result}.png`)});
  await launch.getByRole('button',{name:'Back to workspace',exact:true}).click();
  await page.getByRole('button',{name:/^New session/}).first().click();await expect(task).toHaveValue('Retain after failure');expect(calls).toBe(1);
});

test('provider bypass still needs consent and cannot override Security',async({page})=>{
  const project=await gitProject(page,'launch-security');await shell(page);
  await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true});
  await launch.locator('summary').filter({hasText:'Permissions and limits'}).click();
  await launch.getByRole('combobox',{name:'Permissions',exact:true}).selectOption('bypassPermissions');
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeDisabled();
  await launch.getByRole('checkbox',{name:/The agent will run commands without asking/}).check();
  await expect(launch.getByRole('alert')).toContainText(/Security|prompts|permission/i);
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeDisabled();
});

test('explicit fake-provider launch uses existing defaults and clears the consumed draft',async({page})=>{
  const project=await gitProject(page,'shell-success');await shell(page);
  await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true});
  await launch.getByRole('textbox',{name:'Task (optional)',exact:true}).fill('A disposable launch');
  await expect(launch.getByRole('button',{name:'Launch',exact:true})).toBeEnabled({timeout:20000});
  const request=page.waitForRequest(request=>request.method()==='POST'&&new URL(request.url()).pathname==='/api/sessions');
  await launch.getByRole('button',{name:'Launch',exact:true}).click();
  const body=(await request).postDataJSON();expect(body.provider).toBe('claude');expect(body.machine).toBe('local');expect(body.permission_mode).toBe('acceptEdits');expect(body.model).toBe('');
  await expect(page).toHaveURL(/\/sessions\/[^/?]+$/);
  const id=new URL(page.url()).pathname.split('/').pop();
  try {
    await expect(page.locator('.xterm')).toBeVisible({timeout:20000});
    const original=await page.locator('.xterm-helper-textarea').elementHandle();
    const terminalBox=await page.locator('.xterm').boundingBox(), mainBox=await page.getByRole('main').boundingBox();
    expect(terminalBox!.y+terminalBox!.height).toBeGreaterThan(mainBox!.y+mainBox!.height-80);
    await page.getByRole('button',{name:/^New session/}).first().click();
    await expect(launch.getByRole('textbox',{name:'Task (optional)',exact:true})).toHaveValue('');
    await launch.getByRole('button',{name:'Back to workspace',exact:true}).click();
    expect(await original!.evaluate(node=>node===document.querySelector('.xterm-helper-textarea'))).toBe(true);
    await expect(page.locator('.xterm')).toBeVisible();
  } finally {await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)});}
});

test('cancelling brief navigation keeps the workspace instead of opening a hidden launch page',async({page})=>{
  const project=await gitProject(page,'brief-launch-cancel');await shell(page);
  await page.goto(`/projects/${project.id}#project-brief`);
  const brief=page.getByRole('textbox',{name:'Product or feature',exact:true});
  await brief.fill('Unsaved project context');
  await page.getByRole('button',{name:/^New session/}).first().click();
  const warning=page.getByRole('dialog',{name:'Leave with an unsaved project brief?',exact:true});
  await expect(warning).toBeVisible();
  await warning.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(brief).toBeVisible();await expect(brief).toHaveValue('Unsaved project context');
  await page.screenshot({path:test.info().outputPath('cancelled-brief-navigation.png')});
  await expect(page.getByRole('region',{name:'New session',exact:true})).toHaveCount(0);
});

test('workspace launch uses the draft reload warning and exposes the real helper grant',async({page})=>{
  const project=await gitProject(page,'shell-retained-grant');await shell(page);
  await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
  const launch=page.getByRole('region',{name:'New session',exact:true}),task=launch.getByRole('textbox',{name:'Task (optional)',exact:true});
  await task.fill('Retain with native leave protection');
  const prompt=page.waitForEvent('dialog');void page.reload({waitUntil:'domcontentloaded',timeout:5000}).catch(()=>null);
  const warning=await prompt;expect(warning.type()).toBe('beforeunload');await warning.dismiss();
  await expect(task).toHaveValue('Retain with native leave protection');
  await launch.getByRole('combobox',{name:'Run',exact:true}).selectOption('helpers');
  await expect(launch.getByRole('group',{name:'Team limits',exact:true})).toBeVisible();
  await expect(launch.getByRole('combobox',{name:'Team run mode',exact:true})).toBeVisible();
});

test('a slow project list cannot turn a project launch into the first available project',async({page})=>{
  await gitProject(page,'first-cedar');const project=await gitProject(page,'target-cedar');await shell(page);
  let release!:()=>void;const ready=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/api/projects',async route=>{await ready;await route.continue();});
  try {
    await page.goto(`/projects/${project.id}`);await page.getByRole('button',{name:/^New session/}).first().click();
    const launch=page.getByRole('region',{name:'New session',exact:true});await expect(launch).toBeVisible();release();
    await expect(launch.getByRole('combobox',{name:'Project',exact:true,includeHidden:true})).toHaveValue(String(project.id));
  } finally {release();await page.unrouteAll({behavior:'wait'});}
});

test('L1 legacy modal keeps provider selects in its two-column model grid',async({page})=>{
  const project=await gitProject(page,'legacy-options-grid');await shell(page,false);
  await page.setViewportSize({width:1440,height:1000});await page.goto(`/projects/${project.id}`);
  await page.getByRole('main').getByRole('button',{name:'New session',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'New session',exact:true});
  await expect(dialog.getByRole('button',{name:'Launch',exact:true})).toBeEnabled({timeout:20000});
  await dialog.getByRole('button',{name:/^Advanced/}).click();
  const permissions=dialog.getByRole('combobox',{name:'Permissions',exact:true});
  await expect(permissions).toBeVisible();
  const layout=await permissions.evaluate(el=>{
    const grid=el.closest('.grid');
    return {columns:grid?getComputedStyle(grid).gridTemplateColumns.split(' ').length:0,
      width:el.getBoundingClientRect().width,gridWidth:grid?.getBoundingClientRect().width??0,
      containsModel:!!grid?.querySelector('select option[value="__custom"]')};
  });
  expect(layout.columns).toBe(2);expect(layout.containsModel).toBe(true);
  expect(layout.width).toBeLessThan(layout.gridWidth/2+1);
});

test('L2 launch-only navigation preserves dirty file edits without a discard prompt',async({page})=>{
  const id=await sessionWithChanges(page,'launch-dirty-file');await shell(page);
  try {
    await page.goto(`/sessions/${id}?tab=files&file=app.py`);
    const editor=page.getByRole('textbox',{name:'File content',exact:true});
    await expect(editor).toContainText('changed',{timeout:20000});await editor.fill('print("Retain this edit")\n');
    const launch=page.getByRole('region',{name:'New session',exact:true});
    const warning=page.getByRole('dialog',{name:'Discard unsaved edits?',exact:true});
    await page.getByRole('button',{name:/^New session/}).first().click();
    await expect(launch).toBeVisible();await expect(warning).toHaveCount(0);
    await launch.getByRole('button',{name:'Back to workspace',exact:true}).click();
    await expect(editor).toBeVisible();await expect(editor).toHaveText('print("Retain this edit")\n');
    await expect(warning).toHaveCount(0);
    await page.getByRole('button',{name:/^New session/}).first().click();await expect(launch).toBeVisible();
    await page.goBack();await expect(editor).toBeVisible();await expect(warning).toHaveCount(0);
    await expect(editor).toHaveText('print("Retain this edit")\n');
    // P2 retains the dirty file during navigation; only closing that view discards it.
    await page.getByRole('link',{name:'Rynhult',exact:true}).click();
    await expect(warning).toHaveCount(0);
    await page.goBack();
    await expect(editor).toHaveText('print("Retain this edit")\n');
    await page.getByRole('button',{name:'Close view',exact:true}).click();
    const closeWarning=page.getByRole('dialog',{name:'Discard unsaved edits?',exact:true});
    await expect(closeWarning).toBeVisible();
    await closeWarning.getByRole('button',{name:'Cancel',exact:true}).click();
    await expect(editor).toHaveText('print("Retain this edit")\n');
  } finally {await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)});}
});

test('sign-out clears global announcements',async({page})=>{
  await gitProject(page,'announce-signout');await shell(page);
  await page.goto('/projects');
  const status=page.getByRole('status',{name:'Announcements',exact:true});
  await expect(status).toContainText('Projects');
  // The shared fixture session must survive: only the client's sign-out path is under test.
  await page.route('**/api/auth/logout',route=>route.fulfill({json:{}}));
  await page.getByRole('button',{name:/^Account:/}).first().click();
  await page.getByRole('button',{name:'Sign out'}).first().click();
  await expect(status).not.toContainText('Projects');
});
