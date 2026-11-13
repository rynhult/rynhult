import { test } from './shell-state';
import {writeFileSync} from 'node:fs';
import {expect,type Page} from '@playwright/test';
import {AUTH_FILE,csrfHeaders,expectAxeClean,gitProject,info,PASSWORD,totp,verifyAgain} from './helpers';
// The member journey intentionally saves that person's shell choice.
test.use({storageState:AUTH_FILE,presentationChanges:['layouts',]});

test('owner scopes converge on existing editors and preserve account drafts across scopes',async({page})=>{
  test.setTimeout(150000);const project=await gitProject(page,'cedar-settings');
  for(const width of [1440,390])for(const theme of ['light','dark'] as const){
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme:theme});await page.goto('/settings#passkeys');
    await expect(page.getByRole('navigation',{name:'Settings scopes',exact:true})).toHaveCount(0);
    await expect(page.getByRole('combobox',{name:'Settings section',exact:true})).toBeHidden();
    if(width===1440) { const y=(await page.locator('#passkeys').boundingBox())!.y; console.log('density Settings section y',y); expect(y).toBeLessThan(240); }
    const draft=page.getByRole('textbox',{name:'Passkey name',exact:true});await draft.fill('Travel laptop draft');
    await expectAxeClean(page);
    await page.getByRole('main').evaluate(()=>location.hash='admin-features');await expect(page.locator('#admin-features')).toBeVisible();
    await page.getByRole('main').evaluate(()=>location.hash='passkeys');await expect(draft).toHaveValue('Travel laptop draft');
    await page.getByRole('main').evaluate(()=>location.hash='project-settings');
    const contextual=page.getByRole('main').getByRole('region',{name:'Project settings',exact:true});await contextual.getByRole('combobox',{name:'Settings project',exact:true}).selectOption(String(project.id));
    await expect(contextual.getByRole('link',{name:'Keys',exact:true})).toHaveAttribute('href',`/projects/${project.id}#project-keys`);
    await expect(contextual.getByRole('link',{name:'Allowed folders',exact:true})).toHaveAttribute('href',/\/settings\/security\/folders\?machine=local&account=/);
    if(theme==='dark')await contextual.screenshot({path:`../.acc/artifacts/settings-p1/project-${width}-dark.png`});
    await contextual.getByRole('link',{name:'Security',exact:true}).click();await expect(page.locator('#security')).toBeVisible();
    await expect(page.getByRole('combobox',{name:'Security scope',exact:true})).toHaveValue('project');await expect(page.getByRole('combobox',{name:'Project security',exact:true})).toHaveValue(String(project.id));
    await expect(page.locator('[data-settings-section="Security"]')).toHaveCount(1);await expect(page.locator('#security-force_push')).toHaveCount(1);
    await expectAxeClean(page);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if(theme==='dark')await page.screenshot({path:`../.acc/artifacts/settings-p1/owner-${width}-${theme}.png`,fullPage:true});
    if(width===390)await page.getByRole('button',{name:'More',exact:true}).click();
    await page.getByRole('button',{name:'Settings',exact:true}).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByRole('main').getByRole('heading',{name:'Settings',exact:true,level:1})).toBeVisible();
    if(width===390)await page.getByRole('navigation',{name:'Phone rail'}).getByRole('button',{name:'More',exact:true}).click();
    const sidebar=width===390?page.getByRole('dialog',{name:'Browse',exact:true}):page.getByRole('complementary',{name:'Context sidebar',exact:true});
    await expect(sidebar.getByRole('heading',{name:'Account',exact:true})).toBeVisible();await expect(sidebar.getByRole('heading',{name:'Project',exact:true})).toBeVisible();await expect(sidebar.getByRole('heading',{name:'Installation',exact:true})).toBeVisible();
    await expect(sidebar.getByRole('link',{name:'Allowed folders',exact:true}).last()).toHaveAttribute('href','/settings/security/folders');
    if(width===390)await page.keyboard.press('Escape');
  }
  const second=await gitProject(page,'birch-settings');
  const previous=`/settings?project_id=${project.id}#project-settings`;
  await page.goto(previous);
  await expect(page).toHaveURL(new URL(previous,info().origin).href);
  const picker=page.getByRole('main').locator('#project-settings select');
  await picker.selectOption(String(second.id));
  const security=page.getByRole('main').getByRole('region',{name:'Project settings',exact:true}).getByRole('link',{name:'Security',exact:true});
  const destination=await security.getAttribute('href');
  await security.click();
  await expect(page).toHaveURL(new URL(destination!,info().origin).href);
  await expect(page.getByRole('combobox',{name:'Project security',exact:true})).toHaveValue(String(second.id));
  await expect(picker).toHaveValue(String(second.id));
  await page.goBack();
  await expect(page).toHaveURL(new URL(previous,info().origin).href);
  await expect(picker).toHaveValue(String(project.id));

});
test('member scopes retain source visibility and never mount installation editors',async({page,context})=>{
  test.setTimeout(120000);const privateProject=await gitProject(page,'cedar-settings-private');const account=`settings-${crypto.randomUUID().slice(0,8)}-agent`;writeFileSync(`${info().state}/fixture-accounts/${account}`,'');
  const invite=await page.request.post('/api/users/invites',{headers:await csrfHeaders(page),data:{role:'member',account}});expect(invite.ok(),await invite.text()).toBeTruthy();const token=(await invite.json()).link.split('/').pop();await context.clearCookies();
  const begin=await page.request.post('/api/auth/invite/begin',{headers:{Origin:info().origin},data:{token}});
  const joined=await page.request.post('/api/auth/invite/complete',{headers:{Origin:info().origin},data:{token,username:`blair-settings-${Date.now()}`,password:PASSWORD,code:totp((await begin.json()).totp_secret)}});expect(joined.ok(),await joined.text()).toBeTruthy();
  const project=await gitProject(page,'maple-settings');
  expect((await page.request.get(`/api/projects/${privateProject.id}/knowledge`)).status()).toBe(404);
  const forbidden:string[]=[];page.on('request',r=>{const path=new URL(r.url()).pathname;if(['/api/security/scopes','/api/users','/api/nodes','/api/board','/api/security/folders'].includes(path))forbidden.push(path);});
  for(const width of [1440,390])for(const theme of ['light','dark'] as const){
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme:theme});await page.goto('/settings#passkeys');
    const scopes=page.getByRole('complementary',{name:'Context sidebar',exact:true});await expect(scopes.getByRole('heading',{name:'Installation',exact:true})).toHaveCount(0);await expect(page.locator('[data-settings-section="People"]')).toHaveCount(0);
    await expectAxeClean(page);await page.getByRole('main').evaluate(()=>location.hash='project-settings');const contextual=page.getByRole('main').getByRole('region',{name:'Project settings',exact:true});
    await expect(contextual.getByRole('combobox',{name:'Settings project',exact:true}).getByRole('option',{name:privateProject.slug,exact:true})).toHaveCount(0);
    await contextual.getByRole('combobox',{name:'Settings project',exact:true}).selectOption(String(project.id));await expect(contextual.getByRole('link',{name:'Allowed folders',exact:true})).toHaveCount(0);
    await contextual.getByRole('link',{name:'Security',exact:true}).click();await expect(page.locator('#security')).toContainText('An owner manages these settings.');await expect(page.getByRole('combobox',{name:'Security scope',exact:true})).toHaveCount(0);await expect(page.locator('#security')).toContainText('Effective protections');
    await expectAxeClean(page);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if(theme==='dark')await page.screenshot({path:`../.acc/artifacts/settings-p1/member-${width}-${theme}.png`,fullPage:true});
  }
  expect(forbidden).toEqual([]);
});
test('every legacy Settings fragment and dedicated URL still resolves in the new layout',async({page})=>{
  test.setTimeout(150000);
  const targets=['access-devices','passkeys','password','sign-ins','notifications','notification-devices','keys','risk-labels','guardrails','security','certificate','admin-features','remote-access','usage-and-bug-reports','updates','publishing','people','company-sign-in','dns','issue-sources','tool-approvals','budgets','selection-allow-lists','image-access','container-limits'];
  const aliases={'your-devices':'access-devices',devices:'notification-devices',telemetry:'usage-and-bug-reports','infrastructure-keys':'dns','dns-certificates':'dns'};
  await page.goto('/settings');
  await expect(page.getByRole('navigation',{name:'Settings scopes',exact:true})).toHaveCount(0);
  for(const target of targets){await page.goto(`/settings#${target}`);await expect(page.locator(`#${target}`)).toBeVisible();await expect(page.locator(`[data-settings-section][id="${target}"]`)).toHaveCount(1);}
  for(const [alias,target] of Object.entries(aliases)){await page.goto(`/settings#${alias}`);await expect(page.locator(`#${target}`)).toBeVisible();}
  for(const alias of ['layout','all','settings-index','future-settings-anchor']){await page.goto(`/settings#${alias}`);await expect(page.locator('#passkeys')).toBeVisible();await expect(page.locator('#settings-index')).toBeVisible();}
  await page.goto('/settings/security/folders');await expect(page.getByRole('heading',{name:'Allowed folders',exact:true,level:1})).toBeVisible();
  await page.goto('/settings/remote-access');await expect(page.getByRole('heading',{name:'Remote access',exact:true,level:1})).toBeVisible();
});

