import { environmentValue } from '../../src/acc/env.mjs';
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { expect, test } from './fixtures';
import { AUTH_FILE, control, csrfHeaders, info, sessionWithChanges } from './helpers';

test.use({ storageState: AUTH_FILE });
test('OSC output changes the status chip without an approval', async ({ page }) => {
  const id = await sessionWithChanges(page, 'osc-status');
  try {
    await expect.poll(async () => (await (await page.request.get(`/api/sessions/${id}`)).json()).status_source).toBe('claude_hook');
    // Model a hookless CLI in this test-owned session, after fixture startup settles.
    execFileSync(environmentValue('ACC_E2E_PYTHON') ?? 'python3', ['-c',
      'import sqlite3,sys\nwith sqlite3.connect(sys.argv[1]) as db:\n db.execute("delete from event where session_id=?", (sys.argv[2],))\n db.execute("update session set status_source=\'supervisor\' where id=?", (sys.argv[2],))',
      `${info().state}/data/acc.db`, id]);
    await page.goto(`/sessions/${id}`);
    const chip = page.locator('[title^="Source: script status (OSC 7501, unverified)"]');
    for (const [state, label] of [['working', 'Working'], ['blocked', 'Needs you'], ['error', 'Idle'], ['done', 'Idle']]) {
      writeFileSync(`${info().run_dir}/${id}/fake.screen`, `\x1b]7501;state=${state}: app=example\x07`);
      control(id, 'screen');
      await expect(chip.first()).toHaveText(label, { timeout: 15000 });
      if (state === 'error') await expect(page.getByText('Script reported an error (OSC 7501)', { exact: true }).first()).toBeVisible();
      if (state === 'done') await expect(page.getByText('Script reported an error (OSC 7501)', { exact: true })).toHaveCount(0);
      const response = await page.request.get('/api/attention');
      expect(response.ok()).toBe(true);
      expect(JSON.stringify(await response.json())).not.toContain(`needs_input:${id}`);
    }
  } finally {
    expect((await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
});
