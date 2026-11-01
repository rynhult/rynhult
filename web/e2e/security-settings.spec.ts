import {execFileSync} from 'node:child_process';
import {environmentValue} from '../../src/acc/env.mjs';
import {expect,test} from '@playwright/test';
import {AUTH_FILE,csrfHeaders,freshStep,gitProject,expectAxeClean,loadSecret,PASSWORD,totp,wikiShotPath,info} from './helpers';
test.use({storageState:AUTH_FILE});
test.setTimeout(90000);

for(const theme of ['dark','light'] as const) test(`Security scope editor in ${theme}`,async({page},info)=>{
  await page.emulateMedia({colorScheme:theme});
  await page.goto('/settings#security');
  const panel=page.locator('#security');
  await expect(panel.getByRole('heading',{name:'Security',exact:true})).toBeVisible();
  await expect(panel.getByRole('combobox',{name:'Security scope',exact:true})).toHaveValue('instance');
  const registry=await (await page.request.get('/api/security/scopes')).json();
  for(const row of registry.registry) await expect(panel.getByText(row.label,{exact:true})).toBeVisible();
  await panel.getByRole('combobox',{name:'Security mode',exact:true}).selectOption('unrestricted');
  await panel.getByRole('button',{name:'Review changes',exact:true}).click();
  const verify=page.getByRole('dialog',{name:'Verify your sign-in',exact:true});
  const review=page.getByRole('dialog',{name:'Review Security changes',exact:true});
  await expect(verify.or(review)).toBeVisible();
  if(await verify.isVisible()) {
    await freshStep(page);
    await verify.getByLabel('Verification password').fill(PASSWORD);
    await verify.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
    await verify.getByRole('button',{name:'Verify with password and authenticator'}).click();
  }
  await expect(review).toBeVisible();
  await expect(review).toContainText('Authentication, owner approvals');
  await expect(review).toContainText('account folder check still on');
  for(const row of registry.registry) await expect(review.getByText(row.label,{exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await expectAxeClean(page);
  await page.screenshot({path:wikiShotPath(info,`security-review-${info.project.name}-${theme}.png`)});
  await review.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(panel.getByRole('button',{name:'Review changes',exact:true})).toBeFocused();
  expect((await (await page.request.get('/api/security/scopes')).json()).revision).toBe(registry.revision);
});


async function reviewChanges(page:import('@playwright/test').Page,label='Review changes') {
  await page.locator('#security').getByRole('button',{name:label,exact:true}).click();
  const review=page.getByRole('dialog',{name:'Review Security changes',exact:true});
  const verify=page.getByRole('dialog',{name:'Verify your sign-in',exact:true});
  await expect(review.or(verify)).toBeVisible();
  if(await verify.isVisible()) {
    await freshStep(page);
    await verify.getByLabel('Verification password').fill(PASSWORD);
    await verify.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
    await verify.getByRole('button',{name:'Verify with password and authenticator',exact:true}).click();
  }
  await expect(review).toBeVisible();return review;
}
async function saveScope(page:import('@playwright/test').Page,scope:unknown,overrides:Record<string,boolean>,preset='custom') {
  const state=await (await page.request.get('/api/security/scopes')).json(),headers=await csrfHeaders(page);
  const p=await page.request.post('/api/security/changes/preview',{headers,data:{scope,overrides,preset,revision:state.revision}});
  expect(p.ok(),await p.text()).toBe(true);
  const preview=await p.json();
  const result=await page.request.post(`/api/security/changes/${preview.id}/apply`,{headers,data:{digest:preview.digest}});
  expect(result.ok(),await result.text()).toBe(true);
}

test('project changes are reviewed, apply once, and frozen sessions keep their own revision',async({page})=>{
  const project=await gitProject(page,'security-project');let sessionId:string|undefined;
  try {
    await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
    const panel=page.locator('#security');
    await panel.getByRole('switch',{name:'Block force pushes',exact:true}).uncheck();
    const review=await reviewChanges(page);
    let applied=0;page.on('request',r=>{if(r.method()==='POST'&&/\/api\/security\/changes\/[^/]+\/apply$/.test(new URL(r.url()).pathname))applied++;});
    await review.getByRole('button',{name:'Apply Security changes',exact:true}).dblclick();
    await expect(review).toHaveCount(0);expect(applied).toBe(1);
    await expect(panel.getByRole('region',{name:'Effective Security status',exact:true})).toBeFocused();
    await expect(panel.getByText('Effective mode:',{exact:false})).toContainText('Custom');
    const policy=await (await page.request.get(`/api/security/effective?project_id=${project.id}`)).json();
    expect(policy.switches.force_push).toBe(false);expect(policy.sources.force_push).toEqual({kind:'project',project_id:project.id});
    await page.getByRole('button',{name:'New session',exact:true}).click();
    const launch=page.getByRole('dialog',{name:'New session',exact:true});
    await expect(launch.getByRole('combobox',{name:'Project',exact:true})).toBeVisible();
    await launch.getByRole('combobox',{name:'Project',exact:true}).selectOption(String(project.id));
    await expect(launch.getByRole('button',{name:/Custom security.*This launch/})).toBeVisible();
    await launch.getByRole('button',{name:'Cancel',exact:true}).click();
    const created=await page.request.post('/api/sessions',{headers:project.headers,data:{project_id:project.id,provider:'claude',name:'Frozen security example'}});
    expect(created.ok(),await created.text()).toBe(true);sessionId=(await created.json()).id;
    // Loosen a second control while the first session continues using its original record.
    await saveScope(page,{kind:'project',project_id:project.id},{force_push:false,secrets:false});
    await page.goto(`/sessions/${sessionId}`);
    await expect(page.getByRole('button',{name:/Custom security · 2 off/}).first()).toBeVisible();
    const frozen=page.getByRole('button',{name:/Custom security · 1 off.*This session.*frozen launch/}).first();
    await expect(frozen).toBeVisible();await frozen.click();
    await expect(page.getByRole('dialog',{name:'Security protections'})).toContainText(`Frozen launch revision ${policy.revision}`);
    await page.keyboard.press('Escape');
  } finally {
    if(sessionId) await page.request.post(`/api/sessions/${sessionId}/stop`,{headers:project.headers});
  }
});

for(const width of [1440,390]) test(`tightening with a running weaker session at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:900});
  const project=await gitProject(page,'tighten');let sid:string|undefined;
  const panel=page.locator('#security');
  try {
    await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
    await panel.getByRole('switch',{name:'Block force pushes',exact:true}).uncheck();
    let review=await reviewChanges(page);
    await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
    await expect(review).toHaveCount(0);
    const made=await page.request.post('/api/sessions',{headers:project.headers,data:{project_id:project.id,provider:'claude',name:`Older settings ${width}`}});
    expect(made.ok(),await made.text()).toBe(true);sid=(await made.json()).id;
    await panel.getByRole('switch',{name:'Block force pushes',exact:true}).check();
    review=await reviewChanges(page);
    await expect(review).toContainText('1 running session keeps its older, looser settings');
    await expect(review.getByRole('link',{name:`Older settings ${width}`,exact:true})).toBeVisible();
    await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
    await expect(review).toHaveCount(0);
    await expect(panel.getByText('Server policy saved.',{exact:false})).toBeVisible();
    await expect(panel.getByRole('button',{name:/Current protections On · 1 session on older settings/})).toBeVisible();
    const old=panel.getByRole('listitem').filter({has:page.getByRole('link',{name:`Older settings ${width}`,exact:true})});
    await expect(page.getByRole('button',{name:`Current protections On · 1 session on older settings · ${project.slug} project`,exact:true})).toBeVisible();
    await expectAxeClean(page,'#security');
    await expect(old.getByRole('button',{name:'Stop',exact:true})).toBeVisible();
    await expect(old.getByRole('button',{name:'Start a new session',exact:true})).toBeVisible();
    await page.goto(`/sessions/${sid}`);
    const frozen=page.getByRole('button',{name:/older settings.*This session.*frozen launch/}).first();
    await frozen.click();
    await expect(page.getByRole('dialog',{name:'Security protections'})).toContainText('Frozen launch revision');
    await page.keyboard.press('Escape');
    await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
    await old.getByRole('button',{name:'Stop',exact:true}).click();
    const stop=page.getByRole('dialog',{name:`Stop Older settings ${width}?`,exact:true});
    await stop.getByRole('button',{name:'Cancel',exact:true}).click();
    await expect(old).toBeVisible();
    await old.getByRole('button',{name:'Start a new session',exact:true}).click();
    await expect(page.getByRole('dialog',{name:'New session',exact:true})).toBeVisible();
    await page.getByRole('dialog',{name:'New session',exact:true}).getByRole('button',{name:'Cancel',exact:true}).click();
    await old.getByRole('button',{name:'Stop',exact:true}).click();
    await stop.getByRole('button',{name:'Stop',exact:true}).click();
    await expect(panel.getByText('0 sessions on older settings.',{exact:false})).toBeVisible();
    const next=await page.request.post('/api/sessions',{headers:project.headers,data:{project_id:project.id,provider:'claude',name:'Current protection'}});
    expect(next.ok(),await next.text()).toBe(true);
    const current=await next.json();
    expect(JSON.parse(current.launch_spec.files['guardrails.json']).security.switches.force_push).toBe(true);
    await page.request.post(`/api/sessions/${current.id}/stop`,{headers:project.headers});
  } finally {if(sid)await page.request.post(`/api/sessions/${sid}/stop`,{headers:project.headers});}
});

function remoteOlderFixture(projectId:number) {
  const python=environmentValue('ACC_E2E_PYTHON')??'python3';
  const script=`
import json,sys,uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from acc.server.models import Node,Session,LaunchSpec
from acc.server.security_settings import resolve
with DbSession(create_engine('sqlite:///'+sys.argv[1])) as db:
    if sys.argv[2]=='create':
        identity=uuid.uuid4().hex
        machine='retired-'+identity[:10]
        db.add(Node(id=identity,name=machine,status='active',info={'accounts':['other-agent']}))
        frozen=resolve(db,project_id=int(sys.argv[3]),machine=machine,account='other-agent')
        spec=LaunchSpec(provider='claude',inputs={'provider':'claude','permission_mode':'bypassPermissions'},files={'guardrails.json':json.dumps({'custom':[]})},digest=identity)
        db.add(Session(id=identity,project_id=int(sys.argv[3]),provider='claude',name='Older remote generation',tmux_name='fixture-'+identity,
            owner='alex',run_as='other-agent',team_host=machine,desired_state='archived',observed_state='archived',
            last_error='stop not confirmed: unreachable',launch_spec=spec))
        db.commit()
        print(json.dumps({'id':identity,'machine':machine}))
    else:
        row=db.get(Session,sys.argv[3])
        if row is not None: db.delete(row)
        node=db.get(Node,sys.argv[3])
        if node is not None: db.delete(node)
        db.commit()
`;
  const args=['-c',script,`${info().state}/data/acc.db`];
  const options={cwd:'..',env:{...process.env,PYTHONPATH:'src'},encoding:'utf8' as const};
  const fixture=JSON.parse(execFileSync(python,[...args,'create',String(projectId)],options)) as {id:string;machine:string};
  return {...fixture,cleanup:()=>execFileSync(python,[...args,'delete',fixture.id],options)};
}

for(const width of [1440,390]) test(`owner badge counts other accounts and confirmed machine-gone recovery at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:900});
  const project=await gitProject(page,'global-older');
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  await page.locator('#security').getByRole('switch',{name:'Block credential-file and environment reads',exact:true}).uncheck();
  const review=await reviewChanges(page);
  await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
  await expect(review).toHaveCount(0);
  const fixture=remoteOlderFixture(project.id);
  try {
    for(const path of ['/', '/sessions', `/projects/${project.id}`]) {
      await page.goto(path);
      const badge=page.getByRole('button',{name:/(Custom security|Current protections On).*1 session on older settings/}).filter({visible:true}).first();
      await expect(badge).toBeVisible();
      await expect(badge).toContainText(path.startsWith('/projects/')?'1 off':'Current protections On');
      await expect(badge).not.toContainText('1 sessions');
    }
    await page.goto(`/sessions/${fixture.id}`);
    await expect(page.getByText('This session · older settings · frozen protections unavailable',{exact:true}).filter({visible:true}).first()).toBeVisible();
    const removed=await page.request.post(`/api/nodes/${fixture.machine}/remove`,{headers:project.headers});
    expect(removed.ok(),await removed.text()).toBe(true);
    await page.goto('/settings#security');
    const panel=page.locator('#security');
    const row=panel.getByRole('listitem').filter({has:page.getByRole('link',{name:'Older remote generation',exact:true})});
    await expect(row).toBeVisible();
    await row.getByRole('button',{name:'Machine is gone',exact:true}).click();
    const confirmation=page.getByRole('dialog',{name:`Confirm ${fixture.machine} is gone?`,exact:true});
    await expect(confirmation).toContainText('does not prove a stop');
    await confirmation.getByRole('button',{name:'Cancel',exact:true}).click();
    await expect(row).toBeVisible();
    await row.getByRole('button',{name:'Machine is gone',exact:true}).click();
    await confirmation.getByRole('button',{name:'Machine is gone',exact:true}).click();
    await expect(row).toHaveCount(0);
    await expect(panel.getByText('0 sessions on older settings.',{exact:false})).toBeVisible();
  } finally {fixture.cleanup();}
});

