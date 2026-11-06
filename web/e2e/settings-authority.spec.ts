import { test } from './shell-state';
import {writeFileSync} from 'node:fs';
import {expect,type Page} from '@playwright/test';
import {AUTH_FILE,csrfHeaders,gitProject,info,PASSWORD,totp,verifyAgain,waitForSessionState} from './helpers';
// Each demotion pair deliberately opts a second person into the shell.
test.use({storageState:AUTH_FILE,presentationChanges:['layouts','ui_shell']});
async function shell(page:Page){
  const state=await(await page.request.get('/api/admin-features')).json();
  let response=await page.request.put('/api/admin-features/new_shell',{headers:await csrfHeaders(page),data:{enabled:true,revision:state.revision}});
  if(!response.ok()){await verifyAgain(page);const fresh=await(await page.request.get('/api/admin-features')).json();response=await page.request.put('/api/admin-features/new_shell',{headers:await csrfHeaders(page),data:{enabled:true,revision:fresh.revision}});}
  expect(response.ok(),await response.text()).toBeTruthy();
}
async function pair(owner:Page,browser:import('@playwright/test').Browser){
  await shell(owner);const foreign=await gitProject(owner,'cedar-private');
  const suffix=crypto.randomUUID().slice(0,8);const account=`demotion-${suffix}-agent`;writeFileSync(`${info().state}/fixture-accounts/${account}`,'');
  const inviteBody={role:'owner',account};
  let invite=await owner.request.post('/api/users/invites',{headers:await csrfHeaders(owner),data:inviteBody});
  if(invite.status()===403){await verifyAgain(owner);invite=await owner.request.post('/api/users/invites',{headers:await csrfHeaders(owner),data:inviteBody});}
  expect(invite.ok(),await invite.text()).toBeTruthy();const token=(await invite.json()).link.split('/').pop();
  const context=await browser.newContext({baseURL:info().origin,viewport:{width:1440,height:900}});const page=await context.newPage();
  const begin=await page.request.post('/api/auth/invite/begin',{headers:{Origin:info().origin},data:{token}});
  const username=`blair-demotion-${suffix}`;
  const joined=await page.request.post('/api/auth/invite/complete',{headers:{Origin:info().origin},data:{token,username,password:PASSWORD,code:totp((await begin.json()).totp_secret)}});
  expect(joined.ok(),await joined.text()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell',{headers:await csrfHeaders(page),data:{enabled:true}})).ok()).toBeTruthy();
  const own=await gitProject(page,'maple-own');
  const users=await(await owner.request.get('/api/users')).json();const user=users.users.find((u:{username:string})=>u.username===username);
  async function demote(){
    const response=await owner.request.post(`/api/users/${user.id}/role`,{headers:await csrfHeaders(owner),data:{role:'member'}});expect(response.ok(),await response.text()).toBeTruthy();
    expect((await(await page.request.get('/api/auth/state')).json()).role).toBe('member');
    const authorized=await(await page.request.get('/api/projects')).json();expect(authorized.map((p:{id:number})=>p.id)).not.toContain(foreign.id);expect(authorized.map((p:{id:number})=>p.id)).toContain(own.id);
  }
  return {context,page,foreign,own,demote};
}
async function roleMarker(page:Page){
  // Observe owner-only chrome without navigating away from the protected view.
  const marker=page.locator('#shell-header').getByRole('link',{name:/^Security:/});
  await expect(marker).toBeVisible();return marker;
}

test('real demotion drops private Settings choices and retained selection without reload',async({page:owner,browser})=>{
  test.setTimeout(150000);const {context,page,foreign,own,demote}=await pair(owner,browser);
  try{
    await page.goto('/settings#project-settings');const main=page.getByRole('main');const picker=main.getByRole('combobox',{name:'Settings project',exact:true});
    await picker.selectOption(String(foreign.id));await expect(main.getByRole('region',{name:'Project settings',exact:true})).toContainText(foreign.slug);
    await demote();await expect(page.getByRole('complementary',{name:'Context sidebar',exact:true}).getByRole('heading',{name:'Installation',exact:true})).toHaveCount(0,{timeout:15000});
    await expect(picker.getByRole('option',{name:foreign.slug,exact:true})).toHaveCount(0);
    await expect(picker.getByRole('option',{name:own.slug,exact:true})).toHaveCount(1);
    await expect(picker).toHaveValue('');await expect(main).not.toContainText(foreign.slug);await expect(main).not.toContainText(foreign.folder);
  }finally{await context.close();}
});
test('real demotion drops private project pins recents and selected sidebar context',async({page:owner,browser})=>{
  test.setTimeout(150000);const {context,page,foreign,own,demote}=await pair(owner,browser);
  try{
    await page.goto('/settings#passkeys');await page.getByRole('button',{name:'Projects',exact:true}).click();const sidebar=page.getByRole('complementary',{name:'Context sidebar',exact:true});
    await sidebar.getByRole('region',{name:'Available projects'}).getByRole('link',{name:new RegExp(`^${foreign.slug}`)}).click();
    await sidebar.getByRole('region',{name:`${foreign.slug} context`,exact:true}).getByRole('button',{name:`Pin ${foreign.slug}`,exact:true}).click();await expect(sidebar).toContainText(foreign.slug);
    await demote();await expect(sidebar).not.toContainText(foreign.slug,{timeout:15000});
    await expect(sidebar).not.toContainText(foreign.slug);await expect(sidebar).toContainText(own.slug);
    await expect(sidebar.getByRole('region',{name:`${foreign.slug} context`,exact:true})).toHaveCount(0);
  }finally{await context.close();}
});
test('real demotion unmounts foreign Project Overview and its Continue children without reload',async({page:owner,browser})=>{
  test.setTimeout(150000);const {context,page,foreign,demote}=await pair(owner,browser);
  try{
    await page.goto(`/projects/${foreign.id}`);await expect(page.getByRole('main')).toContainText(foreign.slug);const ownerMarker=await roleMarker(page);
    await demote();await expect(ownerMarker).toHaveCount(0,{timeout:15000});
    await expect(page.getByRole('main')).not.toContainText(foreign.slug);await expect(page.getByRole('main')).not.toContainText(foreign.folder);
    await expect(page.getByRole('region',{name:'Sessions',exact:true})).toHaveCount(0);await expect(page.getByRole('main').getByText('View unavailable. Access may have ended or the resource may have been removed.',{exact:true})).toBeVisible();
  }finally{await context.close();}
});
test('failed session refresh cannot retain another person’s Continue rows after real demotion',async({page:owner,browser})=>{
  test.setTimeout(150000);const {context,page,own,demote}=await pair(owner,browser);
  try{
    const launch=async(actor:Page,name:string)=>{
      const response=await actor.request.post('/api/sessions',{headers:await csrfHeaders(actor),data:{project_id:own.id,provider:'claude',name}});expect(response.ok(),await response.text()).toBeTruthy();const session=await response.json();
      await waitForSessionState(actor,session.id,'idle');expect((await actor.request.post(`/api/sessions/${session.id}/stop`,{headers:await csrfHeaders(actor)})).ok()).toBeTruthy();return session;
    };
    const foreign=await launch(owner,'Cedar private session');const mine=await launch(page,'Maple own session');
    await page.goto(`/projects/${own.id}`);const continued=page.getByRole('region',{name:'Sessions',exact:true});await expect(continued).toContainText(foreign.name);await expect(continued).toContainText(mine.name);
    const ownerMarker=await roleMarker(page);
    await page.route('**/api/sessions',route=>route.fulfill({status:503,json:{detail:'Fixture list refresh unavailable'}}));
    await demote();await expect(ownerMarker).toHaveCount(0,{timeout:15000});
    await expect(page.getByRole('main')).not.toContainText(foreign.name);
    await page.unroute('**/api/sessions');await expect(continued).toContainText(mine.name,{timeout:15000});await expect(continued).not.toContainText(foreign.name);
  }finally{await context.close();}
});
test('fresh project read failure after real demotion never restores cached owner choices',async({page:owner,browser})=>{
  test.setTimeout(150000);const {context,page,foreign,own,demote}=await pair(owner,browser);
  try{
    await page.goto('/settings#project-settings');const main=page.getByRole('main');const choices=main.getByRole('region',{name:'Project settings',exact:true});
    await choices.getByRole('combobox',{name:'Settings project',exact:true}).selectOption(String(foreign.id));
    await page.route('**/api/projects',route=>route.fulfill({status:503,json:{detail:'Fixture project read unavailable'}}));
    await demote();await expect(page.getByRole('complementary',{name:'Context sidebar',exact:true}).getByRole('heading',{name:'Installation',exact:true})).toHaveCount(0,{timeout:15000});
    await expect(choices).not.toContainText(foreign.slug);await expect(choices).not.toContainText(foreign.folder);
    await expect(choices.getByRole('button',{name:'Retry',exact:true})).toBeVisible();
    await page.unroute('**/api/projects');await choices.getByRole('button',{name:'Retry',exact:true}).click();
    const picker=choices.getByRole('combobox',{name:'Settings project',exact:true});await expect(picker.getByRole('option',{name:own.slug,exact:true})).toHaveCount(1);await expect(picker).toHaveValue('');
    await expect(choices).not.toContainText(foreign.slug);
  }finally{await context.close();}
});
