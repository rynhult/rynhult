import {expect,test} from '@playwright/test';
import {AUTH_FILE,demoSessions} from './helpers';
test.use({storageState:AUTH_FILE});
for(const [provider,inputs,words] of [
 ['claude',{permission_mode:'acceptEdits'},'Accept edits'],
 ['codex',{sandbox:'workspace-write',approval:'on-request'},'Can edit the project · Asks when it needs to'],
 ['copilot',{options:{permissions:'tools'}},'All tools allowed'],
 ['cursor',{options:{force:true}},'Runs without asking'],
] as const) test(`Setup agrees with Team about ${provider} permissions`,async({page})=>{
 const row=demoSessions()[0];await page.route(`**/api/sessions/${row.id}`,r=>r.fulfill({json:{...row,provider,launch_spec:{inputs:{...inputs,provider},files:{},digest:'example'},runs:[],editor:null}}));
 await page.goto(`/sessions/${row.id}?tab=spec`);
 await expect(page.locator('dt').filter({hasText:'Permissions'}).locator('xpath=following-sibling::dd[1]')).toHaveText(words);
});