// A unique offline metadata-only node isolates account-wide transitions from
// other browser journeys. No broker, target acknowledgement or OS grant is faked.
function policyAccountFixture() {
  const python=environmentValue('ACC_E2E_PYTHON')??'python3';
  const script=`
import json,sys,uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from acc.server.models import Node,SecurityPolicy,SecurityFolderPolicy
from acc.server.security_settings import scope_key
with Session(create_engine('sqlite:///'+sys.argv[1])) as db:
    if sys.argv[2]=='create':
        identity=uuid.uuid4().hex
        scope={'kind':'account','machine':'security-'+identity[:12],'machine_id':identity,'account':'alex-agent'}
        db.add(Node(id=identity,name=scope['machine'],status='active',info={'accounts':['alex-agent']}))
        db.commit()
        print(json.dumps(scope))
    else:
        scope=json.loads(sys.argv[3])
        for model,key in [(SecurityPolicy,scope_key(scope)),(SecurityFolderPolicy,scope_key(scope)),(Node,scope['machine_id'])]:
            row=db.get(model,key)
            if row is not None: db.delete(row)
        db.commit()
`;
  const args=['-c',script,`${info().state}/data/acc.db`];
  const options={cwd:'..',env:{...process.env,PYTHONPATH:'src'},encoding:'utf8' as const};
  const scope=JSON.parse(execFileSync(python,[...args,'create'],options));
  return {scope,cleanup:()=>execFileSync(python,[...args,'delete',JSON.stringify(scope)],options)};
}

