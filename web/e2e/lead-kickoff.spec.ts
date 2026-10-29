import { expect, test } from '@playwright/test';
import { AUTH_FILE, gitProject, openAdvanced } from './helpers';

test.use({ storageState: AUTH_FILE });

test('With helpers shows an editable brief kickoff and keeps the person’s prompt', async ({ page }) => {
  const { id, slug, headers } = await gitProject(page, 'lead-kickoff');
  const saved = await page.request.post(`/api/projects/${id}/brief`, { headers, data: {
    version: 0, fields: { product: 'PRIVATE kickoff body', run_mode: 'build' },
  } });
  expect(saved.ok()).toBe(true);
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await openAdvanced(dialog);
  const task = dialog.getByRole('textbox', { name: 'Task (optional)', exact: true });
  await expect(task).toHaveValue('');
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await expect(task).toHaveValue(/Read the pinned project brief v1 with the brief tool/);
  await expect(task).not.toHaveValue(/brief_read/);
  await expect(task).toHaveValue(new RegExp(slug));
  await expect(task).toHaveValue(/v1/);
  await expect(task).not.toHaveValue(/PRIVATE kickoff body/);
  await dialog.getByRole('combobox', { name: 'Team run mode', exact: true }).selectOption('prepare');
  await expect(task).toHaveValue(/no helpers/);
  await task.fill('Review my specific migration question first.');
  await dialog.getByRole('combobox', { name: 'Team run mode', exact: true }).selectOption('build');
  await expect(task).toHaveValue('Review my specific migration question first.');
  await expect.poll(async () => {
    const preview = await page.request.post('/api/sessions/preview', { headers, data: {
      project_id: id, provider: 'claude', name: 'Preview kickoff', worktree: true,
      orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 3, total: 10, depth: 2, hours: 8 },
      initial_prompt: await task.inputValue(),
    } });
    return (await preview.json()).inputs?.initial_prompt;
  }).toBe('Review my specific migration question first.');
});

test('With helpers without a brief starts discovery and Once stays empty', async ({ page }) => {
  const { id } = await gitProject(page, 'lead-no-brief');
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await openAdvanced(dialog);
  const task = dialog.getByRole('textbox', { name: 'Task (optional)', exact: true });
  await dialog.getByRole('radio', { name: /With helpers/ }).check();
  await expect(task).toHaveValue(/no accepted brief/);
  await expect(task).toHaveValue(/success/);
  await dialog.getByRole('radio', { name: /^Once/ }).check();
  await expect(task).toHaveValue('');
});
