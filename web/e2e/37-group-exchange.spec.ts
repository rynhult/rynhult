import { environmentValue } from "../../src/acc/env.mjs";
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, info } from './helpers';
import { E2E_BASE } from '../playwright.config';

test.use({ storageState: AUTH_FILE });

test('published Files and Wiki display committed text, with accessible phone controls', async ({ page }, testInfo) => {
  const scratch = mkdtempSync(`${E2E_BASE}/exchange-`);
  try {
    const git = (...args: string[]) => execFileSync('git', ['-C', scratch, ...args]);
    git('init', '-q', '-b', 'main');
    git('config', 'user.name', 'Alex Example'); git('config', 'user.email', 'alex@example.com');
    mkdirSync(`${scratch}/docs/wiki`, { recursive: true });
    writeFileSync(`${scratch}/README.md`, 'Published example code\n');
    writeFileSync(`${scratch}/docs/wiki/Home.md`, '# Example guide\nPublished instructions.\n');
    git('add', '.'); git('commit', '-qm', 'Publish example');
    const sha = git('rev-parse', 'HEAD').toString().trim();
    const pack = execFileSync('git', ['-C', scratch, 'pack-objects', '--stdout', '--revs'], { input: sha + '\n' }).toString('base64');
    writeFileSync(`${scratch}/README.md`, 'Unpublished dirty canary');
    await page.goto('/groups');
    const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
    const headers = { 'X-ACC-CSRF': csrf, Origin: info().origin };
    const state = await (await page.request.get('/api/auth/state')).json();
    const roster = await (await page.request.get('/api/users')).json();
    const person = roster.users.find((u: { username: string }) => u.username === state.username);
    const group = await (await page.request.post('/api/groups', { headers, data: { name: 'Published example team', maintainer_id: person.id } })).json();
    const project = await (await page.request.post(`/api/groups/${group.id}/projects`, { headers, data: { name: 'Example source', slug: 'exchange-example', owners: [person.id], revision: 1 } })).json();
    const base = `/api/groups/${group.id}/projects/${project.id}`;
    const placement = await (await page.request.post(base + '/placements', { headers, data: { user_id: person.id, revision: 2 } })).json();
    const grant = await (await page.request.post(base + '/exchange/grants', { headers, data: {
      operation: 'publish', placement_id: placement.id, session_id: 'a'.repeat(12), old_sha: '0'.repeat(40), new_sha: sha,
    } })).json();
    const published = await page.request.post(base + '/exchange/transfer', { headers: { Authorization: `Bearer ${grant.token}` }, data: { data: pack } });
    expect(published.status(), await published.text()).toBe(200);
    expect((await page.request.post(base + '/exchange/tags', { headers, data: { name: 'v1', commit: sha } })).status()).toBe(201);
    await page.reload();
    await page.getByRole('article', { name: 'Published example team' }).getByRole('button', { name: 'Projects', exact: true }).click();
    await page.getByRole('button', { name: 'Published Files & Wiki', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Example source · Published' });
    await dialog.getByLabel('Published branch or tag').selectOption({ label: `v1 · ${sha.slice(0, 12)}` });
    await expect(dialog.getByLabel('Published branch or tag').locator('option:checked')).toHaveText(`v1 · ${sha.slice(0, 12)}`);
    await dialog.getByLabel('Published file').selectOption('README.md');
    await expect(dialog.getByRole('region', { name: 'Published text' })).toHaveText('Published example code\n');
    await expect(dialog).not.toContainText('Unpublished dirty canary');
    await dialog.getByRole('combobox', { name: 'Compare with published base', exact: true }).selectOption(sha);
    await expect(dialog.getByRole('region', { name: 'Published diff', exact: true })).toContainText('No changes between these published commits.');
    await dialog.getByLabel('View published content').selectOption('wiki');
    await dialog.getByLabel('Published file').selectOption('docs/wiki/Home.md');
    await expect(dialog.getByRole('region', { name: 'Published text' })).toContainText('Published instructions.');
    expect((await new AxeBuilder({ page }).include('[role="dialog"]').analyze()).violations).toEqual([]);
    await page.screenshot({ path: environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1'
      ? '../docs/wiki/.attachments/groups-published.png' : testInfo.outputPath('group-published.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Published example team · Projects' })).toBeVisible();
  } finally { rmSync(scratch, { recursive: true, force: true }); }
});
