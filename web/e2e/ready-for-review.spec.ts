import { expect, test } from './fixtures';
import { AUTH_FILE, demoSessions, expectAxeClean } from './helpers';
import { groupSessions, inboxGroupOf } from '../src/lib/order';
import { categoryOf } from '../src/components/shell/inbox';
import type { AttentionItem } from '../src/lib/types';

test.use({ storageState: AUTH_FILE });

test('ready review groups and Inbox category stay separate from approvals', () => {
  const session = { ...demoSessions()[0], observed_state: 'idle' as const, ready_for_review: true };
  expect(groupSessions([session])[0].key).toBe('ready_for_review');
  expect(groupSessions([{ ...session, ready_for_review: false }])[0].key).toBe('idle');
  const item = { kind: 'ready_for_review', severity: 'info' } as AttentionItem;
  expect(inboxGroupOf(item)).toBe('ready_for_review');
  expect(categoryOf(item)).toBe('ready_for_review');
});

for (const width of [1440, 390]) {
  test(`review filters and dismissible Inbox on ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const base = demoSessions()[0];
    const sessions = [
      { ...base, id: 'review-ready', name: 'Review fixture', observed_state: 'idle', ready_for_review: true,
        pull_lifecycle: { number: 42, url: 'https://github.com/example/demo/pull/42', state: 'checks_failed' } },
      { ...base, id: 'review-working', name: 'Working fixture', observed_state: 'active', ready_for_review: false },
    ];
    await page.route(/\/api\/sessions(?:\?.*)?$/, route => route.fulfill({ json: sessions }));
    let items = [{ id: 9001, kind: 'ready_for_review', severity: 'info', verified: true, source: 'workspace changes',
      title: 'Review fixture: Ready for review', session_id: 'review-ready', session_name: 'Review fixture',
      project: base.project, detail: 'Open Changes to review.', created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(), acked_at: null, resolved_at: null, bulk_dismissible: true }];
    await page.route('**/api/attention**', async route => {
      if (route.request().method() === 'POST') {
        items = [];
        await route.fulfill({ json: { batch: 1, dismissed: 1, skipped: 0 } });
      } else await route.fulfill({ json: items });
    });
    await page.goto('/');
    await expect(page.getByRole('region', { name: 'Ready for review', exact: true })).toBeVisible();
    await page.getByLabel('Review state', { exact: true }).selectOption('ready');
    await expect(page.getByRole('main').getByRole('link', { name: /Working fixture/ })).toHaveCount(0);
    await page.getByLabel('Pull request state', { exact: true }).selectOption('checks_failed');
    await expect(page.getByText('PR #42: checks failed')).toBeVisible();
    await page.getByLabel('Pull request state', { exact: true }).selectOption('merged');
    await expect(page.getByText('PR #42: checks failed')).toHaveCount(0);
    await page.goto('/inbox?filter=ready_for_review');
    await expect(page.getByRole('heading', { name: 'Ready for review', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Dismiss all shown' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Approve/ })).toHaveCount(0);
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByRole('button', { name: 'Dismiss all shown' }).click();
    await expect(page.getByText(/1 notices dismissed/)).toBeVisible();
  });
}
