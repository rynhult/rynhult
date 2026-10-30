import { expect, test } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, control, csrfHeaders, gitProject, info, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });

test('launch task reaches the fake provider and a later message is acknowledged', async ({ page }) => {
  await page.goto('/projects');
  const { id, headers } = await gitProject(page, 'delivery-demo');
  const response = await page.request.post('/api/sessions', { headers,
    data: { project_id: id, provider: 'claude', name: 'Delivery demo', task: 'Review the demo project.' } });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const delivery = session.delivery.id;
  await expect.poll(async () => {
    const result = await page.request.get(`/api/deliveries/${delivery}`);
    return (await result.json()).delivery.state;
  }, { timeout: 20_000 }).toBe('acknowledged');
  const sent = await page.request.post(`/api/sessions/${session.id}/message?wait=0.2`, { headers: await csrfHeaders(page),
    data: { text: 'Then check the tests.' } });
  expect(sent.status()).toBe(202);
  const queued = (await sent.json()).delivery;
  expect(queued.state).toBe('queued');
  control(session.id, 'turn');
  await expect.poll(async () => {
    return (await (await page.request.get(`/api/deliveries/${queued.id}`)).json()).delivery.state;
  }, { timeout: 20_000 }).toBe('acknowledged');
  const result = (await (await page.request.get(`/api/deliveries/${queued.id}`)).json()).delivery;
  expect(result.ack).toBe('hook:UserPromptSubmit');
  expect(result).not.toHaveProperty('body');
  // The real browser still opens this session with its unchanged terminal and activity view.
  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeVisible();
});

test('project plus topic is refused before creating a local workspace', async ({ page }) => {
  await page.goto('/projects');
  const { id, headers } = await gitProject(page, 'launch-validation');
  const refused = await page.request.post('/api/sessions', { headers,
    data: { project_id: id, topic: 'Wrong workspace', provider: 'claude', name: 'Refused launch' } });
  expect(refused.status()).toBe(422);
  expect(await refused.text()).toContain('did you mean task');
});

