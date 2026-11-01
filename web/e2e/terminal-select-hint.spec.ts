import {expect,test} from '@playwright/test';
import {writeFileSync} from 'node:fs';
import {AUTH_FILE,control,csrfHeaders,info,sessionWithChanges} from './helpers';
test.use({storageState:AUTH_FILE});
for(const platform of [
  {name:'Mac fallback',legacy:'MacIntel',modern:null,modifier:'Alt',hint:'Hold ⌥ Option while dragging to select text',copy:'⌘C'},
  {name:'other desktop',legacy:'Linux x86_64',modern:null,modifier:'Shift',hint:'Hold Shift while dragging to select text',copy:'Ctrl+Shift+C'},
  {name:'Mac client hints take precedence',legacy:'Linux x86_64',modern:'macOS',modifier:'Alt',hint:'Hold ⌥ Option while dragging to select text',copy:'⌘C'},
]) test(`mouse-mode selection hint: ${platform.name}`,async({page})=>{
  await page.addInitScript(({legacy,modern})=>{
    Object.defineProperty(navigator,'platform',{configurable:true,value:legacy});
    Object.defineProperty(navigator,'userAgentData',{configurable:true,value:modern?{platform:modern}:undefined});
  },platform);
  const id=await sessionWithChanges(page,'selection-hint');
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write',{exact:true})).toBeVisible();
    const tree=page.locator('.xterm-accessibility-tree');
    await expect(tree).toContainText('fake-claude ready');
    const hint=page.getByRole('group',{name:'Terminal selection hint',exact:true});
    const screen=page.locator('.xterm-screen');
    const drag=async()=>{
      const box=(await screen.boundingBox())!;
      await page.mouse.move(box.x+15,box.y+8);await page.mouse.down();
      await page.mouse.move(box.x+150,box.y+8,{steps:8});await page.mouse.up();
    };
    await drag();await expect(hint).toHaveCount(0);
    // Print the readiness marker after mouse-mode activation: seeing text
    // before its trailing control sequence was not proof the mode was parsed.
    const paint=(suffix='')=>{writeFileSync(`${info().run_dir}/${id}/fake.screen`,'\x1b[2J\x1b[H\x1b[?1000hSelection hint marker'+suffix+'\r\n');control(id,'screen');};
    paint();await expect(tree).toContainText('Selection hint marker');
    await page.keyboard.down(platform.modifier);await drag();await page.keyboard.up(platform.modifier);
    await expect(hint).toHaveCount(0);
    const box=(await screen.boundingBox())!;
    await page.mouse.click(box.x+15,box.y+8);await expect(hint).toHaveCount(0);
    await page.locator('.xterm-helper-textarea').focus();
    const before=await page.locator('.xterm').boundingBox();
    await drag();await expect(hint).toContainText(platform.hint);
    await hint.screenshot({path:`../.acc/selection-hint-${platform.modern?'client-hints':platform.legacy.includes('Mac')?'mac':'other'}.png`});
    await expect(page.getByLabel('Announcements',{exact:true})).toContainText(platform.hint);
    await expect(page.locator('.xterm-helper-textarea')).toBeFocused();
    expect(await page.locator('.xterm').boundingBox()).toEqual(before);
    expect(await page.getByRole('button',{name:'Select text',exact:true}).first().getAttribute('title')).toContain(platform.copy);
    await hint.getByRole('button',{name:'Select text',exact:true}).click();
    const selectable=page.getByRole('region',{name:'Selectable terminal text',exact:true});
    await expect(selectable).toContainText('Selection hint marker');await expect(hint).toHaveCount(0);
    await selectable.evaluate(el=>{const range=document.createRange();range.selectNodeContents(el);const s=window.getSelection()!;s.removeAllRanges();s.addRange(range);document.dispatchEvent(new Event('selectionchange'));});
    await expect(page.getByRole('button',{name:'Copy selection',exact:true})).toHaveAttribute('title',`Copy selected text (${platform.copy})`);
    await page.getByRole('button',{name:'Select text',exact:true}).click();
    await drag();await expect(hint).toHaveCount(0);
    await page.reload();await expect(page.getByText('Read-write',{exact:true})).toBeVisible();
    paint(' again');await expect(tree).toContainText('Selection hint marker again');
    if(platform.modern)await page.clock.install();
    await drag();await expect(hint).toBeVisible();
    if(platform.modern)await page.clock.fastForward(10001);
    else {
      await hint.getByRole('button',{name:'Dismiss selection hint',exact:true}).click();
      await expect(page.locator('.xterm-helper-textarea')).toBeFocused();
    }
    await drag();await expect(hint).toHaveCount(0);
  } finally {await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)});}
});