test('undeliverable account Unrestricted refuses preview and keeps the draft',async({page})=>{
  const {scope,cleanup}=policyAccountFixture();
  try {
    await page.goto(`/settings?security_scope=account&machine=${encodeURIComponent(scope.machine)}&account=${encodeURIComponent(scope.account)}#security`);
    const panel=page.locator('#security');
    await panel.getByRole('combobox',{name:'Security mode',exact:true}).selectOption('unrestricted');
    await panel.getByRole('button',{name:'Review changes',exact:true}).click();
    const verification=page.getByRole('dialog',{name:'Verify your sign-in',exact:true});
    await expect(verification.or(panel.getByRole('alert'))).toBeVisible();
    if(await verification.isVisible()) {
      await freshStep(page);
      await verification.getByLabel('Verification password').fill(PASSWORD);
      await verification.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
      await verification.getByRole('button',{name:'Verify with password and authenticator',exact:true}).click();
    }
    await expect(panel.getByRole('alert')).toContainText('cannot be turned Off');
    await expect(panel.getByRole('combobox',{name:'Security mode',exact:true})).toHaveValue('unrestricted');
    const effective=await (await page.request.get(`/api/security/effective?machine=${encodeURIComponent(scope.machine)}&account=${encodeURIComponent(scope.account)}`)).json();
    expect(effective.switches.allowed_folders).toBe(true);
  } finally {cleanup();}
});

