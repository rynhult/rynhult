import { expect, test } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { AUTH_FILE, control, csrfHeaders, gitProject, info } from './helpers';

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
  const panel = page.getByRole('region', { name: 'Message delivery' });
  await expect.poll(async () => {
    const result = (await (await page.request.get(`/api/deliveries/${delivery.id}`)).json()).delivery;
    return { state: result.state, error: result.error, attempts: result.attempts };
  }).toEqual({ state: 'queued', error: 'terminal_in_use', attempts: 0 });
  // Settled/empty history polls every ten seconds; observing a newly queued
  // external message can take that interval. Pending history then polls at 2s.
  await expect(panel.getByText('Not delivered: another window controls the terminal. Close it, then retry.', { exact: true }))
    .toBeVisible({ timeout: 15_000 });
  await expect(panel.getByRole('button', { name: 'Retry delivery', exact: true })).toBeVisible();
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
