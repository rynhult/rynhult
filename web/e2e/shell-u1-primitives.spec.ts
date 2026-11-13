import { test, expect, type Page } from './fixtures';
import { mkdirSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, verifyAgain, gitProject, expectAxeClean } from './helpers';
test.use({ presentationChanges: ['admin_features', 'layouts'],storageState:AUTH_FILE,viewport:{width:1440,height:1000}});

async function capture(page:Page,name:string){
  if(process.env.ACC_U1_CAPTURE){mkdirSync('../docs/handoffs/shell-u1-primitives',{recursive:true});await page.screenshot({path:`../docs/handoffs/shell-u1-primitives/${process.env.ACC_U1_CAPTURE}-${name}.png`,animations:'disabled'});}
}
test('Overview and Files visual adoption gate, including reload',async({page})=>{
  test.setTimeout(180000);
  const project=await gitProject(page,'cedar-primitives');writeFileSync(`${project.folder}/README.md`,'# Cedar notes\n\nKeep the original workshop notes.\n\n## Restore\n\n- Review changes\n- Keep originals\n');
  await page.addInitScript(()=>localStorage.removeItem('acc-theme'));
  for(const theme of ['dark','light'] as const){
    await page.emulateMedia({colorScheme:theme,reducedMotion:'reduce'});
    await page.goto('/');await expect(page.getByRole('navigation',{name:'Rail',exact:true})).toBeVisible();
    await expect(page.getByRole('main').getByRole('heading',{name:'Overview',exact:true})).toBeVisible();
    await expect(page.locator('main .animate-pulse')).toHaveCount(0);await capture(page,`overview-${theme}`);
    await expectAxeClean(page);
    await page.goto(`/projects/${project.id}?tab=files&file=README.md`);
    await expect(page.getByRole('textbox',{name:'File content'})).toContainText('Keep the original workshop notes');
    await capture(page,`files-${theme}`);await expectAxeClean(page);
    if(process.env.ACC_U1_CAPTURE!=='before'){
      await expect(page.locator('[data-shell-files] .shell-surface')).toHaveCount(2);
      expect(await page.locator('[data-shell-files] .shell-surface').first().evaluate(n=>getComputedStyle(n).borderRadius)).toBe('0px');
    }
    await page.goto('/');await expect(page.getByRole('navigation',{name:'Rail',exact:true})).toBeVisible();
    await expect(page.locator('[data-shell-page]')).toBeVisible();
    expect(await page.getByRole('main').getByRole('heading',{level:1}).evaluate(n=>getComputedStyle(n).fontSize)).toBe('20px');
    await page.goto(`/projects/${project.id}?tab=files&file=README.md`);await expect(page.getByRole('textbox',{name:'File content'})).toBeVisible();
    await expect(page.locator('[data-shell-files]')).toBeVisible();
    expect(await page.getByRole('region',{name:'Folder tree'}).locator('section').first().evaluate(n=>getComputedStyle(n).borderRadius)).toBe('0px');
  }
  await page.goto('/');
  await expect(page.locator('[data-shell-page]')).toBeVisible();
  await expect(page.locator('.shell-workbar h1')).toBeVisible();
});

test('core routes use compact workbars and surfaces across reload',async({page})=>{
  test.skip(process.env.ACC_U1_STAGE==='gate','Gate the initial Overview/Files slice before route rollout.');
  test.setTimeout(180000);await page.addInitScript(()=>localStorage.removeItem('acc-theme'));
  for(const theme of ['dark','light'] as const){
    await page.emulateMedia({colorScheme:theme,reducedMotion:'reduce'});
    for(const route of ['/sessions','/inbox','/settings','/host','/catalog']){
      await page.goto(route);await expect(page.locator('[data-shell-page]')).toBeVisible();
      await expect(page.locator('.shell-workbar h1')).toBeVisible();
      expect(await page.locator('.shell-workbar h1').evaluate(n=>getComputedStyle(n).fontSize)).toBe('20px');
      await expect(page.locator('main .animate-pulse')).toHaveCount(0);await expectAxeClean(page);
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
      await capture(page,`${route.slice(1)}-${theme}`);
      const tabs=page.locator('.shell-pane-tabs [role=tab]');
      if(await tabs.count()){
        const tab=tabs.first();await tab.focus();await page.keyboard.press('ArrowRight');
        await expect(tabs.nth(1)).toBeFocused();await expect(tabs.nth(1)).toHaveAttribute('aria-selected','true');
      }
      await page.goto(route);await expect(page.getByRole('navigation',{name:'Rail',exact:true})).toBeVisible();
      await expect(page.locator('[data-shell-page]')).toBeVisible();
      await expect(page.locator('.shell-workbar')).toBeVisible();
      expect(await page.getByRole('main').getByRole('heading',{level:1}).evaluate(n=>getComputedStyle(n).fontSize)).toBe('20px');
    }
  }
});
