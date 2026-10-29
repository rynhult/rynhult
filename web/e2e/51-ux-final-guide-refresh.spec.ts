import {expect,test} from '@playwright/test';
import {AUTH_FILE,gitProject,routeDemoSessions,sessionWithChanges,wikiShotPath,expectAxeClean} from './helpers';
test.use({storageState:AUTH_FILE});
test('refresh changed Guide screens with artificial projects and certificate data',async({page},testInfo)=>{
 test.setTimeout(90000);await gitProject(page,'guide-review');await routeDemoSessions(page);
 await page.route('**/api/attention',r=>r.fulfill({json:[{id:9001,kind:'needs_input',severity:'warn',source:'claude_hook',verified:true,title:'api-refactor needs you: permission to run tests',detail:null,session_id:'aaaaaaaaaaa6',session_name:'api-refactor',project:'demo-api',created_at:new Date(Date.now()-1800000).toISOString(),updated_at:new Date(Date.now()-1800000).toISOString(),acked_at:null,resolved_at:null,publish_id:null}]}));
 await page.route('**/api/wiki/guide/**/page?**',async r=>{const response=await r.fetch();const data=await response.json();await r.fulfill({response,json:{...data,author:'Alex Example',commit:'abc12345',committed_at:'2026-10-23T12:00:00Z'}});});
 const captures=[['/','overview.png'],['/projects','projects.png'],['/host','host.png'],['/providers','providers.png'],['/catalog','catalog-profiles.png'],['/settings','settings.png'],['/guide','guide.png']] as const;
 for(const [url,name] of captures){await page.goto(url);await page.waitForLoadState('networkidle');await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,name)});}
 await page.emulateMedia({colorScheme:'light'});await page.goto('/');await page.waitForLoadState('networkidle');await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'overview-light.png')});
 await page.emulateMedia({colorScheme:'dark'});await page.setViewportSize({width:390,height:844});await page.goto('/');await page.waitForLoadState('networkidle');await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'phone-overview.png')});
 await page.setViewportSize({width:1440,height:900});await page.goto('/host');await page.getByRole('heading',{name:'Machines',exact:true}).evaluate(e=>e.scrollIntoView({block:'start'}));await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'host-machines.png')});
 await page.getByRole('heading',{name:'Agent accounts',exact:true}).locator('xpath=ancestor::section[1]').screenshot({animations:'disabled',path:wikiShotPath(testInfo,'host-accounts.png')});
 await page.route('**/api/certificate',r=>r.fulfill({json:{kind:'caddy',primary:'https://acc.example.lan',subject:'Example Rynhult root',fingerprint:Array(32).fill('AA').join(':'),file:'/demo/rynhult-root.crt',not_before:'2026-01-01T00:00:00Z',not_after:'2036-01-01T00:00:00Z'}}));
 await page.goto('/settings#certificate');await page.getByRole('button',{name:'Copy Linux trust command'}).scrollIntoViewIfNeeded();await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'settings-certificate.png')});await expectAxeClean(page,'#certificate');
 await page.goto('/settings#people');await page.getByRole('heading',{name:'People',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'settings-people.png')});
 await page.goto('/settings#updates');await page.locator('#updates').screenshot({animations:'disabled',path:wikiShotPath(testInfo,'updates-settings.png')});
 const id=await sessionWithChanges(page,'guide-final-diff');await page.goto(`/sessions/${id}?tab=changes`);await expect(page.getByRole('region',{name:/Changes in app.py/})).toBeVisible();await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'session-changes.png')});await expectAxeClean(page);
 await page.goto('/catalog');await page.getByRole('tab',{name:/^Skills/}).click();await page.screenshot({animations:'disabled',path:wikiShotPath(testInfo,'catalog-skills.png')});
});
for(const theme of ['dark','light'] as const) for(const width of [390,1440]) test(`forced 503 details contrast at ${width} in ${theme}`,async({page})=>{
 await page.emulateMedia({colorScheme:theme});await page.setViewportSize({width,height:900});await page.route('**/api/dns/credentials',r=>r.fulfill({status:503,json:{detail:'Example service unavailable'}}));await page.goto('/settings#infrastructure-keys');
 const note=page.locator('#infrastructure-keys');await expect(note.getByRole('alert')).toBeVisible();await expectAxeClean(page,'#infrastructure-keys');await note.locator('summary').click();await expectAxeClean(page,'#infrastructure-keys');
});
test('member section index lists existing cards and private-CA trust has exact Copy',async({page,context})=>{
 await context.grantPermissions(['clipboard-read','clipboard-write']);let privateCa=false;
 await page.route('**/api/auth/state',async r=>{const data=await(await r.fetch()).json();await r.fulfill({json:{...data,role:'member',instance:{...data.instance,private_ca:privateCa}}});});
 await page.goto('/settings#all');const index=page.getByRole('navigation',{name:'Settings sections'});await expect(index.getByRole('link',{name:'Certificate',exact:true})).toHaveCount(0);
 privateCa=true;await page.reload();await index.getByRole('link',{name:'Certificate',exact:true}).click();await page.getByRole('button',{name:'Copy Linux trust command'}).click();
 expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe('sudo cp rynhult-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates');
 await expectAxeClean(page,'#certificate');
});
