import { expect, test } from '@playwright/test';
import {AUTH_FILE,expectAxeClean,wikiShotPath} from './helpers';
test.use({storageState:AUTH_FILE});
test('Guide copy controls and Settings section index screenshots use artificial metadata',async({page},testInfo)=>{
 await page.route('**/api/wiki/guide/**/page?**',async r=>{const response=await r.fetch();const data=await response.json();await r.fulfill({response,json:{...data,author:'Alex Example',commit:'abc12345',committed_at:'2026-10-23T12:00:00Z'}});});
 await page.goto('/guide/User-Guide/Install');await expect(page.locator('article').getByRole('button',{name:'Copy code'}).first()).toBeVisible();
 await page.locator('article pre').first().scrollIntoViewIfNeeded();await page.screenshot({path:wikiShotPath(testInfo,'guide-command-copy.png')});await expectAxeClean(page);
 await page.goto('/settings');await expect(page.getByRole('navigation',{name:'Settings sections'})).toBeVisible();await page.screenshot({path:wikiShotPath(testInfo,'settings-sections.png')});
});
