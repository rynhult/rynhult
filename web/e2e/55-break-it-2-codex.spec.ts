import { expect, test } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { E2E_BASE } from '../playwright.config';
import { AUTH_FILE, control, gitProject, info } from './helpers';

test.use({ storageState: AUTH_FILE });

test('phone terminal still detects Codex questions and turn errors', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { id: project, headers } = await gitProject(page, 'phone-codex-footer');
  const started = await page.request.post('/api/sessions', { headers, data: {
    project_id: project, provider: 'codex', name: 'Phone footer checks',
  } });
  expect(started.status(), await started.text()).toBe(201);
  const { id } = await started.json();
  const state = async () => (await (await page.request.get(`/api/sessions/${id}`)).json());
  try {
    await expect.poll(async () => (await state()).observed_state).toBe('idle');
    await page.goto(`/sessions/${id}`);
    await expect(page.getByText('Read-write', { exact: true })).toBeVisible();
    // The real browser terminal resizes its tmux pane to phone columns before output arrives.
    const fixture = (name: string) => readFileSync(`../tests/fixtures/codex-${name}.txt`, 'utf8');
    for (const [name, expected, activity] of [
      ['question-pending', 'waiting_input', 'Codex has a question'],
      ['question-open', 'waiting_input', 'Which environment should receive the demo deployment?'],
      ['question-answered', 'active', null],
      ['error-idle', 'idle', 'Selected model is at capacity. Please try a different model.'],
    ] as const) {
      await test.step(name, async () => {
        writeFileSync(`${info().run_dir}/${id}/fake.screen`, '\x1b[2J\x1b[H' + fixture(name));
        control(id, 'screen');
        await expect.poll(async () => (await state()).observed_state, { timeout: 15_000 }).toBe(expected);
        if (activity) await expect.poll(async () => (await state()).activity).toContain(activity);
      });
    }
  } finally {
    const current = await state();
    const { socket } = JSON.parse(readFileSync(`${E2E_BASE}/processes.json`, 'utf8'));
    writeFileSync(testInfo.outputPath('raw-pane.txt'), execFileSync('tmux', ['-L', socket, 'capture-pane', '-p', '-t', `=${current.tmux_name}:`]));
    writeFileSync(testInfo.outputPath('session.json'), JSON.stringify(current));
    await page.request.post(`/api/sessions/${id}/stop`, { headers }); }
});
