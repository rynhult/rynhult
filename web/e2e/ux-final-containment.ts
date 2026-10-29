import { expect, test } from '@playwright/test';
import {AUTH_FILE,AXE_PAGES} from './helpers';
export function containmentTests(widths: readonly number[] = [320,390]) {
test.use({storageState:AUTH_FILE});
for(const theme of ['dark','light'] as const) test(`all pages bound root and main to requested widths in ${theme}`,async({page})=>{
 test.setTimeout(120000);await page.emulateMedia({colorScheme:theme});
 for(const width of widths){
  await page.setViewportSize({width,height:844});
  for(const path of [...AXE_PAGES,'/groups','/guide/User-Guide/Install']){
   await page.goto(path);await page.waitForLoadState('networkidle');
   const size=await page.evaluate(()=>({root:document.documentElement.scrollWidth,main:document.querySelector('main')?.scrollWidth??0,client:document.querySelector('main')?.clientWidth??0}));
   expect(size.root,`${path} root at ${width}`).toBeLessThanOrEqual(width);
   expect(size.main,`${path} main at ${width}`).toBeLessThanOrEqual(size.client);
  }
 }
});

}
