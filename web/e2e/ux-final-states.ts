import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, demoSessions, wikiShotPath } from './helpers';

export function finalStateTests() {
test.use({ storageState: AUTH_FILE });
for (const theme of ['dark', 'light'] as const) {
  for (const width of [320, 390, 1440]) {
    test(`first-run copy respects requested ${width}px viewport in ${theme}`, async ({ page, context }, testInfo) => {
      await page.emulateMedia({ colorScheme: theme });
      await page.setViewportSize({ width, height: 844 });
      await context.grantPermissions(['clipboard-read', 'clipboard-write']);
      await page.route('**/api/auth/state', r => r.fulfill({ json: { setup_required: true, authenticated: false, username: null } }));
      await page.goto('/setup');
      await page.getByRole('button', {name:'Copy setup command'}).click();
      expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('sudo cat /srv/acc/data/secrets/setup_token');
      await expect(page.getByRole('status', { name: 'Announcements' })).toHaveText('Copied');
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      await expectAxeClean(page);
      if (theme === 'dark' && width === 390) await page.screenshot({path:wikiShotPath(testInfo,'setup-copy-phone.png')});
    });
  }
  test(`expanded Setup stays bounded and copies exact files in ${theme}`, async ({ page, context }) => {
    await page.emulateMedia({ colorScheme: theme });
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const row = demoSessions()[0];
    await page.route(`**/api/sessions/${row.id}`, async r => r.fulfill({json:{...row,
      launch_spec:{inputs:{provider:'claude',permission_mode:'acceptEdits',task:'x'.repeat(2500)},digest:'example1234567',files:{'very-long-generated-path/demo.txt':'first\n'+'x'.repeat(2500)}},runs:[],editor:null}}));
    for (const width of [320,390,1440]) {
      await page.setViewportSize({width,height:900});
      await page.goto(`/sessions/${row.id}?tab=spec`);
      await page.getByRole('tabpanel').locator('summary').filter({hasText:/^Exact inputs/}).click();
      await expect(page.getByRole('button',{name:'Copy launch inputs'})).toBeVisible();
      await page.getByRole('tabpanel').locator('summary').filter({hasText:/^Generated files/}).click();
      expect(await page.locator('main').evaluate(e=>e.scrollWidth<=e.clientWidth)).toBe(true);
      await page.getByRole('button',{name:'Copy very-long-generated-path/demo.txt'}).click();
      expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe('first\n'+'x'.repeat(2500));
      await expectAxeClean(page);
    }
  });
  test(`certificate trust command and fingerprint copy with honest fallback in ${theme}`, async ({ page, context }) => {
    await page.emulateMedia({ colorScheme: theme }); await page.setViewportSize({width:320,height:844});
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.route('**/api/certificate',r=>r.fulfill({json:{kind:'caddy',primary:'https://acc.example.lan',fingerprint:'AA:BB:CC:DD',subject:'Example Rynhult root',file:'/demo/root.crt'}}));
    await page.goto('/settings#certificate');
    await page.getByRole('button',{name:'Copy Linux trust command'}).click();
    expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe('sudo cp rynhult-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates');
    await page.getByRole('button',{name:'Copy the fingerprint'}).click();
    expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe('AA:BB:CC:DD');
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:undefined}));
    await page.getByRole('button',{name:'Copy the fingerprint'}).click();
    await expect(page.locator('#certificate').getByText(/Clipboard unavailable/)).toBeVisible();
    expect(await page.locator('main').evaluate(e=>e.scrollWidth<=e.clientWidth)).toBe(true);
    await expectAxeClean(page,'#certificate');
  });
}
test('providers manual refresh failure retains known state and retries',async({page})=>{
 await page.route('**/api/providers?refresh=1',r=>r.fulfill({status:503,json:{detail:'demo unavailable'}}));
 await page.goto('/providers');await page.getByRole('button',{name:'Check now',exact:true}).click();
 await expect(page.getByRole('alert').filter({hasText:"Couldn't check again"})).toBeVisible();
 await page.unroute('**/api/providers?refresh=1');await page.getByRole('alert').filter({hasText:"Couldn't check again"}).getByRole('button',{name:'Retry'}).click();
 await expect(page.getByRole('alert').filter({hasText:"Couldn't check again"})).toHaveCount(0);
});
for (const theme of ['dark','light'] as const) test(`long comparison diffs are named and keyboard scrollable in ${theme}`,async({page})=>{
 await page.emulateMedia({colorScheme:theme});
 const rows=demoSessions().slice(0,2);
 const group={id:'eeeeeeeeeeee',name:'Example alternatives',project_id:1,project:'demo-api',owner:'Alex Example',created_at:'2026-10-23T12:00:00Z',winner_session_id:null,
 runs:rows.map((session,position)=>({position,choice:{provider:'claude',profile:null,model:'',options:{},confirm_dangerous:false},session,error:null,usage:null,ci:null}))};
 await page.route('**/api/multiruns/eeeeeeeeeeee',r=>r.fulfill({json:group}));
 const changes={git:true,list:[{path:'example.txt',status:'modified'}],diff:'diff --git a/example.txt b/example.txt\n--- a/example.txt\n+++ b/example.txt\n@@ -1,1 +1,120 @@\n-old\n'+Array.from({length:120},(_,i)=>`+Example ${i} ${'long '.repeat(60)}`).join('\n')+'\n'};
 await page.route('**/api/multiruns/eeeeeeeeeeee/compare?**',r=>r.fulfill({json:{left:changes,right:changes}}));
 for(const width of [320,390,1440]){
  await page.setViewportSize({width,height:900});await page.goto('/multiruns/eeeeeeeeeeee');
  const diffs=page.locator('pre');await expect(diffs).toHaveCount(2);
  for(const diff of await diffs.all()){await expect(diff).toHaveAttribute('tabindex','0');await expect(diff).toHaveAttribute('aria-label',/example\.txt/);await diff.focus();await page.keyboard.press('End');}
  expect(await page.locator('main').evaluate(e=>e.scrollWidth<=e.clientWidth)).toBe(true);await expectAxeClean(page);
 }
});
for(const [source,label,interval] of [['sessions','Sessions',3000],['attention','Inbox and recent alerts',3000],['observations','Plan usage',30000]] as const){
 test(`board marks only cached ${source} stale after an independent failure`,async({page})=>{
  test.setTimeout(65000);let fail=false;
  await page.route(`**/api/${source}`,r=>fail?r.fulfill({status:503,json:{detail:'demo unavailable'}}):r.continue());
  await page.goto('/board');await expect(page.getByRole('region',{name:'Machines',exact:true})).toContainText('acc-host');
  await page.waitForLoadState('networkidle');
  fail=true;
  const warning=page.getByRole('status').filter({hasText:`${label}: refresh failed`});
  await expect(warning).toBeVisible({timeout:interval+15000});
  await expect(page.getByText('Machines and projects: refresh failed',{exact:false})).toHaveCount(0);
  await expect(warning).toContainText('last successful update');
  fail=false;await warning.getByRole('button',{name:`Retry ${label}`}).click();await expect(warning).toHaveCount(0);
 });
}
test('invitation copying reports denial and preserves the selectable URL',async({page,context})=>{
 await context.grantPermissions(['clipboard-read','clipboard-write']);
 await page.route('**/api/users/invites',r=>r.fulfill({json:{link:'/invite/EXAMPLE'}}));
 await page.goto('/settings#people');await page.getByRole('combobox',{name:'Role',exact:true}).selectOption('owner');await page.getByRole('button',{name:'Create invite link'}).click();
 const copy=page.getByRole('button',{name:'Copy link',exact:true});await copy.click();
 expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(new URL('/invite/EXAMPLE',page.url()).href);
 await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{value:{writeText:()=>Promise.reject(new Error('denied'))}}));
 await copy.click();await expect(page.locator('#people').getByText(/Clipboard unavailable/)).toBeVisible(); // the screen-reader announcement repeats it
 expect(await page.evaluate(()=>getSelection()?.toString())).toBe(new URL('/invite/EXAMPLE',page.url()).href);
});
test('phone page ends at real content and the modal contains exhausted scrolling',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/settings');
 // Measure the real end after query-populated Settings sections have loaded.
 await page.waitForLoadState('networkidle');
 const main=page.locator('main');await main.focus();await page.keyboard.press('Control+End');await main.evaluate(e=>e.scrollTop=e.scrollHeight);
 const before=await main.evaluate(e=>e.scrollTop);await main.hover();await page.mouse.wheel(0,100000);
 await expect.poll(()=>main.evaluate(e=>e.scrollTop)).toBe(before);expect(await page.evaluate(()=>window.scrollY)).toBe(0);
 expect(await main.locator(':scope > div').first().evaluate(e=>parseFloat(getComputedStyle(e).paddingBottom))).toBeLessThanOrEqual(32);
 const touch=await page.context().newCDPSession(page);
 await touch.send('Input.synthesizeScrollGesture',{x:100,y:350,yDistance:-400,gestureSourceType:'touch'});
 await touch.send('Input.synthesizeScrollGesture',{x:100,y:350,xDistance:-200,gestureSourceType:'touch'});
 expect(await main.evaluate(e=>e.scrollTop)).toBe(before);expect(await main.evaluate(e=>e.scrollLeft)).toBe(0);
 expect(await page.evaluate(()=>window.scrollY)).toBe(0);await touch.detach();
 await page.getByRole('button',{name:'New session',exact:true}).click();
 const dialog=page.getByRole('dialog',{name:'New session',exact:true});await dialog.hover();await page.mouse.wheel(0,100000);await page.mouse.wheel(0,100000);
 expect(await main.evaluate(e=>e.scrollTop)).toBe(before);await page.keyboard.press('Escape');
});
test('unverified passkey changes guide verification and server expiry clears it',async({page})=>{
 await page.route('**/api/auth/passkeys/reauth',r=>r.fulfill({json:{ok:true}}));
 let changes=0;await page.route('**/api/auth/passkeys/policy',r=>{changes++;return r.fulfill({status:403,json:{detail:'Verify your sign-in again in Settings → Passkeys, then try again within five minutes.'}});});
 await page.goto('/settings#passkeys');
 await page.getByRole('button',{name:'Save passkey policy'}).click();expect(changes).toBe(0);
 await expect(page.getByRole('heading',{name:'1. Verify your sign-in'}).locator('..')).toBeFocused();
 await page.getByLabel('Verification password',{exact:true}).fill('example password');await page.getByLabel('Verification authenticator code').fill('123456');
 await page.getByRole('button',{name:'Verify with password and authenticator'}).click();await expect(page.getByText('Verified for five minutes.')).toBeVisible();
 await page.getByRole('button',{name:'Save passkey policy'}).click();await expect(page.getByText('Verification expired. Verify again to make changes.')).toBeVisible();expect(changes).toBe(1);
});
test('copy waits for the real clipboard write before announcing success',async({page,context})=>{
 await context.grantPermissions(['clipboard-read','clipboard-write']);await page.goto('/guide/User-Guide/Install');
 await page.evaluate(()=>{
  const write=navigator.clipboard.writeText.bind(navigator.clipboard);
  Object.defineProperty(navigator,'clipboard',{value:{writeText:async(text:string)=>{await new Promise<void>(resolve=>(window as unknown as {finishCopy:()=>void}).finishCopy=resolve);await write(text);}}});
 });
 await page.locator('article').getByRole('button',{name:'Copy code'}).first().click();
 await expect(page.getByRole('status',{name:'Announcements'})).toBeEmpty();
 await page.evaluate(()=>(window as unknown as {finishCopy:()=>void}).finishCopy());
 await expect(page.getByRole('status',{name:'Announcements'})).toHaveText('Copied');
});
test('Team describes helper permissions and singular limits in plain language',async({page})=>{
 const row=demoSessions()[0];
 await page.route(`**/api/sessions/${row.id}`,r=>r.fulfill({json:{...row,team_id:row.id,launch_spec:{inputs:{provider:'claude'},files:{},digest:'example'},runs:[],editor:null}}));
 await page.route(`**/api/sessions/${row.id}/team`,r=>r.fulfill({json:{limits:{at_once:2,total:4,depth:2,hours:1,machines:['local'],agents:[
  {agent:'claude',profile:null,inputs:{permission_mode:'acceptEdits'}},{agent:'codex',profile:null,inputs:{sandbox:'workspace-write',approval:'on-request'}},
  {agent:'copilot',profile:null,inputs:{options:{permissions:'tools'}}},{agent:'cursor',profile:null,inputs:{options:{force:true}}}]},used:{started:0,running:0},expires_at:new Date(Date.now()+3600000).toISOString(),stopped:false,members:[],decisions:[]}}));
 await page.goto(`/sessions/${row.id}?tab=team#settings`);
 await expect(page.getByText('Machines: acc-host · main · Time limit: 1 hour · Counts from Rynhult')).toBeVisible();
 const limits=page.getByRole('heading',{name:'Team limits',exact:true}).locator('../..');
 await expect(limits.getByText(/Accept edits/)).toBeVisible();await expect(limits.getByText(/Can edit the project · Asks when it needs to/)).toBeVisible();
 await expect(limits.getByText(/All tools allowed/)).toBeVisible();await expect(limits.getByText(/Runs without asking/)).toBeVisible();
 await expect(page.getByText(/acceptEdits/)).toHaveCount(0);await expectAxeClean(page);
});
for (const theme of ['dark','light'] as const) test(`Overview actions show a resting affordance and keyboard ring in ${theme}`,async({page})=>{
 await page.emulateMedia({colorScheme:theme});await page.goto('/');
 for(const name of ['Provider details','All projects','Host details']){
  const link=page.getByRole('link',{name,exact:true});await expect(link).toBeVisible();
  expect(await link.evaluate(e=>getComputedStyle(e).textDecorationLine)).toContain('underline');
  await link.focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');
  await expect(link).toBeFocused();expect(await link.evaluate(e=>getComputedStyle(e).outlineWidth)).toBe('2px');
 }
});

}
