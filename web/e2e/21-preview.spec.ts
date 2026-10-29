import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFileSync, mkdirSync } from "node:fs";
import { AUTH_FILE, expectAxeClean, info, wikiShotPath } from "./helpers";

test("Preview embeds an isolated demo app on project and session pages, including phones", async ({ page, context }, testInfo) => {
  test.setTimeout(90_000);
  mkdirSync("e2e/screenshots", {recursive:true});
  const state = JSON.parse(readFileSync(AUTH_FILE, "utf8"));
  const port = new URL(info().origin).port;
  const origin = `http://acc.preview.localhost:${port}`;
  await context.addCookies(state.cookies.map((c: Record<string, unknown>) => ({ ...c, domain: "acc.preview.localhost" })));
  await page.goto(origin);
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  const setup = await page.evaluate(async ({ workspace, slug }) => {
    const csrf = document.cookie.split("; ").find(c => c.startsWith("acc_csrf="))!.split("=")[1];
    const headers = { "Content-Type": "application/json", "X-ACC-CSRF": csrf };
    const created = await fetch('/api/projects', {method:'POST', headers, body:JSON.stringify({slug, name:'Preview demo', workspace_path:workspace})});
    if (!created.ok) throw new Error(await created.text());
    const project = await created.json();
    const trusted = await fetch(`/api/projects/${project.id}/trust`, {method:'POST', headers});
    if (!trusted.ok) throw new Error(await trusted.text());
    const demoPort = await fetch('/preview-test-port').then(r => r.json());
    const reserve = await fetch(`/api/projects/${project.id}/ports`, {method:'POST', headers, body:JSON.stringify({port:demoPort.port})});
    if (!reserve.ok) throw new Error(await reserve.text());
    try {
      const launched = await fetch('/api/sessions', {method:'POST', headers, body:JSON.stringify({project_id:project.id, provider:'claude', name:'Preview session'})});
      if (!launched.ok) throw new Error(await launched.text());
      const session = await launched.json();
      return { pid: project.id, sid: session.id, port: demoPort.port };
    } catch (error) {
      const released = await fetch(`/api/ports/${demoPort.port}/release`, {method:'POST', headers});
      if (!released.ok) throw new Error(await released.text(), { cause: error });
      throw error;
    }
  }, { workspace: info().workspace, slug: `preview-${randomUUID().slice(0, 8)}` });
  let released = false;
  try {
    expect(setup.sid).toBeTruthy();
    await page.goto(`${origin}/projects/${setup.pid}`);
    await expect(page.getByRole('tab', {name:'Overview', exact:true})).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole("heading", { name: "Preview demo", level: 1 })).toBeVisible();
    await page.screenshot({path:'e2e/screenshots/preview-overview.png', fullPage:true});
    await page.goto(`${origin}/projects/${setup.pid}?tab=preview`);
    await expect(page.getByRole('tab', {name:'Preview', exact:true})).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByLabel('Preview port')).toHaveValue(String(setup.port));
    const demo = page.frameLocator('iframe[title="Project preview"]').frameLocator('iframe[title="Preview app"]');
    await expect(page.getByRole('button', {name:'Pick element', exact:true})).toBeVisible();
    await expect(demo.getByRole('heading', {name:'Demo preview'})).toBeVisible();
    const frame = page.frames().find(f => f.url().includes('/__acc_preview/') || f.url().includes(`preview-${setup.pid}-`))!;
    expect(new URL(frame.url()).hostname).not.toBe(new URL(origin).hostname);
    expect(await page.evaluate(() => document.cookie)).toContain('acc_csrf=');
    expect(await frame.evaluate(() => document.cookie)).not.toContain('acc_csrf');
    expect(await frame.evaluate(() => document.cookie)).not.toContain('acc_session');
    expect(await frame.evaluate(() => { try { return parent.document.title; } catch { return 'isolated'; } })).toBe('isolated');
    expect(await frame.evaluate(async (acc) => {try {await fetch(`${acc}/api/projects`, {credentials:'include'}); return 'readable';} catch {return 'blocked';}}, origin)).toBe('blocked');
    // The person must arm the picker and confirm the exact sanitized message.
    let prepared = 0, sent = 0;
    page.on('request', request => {
      if (request.method() !== 'POST') return;
      if (new URL(request.url()).pathname === `/api/sessions/${setup.sid}/preview-element`) prepared++;
      if (new URL(request.url()).pathname.startsWith(`/api/sessions/${setup.sid}/preview-element/`)) sent++;
    });
    await page.getByLabel('Send to session').selectOption(setup.sid);
    await expect(page.getByRole('button', {name:'Pick element', exact:true})).toBeEnabled();
    await frame.evaluate(() => {
      window.addEventListener('message', event => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'pick') (window as unknown as {pickId: string}).pickId = data.id;
        } catch { /* unrelated page messages */ }
      });
    });
    await page.getByRole('button', {name:'Pick element', exact:true}).click();
    await expect(page.getByRole('button', {name:'Cancel picking'})).toBeVisible();
    await expect.poll(() => frame.evaluate(() => (window as unknown as {pickId: string}).pickId)).toBeTruthy();
    const nonce = await frame.evaluate(() => (window as unknown as {pickId: string}).pickId);
    const forged = {v:1, type:'selected', id:nonce, element:{selector:'h1:nth-of-type(1)', text:'Forged', box:{x:0,y:0,width:1,height:1}}};
    // Same-origin dashboard messages, a sibling child frame and stale/oversized replies must not open a draft.
    await page.evaluate(data => window.postMessage(JSON.stringify(data), location.origin), forged);
    const child = frame.childFrames()[0];
    expect(await child.evaluate(() => {try {return top!.document.title;} catch {return 'isolated';}})).toBe('isolated');
    await child.evaluate(({data, acc}) => top!.postMessage(JSON.stringify(data), acc), {data:forged, acc:origin});
    await frame.evaluate(({data, acc}) => {
      parent.postMessage(JSON.stringify({...data, id:'stale-pick'}), acc);
      parent.postMessage(JSON.stringify({...data, element:{...data.element, text:'x'.repeat(5000)}}), acc);
    }, {data:forged, acc:origin});
    await demo.getByRole('heading', {name:'Demo preview'}).click();
    const confirmation = page.getByRole('dialog', {name:'Send element to agent?'});
    await expect(confirmation).toBeVisible();
    // A second click or Enter meant for the preview (or timed by it) can't land on Send: Cancel has focus, and Send
    // stays disabled for a moment after the dialog opens.
    await expect(confirmation.getByRole('button', {name:'Cancel', exact:true})).toBeFocused();
    await expect(confirmation.getByRole('button', {name:'Send to agent', exact:true})).toBeDisabled();
    await expect(confirmation.getByRole('button', {name:'Send to agent', exact:true})).toBeEnabled();
    await expect(confirmation.getByLabel('Element message')).toContainText('untrusted-preview-element');
    await expect(confirmation.getByLabel('Element message')).toContainText('Demo preview');
    await frame.evaluate(({data, acc}) => parent.postMessage(JSON.stringify(data), acc), {data:forged, acc:origin});
    await expect(confirmation.getByLabel('Element message')).not.toContainText('Forged');
    expect(prepared).toBe(1);
    expect(sent).toBe(0);
    await expectAxeClean(page, '[role=dialog]');
    await page.screenshot({path:wikiShotPath(testInfo, 'preview-pick-element.png'), fullPage:true});
    await confirmation.getByRole('button', {name:'Cancel', exact:true}).click();
    await expect(confirmation).not.toBeVisible();
    expect(sent).toBe(0);
    await expect(page.getByRole('button', {name:'Pick element', exact:true})).toBeFocused();

    await child.evaluate(() => {
      const p = document.querySelector('p')!;
      p.textContent = 'Inspect <img src=x onerror="alert(1)"> token=example-secret-value';
      p.tabIndex = 0;
      const input = document.createElement('input'); input.type='password'; input.value='never-send-this-value'; p.append(input);
      const editable = document.createElement('span'); editable.contentEditable='true'; editable.textContent='never-send-editable'; p.append(editable);
    });
    await page.getByRole('button', {name:'Pick element', exact:true}).click();
    // Arming crosses origins by postMessage; until it lands, Enter on the paragraph does nothing, so retry.
    await expect(async () => {
      await demo.locator('p').focus();
      await demo.locator('p').press('Enter');
      await expect(confirmation).toBeVisible({timeout:1000});
    }).toPass({timeout:10_000});
    const approved = await confirmation.getByLabel('Element message').innerText();
    expect(approved).toContain('<img');
    expect(approved).not.toContain('example-secret-value');
    expect(approved).not.toContain('never-send-this-value');
    expect(approved).not.toContain('never-send-editable');
    expect(await confirmation.locator('img').count()).toBe(0);
    await confirmation.getByRole('button', {name:'Send to agent', exact:true}).dblclick();
    await expect(confirmation).not.toBeVisible();
    await expect.poll(async () => page.evaluate(async sid => {
      const events = await fetch(`/api/sessions/${sid}/events`).then(r => r.json());
      return events.filter((event: {kind:string; payload:{prompt?:string}}) => event.kind === 'UserPromptSubmit').map((event: {payload:{prompt?:string}}) => event.payload.prompt);
    }, setup.sid)).toContain(approved);
    expect(sent).toBe(1);
    expect(prepared).toBe(2);
    await child.evaluate(() => {
      const section = document.createElement('section'); section.id = 'bounded-probe'; section.style.minHeight = '48px';
      for (let i = 0; i < 300; i++) { const hidden = document.createElement('span'); hidden.hidden = true; hidden.textContent = 'Hidden'; section.append(hidden); }
      section.append(document.createTextNode('beyond-budget')); document.body.append(section);
    });
    await page.getByRole('button', {name:'Pick element', exact:true}).click();
    await demo.locator('#bounded-probe').click({position:{x:2,y:2}});
    await expect(confirmation).toBeVisible();
    await expect(confirmation.getByLabel('Element message')).not.toContainText('beyond-budget');
    await confirmation.getByRole('button', {name:'Cancel', exact:true}).click();

    await page.getByRole('button', {name:'Reload preview', exact:true}).click();
    await expect(demo.getByRole('heading', {name:'Demo preview'})).toBeVisible();
    const popupPromise = page.waitForEvent('popup');
    await page.getByRole('link', {name:'Open in new tab'}).click();
    const popup = await popupPromise;
    await expect(popup.getByRole('heading', {name:'Demo preview'})).toBeVisible();
    await popup.close();
    await page.screenshot({path:wikiShotPath(testInfo, 'preview-project.png'), fullPage:true});
    await page.setViewportSize({width:390, height:844});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path:wikiShotPath(testInfo, 'preview-phone.png'), fullPage:true});
    if (setup.sid) {
      await page.setViewportSize({width:1440, height:900});
      await page.goto(`${origin}/sessions/${setup.sid}`);
      await expect(page.getByRole('tab', {name:'Terminal', exact:true})).toHaveAttribute('aria-selected', 'true');
      await expect(page.locator('.xterm')).toBeVisible();
      await page.screenshot({path:'e2e/screenshots/preview-terminal.png', fullPage:true});
      await page.goto(`${origin}/sessions/${setup.sid}?tab=preview`);
      await expect(demo.getByRole('heading', {name:'Demo preview'})).toBeVisible();
      await expect(page.getByLabel('Send to session')).toHaveCount(0);
      // A real touch context uses the same confirmation boundary as the desktop.
      await page.setViewportSize({width:390, height:844});
      await page.getByRole('button', {name:'Pick element', exact:true}).click();
      await demo.getByRole('heading', {name:'Demo preview'}).click();
      await expect(confirmation).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const touch = await context.browser()!.newContext({hasTouch:true, isMobile:true, viewport:{width:390,height:844},
        colorScheme:'dark', storageState:await context.storageState()});
      try {
        const phone = await touch.newPage();
        await phone.goto(`${origin}/sessions/${setup.sid}?tab=preview`);
        await phone.getByRole('button', {name:'Pick element', exact:true}).click();
        await phone.frameLocator('iframe[title="Project preview"]').frameLocator('iframe[title="Preview app"]').getByRole('heading', {name:'Demo preview'}).tap();
        const review = phone.getByRole('dialog', {name:'Send element to agent?'});
        await expect(review).toBeVisible();
        expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await expectAxeClean(phone, '[role=dialog]');
        await phone.screenshot({path:wikiShotPath(testInfo, 'preview-pick-phone.png'), fullPage:true});
        await review.getByRole('button', {name:'Cancel', exact:true}).click();
      } finally { await touch.close(); }
      await confirmation.getByRole('button', {name:'Cancel', exact:true}).click();
      await page.getByRole('button', {name:'Pick element', exact:true}).click();
      await demo.locator('body').press('Escape');
      await expect(page.getByRole('button', {name:'Cancel picking'})).toHaveCount(0);
      await page.getByRole('button', {name:'Pick element', exact:true}).click();
      await page.getByRole('button', {name:'Reload preview', exact:true}).click();
      await expect(demo.getByRole('heading', {name:'Demo preview'})).toBeVisible();
      await expect(page.getByRole('button', {name:'Cancel picking'})).toHaveCount(0);
      expect(sent).toBe(1);
      await page.setViewportSize({width:1440, height:900});
      await page.screenshot({path:wikiShotPath(testInfo, 'preview-session.png'), fullPage:true});
    }
    await page.getByRole('button', {name:'Pick element', exact:true}).click();
    await demo.getByRole('heading', {name:'Demo preview'}).click();
    await expect(confirmation).toBeVisible();
    await page.evaluate(async (port) => {
      const csrf = document.cookie.split('; ').find(c => c.startsWith('acc_csrf='))!.split('=')[1];
      const response = await fetch(`/api/ports/${port}/release`, {method:'POST', headers:{'X-ACC-CSRF':csrf}});
      if (!response.ok) throw new Error(await response.text());
    }, setup.port);
    released = true;
    await expect(page.getByText('Nothing is listening on a reserved port.')).toBeVisible({timeout:10_000});
    await confirmation.getByRole('button', {name:'Send to agent', exact:true}).click();
    await expect(confirmation).not.toBeVisible();
    expect(sent).toBe(1); // port change invalidates even an already displayed confirmation
    await page.goto(`${origin}/projects/${setup.pid}?tab=preview`);
    await expect(page.getByText('Nothing is listening on a reserved port.')).toBeVisible();
  } finally {
    await page.evaluate(async ({ port, sid, pid, released }) => {
      const csrf = document.cookie.split('; ').find(c => c.startsWith('acc_csrf='))!.split('=')[1];
      const headers = { 'X-ACC-CSRF': csrf };
      if (!released) {
        const response = await fetch('/api/ports');
        if (!response.ok) throw new Error(await response.text());
        const ports = await response.json();
        if (ports.some((reservation: { port: number; project_id: number }) => reservation.port === port && reservation.project_id === pid)) {
          const released = await fetch(`/api/ports/${port}/release`, { method: 'POST', headers });
          if (!released.ok) throw new Error(await released.text());
        }
      }
      const stopped = await fetch(`/api/sessions/${sid}/stop`, { method: 'POST', headers });
      if (!stopped.ok) throw new Error(await stopped.text());
    }, { ...setup, released });
  }
});

