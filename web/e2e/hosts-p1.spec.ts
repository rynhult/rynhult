import { test } from './shell-state';
import {writeFileSync} from 'node:fs';
import {expect,type Page,type Cookie} from '@playwright/test';
import {AUTH_FILE,expectAxeClean,info,PASSWORD,totp,verifyAgain} from './helpers';
// Member journeys deliberately save their own shell preference; owner restoration
// alone cannot restore a different person's setting. The shared fixture owns both.
test.use({storageState:AUTH_FILE,presentationChanges:['layouts','ui_shell']});
const headers=async(page:Page)=>({'x-acc-csrf':(await page.context().cookies()).find(c=>/acc_csrf$/.test(c.name))!.value});
async function enable(page:Page) {
  const put=async()=>{
    const state=await(await page.request.get('/api/admin-features')).json();
    return page.request.put('/api/admin-features/new_shell',{headers:await headers(page),data:{enabled:true,revision:state.revision}});
  };
  let response=await put();
  if(response.status()===403){await verifyAgain(page);response=await put();}
  expect(response.ok(),await response.text()).toBeTruthy();
}
async function preference(page:Page,enabled=true) {
  expect((await page.request.put('/api/preferences/ui-shell',{headers:await headers(page),data:{enabled}})).ok()).toBeTruthy();
}
let memberCookies:Cookie[]|undefined;
const project={id:901,slug:'cedar',name:'Cedar archive',host:'local',account:'finn-agent',workspace_path:'/home/alex/src/cedar',repo:null,remote_url:null,import_mode:'managed',trusted:true,archived:false,notes:null,compose_autostart:false,compose_approved:false,git_backup:false};
async function data(page:Page,owner:boolean) {
  await page.route('**/api/projects',r=>r.fulfill({json:[project]}));
  await page.route('**/api/sessions',r=>r.fulfill({json:[{id:'saved-edge',project_id:901,project:'Cedar archive',name:'Saved offline work',host:'edge',desired_state:'running',observed_state:'idle',provider:'claude',machine_offline:true}]}));
  await page.route('**/api/groups',r=>r.fulfill({json:[{id:'shared',role:owner?'maintainer':'member'}]}));
  await page.route('**/api/groups/shared/projects',r=>r.fulfill({json:[{id:902,name:'Maple service'}]}));
  await page.route('**/api/groups/shared/projects/902/placements',r=>r.fulfill({json:owner?[{id:1,host:'edge',account:'alex-agent',path:'/home/alex/checkouts/cedar',sync_status:'current',trust:'trusted'}]:[{id:2,host:'local',account:'finn-agent',path:'/home/blair/checkouts/maple',sync_status:'current',trust:'trusted'}]}));
  const now=Date.now()/1000;
  if(owner) await page.route('**/api/board',r=>r.fulfill({json:{generated_at:now,machines:[
    {name:'local',label:'Home lab',local:true,connected:true,light:{collected_at:now-20,cpu_percent:24,memory_percent:25,disk:{mount:'/',percent:91,free:1000}},alerts:[{severity:'warn',title:'Host health readings are out of date'}]},
    {name:'edge',label:'Edge machine',local:false,connected:false,last_seen:new Date(Date.now()-3*86400000).toISOString(),light:null,alerts:[]},
    {name:'stale',label:'Stale machine',local:false,connected:true,last_seen:new Date().toISOString(),stale:true,light:{collected_at:now-3600,cpu_percent:12,memory_percent:38},alerts:[]},
  ],projects:[]}}));
  await page.route('**/api/host',r=>r.fulfill({json:{live:{measured_at:now-20,cpu_percent:24,memory:{percent:25}},snapshot:{collected_at:now-3600,disks:[{mount:'/',device:'/dev/demo',percent:91,total:10000,used:9100,free:900}]}}}));
}
for(const width of [1440,390]) for(const theme of ['light','dark'] as const) {
  test(`owner Hosts preserves resources and honest observations ${width} ${theme}`,async({page})=>{
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme:theme});
    await enable(page);await preference(page);await data(page,true);
    await page.goto('/projects');
    if(width===390) await page.getByRole('button',{name:'More',exact:true}).click();
    await page.getByRole('button',{name:'Hosts',exact:true}).click();
    await expect(page).toHaveURL(/\/hosts$/);
    // The rail opens Hosts directly; machine links remain useful sub-navigation.
    await expect(page.getByRole('heading',{name:'Hosts',exact:true,level:1})).toBeVisible();
    const edge=page.getByRole('region',{name:'Edge machine',exact:true});
    await expect(edge).toContainText('Offline');await expect(edge).toContainText('Last seen');
    await expect(edge).toContainText('Unavailable');await expect(edge).not.toContainText('Idle');await expect(edge).not.toContainText('CPU 0%');
    await expect(edge.getByRole('link',{name:'Maple service',exact:true})).toHaveAttribute('href','/groups?group=shared&tab=projects');
    await expect(edge).toContainText('/home/alex/checkouts/cedar');
    await expect(edge.getByRole('link',{name:'Open host',exact:true})).toHaveAttribute('href','/host?machine=edge');
    const stale=page.getByRole('region',{name:'Stale machine',exact:true});await expect(stale).toContainText('Stale');await expect(stale).toContainText('ago');
    const home=page.getByRole('region',{name:'Home lab',exact:true});
    await expect(home).toContainText('/home/alex/src/cedar');
    await expect(home.getByRole('group',{name:'Live readings',exact:true})).toContainText(/CPU 24%.*Memory 25%.*Measured just now/);
    await expect(home.getByRole('group',{name:'Collector readings',exact:true})).toContainText(/Disk 91%.*Stale.*1h ago/);
    await expect(home).toContainText('Host health readings are out of date');
    await expectAxeClean(page);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if(width===1440&&theme==='light'||width===390&&theme==='dark')await page.screenshot({path:`../.acc/artifacts/hosts-p1/owner-${width}-${theme}.png`,fullPage:true});
  });
  test(`member Hosts stays within existing host visibility ${width} ${theme}`,async({page,context})=>{
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme:theme});
    await enable(page);
    if(!memberCookies) {
      const account=`hosts-${crypto.randomUUID().slice(0,8)}-agent`;
      writeFileSync(`${info().state}/fixture-accounts/${account}`,'');
      const invite=await page.request.post('/api/users/invites',{headers:await headers(page),data:{role:'member',account}});
      expect(invite.ok(),await invite.text()).toBeTruthy();const token=(await invite.json()).link.split('/').pop();await context.clearCookies();
      const begin=await page.request.post('/api/auth/invite/begin',{headers:{Origin:info().origin},data:{token}});
      const secret=(await begin.json()).totp_secret;const username=`blair-hosts-${Date.now()}`;
      const joined=await page.request.post('/api/auth/invite/complete',{headers:{Origin:info().origin},data:{token,username,password:PASSWORD,code:totp(secret)}});
      expect(joined.ok(),await joined.text()).toBeTruthy();memberCookies=await context.cookies();
    } else {
      await context.clearCookies();await context.addCookies(memberCookies);
    }
    await preference(page);await data(page,false);
    const forbidden:string[]=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname==='/api/board'||u.pathname==='/api/nodes'||u.pathname==='/api/host'&&u.searchParams.has('machine'))forbidden.push(u.pathname+u.search);});
    await page.goto('/hosts?machine=edge');
    await expect(page.getByRole('heading',{name:'Hosts',exact:true,level:1})).toBeVisible();
    await expect(page.getByRole('main')).toContainText('/home/alex/src/cedar');
    const live=page.getByRole('main').getByRole('group',{name:'Live readings',exact:true});
    await expect(live).toContainText(/CPU 24%.*Memory 25%.*Measured just now/);await expect(live).not.toContainText('1h ago');
    await expect(page.getByRole('main').getByRole('group',{name:'Collector readings',exact:true})).toContainText(/Disk 91%.*Stale.*1h ago/);
    await expect(page.getByRole('main')).toContainText('Host health readings are out of date');
    await expect(page.getByRole('main')).toContainText('/home/blair/checkouts/maple');
    await expect(page.getByRole('main')).not.toContainText('Edge machine');await expect(page.getByRole('main')).not.toContainText('/home/alex/checkouts/cedar');
    await expect(page.getByRole('main').getByRole('link',{name:'Open host',exact:true})).toHaveAttribute('href','/host');
    expect(forbidden).toEqual([]);await expectAxeClean(page);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  });
}
test('standard shell keeps its Host destination',async({page})=>{
  // The previous member tests deliberately use separate contexts; this is the owner fixture again.
  await preference(page,false);await page.goto('/hosts');
  await expect(page).toHaveURL(/\/host$/);await expect(page.getByRole('navigation',{name:'Main',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Host',exact:true,level:1})).toBeVisible();
});

test('failed host refresh keeps dated observations without claiming a connection',async({page})=>{
  await enable(page);await preference(page);await data(page,true);await page.goto('/hosts');
  await expect(page.getByRole('region',{name:'Home lab',exact:true})).toContainText('Connected');
  await page.route('**/api/board',r=>r.fulfill({status:503,json:{detail:'Host summary temporarily unavailable'}}));
  await page.getByRole('button',{name:'Refresh hosts',exact:true}).click();
  await expect(page.getByRole('main')).toContainText('Showing the last data received');
  const home=page.getByRole('region',{name:'Home lab',exact:true});await expect(home).toContainText('Unavailable');await expect(home).not.toContainText('Connected');await expect(home).toContainText(/Last seen (just now|.*ago)/);
  await expectAxeClean(page);
});

test('failed session refresh dates saved work in cards while the sidebar stays names only',async({page})=>{
  await enable(page);await preference(page);await data(page,true);await page.clock.install({time:Date.now()});
  await page.route('**/api/sessions',route=>route.fulfill({json:[{id:'saved-local',project_id:901,project:'Cedar archive',name:'Earlier active work',host:'local',desired_state:'running',observed_state:'active',provider:'claude',machine_offline:false}]}));
  await page.goto('/hosts');const home=page.getByRole('region',{name:'Home lab',exact:true});await expect(home).toContainText('Working 1');
  await page.getByRole('button',{name:'Hosts',exact:true}).click();
  const sidebar=page.getByRole('complementary',{name:'Context sidebar',exact:true});
  const local=sidebar.getByRole('link').filter({hasText:'Home lab'});await expect(local).toHaveText('Home lab');
  await expect(local).toHaveAttribute('href','/host');
  await page.route('**/api/sessions',route=>route.fulfill({status:503,json:{detail:'Fixture session refresh unavailable'}}));
  const future=Date.now()+3600000;
  await page.route('**/api/board',route=>route.fulfill({json:{generated_at:future/1000,machines:[{name:'local',label:'Home lab',local:true,connected:true,light:{collected_at:future/1000,cpu_percent:24,memory_percent:25},alerts:[]}],projects:[]}}));
  await page.route('**/api/host',route=>route.fulfill({json:{live:{measured_at:future/1000,cpu_percent:24,memory:{percent:25}},snapshot:{collected_at:future/1000,disks:[]}}}));
  await page.clock.setSystemTime(future);await page.getByRole('button',{name:'Refresh hosts',exact:true}).click();
  await expect(home).toContainText(/Last received 1h ago.*Saved counts.*Working 1/,{timeout:10000});
  await expect(local).toHaveText('Home lab');
  await expect(sidebar).not.toContainText(/Saved counts|Last received|CPU/);
  await expect(home.getByRole('group',{name:'Live readings',exact:true})).toContainText('Measured just now');
  await expect(home).toContainText('Active work: unavailable');await expectAxeClean(page);
});