test('a stale reviewed policy cannot overwrite a newer tab and the draft is kept',async({page})=>{
  const project=await gitProject(page,'security-stale');
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  const panel=page.locator('#security');
  await panel.getByRole('switch',{name:'Block force pushes',exact:true}).uncheck();
  const review=await reviewChanges(page);
  await saveScope(page,{kind:'project',project_id:project.id},{secrets:false});
  await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
  await expect(review.getByRole('alert')).toContainText('changed');
  await review.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(panel.getByRole('switch',{name:'Block force pushes',exact:true})).not.toBeChecked();
  await expect(panel.getByText('Security changed elsewhere.',{exact:false})).toBeVisible();
  await panel.getByRole('button',{name:'Discard draft and reload',exact:true}).click();
  await expect(panel.getByRole('switch',{name:'Block force pushes',exact:true})).toBeChecked();
  await expect(panel.getByRole('switch',{name:'Block credential-file and environment reads',exact:true})).not.toBeChecked();
});

test('member Security view is read-only and never requests owner scopes or account metadata',async({page})=>{
  const project=await gitProject(page,'security-member');let forbidden=0;
  await page.route('**/api/auth/state',async route=>{const r=await route.fetch();await route.fulfill({response:r,json:{...await r.json(),role:'member'}});});
  await page.route('**/api/security/scopes',route=>{forbidden++;return route.fulfill({status:403,json:{detail:'owner only'}});});
  await page.route('**/api/nodes',route=>{forbidden++;return route.fulfill({status:403,json:{detail:'owner only'}});});
  await page.route('**/api/users',route=>{forbidden++;return route.fulfill({status:403,json:{detail:'owner only'}});});
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  const panel=page.locator('#security');
  await expect(panel.getByText('An owner manages these settings.',{exact:false})).toBeVisible();
  await expect(panel.getByRole('heading',{name:'Effective protections'})).toBeVisible();
  await expect(panel.getByRole('switch')).toHaveCount(0);await expect(panel.getByRole('button',{name:'Review changes',exact:true})).toHaveCount(0);
  expect(forbidden).toBe(0);await expectAxeClean(page,'#security');
});

