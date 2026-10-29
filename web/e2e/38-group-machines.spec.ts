import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean } from './helpers';
import { E2E_BASE } from '../playwright.config';
import { environmentValue } from '../../src/acc/env.mjs';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) {
  test(`own-machine approval and group launch at ${width}px`, async ({ page }, testInfo) => {
    test.skip(environmentValue('ACC_GROUPS_REMOTE') !== '1', 'explicit own-machine qualification run');
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/groups');
    const headers = await csrfHeaders(page);
    const roster = await (await page.request.get('/api/users')).json();
    const state = await (await page.request.get('/api/auth/state')).json();
    const person = roster.users.find((row: { username: string }) => row.username === state.username);
    const group = await (await page.request.post('/api/groups', { headers, data: { name: `Own machine ${width}`, maintainer_id: person.id } })).json();
    const project = await (await page.request.post(`/api/groups/${group.id}/projects`, { headers, data: { name: 'Own-machine review', slug: `own-machine-${width}`, owners: [person.id], revision: 1 } })).json();
    const base = `/api/groups/${group.id}/projects/${project.id}`;
    // Independent node/account per viewport; node IDs come from the enrolled demo registry.
    const host = width === 1440 ? 'lab-box' : 'studio-mac';
    const node = (await (await page.request.get('/api/nodes')).json()).find((row: { name: string }) => row.name === host);
    await page.reload();
    await page.getByRole('article', { name: group.name, exact: true }).getByRole('button', { name: 'Work board', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: `${group.name} · Work board`, exact: true });
    await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('launch');
    await dialog.getByRole('combobox', { name: 'Group project', exact: true }).selectOption(String(project.id));
    const machines = dialog.getByRole('region', { name: 'Own-machine collaboration' });
    await machines.getByLabel('Enrolled machine ID', { exact: true }).fill(node.id);
    await machines.getByLabel('Your broker account', { exact: true }).fill(node.accounts[0]);
    const requested = page.waitForResponse(response => response.url().endsWith('/api/group-machines') && response.request().method() === 'POST');
    await machines.getByRole('button', { name: 'Request machine account', exact: true }).click();
    const binding = await (await requested).json();
    const approve = machines.getByRole('button', { name: 'Approve machine account', exact: true });
    await expect(approve).toBeDisabled();
    await expect(machines.getByText(/Unverified/)).toBeVisible();
    expect((await page.request.post(`/api/group-machines/${binding.id}/approve`, { headers })).status()).toBe(409);
    const fixture = JSON.parse(readFileSync(`${E2E_BASE}/info.json`, 'utf8'));
    expect(fixture.run_dir.startsWith(E2E_BASE + '/')).toBe(true);
    // Demo nodes share one test broker. Drop only the previous viewport's consumed challenge.
    const code = execFileSync(environmentValue('ACC_E2E_PYTHON')!, ['-c', `
import json, sys
from pathlib import Path
from acc.node.group_proof import issue
root = Path(sys.argv[1])
state = root / '.group-proof.json'
if state.exists():
    assert json.loads(state.read_text())['used']
    state.unlink()
print(issue(root, sys.argv[2], sys.argv[3], sys.argv[4]))
`, fixture.run_dir, node.id, node.accounts[0], binding.id], { cwd: '..', env: { ...process.env, PYTHONPATH: 'src' } }).toString().trim();
    await machines.getByLabel(`Confirmation code for ${host} · ${node.accounts[0]}`, { exact: true }).fill(code);
    await machines.getByRole('button', { name: 'Verify on the machine', exact: true }).click();
    await expect(machines.getByText(/Verified on the machine/)).toBeVisible();
    await expect(approve).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath(`account-proof-${width}.png`), fullPage: true });
    await approve.click();
    await machines.getByRole('combobox', { name: 'Your approved machine account', exact: true }).selectOption({ label: `${host} · ${node.accounts[0]}` });
    await machines.getByRole('button', { name: 'Request project placement', exact: true }).click();
    await machines.getByRole('button', { name: 'Approve project placement', exact: true }).click();
    const placements = await (await page.request.get(base + '/placements')).json();
    const placement = placements.find((row: { host: string }) => row.host === host);
    await expect(dialog.getByRole('combobox', { name: 'Your private checkout', exact: true }).locator('option', { hasText: host })).toHaveCount(1);
    const scratch = mkdtempSync(`${E2E_BASE}/remote-`);
    try {
      const git = (...args: string[]) => execFileSync('git', ['-C', scratch, ...args]);
      git('init', '-q', '-b', 'main'); git('config', 'user.name', 'Alex Example'); git('config', 'user.email', 'alex@example.com');
      writeFileSync(`${scratch}/README.md`, 'Reviewed example source\n'); git('add', '.'); git('commit', '-qm', 'Reviewed source');
      const sha = git('rev-parse', 'HEAD').toString().trim();
      const pack = execFileSync('git', ['-C', scratch, 'pack-objects', '--stdout', '--revs'], { input: sha + '\n' }).toString('base64');
      const grant = await (await page.request.post(base + '/exchange/grants', { headers, data: { operation: 'publish', placement_id: placement.id, session_id: 'a'.repeat(12), old_sha: '0'.repeat(40), new_sha: sha } })).json();
      expect((await page.request.post(base + '/exchange/transfer', { headers: { Authorization: `Bearer ${grant.token}` }, data: { data: pack } })).status()).toBe(200);
      // Reopen to fetch the newly published ref.
      await page.goto('/groups');
      await page.getByRole('article', { name: group.name, exact: true }).getByRole('button', { name: 'Work board', exact: true }).click();
      await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('launch');
      await dialog.getByRole('combobox', { name: 'Group project', exact: true }).selectOption(String(project.id));
      await dialog.getByRole('combobox', { name: 'Your private checkout', exact: true }).selectOption(String(placement.id));
      await dialog.getByRole('combobox', { name: 'Reviewed published base', exact: true }).selectOption(sha);
      await dialog.getByLabel('Group-safe task', { exact: true }).fill('Review own-machine validation');
      await dialog.getByRole('checkbox').check();
      await dialog.getByRole('button', { name: 'Launch group session', exact: true }).click();
      await expect(dialog.getByRole('link', { name: 'Open session', exact: true })).toBeVisible();
      await expectAxeClean(page, '[role="dialog"]');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`own-machine-${width}.png`), fullPage: true });
    } finally { rmSync(scratch, { recursive: true, force: true }); }
  });
}