test("Preview reaches the demo machine over its link", async ({ page, context }, testInfo) => {
  const state = JSON.parse(readFileSync(AUTH_FILE, "utf8"));
  const origin = `http://acc.preview.localhost:${new URL(info().origin).port}`;
  await context.addCookies(state.cookies.map((c: Record<string, unknown>) => ({ ...c, domain: "acc.preview.localhost" })));
  await page.goto(origin);
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  const setup = await page.evaluate(async ({ workspace, slug }) => {
    const csrf = document.cookie.split('; ').find(c => c.startsWith('acc_csrf='))!.split('=')[1];
    const headers = {'Content-Type':'application/json', 'X-ACC-CSRF':csrf};
    const result = await fetch('/api/projects', {method:'POST', headers,
      body:JSON.stringify({slug,name:'Remote preview demo',workspace_path:workspace,host:'lab-box'})});
    if (!result.ok) throw Error(await result.text());
    const project = await result.json();
    const demoPort = await fetch('/preview-test-port').then(r => r.json());
    const reservation = await fetch(`/api/projects/${project.id}/ports`, {method:'POST',headers,body:JSON.stringify({port:demoPort.port})});
    if (!reservation.ok) throw Error(await reservation.text());
    return {pid:project.id,port:demoPort.port};
  }, { workspace: info().workspace, slug: `remote-preview-${randomUUID().slice(0, 8)}` });
  let released = false;
  try {
    await page.goto(`${origin}/projects/${setup.pid}?tab=preview`);
    await expect(page.getByLabel('Preview port')).toHaveValue(String(setup.port));
    const app = page.frameLocator('iframe[title="Project preview"]').frameLocator('iframe[title="Preview app"]');
    await expect(app.getByRole('heading', {name:'Demo preview'})).toBeVisible();
    const frame = page.frames().find(f => f.url().includes(`preview-${setup.pid}-`))!;
    expect(await frame.evaluate(() => { try { return parent.document.title; } catch { return 'isolated'; } })).toBe('isolated');
    // The linked app uses the same inspector. A valid source/origin reply still
    // cannot prepare a dashboard draft when its UI has not armed a pick.
    let draftRequests = 0;
    page.on('request', request => { if (request.url().includes('/preview-element')) draftRequests++; });
    await page.evaluate(previewOrigin => {
      const frame = document.querySelector<HTMLIFrameElement>('iframe[title="Project preview"]')!;
      window.addEventListener('message', event => {
        if (event.source !== frame.contentWindow || event.origin !== previewOrigin || typeof event.data !== 'string') return;
        const data = JSON.parse(event.data);
        if (data.type === 'selected') (window as unknown as {remoteElement: {text:string}}).remoteElement = data.element;
      });
      frame.contentWindow!.postMessage(JSON.stringify({v:1, type:'pick', id:'remote-inspector-test-pick'}), previewOrigin);
    }, new URL(frame.url()).origin);
    await app.getByRole('heading', {name:'Demo preview'}).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as {remoteElement?: {text:string}}).remoteElement?.text)).toBe('Demo preview');
    expect(draftRequests).toBe(0);
    await expect(page.getByRole('dialog', {name:'Send element to agent?'})).toHaveCount(0);
    await page.screenshot({path:wikiShotPath(testInfo, 'preview-remote.png'),fullPage:true});
    await page.evaluate(async (port) => {
      const csrf = document.cookie.split('; ').find(c => c.startsWith('acc_csrf='))!.split('=')[1];
      const response = await fetch(`/api/ports/${port}/release`, {method:'POST',headers:{'X-ACC-CSRF':csrf}});
      if (!response.ok) throw new Error(await response.text());
    }, setup.port);
    released = true;
    await page.reload();
    await expect(page.getByText('Nothing is listening on a reserved port.')).toBeVisible();
  } finally {
    if (!released) await page.evaluate(async ({ port, pid }) => {
      const csrf = document.cookie.split('; ').find(c => c.startsWith('acc_csrf='))!.split('=')[1];
      const response = await fetch('/api/ports');
      if (!response.ok) throw new Error(await response.text());
      const ports = await response.json();
      if (ports.some((reservation: { port: number; project_id: number }) => reservation.port === port && reservation.project_id === pid)) {
        const released = await fetch(`/api/ports/${port}/release`, { method: 'POST', headers: { 'X-ACC-CSRF': csrf } });
        if (!released.ok) throw new Error(await released.text());
      }
    }, setup);
  }
});