test('demotion does not reuse the owner project selector cache',async({page})=>{
  const project=await gitProject(page,'security-demotion');let member=false;
  await page.route('**/api/auth/state',async route=>{const response=await route.fetch();await route.fulfill({response,json:{...await response.json(),role:member?'member':'owner'}});});
  await page.route('**/api/projects',async route=>{
    const response=await route.fetch();const list=await response.json();
    const mine=list.find((p:{id:number})=>p.id===project.id);
    await route.fulfill({response,json:member?[mine]:[mine,{...mine,id:99991,name:'Private owner repository',account:'private-agent'}]});
  });
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  await expect(page.locator('#security').getByRole('option',{name:/Private owner repository/})).toHaveCount(1);
  member=true;
  await expect(page.locator('#security').getByText('An owner manages these settings.',{exact:false})).toBeVisible({timeout:15000});
  await expect(page.locator('#security').getByRole('option',{name:/Private owner repository/})).toHaveCount(0);
});

test('acknowledged policy fixtures keep current and frozen Unrestricted badges distinct',async({page})=>{
  const project=await gitProject(page,'security-badge');
  const response=await page.request.post('/api/sessions',{headers:project.headers,data:{project_id:project.id,provider:'claude',name:'Acknowledged policy fixture'}});
  expect(response.ok()).toBe(true);const session=await response.json();
  try {
    // Session creation queues launch; the frozen record is published asynchronously.
    await expect.poll(async()=>Boolean((await (await page.request.get(`/api/sessions/${session.id}`)).json()).launch_spec?.files?.['guardrails.json']), {timeout:15000}).toBe(true);
    await page.route('**/api/security/effective**',async route=>{
      const response=await route.fetch();const p=await response.json();
      await route.fulfill({response,json:{...p,mode:'unrestricted',policy_mode:'unrestricted',revision:99,
        switches:Object.fromEntries(Object.keys(p.switches).map(id=>[id,false])),enforcement:{allowed_folders:'applied'}}});
    });
    await page.route(`**/api/sessions/${session.id}`,async route=>{
      const response=await route.fetch();const data=await response.json();
      const guard=JSON.parse(data.launch_spec.files['guardrails.json']);
      guard.security={...guard.security,revision:77,mode:'unrestricted',policy_mode:'unrestricted',enforcement:{allowed_folders:'applied'},
        switches:Object.fromEntries(Object.keys(guard.security.switches).map(id=>[id,false]))};
      await route.fulfill({response,json:{...data,launch_spec:{...data.launch_spec,files:{...data.launch_spec.files,'guardrails.json':JSON.stringify(guard)}}}});
    });
    await page.goto(`/sessions/${session.id}`);
    const frozen=page.getByRole('button',{name:/Unrestricted.*This session.*frozen launch/}).filter({visible:true});
    await expect(frozen).toBeVisible();await frozen.click();
    await expect(page.getByRole('dialog',{name:'Security protections'})).toContainText('Frozen launch revision 77');
    await page.keyboard.press('Escape');
    await page.getByRole('button',{name:new RegExp(`Unrestricted.*${project.slug} project`)}).first().click();
    await expect(page.getByRole('dialog',{name:'Security protections'})).toContainText('Current policy revision 99');
    await expect(page.getByRole('dialog',{name:'Security protections'})).toContainText('Authentication, owner approvals');
    await page.keyboard.press('Escape');
  } finally {await page.request.post(`/api/sessions/${session.id}/stop`,{headers:project.headers});}
});

