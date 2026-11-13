import { expect, test } from './fixtures';
import { AUTH_FILE } from './helpers';
import { visibleText } from '../src/lib/visibleText';
import type { AttentionItem } from '../src/lib/types';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) {
  test(`literal approval card ${width}px ${colorScheme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme });
    const command = '\u202eprintf "<img src=x onerror=alert(1)> **literal**" ' + 'long-argument'.repeat(40);
    const diff = '--- a/example\n+++ b/example\n@@ -1 +1 @@\n-before\n+<script>alert(1)</script>\n' + '+line\n'.repeat(50) + '+last-line';
    const stamp = new Date().toISOString();
    const item = { id: 901, kind: 'needs_input', severity: 'warn', source: 'claude_hook', verified: true,
      title: 'Review exact CLI request', detail: null, session_id: 'card-demo',
      parent_chain: ['lead-demo', 'card-demo'], root_owner: 'casey', created_at: stamp, updated_at: stamp,
      acked_at: null, resolved_at: null, permission_id: 'a'.repeat(32), permission_can_approve: true,
      permission_expires_at: new Date(Date.now() + 50000).toISOString(), permission_input: JSON.stringify({command}) as string | null,
      approval_card: { command, cwd: '/tmp/<example>/project', diff, label: 'No specific hint' } as NonNullable<AttentionItem['approval_card']> };
    await page.route('**/api/attention*', route => route.fulfill({ json: [item] }));
    const decisions: unknown[] = [];
    await page.route('**/api/attention/901/permission', route => {
      decisions.push(route.request().postDataJSON());
      return route.fulfill({ json: { answered: true } });
    });
    await page.goto('/inbox?item=901');
    await expect(page.getByText('Parent chain: lead-demo → card-demo · Owning person: casey', { exact: true })).toBeVisible();
    const card = page.getByRole('region', { name: 'CLI approval details' });
    await expect(card.getByLabel('CLI command', { exact: true })).toHaveText(visibleText(command));
    await expect(card.getByText('Hidden characters shown', { exact: true })).toBeVisible();
    await expect(card.getByLabel('CLI working directory')).toHaveText('/tmp/<example>/project');
    await expect(card.getByText('Label is a hint, not a check', { exact: true })).toBeVisible();
    await expect(card.getByLabel('CLI diff')).not.toContainText('+last-line');
    await card.getByRole('button', { name: /^Show all/ }).click();
    await expect(card.getByLabel('CLI diff')).toHaveText(diff);
    await expect(card.locator('img,script')).toHaveCount(0);
    expect(await card.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    expect(decisions).toEqual([{ token: 'a'.repeat(32), decision: 'allow' }]);
    item.approval_card = { command: null, cwd: null, diff: null, label: 'No specific hint', tool_name: 'Edit', file_path: '/tmp/example' };
    item.permission_input = JSON.stringify({file_path: '/tmp/example', old_string: 'before', new_string: '\u202eafter'});
    await page.reload();
    await expect(card.getByText('This tool has no command; see tool input below.', { exact: true })).toBeVisible();
    await expect(card.getByText('No unified diff supplied; review the tool input when available.', { exact: true })).toBeVisible();
    await expect(card.getByText('Tool: Edit', { exact: true })).toBeVisible();
    await expect(card.getByLabel('CLI file path')).toHaveText('/tmp/example');
    await expect(card.getByLabel('Complete tool input')).toBeVisible();
    await expect(card.getByLabel('Complete tool input')).toContainText('⟨U+202E⟩after');
    await expect(card.locator('details')).toHaveCount(0);
    item.permission_input = null;
    await page.reload();
    await expect(card.getByText('Complete tool input unavailable or too large (over 8 kB).')).toBeVisible();
    await expect(card.getByLabel('Complete tool input')).toHaveCount(0);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const malformed of [{}, { command: 5, label: {} },
      { command: null, cwd: [], diff: null, label: 'No specific hint' },
      { command: null, cwd: null, diff: null, label: 'Safe' }]) {
      item.approval_card = malformed as unknown as NonNullable<AttentionItem['approval_card']>;
      await page.reload();
      await expect(card.getByText('Approval details unavailable: invalid card data. Review the request in the terminal.')).toBeVisible();
      await expect(page.getByRole('main').getByText('Review exact CLI request', { exact: true }).first()).toBeVisible();
    }
    expect(errors).toEqual([]);
  });
}
