import { expect, test, type Page } from './fixtures';
import {AUTH_FILE,AXE_PAGES} from './helpers';
/** Include geometry and selectors so accumulated-state failures identify their cause. */
export async function containmentSize(page: Page) {
 return page.evaluate(() => {
  const main = document.querySelector('main')!;
  const selector = (element: Element): string => {
   if (element.id) return `#${CSS.escape(element.id)}`;
   const part = element.tagName.toLowerCase() + [...element.classList].map(name => `.${CSS.escape(name)}`).join('');
   return element.parentElement ? `${selector(element.parentElement)} > ${part}:nth-child(${[...element.parentElement.children].indexOf(element) + 1})` : part;
  };
  const elements = [...main.querySelectorAll('*')].map(element => {
   const rect = element.getBoundingClientRect();
   return { selector: selector(element), width: rect.width, right: rect.right, text: element.textContent?.slice(0, 120) };
  }).filter(element => element.width > 0);
  return { root: document.documentElement.scrollWidth, main: main.scrollWidth, client: main.clientWidth,
   widest: elements.sort((a, b) => b.width - a.width)[0],
   overflow: elements.filter(element => element.right > main.getBoundingClientRect().right).slice(0, 12) };
 });
}
export const containmentMessage = (path: string, width: number, size: Awaited<ReturnType<typeof containmentSize>>) =>
 `${path} at ${width}px: ${JSON.stringify(size)}`;
export function containmentTests(widths: readonly number[] = [320,390]) {
test.use({storageState:AUTH_FILE});
for(const theme of ['dark','light'] as const) test(`all pages bound root and main to requested widths in ${theme}`,async({page})=>{
 test.setTimeout(120000);await page.emulateMedia({colorScheme:theme});
 for(const width of widths){
  await page.setViewportSize({width,height:844});
  for(const path of [...AXE_PAGES,'/groups','/guide/User-Guide/Install']){
   await page.goto(path);await page.waitForLoadState('networkidle');
   const size=await containmentSize(page);
   const message=containmentMessage(path,width,size);
   expect(size.root,message).toBeLessThanOrEqual(width);
   expect(size.main,message).toBeLessThanOrEqual(size.client);
  }
 }
});

}