test('expired review refuses apply without discarding its unsaved choices',async({page})=>{
  const project=await gitProject(page,'security-expiry');
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  const panel=page.locator('#security');
  await panel.getByRole('switch',{name:'Block force pushes',exact:true}).uncheck();
  const review=await reviewChanges(page);
  await page.clock.setSystemTime(Date.now()+360000);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await expect(review.getByText('This preview expired.',{exact:false})).toBeVisible();
  await expect(review.getByRole('button',{name:'Apply Security changes',exact:true})).toBeDisabled();
  await review.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(panel.getByRole('switch',{name:'Block force pushes',exact:true})).not.toBeChecked();
});

// Display fixtures are not root-worker qualification; storage/authz proofs live in Python.
test('SS-1 old Tool approvals cannot save unreviewed policy',async({page})=>{
  let writes=0;
  page.on('request',r=>{if(r.method()==='PUT'&&new URL(r.url()).pathname==='/api/tools/policy')writes++;});
  await page.goto('/settings#tool-approvals');
  const panel=page.locator('#tool-approvals');
  await expect(panel.getByRole('heading',{name:'Tool approvals',exact:true})).toBeVisible();
  await expect(panel.getByRole('checkbox')).toHaveCount(0);
  const link=panel.getByRole('link',{name:'Open reviewed Security settings',exact:true});
  await expect(link).toBeVisible();await link.click();
  await expect(page.locator('#security').getByRole('heading',{name:'Security',exact:true})).toBeVisible();
  expect(writes).toBe(0);
});

