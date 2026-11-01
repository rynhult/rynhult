import {writeFileSync} from 'node:fs';
import {expect,test,type Page} from '@playwright/test';
import {AUTH_FILE,control,info as serverInfo,csrfHeaders,sessionWithChanges,wikiShotPath,expectAxeClean} from './helpers';
test.use({storageState:AUTH_FILE});
function terminalRows(page:Page,id:string) {
  let rows=0,tail='';
  page.on('websocket', socket=>{
    if (!socket.url().includes(`/sessions/${id}/tty?`)) return;
    socket.on('framereceived',event=>{
      if(typeof event.payload==='string')return;
      tail+=event.payload.toString('utf8');
      // Tmux's scroll region reports the height applied by its attached client.
      for(const match of tail.matchAll(/\x1b\[1;(\d+)r/g))rows=Number(match[1]);
      tail=tail.slice(-32);
    });
  });
  return ()=>rows;
}
for (const view of ['session','grid']) test(`${view} terminal expands without reconnecting and remembers this device`,async({page},info)=>{
  const id=await sessionWithChanges(page,'termfull');
  try {
    const frames:{t:string;rows?:number;cols?:number;d?:string}[]=[];
    let sockets=0;
    page.on('websocket',socket=>{if(socket.url().includes(`/sessions/${id}/tty`)){sockets++;socket.on('framesent',event=>{
      try {frames.push(JSON.parse(String(event.payload)));} catch { /* binary output */ }
    });}});
    const url=view==='session'?`/sessions/${id}`:`/grid?ids=${id}`;
    await page.goto(url);
    const input=page.locator('.xterm-helper-textarea');
    await expect(input).toBeAttached();
    await expect.poll(()=>frames.filter(f=>f.t==='r').length).toBeGreaterThan(0);
    const before=frames.filter(f=>f.t==='r').at(-1)!;
    await expect(page.getByRole('button',{name:'Expand terminal',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Expand terminal',exact:true}).click();
    const expanded=page.getByRole('dialog',{name:'Expanded terminal'});
    await expect(expanded).toBeVisible();
    const collapse=expanded.getByRole('button',{name:'Collapse terminal',exact:true});
    await expect(collapse).toHaveAttribute('aria-pressed','true');
    await expect(collapse).toBeFocused();
    await expect.poll(()=>frames.filter(f=>f.t==='r').at(-1)?.rows).toBeGreaterThan(before.rows!);
    expect(sockets).toBe(1);
    const viewport=page.viewportSize()!;
    const box=await expanded.boundingBox();
    expect(box?.x).toBe(0);expect(box?.y).toBe(0);expect(box?.width).toBe(viewport.width);expect(box?.height).toBe(viewport.height);
    const rows=frames.filter(f=>f.t==='r').at(-1)!.rows!;
    await page.setViewportSize({...viewport,height:viewport.height+100});
    await expect.poll(()=>frames.filter(f=>f.t==='r').at(-1)?.rows).toBeGreaterThan(rows);
    await page.setViewportSize(viewport);
    if(await page.evaluate(()=>document.fullscreenEnabled)){
      await expanded.getByRole('button',{name:'Full screen',exact:true}).click();
      await expect.poll(()=>page.evaluate(()=>!!document.fullscreenElement)).toBe(true);
      await page.evaluate(()=>document.exitFullscreen());
      await expect(expanded).toBeVisible();
    }
    await expect(page.getByRole('navigation',{name:'Main',exact:true})).toBeHidden();
    await input.focus();await page.keyboard.press('c');await page.keyboard.press('Escape');
    await expect(expanded).toBeVisible();
    await page.keyboard.press('F6');await expect(collapse).toBeFocused();
    expect(frames.filter(f=>f.t==='i').map(f=>f.d).join('')).not.toContain('\u001b[17');
    if(info.project.name.endsWith('phone')) {
      await expect(expanded.getByRole('toolbar',{name:'Terminal keys'})).toBeVisible();
      expect((await expanded.locator('span[title]').first().boundingBox())!.width).toBeGreaterThan(120);
    }
    const reader=expanded.getByRole('checkbox',{name:'Screen reader output'});
    await expect(reader).toBeVisible();
    await reader.uncheck();await expect(expanded.locator('.xterm-accessibility-tree')).toHaveCount(0);
    await reader.check();await expect(expanded.locator('.xterm-accessibility-tree')).toHaveCount(1);
    expect(sockets).toBe(1);
    await expectAxeClean(page);
    await page.screenshot({path:wikiShotPath(info,`terminal-expanded-${view}-${info.project.name.endsWith('phone')?'phone':'desktop'}.png`)});
    const preference=view==='grid'?'grid':'session';
    expect(await page.evaluate(key=>localStorage.getItem(key),`acc-terminal-expanded:${preference}`)).toBe(id);
    expect(await page.evaluate(key=>localStorage.getItem(key),`acc-terminal-expanded:${preference==='grid'?'session':'grid'}`)).toBeNull();
    await page.emulateMedia({colorScheme:'light',reducedMotion:'reduce'});await expectAxeClean(page);
    await page.reload();await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toBeVisible();
    const activeInput=page.locator('.xterm-helper-textarea');
    await activeInput.focus();await page.keyboard.press('F6');
    const beforeCollapse=frames.length;
    await page.keyboard.press('c');
    await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toHaveCount(0);
    await expect(page.getByRole('button',{name:'Expand terminal',exact:true})).toBeFocused();
    await expect.poll(()=>frames.slice(beforeCollapse).filter(f=>f.t==='r').length).toBeGreaterThan(0);
    expect(frames.slice(beforeCollapse).filter(f=>f.t==='i'&&(f.d==='c'||f.d==='\x1b[17~'))).toHaveLength(0);
    await page.reload();await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toHaveCount(0);
  } finally {if (!page.isClosed()) await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});

test('the selected expanded grid tile survives reload among multiple sessions',async({page},info)=>{
  const first=await sessionWithChanges(page,'first-grid');
  const second=await sessionWithChanges(page,'second-grid');
  try {
    const session=await (await page.request.get(`/api/sessions/${second}`)).json();
    await page.goto(`/grid?ids=${first},${second}`);
    if(info.project.name.endsWith('phone'))await page.getByRole('tab',{name:session.name,exact:true}).click();
    const tile=page.getByRole('region',{name:session.name,exact:true});
    await tile.getByRole('button',{name:'Expand terminal',exact:true}).click();
    await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toContainText(session.name);
    await page.reload();
    await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toContainText(session.name);
    await page.getByRole('button',{name:'Collapse terminal',exact:true}).click();
    await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toHaveCount(0);
    await expect(page.getByRole('button',{name:'Choose sessions'})).toBeVisible();
  } finally {
    if(!page.isClosed())for(const id of [first,second])await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});
  }
});

test('expanded grid keeps the same editor across a phone breakpoint',async({page})=>{
  const id=await sessionWithChanges(page,'grid-resize');
  try {
    let sockets=0;
    page.on('websocket',socket=>{if(socket.url().includes(`/sessions/${id}/tty`))sockets++;});
    await page.goto(`/grid?ids=${id}`);
    await page.getByRole('button',{name:'Expand terminal',exact:true}).click();
    const editor=await page.locator('.xterm-helper-textarea').elementHandle();
    await page.setViewportSize({width:page.viewportSize()!.width<768?1280:390,height:844});
    await expect(page.getByRole('dialog',{name:'Expanded terminal'})).toBeVisible();
    expect(await editor!.evaluate(element=>element.isConnected)).toBe(true);
    expect(sockets).toBe(1);
    await expect(page.getByRole('tab')).toHaveCount(0); // newly inserted phone navigation stays inert too
    await page.getByRole('button',{name:'Collapse terminal',exact:true}).click();
    await expect(page.getByRole('button',{name:'Expand terminal',exact:true})).toBeFocused();
    expect(await editor!.evaluate(element=>element.isConnected)).toBe(true);
    expect(sockets).toBe(1);
  } finally {if(!page.isClosed())await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});


test('selection mode copies plain wrapped text with shortcuts and denied clipboard fallback',async({page,context},info)=>{
  await context.grantPermissions(['clipboard-read','clipboard-write']);
  const id=await sessionWithChanges(page,'term-select');
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write',{exact:true})).toBeVisible();
    // The write lease precedes tmux's first repaint; injecting before it can lose the marker during reflow.
    await expect(page.locator('.xterm-accessibility-tree')).toContainText('fake-claude ready');
    const long='Wrapped selection '.repeat(20).trimEnd();
    writeFileSync(`${serverInfo().run_dir}/${id}/fake.screen`, `\x1b[2J\x1b[HCopy marker   \r\n${long}\r\n\x1b[?1000h`);
    control(id,'screen');
    await expect(page.locator('.xterm-accessibility-tree')).toContainText('Copy marker');
    await page.getByRole('button',{name:'Select text',exact:true}).click();
    const selectable=page.getByRole('region',{name:'Selectable terminal text'});
    await expect(selectable).toContainText(long);
    if(info.project.name.endsWith('phone')) {
      const bounds=(await selectable.boundingBox())!;
      const cdp=await context.newCDPSession(page);
      await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:bounds.x+28,y:bounds.y+12}]});
      // Gesture duration, not a readiness wait: Chromium must receive a real long press.
      await new Promise(resolve=>setTimeout(resolve,800));
      await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
      await expect.poll(()=>page.evaluate(()=>window.getSelection()?.toString())).toBeTruthy();
      await expect(page.getByRole('button',{name:'Copy selection',exact:true})).toBeVisible();
      await cdp.detach();
    }
    const selectAll = () => selectable.evaluate(el=>{
      const range=document.createRange();range.selectNodeContents(el);
      const selection=window.getSelection()!;selection.removeAllRanges();selection.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
    });
    await selectAll();
    const copy=page.getByRole('button',{name:'Copy selection',exact:true});
    await expect(copy).toBeVisible();
    await copy.click();
    const copied=await page.evaluate(()=>navigator.clipboard.readText());
    expect(copied).toContain(`Copy marker\n${long}`);
    expect(copied).not.toMatch(/ +\n/);
    await selectable.focus();await page.keyboard.press('Control+Shift+C');
    expect(await page.evaluate(()=>navigator.clipboard.readText())).toBe(copied);
    await page.evaluate(()=>window.getSelection()?.removeAllRanges());
    await expect(copy).toBeHidden();
    await selectAll();
    await page.evaluate(()=>Object.defineProperty(navigator.clipboard,'writeText',{configurable:true,value:()=>Promise.reject(new Error('denied'))}));
    await copy.click();
    const fallback=page.getByRole('dialog',{name:'Recent terminal output'}).getByRole('textbox');
    await expect(fallback).toHaveValue(copied);
    await expect.poll(()=>fallback.evaluate((el:HTMLTextAreaElement)=>el.selectionEnd-el.selectionStart)).toBe(copied.length);
  } finally {if(!page.isClosed())await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});

