import { test, expect, chooseLayout, type Page } from './fixtures';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { AUTH_FILE, csrfHeaders, expectAxeClean, info, sessionWithChanges, verifyAgain, PASSWORD, totp } from './helpers';
test.use({ presentationChanges: ['admin_features'], storageState: AUTH_FILE });
test.beforeEach(async ({ page }) => {
  const state = await (await page.request.get('/api/admin-features')).json();
  if (!state.features.find((feature: { id: string; enabled: boolean }) => feature.id === 'chat')?.enabled) {
    const response = await page.request.put('/api/admin-features/chat', { headers: await csrfHeaders(page), data: { enabled: true, revision: state.revision } });
    expect(response.ok(), await response.text()).toBeTruthy();
  }
});
const digest = (n: number) => n.toString(16).padStart(64, '0');
const row = (n: number, text = `Message ${n} — ${'Reading a synthetic response. '.repeat(8)}`) => ({ id: digest(n), revision: digest(n + 100), role: n % 2 ? 'user' : 'assistant', text });
async function mocked(page: Page, id: string) {
  const detail = await (await page.request.get(`/api/sessions/${id}`)).json();
  const state = { rows: Array.from({ length: 40 }, (_, i) => row(i + 21)), older: Array.from({ length: 20 }, (_, i) => row(i + 1)), state: 'ready', reset: false, deny: false, follows: 0, fullReads: 0, probes: 0, followState: null as string | null, followCursor: 'follow-cursor', cursors: [] as string[] };
  const envelope = (items = state.rows, before: string | null = 'older') => ({ session_id: id, run_id: detail.runs.at(-1).id, state: state.state, historical_run_attribution: 'unknown', observed_at: '2026-11-01T20:00:00Z', items,
    omitted: state.state === 'gap' ? 2 : 0, reset: state.reset, more: false, reason: null, notices: { tools: 2 }, cursor: 'follow-cursor', before_cursor: before,
    capabilities: { read_history: !['unknown', 'unsupported', 'offline', 'error'].includes(state.state), follow_messages: !['unknown', 'unsupported', 'offline', 'error'].includes(state.state), rich_tools: false, send_text: false } });
  await page.context().route(`**/api/sessions/${id}/chat?*`, route => {
    if (state.deny) return route.fulfill({ status: 404, json: { detail: 'No session here.' } });
    const query = new URL(route.request().url()).searchParams;
    if (query.has('before')) return route.fulfill({ json: envelope(state.older, null) });
    if (query.get('limit') !== '1') state.fullReads++; else state.probes++;
    return route.fulfill({ json: envelope(query.get('limit') === '1' ? state.rows.slice(-1) : state.rows) });
  });
  await page.context().route(`**/api/sessions/${id}/chat/events?*`, route => {
    state.follows++; state.cursors.push(new URL(route.request().url()).searchParams.get('after')!);
    if (state.deny) return route.fulfill({ status: 404, json: { detail: 'No session here.' } });
    const frame = envelope(state.rows, null);
    if (state.followState) { frame.state = state.followState; frame.cursor = state.followCursor; frame.omitted = 0; }
    return route.fulfill({ contentType: 'text/event-stream', body: `event: chat\ndata: ${JSON.stringify(frame)}\n\n` });
  });
  return state;
}
const made: string[] = [];
test.afterEach(async ({ page }) => { for (const id of made.splice(0)) await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) }); });
async function make(page: Page) { const id = await sessionWithChanges(page, `chat-ui-${randomUUID().slice(0, 8)}`); made.push(id); return id; }
const messages = (page: Page) => page.getByRole('region', { name: 'Chat messages', exact: true });