test('SS-3 re-enabled folder protection is visibly pending even with every switch On',async({page})=>{
  const project=await gitProject(page,'security-reenable');
  const details=(await (await page.request.get('/api/projects')).json()).find((p:{id:number})=>p.id===project.id);
  await page.route('**/api/security/effective**',async route=>{
    const response=await route.fetch(),data=await response.json();
    if(new URL(route.request().url()).searchParams.has('account'))await route.fulfill({response,json:{...data,
      switches:Object.fromEntries(Object.keys(data.switches).map(id=>[id,true])),mode:'custom',policy_mode:'default',enforcement:{allowed_folders:'pending'}}});
    else await route.fulfill({response,json:data});
  });
  await page.goto(`/settings?security_scope=account&machine=local&account=${encodeURIComponent(details.account)}#security`);
  const panel=page.locator('#security');
  const pendingBadge=panel.getByRole('button',{name:/Folder protection pending/});
  await expect(pendingBadge).toBeVisible();
  expect((await pendingBadge.getAttribute('aria-label'))?.match(/pending/g)).toHaveLength(1);
  const row=panel.getByRole('listitem').filter({has:page.getByText('Limit account-service operations to allowed folders',{exact:true})});
  await expect(row).toContainText('Desired: On');
  await expect(row).toContainText('Pending target acknowledgement');
  await expect(panel.getByText('No folder-policy change pending',{exact:false})).toHaveCount(0);
  await expectAxeClean(page);
});

test('SS-4 a shared account displays variation without inventing an Off value',async({page})=>{
  const project=await gitProject(page,'security-subject');
  const details=(await (await page.request.get('/api/projects')).json()).find((p:{id:number})=>p.id===project.id);
  await page.route('**/api/security/effective**',async route=>{
    const response=await route.fetch(),data=await response.json();
    if(new URL(route.request().url()).searchParams.has('account'))await route.fulfill({response,json:{...data,
      switches:{...data.switches,tool_install_check:null},sources:{...data.sources,tool_install_check:{kind:'varies'}},
      varying_switches:['tool_install_check'],mode:'custom'}});
    else await route.fulfill({response,json:data});
  });
  await page.goto(`/settings?security_scope=account&machine=local&account=${encodeURIComponent(details.account)}#security`);
  const panel=page.locator('#security');
  const row=panel.getByRole('listitem').filter({has:page.getByText('Ask before account-wide tool changes',{exact:true})});
  await expect(row).toContainText('Effective now: Varies by person');
  await expect(row.getByRole('combobox',{name:'Ask before account-wide tool changes override',exact:true})).toHaveValue('inherit');
  await expect(panel.getByRole('button',{name:/Security varies by person/})).toBeVisible();
  await expectAxeClean(page);
});

test('SS-5 audit distinguishes refusal conflict and failed apply from successful review',async({page})=>{
  await page.route('**/api/audit?**',route=>route.fulfill({json:{items:[
    {id:901,actor:'alex',action:'security.policy.refused',ts:'2026-10-31T00:00:00Z',detail:{reason:'verification_required'}},
    {id:902,actor:'alex',action:'security.policy.conflict',ts:'2026-10-31T00:01:00Z',detail:{reason:'expired'}},
    {id:903,actor:'alex',action:'security.policy.failed',ts:'2026-10-31T00:02:00Z',detail:{reason:'internal_failure'}},
  ]}}));
  await page.goto('/settings#security');
  const audit=page.locator('#security section').filter({has:page.getByRole('heading',{name:'Recent Security audit',exact:true})});
  await expect(audit).toContainText('Request refused');
  await expect(audit).toContainText('Policy conflict');
  await expect(audit).toContainText('Operation failed');
  await expect(audit).not.toContainText('Policy reviewed');
});

test('SS-2 dangerous launch confirmation cannot override provider prompts On',async({page})=>{
  const project=await gitProject(page,'security-provider');
  await page.goto(`/projects/${project.id}`);
  await page.getByRole('main').getByRole('button',{name:'New session',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'New session',exact:true});
  await dialog.getByRole('button',{name:/^Advanced/}).click();
  await dialog.getByRole('combobox',{name:'Permissions',exact:true}).selectOption('bypassPermissions');
  await expect(dialog.getByText('This confirmation cannot override Security.',{exact:false})).toBeVisible();
  await dialog.getByRole('checkbox',{name:/The agent will run commands without asking/}).check();
  await expect(dialog.getByText('Security requires provider prompts.',{exact:false})).toBeVisible();
  await expect(dialog.getByRole('button',{name:'Launch',exact:true})).toBeDisabled();
});

