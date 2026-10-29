import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { readFileSync, existsSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, gitProject, info, openAdvanced } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const source of ['Empty folder', 'Clone repository']) {
  test(`new project description intake is available for ${source}`, async ({ page }) => {
    await page.goto('/projects');
    await page.getByRole('button', { name: 'New project', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: 'New project', exact: true });
    await dialog.getByRole('radio', { name: source, exact: true }).check();
    await expect(dialog.getByRole('textbox', { name: /What do you want to build/ })).toBeVisible();
  });
}

test('unsaved brief launch offers Save and launch, Launch without the brief, and Back', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'bi15-unsaved');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('A room booking board');
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  try {
    for (const name of ['Save and launch', 'Launch without the brief', 'Back']) {
      await expect(dialog.getByRole('button', { name, exact: true })).toBeVisible();
    }
  } finally {
    const sessions = await (await page.request.get('/api/sessions')).json();
    for (const session of sessions.filter((s: { project_id: number }) => s.project_id === id)) {
      await page.request.post(`/api/sessions/${session.id}/team/stop`, { headers });
    }
  }
});

test('empty project starts a team with an edited kickoff and resume sends no second kickoff', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  const projectDialog = page.getByRole('dialog', { name: 'New project', exact: true });
  await projectDialog.getByRole('textbox', { name: 'Name', exact: true }).fill(`bi15-${randomUUID()}`);
  await projectDialog.getByRole('button', { name: 'Create project', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await openAdvanced(dialog);
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  const task = dialog.getByRole('textbox', { name: 'Task (optional)', exact: true });
  await expect(task).toHaveValue(/no accepted brief/);
  await task.fill('Please investigate the room booking workflow first.');
  const created = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/sessions');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = await csrfHeaders(page);
  try {
    await page.goto(`/sessions/${session.id}?tab=activity`);
    const argvPath = `${info().run_dir}/${session.id}/fake.argv.jsonl`;
    const invocations = () => existsSync(argvPath) ? readFileSync(argvPath, 'utf8').trim().split('\n').map(line => JSON.parse(line) as string[]) : [];
    await expect.poll(async () => invocations().length, { timeout: 20_000 }).toBe(1);
    expect(invocations()[0]).toContain('Please investigate the room booking workflow first.');
    const detail = await (await page.request.get(`/api/sessions/${session.id}`)).json();
    expect(detail.launch_spec.inputs.initial_prompt).toBe('Please investigate the room booking workflow first.');
    expect((await page.request.post(`/api/sessions/${session.id}/stop`, { headers })).ok()).toBe(true);
    expect((await page.request.post(`/api/sessions/${session.id}/resume`, { headers })).ok()).toBe(true);
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session.id}`)).json()).observed_state,
      { timeout: 20_000 }).toMatch(/idle|active/);
    await expect.poll(async () => invocations().length, { timeout: 20_000 }).toBe(2);
    expect(invocations()[1]).toContain('--resume');
    expect(invocations()[1]).not.toContain('Please investigate the room booking workflow first.');
  } finally { await page.request.post(`/api/sessions/${session.id}/team/stop`, { headers }); }
});