for (const width of [1440, 390]) for (const theme of ['light', 'dark'] as const) test(`read-only Chat ${width}px ${theme}: safe rendering, split, keyboard and terminal continuity`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 }); await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
  const id = await make(page); const data = await mocked(page, id);
  data.rows[0].text = 'Repeated text'; data.rows[1].text = 'Repeated text';
  data.rows[2].text = '<script>window.chatExecuted=true</script>\n![remote](https://example.com/tracker.png)\n[unsafe](javascript:alert(1)) [safe](https://example.com/help)\n\n```text\n' + 'x'.repeat(250) + '\n```';
  let sockets = 0, remoteImages = 0; const sends: string[] = [];
  page.on('websocket', socket => { if (socket.url().includes(`/sessions/${id}/tty?`)) { sockets++; socket.on('framesent', frame => { if (String(frame.payload).includes('"t":"i"')) sends.push(JSON.parse(String(frame.payload)).d); }); } });
  page.on('request', request => { if (request.url().includes('example.com/tracker')) remoteImages++; });
  await page.goto(`/sessions/${id}`);
  await expect(page.getByRole('tab', { name: 'Chat', exact: true })).toBeVisible();
  await expect(page.locator('.xterm')).toHaveCount(1);
  const original = await page.locator('.xterm-helper-textarea').elementHandle();
  if (width === 1440) {
    await page.getByRole('button', { name: 'Show Chat beside terminal', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Chat · Read-only' })).toBeFocused();
    const separator = page.getByRole('separator', { name: 'Terminal and Chat split' });
    await separator.focus(); await page.keyboard.press('ArrowRight');
    await expect(separator).not.toHaveAttribute('aria-valuenow', '60');
    await page.getByRole('button', { name: 'Reset split' }).click();
    await expect(separator).toHaveAttribute('aria-valuenow', '60');
    await expect(page.getByRole('region', { name: 'Session terminal', exact: true })).toBeVisible();
  } else {
    await expect(page.getByRole('button', { name: 'Show Chat beside terminal', exact: true })).toBeDisabled();
    await expect(page.getByText('Two panes need a wider window.')).toBeVisible();
    await page.getByRole('tab', { name: 'Chat', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Session terminal', exact: true })).toBeHidden();
    await expect(page.getByRole('separator')).toHaveCount(0);
  }
  await expect(messages(page).locator('[data-chat-item]')).toHaveCount(40);
  await expect(messages(page).getByText('Repeated text', { exact: true })).toHaveCount(2);
  expect(await page.evaluate(() => (window as any).chatExecuted)).toBeUndefined(); expect(remoteImages).toBe(0);
  expect(await messages(page).locator('a[href^="javascript:"]').count()).toBe(0);
  await expect(messages(page).getByRole('link', { name: /safe.*https:\/\/example.com\/help/ })).toHaveAttribute('rel', 'noopener noreferrer');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0);
  await expect(page.locator('[contenteditable=true]')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Session chat', exact: true })).toContainText('CLI version not reported');
  expect(await page.locator('details').filter({ hasText: 'Source details' }).getAttribute('open')).toBeNull();
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath(`chat-${width}-${theme}.png`) });
  await page.getByRole('button', { name: 'Open Terminal', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toHaveAttribute('aria-selected', 'true');
  expect(await original!.evaluate(node => node === document.querySelector('.xterm-helper-textarea'))).toBe(true);
  expect(sockets).toBe(1);
  // The already-running xterm may answer device/color queries. Chat controls
  // must not send arrow keys or transcript text into its input.
  expect(sends.join('')).not.toContain('\x1b[C');
  expect(sends.join('')).not.toContain('Repeated text');
});

test('pagination, follow upserts and new-message affordance preserve the reader’s anchor', async ({ page }) => {
  const id = await make(page); const data = await mocked(page, id);
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(messages(page).locator('[data-chat-item]')).toHaveCount(40);
  expect(await page.locator('.xterm').count()).toBe(0); // deep-linked read-only Chat never claims a terminal
  await messages(page).evaluate(el => { el.scrollTop = 0; });
  const anchor = messages(page).locator('[data-chat-item]').first();
  const identity = await anchor.getAttribute('data-chat-item'); const before = (await anchor.boundingBox())!.y;
  await page.getByRole('button', { name: 'Load earlier', exact: true }).click();
  await expect(messages(page).locator('[data-chat-item]')).toHaveCount(60);
  expect(Math.abs((await messages(page).locator(`[data-chat-item="${identity}"]`).boundingBox())!.y - before)).toBeLessThanOrEqual(1);
  data.rows.push(row(61, 'New synthetic reply'));
  await expect(page.getByRole('button', { name: 'New messages · Jump to latest' })).toBeVisible();
  expect(Math.abs((await messages(page).locator(`[data-chat-item="${identity}"]`).boundingBox())!.y - before)).toBeLessThanOrEqual(1);
  data.rows[data.rows.length - 1] = { ...row(61, 'Updated synthetic reply'), revision: digest(999) };
  await expect(messages(page).getByText('Updated synthetic reply', { exact: true })).toHaveCount(1);
  await expect(messages(page).locator('[data-chat-item]')).toHaveCount(61);
  await page.getByRole('button', { name: 'New messages · Jump to latest' }).click();
  await expect(messages(page).getByText('Updated synthetic reply', { exact: true })).toBeInViewport();
});

test('honest source states, reset and revoked access never look like empty history', async ({ page }) => {
  const id = await make(page); const data = await mocked(page, id);
  data.state = 'unknown'; data.rows = [];
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText(/The Chat source is not established/)).toBeVisible();
  for (const [state, words] of [['empty', 'No messages yet.'], ['offline', 'Machine offline. Retained'], ['gap', 'This history is incomplete.'], ['error', 'The account service could not read Chat.']]) {
    data.state = state;
    await page.getByRole('button', { name: 'Reload Chat', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Session chat', exact: true })).toContainText(words);
  }
  data.state = 'ready'; data.rows = [row(1, 'Private synthetic message')];
  await page.getByRole('button', { name: 'Reload Chat', exact: true }).click();
  await expect(messages(page).getByText('Private synthetic message')).toBeVisible();
  data.reset = true;
  await expect(page.getByText(/source or session run changed/)).toBeVisible();
  data.reset = false; data.deny = true;
  await page.getByRole('button', { name: 'Reload Chat', exact: true }).click();
  await expect(page.getByText('You no longer have access to this Chat.')).toBeVisible();
  await expect(page.getByText('Private synthetic message')).toHaveCount(0);
  await expect(page.getByText(/Tool records · at least/)).toHaveCount(0);
});

test('real backend: owner and authorized member can read only their permitted native source', async ({ page, browser }) => {
  const ownerSession = await make(page);
  const detail = await (await page.request.get(`/api/sessions/${ownerSession}`)).json();
  const nativeFile = (session: { native_id: string; cwd: string }) => {
    const root = `${info().state}/home/agent/.claude/projects/${session.cwd.replace(/[^a-z0-9]/gi, '-')}`;
    mkdirSync(root, { recursive: true }); return `${root}/${session.native_id}.jsonl`;
  };
  const ownerFile = nativeFile(detail);
  const record = (native: string, text: string) => JSON.stringify({ type: 'assistant', sessionId: native, version: '2.1.308', uuid: randomUUID(), message: { content: [{ type: 'text', text }] } }) + '\n';
  writeFileSync(ownerFile, record(detail.native_id, 'Owner source: synthetic only'));
  await page.goto(`/sessions/${ownerSession}?tab=chat`);
  await expect(messages(page).getByText('Owner source: synthetic only')).toBeVisible();
  appendFileSync(ownerFile, record(detail.native_id, 'Live synthetic append'));
  await expect(messages(page).getByText('Live synthetic append')).toBeVisible();
  await verifyAgain(page);
  const suffix = randomUUID().slice(0, 6), account = `chat-${suffix}-agent`, username = `chat-reader-${suffix}`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'owner', account } }); expect(invited.ok()).toBe(true);
  const token = (await invited.json()).link.split('/').pop();
  const member = await browser.newContext();
  try {
    const headers = { Origin: info().origin };
    const begin = await member.request.post('/api/auth/invite/begin', { headers, data: { token } });
    const joined = await member.request.post('/api/auth/invite/complete', { headers, data: { token, username, password: PASSWORD, code: totp((await begin.json()).totp_secret) } }); expect(joined.ok()).toBe(true);
    await chooseLayout(member, false);
    const view = await member.newPage();
    const memberCsrf = (await member.cookies()).find(cookie => cookie.name === 'acc_csrf')!.value;
    const own = await member.request.post('/api/sessions', { headers: { ...headers, 'X-ACC-CSRF': memberCsrf }, data: { project_id: detail.project_id, provider: 'claude', name: 'Member Chat fixture' } }); expect(own.ok(), await own.text()).toBe(true);
    const ownId = (await own.json()).id;
    await expect.poll(async () => (await (await member.request.get(`/api/sessions/${ownId}`)).json()).native_id).toBeTruthy();
    const ownDetail = await (await member.request.get(`/api/sessions/${ownId}`)).json();
    writeFileSync(nativeFile(ownDetail), record(ownDetail.native_id, 'Member source: synthetic only'));
    // Bootstrap the fixture as an owner, then exercise actual member authority.
    // A fresh member cannot launch into another account's private checkout.
    const users = await (await page.request.get('/api/users')).json();
    const memberId = users.users.find((user: { username: string }) => user.username === username).id;
    expect((await page.request.post(`/api/users/${memberId}/role`, { headers: await csrfHeaders(page), data: { role: 'member' } })).ok()).toBe(true);
    expect((await (await member.request.get('/api/auth/state')).json()).role).toBe('member');
    expect((await member.request.get(`/api/sessions/${ownerSession}/chat`)).status()).toBe(404);
    await view.goto(`/sessions/${ownerSession}?tab=chat`);
    await expect(view.getByText("There's no session here")).toBeVisible();
    await expect(view.getByText('Owner source: synthetic only')).toHaveCount(0);
    await view.goto(`/sessions/${ownId}?tab=chat`);
    await expect(messages(view).getByText('Member source: synthetic only')).toBeVisible();
    await expectAxeClean(view);
    await member.request.post(`/api/sessions/${ownId}/stop`, { headers: { ...headers, 'X-ACC-CSRF': memberCsrf } });
  } finally {
    try { await chooseLayout(member, true); }
    finally { await member.close(); }
  }
});