for(const state of ['applied','pending'] as const) test(`folder switch result is reported immediately: ${state}`,async({page})=>{
  const project=await gitProject(page,`folder-result-${state}`);
  await page.goto(`/settings?security_scope=project&project_id=${project.id}#security`);
  const panel=page.locator('#security');
  await panel.getByRole('switch',{name:'Block force pushes',exact:true}).uncheck();
  await page.route('**/api/security/changes/*/apply',async route=>{
    const response=await route.fetch();
    expect(response.ok()).toBe(true);
    await route.fulfill({response,json:{...await response.json(),folder_application:{state}}});
  });
  const review=await reviewChanges(page);
  await expect(review).toContainText('Folder reloads affect running sessions: Off widens');
  await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
  await expect(panel.getByRole('status').filter({hasText:'Folder policy:'})).toContainText(state==='applied'?'acknowledged by the target':'pending target acknowledgement');
  await expect(panel.getByRole('link',{name:'Review or retry in Allowed folders',exact:true})).toBeVisible();
});

for(const width of [1440,390]) test(`X3 confirms unchanged recovery after a refused folder review at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:900});
  const project=await gitProject(page,'confirm-recovery');
  const details=(await (await page.request.get('/api/projects')).json()).find((p:{id:number})=>p.id===project.id);
  let pending=true,reviewed=false;
  await page.route('**/api/security/effective**',async route=>{
    const response=await route.fetch(),data=await response.json();
    const account=new URL(route.request().url()).searchParams.get('account');
    await route.fulfill({response,json:account===details.account?{...data,can_confirm_folder_recovery:pending,
      enforcement:{allowed_folders:pending?'pending':'applied'}}:data});
  });
  await page.route('**/api/security/folders',route=>route.fulfill({json:{revision:2,accounts:[{
    scope:{kind:'account',machine:'local',machine_id:'local',account:details.account},
    state:'pending_policy',stored_state:'pending_policy',retry_available:false,allowed_folders:true,
    roots:['/home/alex-agent/workspaces'],revision:2,applied_revision:2,
    detail:{allowed_folders:true,recovered_from:'a'.repeat(32),message:'Acknowledged policy reloaded. Review folders again, or restore the acknowledged switch in Security.'},projects:[],
  }]}}));
  await page.route('**/api/security/folders/preview',route=>route.fulfill({status:409,json:{detail:'The target refused this folder change. Check access and review a fresh preview.'}}));
  await page.route('**/api/security/changes/preview',async route=>{
    expect(route.request().postDataJSON().overrides).toEqual({});
    reviewed=true;
    const response=await route.fetch();
    expect(response.ok(),await response.text()).toBe(true);
    const data=await response.json();
    expect(data.before).toEqual(data.after);
    await route.fulfill({response,json:data});
  });
  await page.route('**/api/security/changes/*/apply',async route=>{
    const response=await route.fetch();
    expect(response.ok(),await response.text()).toBe(true);
    pending=false;
    await route.fulfill({response});
  });
  await page.goto('/settings/security/folders');
  await page.getByRole('button',{name:'Preview folders',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('target refused');
  await page.getByRole('link',{name:'Open Security to restore this setting',exact:true}).click();
  const panel=page.locator('#security');
  await expect(panel.getByRole('button',{name:'Review acknowledged folder policy',exact:true})).toBeVisible();
  const review=await reviewChanges(page,'Review acknowledged folder policy');
  await expect(review).toContainText('acknowledged');
  await review.getByRole('button',{name:'Apply Security changes',exact:true}).click();
  await expect(review).toHaveCount(0);
  await expect(panel.getByText('Server policy saved.',{exact:false})).toBeVisible();
  await expect(panel.getByRole('button',{name:'Review acknowledged folder policy',exact:true})).toHaveCount(0);
  expect(reviewed).toBe(true);
});
