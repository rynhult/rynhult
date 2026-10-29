import {expect,test} from '@playwright/test';
import {AUTH_FILE,gitProject,expectAxeClean} from './helpers';
test.use({storageState:AUTH_FILE});
test('Files metadata clicks the one semantic row button and preserves its readable name',async({page})=>{
 const {id}=await gitProject(page,'file-hit-review');await page.goto(`/projects/${id}?tab=files`);
 const button=page.getByRole('button',{name:/^Open app.py(,.*)?$/});
 const metadata=button.locator('xpath=following-sibling::span[1]');await expect(metadata).toBeVisible();const box=await metadata.boundingBox();expect(box).not.toBeNull();
 await page.mouse.click(box!.x+box!.width/2,box!.y+box!.height/2);
 await expect(page.getByRole('textbox',{name:'File content'})).toContainText("print('hello')");
 await expect(button).toHaveAccessibleName(/^Open app.py(, (new|modified|ignored))?$/);
 await expectAxeClean(page);
});