test('hidden Chat pauses following; responsive collapse and full-tab return retain focus and one terminal', async ({ page }) => {
  const id = await make(page); const data = await mocked(page, id);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.clock.install();
  await page.goto(`/sessions/${id}`);
  await page.getByRole('button', { name: 'Show Chat beside terminal', exact: true }).click();
  await expect(messages(page).locator('[data-chat-item]')).toHaveCount(40);
  await page.getByRole('heading', { name: 'Chat · Read-only' }).focus();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('separator')).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeFocused();
  await page.getByRole('tab', { name: 'Chat', exact: true }).click();
  await expect(messages(page)).toBeVisible();
  await page.getByRole('button', { name: 'Open Terminal', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeFocused();
  await expect(messages(page)).toBeHidden();
  const follows = data.follows;
  await page.clock.fastForward(10_000);
  expect(data.follows).toBe(follows);
  await expect(page.locator('.xterm')).toHaveCount(1);
  data.state = 'offline';
  await page.getByRole('tab', { name: 'Chat', exact: true }).click();
  await expect(page.getByText('Machine offline. Retained messages may be stale; Chat updates resume after a retry.')).toBeVisible();
});


test('unsupported Chat never attaches a terminal until the person opens it', async ({ page }) => {
  const id = await make(page), data = await mocked(page, id);
  data.state = 'unsupported'; data.rows = [];
  const sockets: string[] = [];
  page.on('websocket', socket => sockets.push(socket.url()));
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('This provider or CLI version does not have a qualified Chat reader. Use Terminal.')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/sessions/${id}\\?tab=chat$`));
  await expect(page.locator('.xterm')).toHaveCount(0);
  expect(sockets).toEqual([]);
  await page.getByRole('button', { name: 'Open Terminal', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.xterm')).toHaveCount(1);
  expect(sockets).toHaveLength(1);
});

test('a source established after startup reveals Chat without a page reload or a hidden follow stream', async ({ page }) => {
  const id = await make(page), data = await mocked(page, id);
  data.state = 'unknown'; data.rows = [];
  await page.goto(`/sessions/${id}`);
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Chat', exact: true })).toHaveCount(0);
  await expect.poll(() => data.probes).toBe(1);
  data.state = 'ready'; data.rows = [row(1)];
  // The shared worker owns coordination; its timers are not page.clock timers.
  await expect(page.getByRole('tab', { name: 'Chat', exact: true })).toBeVisible({timeout:20000});
  expect(data.follows).toBe(0);
  expect(data.fullReads).toBe(0);
});

test('review UI-1/UI-2: hostile Markdown stays bounded and leaves the same Terminal usable', async ({ page }) => {
  const id=await make(page),data=await mocked(page,id);
  data.rows=[row(1,'*a'.repeat(60000)),row(2,'~a'.repeat(60000)),row(3,'> '.repeat(8000))];
  await page.goto(`/sessions/${id}`);
  const original=await page.locator('.xterm-helper-textarea').elementHandle();
  await page.getByRole('button',{name:'Show Chat beside terminal',exact:true}).click();
  await expect(page.getByText('Message shown as plain text within Chat’s formatting safety limits.')).toHaveCount(3);
  expect(await original!.evaluate(node=>node===document.querySelector('.xterm-helper-textarea'))).toBe(true);
  await page.locator('.xterm-helper-textarea').focus();await page.keyboard.type('terminal still alive');
  await expect.poll(async()=> (await (await page.request.get(`/api/sessions/${id}/terminal-output`)).json()).text).toContain('terminal still alive');
});

test('review UI-2: message and panel error boundaries cannot unmount the Terminal', async ({ page }) => {
  const id=await make(page),data=await mocked(page,id);data.rows=[row(1,'[trigger](https://example.com)')];
  await page.addInitScript(()=>{const original=String.prototype.indexOf;String.prototype.indexOf=function(search:string,position?:number){if(String(this)==='[trigger](https://example.com)')throw Error('controlled message rendering failure');return original.call(this,search,position);};});
  await page.goto(`/sessions/${id}`);const field=await page.locator('.xterm-helper-textarea').elementHandle();
  await page.getByRole('button',{name:'Show Chat beside terminal',exact:true}).click();
  await expect(page.getByText('Formatting unavailable. Showing plain text.')).toBeVisible();
  expect(await field!.evaluate(node=>node===document.querySelector('.xterm-helper-textarea'))).toBe(true);
  await page.evaluate(()=>{const original=Element.prototype.getBoundingClientRect;Element.prototype.getBoundingClientRect=function(){if(this.getAttribute('aria-label')==='Chat messages')throw Error('controlled Chat layout failure');return original.call(this);};});
  data.rows.push(row(2,'new message'));
  await expect(page.getByText('Chat could not be displayed. Terminal remains available.')).toBeVisible();
  expect(await field!.evaluate(node=>node===document.querySelector('.xterm-helper-textarea'))).toBe(true);
});

test('review UI-3/UI-4: failed cursors never resume and gap evidence survives heartbeat and tabs', async ({ page }) => {
  const id=await make(page),data=await mocked(page,id);data.state='gap';data.followState='ready';
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('This history is incomplete. Some records were omitted or continuity could not be verified.')).toBeVisible();
  await expect(page.getByText('At least 2 records omitted.',{exact:false})).toHaveCount(1); // inside collapsed source details
  data.followState='error';data.followCursor='POISON-from-failed-read';
  await expect(page.getByText('The account service could not read Chat. Retained messages may be stale.')).toBeVisible();
  data.followState='ready';data.followCursor='follow-cursor';
  await page.getByRole('button',{name:'Open Terminal',exact:true}).click();
  // Returning before Terminal commits can coalesce both navigations: the reader
  // never observes inactive and its stopped error stream is not restarted.
  await expect(page.getByRole('tab',{name:'Terminal',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(messages(page)).toBeHidden();
  await page.getByRole('tab',{name:'Chat',exact:true}).click();
  await expect(page.getByRole('tab',{name:'Chat',exact:true})).toHaveAttribute('aria-selected','true');
  await expect.poll(()=>data.cursors.length).toBeGreaterThan(2);
  expect(data.cursors).not.toContain('POISON-from-failed-read');
  await expect(page.getByText('This history is incomplete. Some records were omitted or continuity could not be verified.')).toBeVisible();
});

test('review UI-6: two views share one full read and busy retries keep the cursor', async ({ page }) => {
  const id=await make(page),data=await mocked(page,id);data.followState='busy';data.followCursor='POISON-busy';
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('Chat source is busy. Waiting to retry; retained messages may be stale.')).toBeVisible();
  const other=await page.context().newPage();
  try {
    await other.goto(`/sessions/${id}?tab=chat`);
    await expect(messages(other).locator('[data-chat-item]')).toHaveCount(40);
    expect(data.fullReads).toBe(1);
    data.followState='ready';data.followCursor='follow-cursor';data.rows.push(row(61,'Shared continuation'));
    await expect(messages(other).getByText('Shared continuation')).toBeVisible();
    expect(data.cursors).not.toContain('POISON-busy');
    await other.close();
    data.rows.push(row(62,'Leader returned'));
    await expect(messages(page).getByText('Leader returned')).toBeVisible();
    expect(data.fullReads).toBe(1);
  }finally{if(!other.isClosed())await other.close();}
});

test('a blocked shared worker leaves a safe Chat error and a usable Terminal', async ({ page }) => {
  const id=await make(page);await mocked(page,id);
  await page.addInitScript(()=>Object.defineProperty(window,'SharedWorker',{value:class{constructor(){throw Error('blocked by browser policy');}}}));
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('This browser could not start a safe Chat reader. Use Terminal.')).toBeVisible();
  await page.getByRole('button',{name:'Open Terminal',exact:true}).click();
  await expect(page.locator('.xterm')).toHaveCount(1);
  await expect(page.getByText('This page couldn’t be shown')).toHaveCount(0);
});

test('review UI-10: commands, arithmetic and delimiter runs read faithfully', async ({ page }) => {
  const id=await make(page), data=await mocked(page,id);
  const literal=['rm *.log *.tmp','2 * 3 * 4','glob src/*/test/*.py','grep -r ** src ** done','x **** y'];
  data.rows=[...literal.map((text,i)=>row(i+1,text)),row(6,'use ``a`b`` here')];
  await page.goto(`/sessions/${id}?tab=chat`);
  for (const text of literal) await expect(messages(page).getByText(text,{exact:true})).toBeVisible();
  await expect(messages(page).locator('code').filter({hasText:'a`b'})).toHaveText('a`b');
  await expect(messages(page).locator('input,textarea,[contenteditable="true"]')).toHaveCount(0);
});

test('review UI-15/UI-16: code-like text and text beside fences read faithfully', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const id=await make(page), data=await mocked(page,id);
  const literal=['handlers[key](event)','arr[0](x) ok','see [docs](javascript:alert(1)) now','vec![a](b)'];
  data.rows=[...literal.map((text,i)=>row(i+1,text)),row(5,'```js\nx*y\n``` tail\nafter'),row(6,'```inline``` text')];
  await page.goto(`/sessions/${id}?tab=chat`);
  for (const text of literal) await expect(messages(page).getByText(text,{exact:true})).toBeVisible();
  await expect(messages(page).locator('pre').filter({hasText:'``` tail'})).toContainText('after');
  await expect(messages(page).locator('code').filter({hasText:/^inline$/})).toBeVisible();
  await expect(messages(page).getByText('text',{exact:false}).last()).toBeVisible();
  await expect(messages(page).locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(messages(page).locator('input,textarea,[contenteditable="true"]')).toHaveCount(0);
});

test('review UI-14: reader capacity is explained without claiming access was revoked', async ({ page }) => {
  const id=await make(page); await mocked(page,id);
  await page.addInitScript(()=>Object.defineProperty(window,'SharedWorker',{value:class {
    port={onmessage:null as ((event:{data:{type:string}})=>void)|null, start(){},close(){},postMessage(message:{type:string}){
      if(message.type==='subscribe')queueMicrotask(()=>this.onmessage?.({data:{type:'capacity'}}));
    }};
  }}));
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('Too many Chat sources are open in this browser. Close another session page, then reload Chat.')).toBeVisible();
  await expect(page.getByText('You no longer have access to this Chat.')).toHaveCount(0);
  await page.getByRole('button',{name:'Open Terminal',exact:true}).click();
  await expect(page.locator('.xterm')).toHaveCount(1);
});


test('Chat Off hides its entry and neither probes nor attaches a linked terminal', async ({ page }) => {
  const id = await make(page);
  const state = await (await page.request.get('/api/admin-features')).json();
  expect((await page.request.put('/api/admin-features/chat', { headers: await csrfHeaders(page), data: { enabled: false, revision: state.revision } })).ok()).toBeTruthy();
  const reads: string[] = [], sockets: string[] = [];
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith(`/api/sessions/${id}/chat`)) reads.push(request.url()); });
  page.on('websocket', socket => sockets.push(socket.url()));
  await page.goto(`/sessions/${id}?tab=chat`);
  await expect(page.getByText('This feature is turned off.', { exact: false })).toBeVisible();
  expect(reads).toEqual([]); expect(sockets).toEqual([]);
  await expect(page.locator('.xterm')).toHaveCount(0);
});
