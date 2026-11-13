import { expect, test } from './fixtures';
import { AUTH_FILE, csrfHeaders, sessionWithChanges } from './helpers';
import type { PullStatus } from '../src/lib/types';

test.use({ storageState: AUTH_FILE, _drainRoutes: true });

for (const width of [1440, 390]) {
  test(`guided make it mergeable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const sid = await sessionWithChanges(page, 'mergeable');
    const root = `/api/sessions/${sid}`;
    const actions: string[] = [];
    const status: PullStatus = {
      provider: 'github', repo: 'example/shop', repo_url: 'https://github.com/example/shop', branch: 'feature',
      default_branch: 'main', pushed: true, signed_in: 'token', threads: 0,
      pull: { number: 42, title: 'Session changes', body: 'Ready for a person to review.', url: 'https://github.com/example/shop/pull/42',
        state: 'open', draft: false, base: 'main', head_sha: 'a'.repeat(40), mergeable: true, mergeable_state: 'clean', reviewers: [], author: 'example' },
      checks: [{ name: 'test', kind: 'check', status: 'completed', conclusion: 'failure', url: null }],
      merge: { methods: ['squash'], blockers: ["Required check 'test' failure."], notes: [], required: ['test'], allowed: false },
    };
    await page.route(`**${root}`, async route => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), repo: 'example/shop' } });
    });
    await page.route(`**${root}/pull**`, async route => {
      const path = new URL(route.request().url()).pathname.slice(`${root}/pull`.length);
      if (route.request().method() === 'POST') {
        actions.push(path);
        if (path === '/mergeable/completed-rebase') expect(route.request().postDataJSON()).toEqual({ sha: 'b'.repeat(40), confirm: true });
        if (path === '/mergeable/base') expect(route.request().postDataJSON()).toEqual({ mode: width === 390 ? 'merge' : 'rebase' });
      }
      const json = path === '' ? status : path === '/mergeable' ? { behind: 2, base: 'main', automatic: false, mode: width === 390 ? 'merge' : 'rebase', force_push_blocked: width === 390, head: 'b'.repeat(40), conflicts: [], summary: { before_count: 3, after_count: 2, subjects: ['Fix login conflict', 'Keep session settings'], omitted: 0 } }
        : path === '/mergeable/base' ? { ok: false, mode: width === 390 ? 'merge' : 'rebase', conflicts: ['src/example.ts'], output: 'Conflict in src/example.ts', finish_instructions: width === 1440 ? 'Resolve files, git rebase --continue, then use the displayed --force-with-lease command.' : undefined }
        : path === '/mergeable/completed-rebase' ? { ok: true, output: 'Pushed completed rebase through hardened transport' }
        : path === '/mergeable/rerun' ? { runs: [12] }
        : path === '/mergeable/feedback' ? { id: 'preview', text: 'Untrusted CI output: bounded failure detail', items: 1, sent: false, refused: 'Agent sign-in changed' }
        : { detail: 'Unexpected test request' };
      await route.fulfill({ json });
    });
    try {
      await page.goto(`/sessions/${sid}?tab=pull`);
      await page.getByRole('button', { name: 'Make it mergeable', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: 'Make it mergeable', exact: true });
      await expect(dialog.getByText('2 commits behind main.', { exact: false })).toBeVisible();
      if (width === 390) {
        await expect(dialog.getByText(/Block force pushes is on/)).toBeVisible();
        await expect(dialog.getByRole('button', { name: 'Rebase', exact: true })).toHaveCount(0);
      }
      await dialog.getByRole('button', { name: width === 390 ? 'Merge base in' : 'Rebase', exact: true }).click();
      await expect(dialog.getByLabel('Conflicted files')).toContainText('src/example.ts');
      await expect(dialog.getByRole('button', { name: 'Ask the agent to resolve' })).toBeVisible();
      if (width === 1440) {
        await expect(dialog.getByRole('note')).toContainText('git rebase --continue');
        await dialog.getByRole('button', { name: 'Push completed rebase', exact: true }).click();
        const consent = page.getByRole('dialog', { name: 'Push completed rebase?', exact: true });
        await expect(consent).toContainText('bbbbbbb');
        await expect(consent).toContainText('Commits before / after: 3 / 2');
        await expect(consent.getByLabel('Rebased commit subjects')).toContainText('Fix login conflict');
        await expect(consent.getByLabel('Rebased commit subjects')).toContainText('Keep session settings');
        await consent.getByRole('button', { name: 'Push with --force-with-lease', exact: true }).click();
        await expect(dialog.getByText('Pushed completed rebase through hardened transport', { exact: false })).toBeVisible();
      } else {
        await expect(dialog.getByRole('button', { name: 'Push completed rebase', exact: true })).toBeDisabled();
      }
      await dialog.getByRole('button', { name: 'Rerun failed CI checks' }).click();
      await expect(dialog.getByText(/Requested 1 CI reruns/)).toBeVisible();
      await dialog.getByRole('button', { name: 'Send to agent', exact: true }).click();
      await expect(dialog.getByLabel('Guided failure preview')).toContainText('bounded failure detail');
      await expect(dialog.getByText(/Automatic send refused: Agent sign-in changed/)).toBeVisible();
      await expect(dialog.getByRole('button', { name: 'Copy message', exact: true })).toBeVisible();
      expect(actions).toEqual(['/mergeable/base', ...(width === 1440 ? ['/mergeable/completed-rebase'] : []), '/mergeable/rerun', '/mergeable/feedback']);
      expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
      await dialog.getByRole('button', { name: 'Review merge decision' }).click();
      await expect(page.getByRole('button', { name: 'Squash and merge', exact: true })).toBeDisabled();
      expect(actions).not.toContain('/merge');
    } finally {
      await page.request.post(`${root}/stop`, { headers: await csrfHeaders(page) });
    }
  });
}