test('Shift drag selects through application mouse mode and interrupt stays with the program',async({page,context},info)=>{
  test.skip(info.project.name.endsWith('phone'),'Desktop modifier-drag; phone uses the native Select text view.');
  await context.grantPermissions(['clipboard-read','clipboard-write']);
  const id=await sessionWithChanges(page,'mouse-selection');
  try {
    const paintedRows=terminalRows(page,id);
    const inputs:string[]=[];
    page.on('websocket',socket=>socket.on('framesent',event=>{try{const f=JSON.parse(String(event.payload));if(f.t==='i')inputs.push(f.d);}catch{/* binary */}}));
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write',{exact:true})).toBeVisible();
    await expect(page.locator('.xterm-accessibility-tree')).toContainText('fake-claude ready');
    const screenRows=page.locator('.xterm-accessibility-tree > div');
    const beforeRows=await screenRows.count();
    await page.getByRole('button',{name:'Expand terminal',exact:true}).click();
    await expect.poll(()=>screenRows.count()).toBeGreaterThan(beforeRows);
    // Tmux resets mouse tracking while repainting a resize, which clears an in-progress selection.
    await expect.poll(paintedRows).toBe(await screenRows.count());
    writeFileSync(`${serverInfo().run_dir}/${id}/fake.screen`,'\x1b[2J\x1b[HMouse selection marker\r\n\x1b[?1000h');control(id,'screen');
    await expect(page.locator('.xterm-accessibility-tree')).toContainText('Mouse selection marker');
    const box=(await page.locator('.xterm-screen').boundingBox())!;
    const rowHeight=await page.locator('.xterm-accessibility-tree > div').first().evaluate(el=>el.getBoundingClientRect().height);
    await page.keyboard.down('Shift');await page.mouse.move(box.x+1,box.y+rowHeight/2);await page.mouse.down();
    await page.mouse.move(box.x+180,box.y+rowHeight/2,{steps:12});await page.mouse.up();await page.keyboard.up('Shift');
    const copy=page.getByRole('button',{name:'Copy selection',exact:true});await expect(copy).toBeVisible();
    expect(inputs.some(data=>/^\x1b\[(?:M|<)/.test(data))).toBe(false);
    await copy.click();expect(await page.evaluate(()=>navigator.clipboard.readText())).toContain('Mouse selection');
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.press('Control+Shift+C');
    expect(await page.evaluate(()=>navigator.clipboard.readText())).toContain('Mouse selection');
    await page.keyboard.press('Meta+C');
    expect(await page.evaluate(()=>navigator.clipboard.readText())).toContain('Mouse selection');
    await expect(page.getByRole('button',{name:'Copy selected text',exact:true})).toBeVisible();
    await page.keyboard.press('Control+C');await expect.poll(()=>inputs.includes('\x03')).toBe(true);
  } finally {if(!page.isClosed())await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});

test('expanding and collapsing keeps the scrolled reading position',async({page})=>{
  const id=await sessionWithChanges(page,'terminal-scroll');
  try {
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write',{exact:true})).toBeVisible();
    writeFileSync(`${serverInfo().run_dir}/${id}/fake.screen`, '\x1b[2J\x1b[H'+Array.from({length:300},(_,i)=>`Reading marker ${i}`).join('\r\n')+'\r\n');
    control(id,'screen');
    const top=page.locator('.xterm-accessibility-tree > div').first();
    await expect(page.locator('.xterm-accessibility-tree')).toContainText('Reading marker 299');
    await page.locator('.xterm-viewport').evaluate(el=>{el.scrollTop=150;el.dispatchEvent(new Event('scroll'));});
    await expect(top).toHaveText(/Reading marker/);
    const before=await top.innerText();
    await page.getByRole('button',{name:'Expand terminal',exact:true}).click();
    await expect(top).toHaveText(before);
    await page.getByRole('button',{name:'Collapse terminal',exact:true}).click();
    await expect(top).toHaveText(before);
  } finally {if(!page.isClosed())await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});

test('expanded terminal allows a global dialog above it and restores focus',async({page})=>{
  const id=await sessionWithChanges(page,'expanded-palette');
  try {
    await page.goto(`/sessions/${id}`);
    await page.getByRole('button',{name:'Expand terminal',exact:true}).click();
    const collapse=page.getByRole('button',{name:'Collapse terminal',exact:true});
    await expect(collapse).toBeFocused();
    await page.keyboard.press('Control+k');
    const palette=page.getByRole('dialog',{name:'Jump to…',exact:true});
    await expect(palette).toBeVisible();
    await expect(palette.getByRole('combobox',{name:'Jump to…',exact:true})).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(palette).toHaveCount(0);
    await expect(collapse).toBeFocused();
    await expect(page.getByRole('dialog',{name:'Expanded terminal',exact:true})).toBeVisible();
    await expectAxeClean(page);
  } finally {if(!page.isClosed())await page.request.post(`/api/sessions/${id}/stop`,{headers:await csrfHeaders(page)}).catch(()=>{});}
});
