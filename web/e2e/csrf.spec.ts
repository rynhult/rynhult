import { expect, test } from '@playwright/test';
import { AUTH_FILE, PASSWORD, loadSecret, totp, freshStep, info } from './helpers';

test.use({ storageState: AUTH_FILE });

test.beforeEach(async ({ page, context }) => {
  test.setTimeout(90_000);
  if (!(await (await page.request.get('/api/auth/state')).json()).authenticated) {
    await freshStep(page);
    const login = await page.request.post('/api/auth/login', { headers: { Origin: info().origin },
      data: { username: 'alex', password: PASSWORD, code: totp(loadSecret()) } });
    expect(login.ok(), await login.text()).toBe(true);
    await context.storageState({ path: AUTH_FILE });
  }
});

async function loadApi(page: import('@playwright/test').Page) {
  // The fixture serves a test module copy of the production API helper; no app auth bypass.
  const { readFileSync } = await import('node:fs');
  const { stripTypeScriptTypes } = await import('node:module');
  const source = stripTypeScriptTypes(readFileSync('src/lib/api.ts', 'utf8'), { mode: 'transform' });
  await page.route('**/csrf-helper.js', r => r.fulfill({ contentType: 'application/javascript', body: source }));
  await page.route('**/csrf-check', r => r.fulfill({ contentType: 'text/html', body: '<!doctype html><title>CSRF fixture</title>' }));
  await page.goto('/csrf-check');
}

for (const missing of [false, true]) test(`HTTPS ignores a stale plain CSRF cookie; secure token missing=${missing}`, async ({ page, browserName }) => {
  await loadApi(page);
  const host = (await page.context().cookies()).find(c => c.name === '__Host-acc_csrf')!;
  expect(host).toBeTruthy();
  await page.context().clearCookies({ name: '__Host-acc_csrf' });
  await page.context().addCookies([{ name: 'acc_csrf', value: 'stale-plain-first', url: page.url(), secure: true },
    { name: 'acc_session', value: 'stale-session', url: page.url(), secure: true, httpOnly: true }]);
  if (!missing) await page.context().addCookies([host]);
  if (browserName === 'chromium') {
    const first = await page.evaluate(() => document.cookie.split(';').map(c => c.trim()).find(c => /^(?:__Host-)?acc_csrf=/.test(c)));
    expect(first).toBe('acc_csrf=stale-plain-first');
  }
  let refreshes = 0;
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/auth/csrf') refreshes++; });
  const result = await page.evaluate(async () => {
    const { api } = await import('/csrf-helper.js');
    return api('/api/projects/new', { json: { source: 'empty', name: `csrf-${crypto.randomUUID()}` } })
      .then(() => ({ status: 201, message: '' })).catch((e: { status: number; message: string }) => ({ status: e.status, message: e.message }));
  });
  expect(result.status, result.message).toBe(201);
  expect(refreshes).toBe(missing ? 1 : 0);
  const logout = await page.evaluate(async () => {
    const { api } = await import('/csrf-helper.js');
    return api('/api/auth/logout', { method: 'POST', headers: { 'X-ACC-CSRF': 'bad' } });
  });
  expect(logout).toEqual({ ok: true });
  expect((await (await page.request.get('/api/auth/state')).json()).authenticated).toBe(false);
});

test('CSRF retries exactly once and never retries ordinary authorization errors', async ({ page }) => {
  await loadApi(page);
  let writes = 0, refreshes = 0;
  await page.route('**/api/csrf-probe', r => {
    writes++;
    return r.fulfill({ status: 403, headers: { 'X-ACC-CSRF-Error': 'mismatch' },
      contentType: 'application/json', body: JSON.stringify({ detail: 'Reload the page and try again.' }) });
  });
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/auth/csrf') refreshes++; });
  const invoke = () => page.evaluate(async () => {
    const { api } = await import('/csrf-helper.js');
    try { await api('/api/csrf-probe', { json: {} }); } catch (e) { return (e as { status: number }).status; }
  });
  expect(await invoke()).toBe(403);
  expect(writes).toBe(2); expect(refreshes).toBe(1);
  await page.route('**/api/csrf-probe', r => {
    writes++;
    return r.fulfill({ status: 403, contentType: 'application/json', body: '{"detail":"Owner only"}' });
  });
  expect(await invoke()).toBe(403);
  expect(writes).toBe(3); expect(refreshes).toBe(1);
});
