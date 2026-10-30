import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, gitProject, info, wikiShotPath } from './helpers';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) test(`Document proposal review and stale-base merge at ${width}px`, async ({ page }, ti) => {
  await page.setViewportSize({ width, height: 900 });
  const { id, headers } = await gitProject(page, `proposal-${width}`);
  const url = `/api/projects/${id}/brief`;
  const docs = await page.request.post(url + '/documents', { headers, data: { files: [{ name: 'scope.txt', data: Buffer.from('Build a room board with calm colours').toString('base64') }] } });
  expect(docs.ok()).toBeTruthy();
  const ref = (await docs.json()).documents[0].reference_id;
  const launch = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Proposal lead', worktree: true,
    orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 1, total: 2, depth: 1, hours: 1 } } });
  expect(launch.ok(), await launch.text()).toBeTruthy();
  const lead = (await launch.json()).id;
  const request = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${lead}/events.jsonl`, JSON.stringify({ acc_session: lead, hook: { hook_event_name: 'AccTeam', request: {
    id: request, action: 'brief_propose_documents', base_version: 0, fields: { product: 'Room board', design: 'Calm colours' },
    citations: { product: [{ reference: ref, location: 'section 1' }], design: [{ reference: ref, location: 'section 1' }] },
  } } }) + '\n');
  await expect.poll(() => existsSync(`${info().run_dir}/${lead}/team/${request}.json`)).toBeTruthy();
  expect(JSON.parse(readFileSync(`${info().run_dir}/${lead}/team/${request}.json`, 'utf8')).ok).toBeTruthy();
  try {
    await page.goto(`/projects/${id}#project-brief`);
    await expect(page.getByText('A brief drafted from your documents is ready to review', { exact: true })).toBeVisible();
    await page.locator('[name="brief-product"]').fill('My unsaved idea');
    const warning = page.getByRole('dialog', { name: 'There is a proposed brief from your documents; review it first?' });
    await expect(warning).toBeVisible();
    await warning.getByRole('button', { name: 'Review', exact: true }).click();
    const review = page.getByRole('dialog', { name: 'Review proposed brief · version 1' });
    await expect(review).toBeVisible();
    await expect(review.getByText(/scope.txt.*section 1/).first()).toBeVisible();
    await review.getByRole('button', { name: 'Close', exact: true }).click();
    await page.locator('[name="brief-product"]').fill('My second draft');
    await expect(warning).toHaveCount(0);
    // Reproduce the owner case: a newer accepted version while proposal v1 still has base 0.
    const saved = await page.request.post(url, { headers, data: { version: 0, fields: { product: 'Current one line', constraints: 'Keep privacy', run_mode: 'prepare' } } });
    expect(saved.ok()).toBeTruthy();
    await page.getByRole('button', { name: 'Discard draft', exact: true }).click();
    await page.goto(`/sessions/${lead}?tab=team`);
    await expect(page.getByRole('link', { name: 'Review proposed brief version 1', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Project brief', exact: true }).click();
    await expect(page.getByText(/Proposed versions are not selectable as saved scope/)).toBeVisible();
    await expect(page.getByLabel('Project brief version').locator('option[value="1"]')).toHaveCount(0);
    // Proposed v1 is intentionally not a pinnable saved scope; the review action explains it.
    await page.getByRole('link', { name: 'Review proposed brief version 1', exact: true }).click();
    await expect(review).toBeVisible();
    await expect(review.getByText(/base version 0.*current version 2/)).toBeVisible();
    await review.getByRole('button', { name: 'Accept Product or feature', exact: true }).click();
    await review.getByRole('button', { name: 'Edit Design direction', exact: true }).click();
    await review.getByLabel('Edit Design direction').fill('Calm blue');
    await review.getByRole('button', { name: 'Skip References', exact: true }).click();
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await review.screenshot({ path: wikiShotPath(ti, `brief-proposal-review-${width}.png`) });
    await review.getByRole('button', { name: 'Save reviewed fields', exact: true }).click();
    await expect(review).toHaveCount(0);
    await expect.poll(async () => (await (await page.request.get(url)).json()).current.submitted.design).toBe('Calm blue');
    const current = (await (await page.request.get(url)).json()).current;
    expect(current.submitted).toEqual({ product: 'Room board', design: 'Calm blue', constraints: 'Keep privacy', run_mode: 'prepare' });
    const card = await page.request.post(`/api/teams/${lead}/worklist/items`, { headers, data: { kind: 'decision', title: 'Review proposed scope',
      steps: `Review: /projects/${id}?brief_review=1#project-brief`, options: [{ id: 'review', label: 'Review' }, { id: 'defer', label: 'Defer' }],
      recommendation: { option: 'review', reason: 'Read the source citations first' } } });
    expect(card.status()).toBe(201);
    await page.goto(`/sessions/${lead}?tab=team#worklist`);
    await page.getByRole('link', { name: 'Review proposed brief', exact: true }).click();
    await expect(review).toBeVisible();
    await review.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByText('Version history', { exact: true }).click();
    await page.getByRole('button', { name: 'Review proposed version 1', exact: true }).click();
    await expect(review).toBeVisible();
    await review.getByRole('button', { name: 'Accept all', exact: true }).click();
    await expect(review).toHaveCount(0);
    expect((await (await page.request.get(url)).json()).current.submitted.constraints).toBe('Keep privacy');
  } finally { await page.request.post(`/api/sessions/${lead}/team/stop`, { headers }); }
});

test('Save warns once and a concurrently changed review requires an explicit rebase', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'proposal-rebase');
  const url = `/api/projects/${id}/brief`;
  expect((await page.request.post(url, { headers, data: { version: 0, fields: { product: 'Current', constraints: 'Keep privacy' } } })).ok()).toBeTruthy();
  await page.goto(`/projects/${id}#project-brief`);
  await expect(page.getByText('Saved as version 1', { exact: true })).toBeVisible();
  const proposed = await page.request.post(url + '/import', { headers, data: { markdown: '# Proposal', base_version: 1 } });
  expect(proposed.ok()).toBeTruthy();
  await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
  const warning = page.getByRole('dialog', { name: 'There is a proposed brief from your documents; review it first?' });
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Continue', exact: true }).press('Enter');
  await expect(page.getByText('Saved as version 3', { exact: true })).toBeVisible();
  await page.getByText('Version history', { exact: true }).click();
  await page.getByRole('button', { name: 'Review proposed version 2', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review proposed brief · version 2' });
  await expect(review).toBeVisible();
  await review.getByRole('button', { name: 'Accept Product or feature', exact: true }).press('Enter');
  expect((await page.request.post(url, { headers, data: { version: 3, fields: { product: 'Concurrent', constraints: 'New privacy requirement' } } })).ok()).toBeTruthy();
  await expect(review.getByRole('button', { name: 'Rebase onto current', exact: true })).toBeVisible({ timeout: 15000 });
  await expect(review.getByRole('button', { name: 'Save reviewed fields', exact: true })).toBeDisabled();
  await review.getByRole('button', { name: 'Rebase onto current', exact: true }).press('Enter');
  await review.getByRole('button', { name: 'Save reviewed fields', exact: true }).press('Enter');
  await expect(review).toHaveCount(0);
  expect((await (await page.request.get(url)).json()).current.submitted.constraints).toBe('New privacy requirement');
});