test('an open terminal shows not-delivered status and retries the same message after leaving Terminal', async ({ page }) => {
  await page.goto('/projects');
  const { id, headers } = await gitProject(page, 'delivery-conflict');
  const created = await page.request.post('/api/sessions', { headers,
    data: { project_id: id, provider: 'claude', name: 'Delivery conflict' } });
  expect(created.status()).toBe(201);
  const session = await created.json();
  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session.id}`)).json()).observed_state).toBe('idle');
  const sent = await page.request.post(`/api/sessions/${session.id}/message`, {
    headers: await csrfHeaders(page), data: { text: 'Deliver this exactly once.' } });
  expect(sent.status()).toBe(202);
  const delivery = (await sent.json()).delivery;
  const panel = page.getByRole('region', { name: 'Message delivery status' });
  await expect.poll(async () => {
    const result = (await (await page.request.get(`/api/deliveries/${delivery.id}`)).json()).delivery;
    return { state: result.state, error: result.error, attempts: result.attempts };
  }).toEqual({ state: 'queued', error: 'terminal_in_use', attempts: 0 });
  // Settled/empty history polls every ten seconds; observing a newly queued
  // external message can take that interval. Pending history then polls at 2s.
  await expect(panel).toContainText('1 message waiting to deliver', { timeout: 15_000 });
  await expect(panel.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  const received = `${info().run_dir}/${session.id}/fake.received`;
  expect(existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('');
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  // Releasing terminal control must flush automatically: no manual Retry click
  // that could race the worker or hide a missing automatic wake-up.
  await expect.poll(async () => (await (await page.request.get(`/api/deliveries/${delivery.id}`)).json()).delivery.state,
    { timeout: 20_000 }).toBe('acknowledged');
  await expect(panel).toHaveCount(0);
  await expect.poll(() => existsSync(received) ? readFileSync(received, 'utf8') : '').toBe('Deliver this exactly once.\n');
});

for (const width of [1440, 390]) test.describe(`delivery geometry ${width}`, () => {
  test.use({ hasTouch: width === 390, isMobile: width === 390 });
  test(`delivery status is compact and keeps terminal geometry at ${width}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  const project = await gitProject(page, 'compact-delivery');
  const created = await page.request.post('/api/sessions', { headers: project.headers,
    data: { project_id: project.id, provider: 'claude', name: 'Compact delivery' } });
  expect(created.ok()).toBe(true);
  const id = (await created.json()).id;
  let pending = false;
  const text = 'Review the example.lan changes. ' + 'Keep the full context available. '.repeat(12);
  const row = { id: 'compact-demo', state: 'queued', preview: text.slice(0, 200), text,
    notice: 'Not delivered: another window controls the terminal. Close it, then retry.' };
  let polls = 0;
  await page.route(`**/api/sessions/${id}/deliveries*`, route => {
    polls++;
    return route.fulfill({ json: { deliveries: pending ? [row] : [{ ...row, state: 'acknowledged' }] } });
  });
  await page.goto(`/sessions/${id}`);
  const field = page.locator('.xterm-helper-textarea');
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Session automation' })).toHaveCount(0);
  if (width === 390) {
    await page.getByRole('button', { name: 'Session details', exact: true }).tap();
    await expect(page.getByRole('group', { name: 'Session details', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
  }
  const original = await field.elementHandle();
  await field.focus();
  await page.keyboard.type('before-');
  const terminal = page.locator('.xterm');
  const initial = (await terminal.boundingBox())!;
  if (width === 390) expect(initial.y).toBeLessThan(250);
  await page.evaluate(() => {
    (window as any).__deliveryAnnouncements = [];
    const region = document.querySelector('[aria-label="Announcements"]')!;
    new MutationObserver(() => {
      if (region.textContent?.includes('waiting to deliver')) (window as any).__deliveryAnnouncements.push(region.textContent);
    }).observe(region, { childList: true, subtree: true, characterData: true });
  });
  pending = true;
  const indicator = page.getByRole('region', { name: 'Message delivery status' });
  await expect(indicator).toContainText('1 message waiting to deliver', { timeout: 15_000 });
  // Queuing is ordinary status, announced once through the app's polite region.
  await expect(indicator).not.toHaveAttribute('role', 'alert');
  await expect(indicator.locator('[role="alert"], [aria-live="assertive"]')).toHaveCount(0);
  await expect(indicator).not.toContainText(text.slice(0, 50));
  const queued = (await terminal.boundingBox())!;
  expect(Math.abs(queued.y - initial.y)).toBeLessThan(1);
  expect(Math.abs(queued.height - initial.height)).toBeLessThan(1);
  const appearedAt = polls;
  await expect.poll(() => polls, { timeout: 10_000 }).toBeGreaterThan(appearedAt + 1);
  expect(await page.evaluate(() => (window as any).__deliveryAnnouncements.length)).toBe(1);
  expect(await original!.evaluate(node => node === document.querySelector('.xterm-helper-textarea'))).toBe(true);
  await expect(field).toBeFocused();
  await page.keyboard.type('after');
  await page.screenshot({ path: wikiShotPath(testInfo, `session-delivery-${width}.png`), animations: 'disabled' });
  // Details is a real keyboard action and releases this terminal's control.
  await indicator.getByRole('button', { name: 'Details', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('tab', { name: 'Activity', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('region', { name: 'Message delivery history' })).toContainText(text);
  await expect(page.getByRole('region', { name: 'Message delivery history' })).toBeFocused();
  await page.getByRole('region', { name: 'Message delivery history' }).screenshot({ path: wikiShotPath(testInfo, `session-delivery-details-${width}.png`), animations: 'disabled' });
  pending = false;
  await expect(indicator).toHaveCount(0, { timeout: 15_000 });
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await expect(terminal).toBeVisible();
  const settled = (await terminal.boundingBox())!;
  expect(Math.abs(settled.y - initial.y)).toBeLessThan(1);
  expect(Math.abs(settled.height - initial.height)).toBeLessThan(1);
  await testInfo.attach('terminal-geometry', { contentType: 'application/json', body: JSON.stringify({ width, initial, queued, settled }) });
  await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, width === 390 ? 'phone-terminal.png' : 'session-page.png'), animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await page.goto(`/sessions/${id}?tab=spec#automation`);
  await expect(page.getByRole('region', { name: 'Session automation' })).toBeVisible();
  await page.request.post(`/api/sessions/${id}/stop`, { headers: project.headers });
  });
});

test('failed delivery details keep the explicit resend check away from the terminal', async ({ page }) => {
  const project = await gitProject(page, 'failed-delivery');
  const created = await page.request.post('/api/sessions', { headers: project.headers,
    data: { project_id: project.id, provider: 'claude', name: 'Failed delivery' } });
  expect(created.ok()).toBe(true);
  const id = (await created.json()).id;
  await page.route(`**/api/sessions/${id}/deliveries*`, route => route.fulfill({ json: { deliveries: [{ id: 'failed-demo',
    state: 'failed', preview: 'Check the example.lan changes.', text_truncated: true,
    notice: 'Submission is unconfirmed. Check the terminal before sending again.' }] } }));
  let sends = 0;
  await page.route(`**/api/sessions/${id}/message`, route => {
    expect(route.request().postDataJSON().text).toBe('Send this only after checking.');
    sends++;
    return route.fulfill({ status: 202, json: { delivery: { id: 'resent-demo', state: 'queued' } } });
  });
  await page.goto(`/sessions/${id}`);
  const indicator = page.getByRole('region', { name: 'Message delivery status' });
  await expect(indicator).toContainText('1 message not delivered');
  await expect(indicator.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Message to send again' })).toHaveCount(0);
  await indicator.getByRole('button', { name: 'Details', exact: true }).click();
  const history = page.getByRole('region', { name: 'Message delivery history' });
  await expect(history).toContainText('Saved preview only');
  await expect(history.getByRole('button', { name: 'Send again after checking' })).toBeDisabled();
  await history.getByRole('textbox', { name: 'Message to send again' }).fill('Send this only after checking.');
  await history.getByRole('button', { name: 'Send again after checking' }).click();
  await expect.poll(() => sends).toBe(1);
  await page.request.post(`/api/sessions/${id}/stop`, { headers: project.headers });
});
