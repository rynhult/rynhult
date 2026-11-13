import { test } from './shell-state';
import {mkdirSync,utimesSync,writeFileSync} from 'node:fs';
import {expect,type Page} from '@playwright/test';
import {AUTH_FILE,csrfHeaders,expectAxeClean,gitProject,info,PASSWORD,totp,verifyAgain,waitForSessionState} from './helpers';

// The member journey saves a different person's shell preference.
test.use({storageState:AUTH_FILE,presentationChanges:['layouts',]});

async function results(page:Page,prefix:string,foreignSession?:string) {
  const project=await gitProject(page,prefix);
  const response=await page.request.post('/api/sessions',{headers:await csrfHeaders(page),data:{project_id:project.id,provider:'claude',name:`${prefix} visible session`}});
  expect(response.ok(),await response.text()).toBeTruthy();const session=await response.json();
  await waitForSessionState(page,session.id,'idle');
  expect((await page.request.post(`/api/sessions/${session.id}/stop`,{headers:await csrfHeaders(page)})).ok()).toBeTruthy();
  const first=`2026-11-01-${session.id}-latest.md`;
  const unknown=`2026-11-01-${foreignSession??'unattributed'}-notes.md`;
  const notes=[['handoffs',first],['council','02-review.md'],['research','03-findings.md'],['handoffs',unknown],['research','05-findings.md'],['handoffs','06-older.md'],['council','07-older.md'],['design','00-design.md']];
  for(const [i,[kind,name]] of notes.entries()) {
    const dir=`${project.folder}/docs/${kind}`;mkdirSync(dir,{recursive:true});
    const path=`${dir}/${name}`;writeFileSync(path,`# ${prefix} result ${i}\nVerified fixture note.\n`);
    const time=new Date(Date.now()-(i===7?30:(i+1)*60)*1000);utimesSync(path,time,time);
  }
  return {...project,session:session.id,first,unknown};
}
async function matrix(page:Page,project:Awaited<ReturnType<typeof results>>,role:string) {
  for(const width of [1440,390])for(const theme of ['light','dark'] as const) {
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme:theme});
    await page.goto(`/projects/${project.id}?section=results`);
    const recent=page.getByRole('region',{name:'Recent results',exact:true});
    await expect(recent).toBeVisible();await expect(recent.getByRole('listitem')).toHaveCount(5);
    await expect(recent.getByRole('listitem').getByRole('button')).toHaveText([project.first,'02-review.md','03-findings.md',project.unknown,'05-findings.md']);
    await expect(recent).not.toContainText('06-older');await expect(recent).not.toContainText('00-design');
    await expect(recent.getByRole('link',{name:`${role} visible session`,exact:true})).toHaveAttribute('href',`/sessions/${project.session}?tab=handoffs`);
    await expect(recent).toContainText('Session not recorded');await expect(recent).toContainText('Updated');
    await expect(recent.locator('time')).toHaveCount(5);
    await expect(recent.getByRole('link',{name:'View all',exact:true})).toHaveAttribute('href',`/projects/${project.id}#project-knowledge`);
    await expectAxeClean(page);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if(theme==='dark')await recent.screenshot({path:`../.acc/artifacts/shell-recent-results/${role}-${width}-${theme}.png`});
    await recent.getByRole('button',{name:project.first,exact:true}).click();
    await expect(page.getByRole('dialog')).toContainText('Verified fixture note.');await expectAxeClean(page);
    await page.getByRole('dialog').getByRole('button',{name:'Close',exact:true}).click();
    await recent.getByRole('link',{name:'View all',exact:true}).click();
    await expect(page.getByRole('tab',{name:'Knowledge',exact:true})).toHaveAttribute('aria-selected','true');
    await expect(page.locator('#project-knowledge')).toContainText('06-older.md');
  }
}

test('owner sees newest five indexed results, opens notes and the complete Knowledge view',async({page})=>{
  test.setTimeout(90000);const project=await results(page,'cedar');
  await matrix(page,project,'cedar');
});
test('member sees own results and never resolves another person’s private session',async({page,context})=>{
  test.setTimeout(90000);
  const foreign=await results(page,'cedar-private');const ownerProject=foreign.id;const ownerSession=foreign.session;
  const account=`results-${crypto.randomUUID().slice(0,8)}-agent`;writeFileSync(`${info().state}/fixture-accounts/${account}`,'');
  const invite=await page.request.post('/api/users/invites',{headers:await csrfHeaders(page),data:{role:'member',account}});
  expect(invite.ok(),await invite.text()).toBeTruthy();const token=(await invite.json()).link.split('/').pop();await context.clearCookies();
  const begin=await page.request.post('/api/auth/invite/begin',{headers:{Origin:info().origin},data:{token}});
  const joined=await page.request.post('/api/auth/invite/complete',{headers:{Origin:info().origin},data:{token,username:`blair-results-${Date.now()}`,password:PASSWORD,code:totp((await begin.json()).totp_secret)}});
  expect(joined.ok(),await joined.text()).toBeTruthy();
  expect((await page.request.get(`/api/projects/${ownerProject}/knowledge`)).status()).toBe(404);
  expect((await page.request.get(`/api/sessions/${ownerSession}`)).status()).toBe(404);
  const project=await results(page,'maple',ownerSession);
  const forbidden:string[]=[];page.on('request',r=>{const path=new URL(r.url()).pathname;if(path==='/api/board'||path.startsWith(`/api/sessions/${ownerSession}`)||path.startsWith(`/api/projects/${ownerProject}/`))forbidden.push(path);});
  await matrix(page,project,'maple');
  await page.getByRole('tab',{name:'Recent results',exact:true}).click();expect(forbidden).toEqual([]);await expect(page.getByRole('region',{name:'Recent results',exact:true})).not.toContainText('cedar-private visible session');
});
test('empty, unavailable and retained results stay distinct; standard Overview stays unchanged',async({page})=>{
  const project=await gitProject(page,'birch');await page.goto(`/projects/${project.id}?section=results`);
  const recent=page.getByRole('region',{name:'Recent results',exact:true});
  await expect(recent).toContainText('Nothing saved here yet');
  await page.route(`**/api/projects/${project.id}/knowledge`,route=>route.fulfill({status:503,json:{detail:'Fixture notes unavailable'}}));
  await page.reload();await expect(recent).toContainText('Results unavailable');await expect(recent).not.toContainText('Nothing saved here yet');
  await page.unroute(`**/api/projects/${project.id}/knowledge`);
  const dir=`${project.folder}/docs/research`;mkdirSync(dir,{recursive:true});writeFileSync(`${dir}/retained.md`,'# Retained');
  await page.reload();await expect(recent).toContainText('retained.md');
  await page.route(`**/api/projects/${project.id}/knowledge`,route=>route.fulfill({status:503,json:{detail:'Fixture notes unavailable'}}));
  await recent.getByRole('button',{name:'Refresh results',exact:true}).click();
  await expect(recent).toContainText('Results unavailable');await expect(recent).toContainText('retained.md');await expect(recent).toContainText('Showing the last');await expectAxeClean(page);
  await page.reload();await expect(recent).toHaveCount(0);
});