test('Settings sidebar supports keyboard navigation and compact attention keeps warning details',async({page})=>{

  await page.route('**/api/certificate',route=>route.fulfill({json:{kind:'unverified',primary:'acc.example.lan'}}));
  await page.goto('/settings');
  const main=page.getByRole('main');
  await expect(main.getByRole('combobox',{name:'Settings section',exact:true})).toBeHidden();
  const attention=main.getByRole('region',{name:'Settings needing attention'});
  const disclosure=attention.locator('summary');
  await expect(disclosure).toContainText('Settings needing attention');
  const y=(await page.locator('#passkeys').boundingBox())!.y; console.log('density Settings default section y',y); expect(y).toBeLessThan(240);
  await disclosure.focus();await page.keyboard.press('Enter');
  await expect(attention).toContainText('Not publicly trusted, and no root is configured');
  await attention.getByRole('link',{name:'Certificate',exact:true}).click();
  await expect(page.locator('#certificate')).toBeFocused();
  const sidebar=page.getByRole('complementary',{name:'Context sidebar',exact:true});
  await sidebar.getByRole('link',{name:'Passkeys',exact:true}).focus();await page.keyboard.press('Enter');
  await expect(page.locator('#passkeys')).toBeFocused();
  await expectAxeClean(page);
});
