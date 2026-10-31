import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, gitProject, wikiShotPath } from './helpers';

test.use({storageState:AUTH_FILE});
for(const width of [1440,390]) for(const theme of ['dark','light'] as const) {
  test(`owner image check at ${width}px ${theme}`,async({page},info)=>{
    await page.setViewportSize({width,height:900});
    await page.emulateMedia({colorScheme:theme});
    const project=await gitProject(page,'image-check');
    let value:boolean|null=null;
    await page.route('**/api/models?project_id=*',route=>route.fulfill({json:{claude:[{id:'synthetic-vision',label:'Synthetic model',image_input:value}]}}));
    await page.route('**/api/providers/image-check',async route=>{
      expect(route.request().postDataJSON()).toEqual({project_id:project.id,provider:'claude',model:'synthetic-vision'});
      value=true;
      await route.fulfill({json:{image_input:true,checked_at:1234567890,evidence:{outcome:'pass'}}});
    });
    await page.goto('/settings#image-access');
    const form=page.getByRole('form',{name:'Check image access'});
    await form.getByRole('combobox',{name:'Execution project',exact:true}).selectOption(String(project.id));
    await form.getByRole('combobox',{name:'Model',exact:true}).selectOption('synthetic-vision');
    await expect(form.getByText('Image access: Unknown')).toBeVisible();
    const button=form.getByRole('button',{name:'Check image access',exact:true});
    await button.focus();await page.keyboard.press('Enter');
    await expect(form.getByRole('status')).toContainText('Verified · pass');
    await expect(form.getByText('Image access: Verified')).toBeVisible();
    await form.scrollIntoViewIfNeeded();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await expectAxeClean(page);
    await page.screenshot({path:wikiShotPath(info,`image-access-${width}-${theme}.png`)});
  });
}

for (const width of [1440, 390]) {
  test(`image need card runs the exact scoped check at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    const action={project_id:42,provider:'claude',model:'synthetic-vision'};
    let requests=0;
    await page.route('**/api/attention?*',route=>route.fulfill({json:[{
      id:98765,kind:'image_check',severity:'warn',source:'image access',verified:true,
      title:'Check image access',detail:'This model is not yet checked. A short read-only check may use provider quota.',
      created_at:new Date().toISOString(),updated_at:new Date().toISOString(),resolved_at:null,acked_at:null,
      session_id:null,session_name:null,project:'Demo',image_check:action,
    }]}));
    await page.route('**/api/providers/image-check',async route=>{
      expect(route.request().postDataJSON()).toEqual(action); requests++;
      await route.fulfill({json:{image_input:true,checked_at:1234567890,evidence:{outcome:'pass'}}});
    });
    await page.goto('/inbox');
    const button=page.getByRole('button',{name:'Check image access',exact:true});
    await expect(button).toBeVisible();
    await button.focus(); await page.keyboard.press('Enter');
    await expect(page.getByRole('status').filter({hasText:'Verified · pass'})).toBeVisible();
    expect(requests).toBe(1);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await expectAxeClean(page);
  });
}
