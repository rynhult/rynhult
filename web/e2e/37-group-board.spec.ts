import { environmentValue } from "../../src/acc/env.mjs";
import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, info, PASSWORD, totp } from './helpers';
import { E2E_BASE } from '../playwright.config';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) {
  test(`group board launches reviewed source and manages own claims at ${width}px`, async ({ page: owner, browser }, testInfo) => {
    const suffix = crypto.randomUUID().slice(0, 8);
    const username = `board-${width}-${suffix}`;
    const account = `${username}-agent`;
    writeFileSync(`${info().state}/fixture-accounts/${account}`, '');
    const scratch = mkdtempSync(`${E2E_BASE}/board-`);
    const member = await browser.newContext({ baseURL: info().origin, viewport: { width, height: width === 390 ? 844 : 900 }, colorScheme: 'dark' });
    let sessionId: string | undefined;
    let sessionHeaders: Record<string, string> | undefined;
    try {
      const git = (...args: string[]) => execFileSync('git', ['-C', scratch, ...args]);
      git('init', '-q', '-b', 'main');
      git('config', 'user.name', 'Alex Example'); git('config', 'user.email', 'alex@example.com');
      writeFileSync(`${scratch}/README.md`, 'Reviewed example source\n');
      git('add', '.'); git('commit', '-qm', 'Reviewed source');
      const sha = git('rev-parse', 'HEAD').toString().trim();
      const pack = execFileSync('git', ['-C', scratch, 'pack-objects', '--stdout', '--revs'], { input: sha + '\n' }).toString('base64');
      await owner.goto('/groups');
      const ownerHeaders = await csrfHeaders(owner);
      const invited = await owner.request.post('/api/users/invites', { headers: ownerHeaders,
        data: { role: 'member', account } });
      expect(invited.status(), await invited.text()).toBe(201);
      const token = (await invited.json()).link.split('/').pop();
      const begin = await member.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
      expect(begin.status(), await begin.text()).toBe(200);
      const joined = await member.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin },
        data: { token, username, password: PASSWORD, code: totp((await begin.json()).totp_secret) } });
      expect(joined.status(), await joined.text()).toBe(200);
      const page = await member.newPage();
      await page.goto('/groups');
      const headers = await csrfHeaders(page);
      sessionHeaders = headers;
      const state = await (await page.request.get('/api/auth/state')).json();
      const roster = await (await owner.request.get('/api/users')).json();
      const person = roster.users.find((u: { username: string }) => u.username === state.username);
      expect(person.account).toBe(account);
      const created = await owner.request.post('/api/groups', { headers: ownerHeaders, data: { name: `Example board ${width} ${suffix}`, maintainer_id: person.id } });
      expect(created.status(), await created.text()).toBe(201);
      const group = await created.json();
      const project = await (await page.request.post(`/api/groups/${group.id}/projects`, { headers, data: { name: 'Example validation', slug: `board-example-${width}-${suffix}`, owners: [person.id], revision: 1 } })).json();
      const base = `/api/groups/${group.id}/projects/${project.id}`;
      const approved = await owner.request.post(base + '/placements', { headers: ownerHeaders, data: { user_id: person.id, revision: 2 } });
      expect(approved.status(), await approved.text()).toBe(201);
      const placement = await approved.json();
      const provision = await owner.request.post(base + `/placements/${placement.id}/provision`, { headers: ownerHeaders });
      expect(provision.status(), await provision.text()).toBe(200);
      const grant = await (await page.request.post(base + '/exchange/grants', { headers, data: { operation: 'publish', placement_id: placement.id, session_id: 'a'.repeat(12), old_sha: '0'.repeat(40), new_sha: sha } })).json();
      expect((await page.request.post(base + '/exchange/transfer', { headers: { Authorization: `Bearer ${grant.token}` }, data: { data: pack } })).status()).toBe(200);
      await page.reload();
      await page.getByRole('article', { name: group.name, exact: true }).getByRole('button', { name: 'Work board', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: `${group.name} · Work board`, exact: true });
      await expect(dialog.getByText('No group sessions yet.', { exact: false })).toBeVisible();
      await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('launch');
      await dialog.getByRole('combobox', { name: 'Group project', exact: true }).selectOption(String(project.id));
      await dialog.getByRole('combobox', { name: 'Your private checkout', exact: true }).selectOption(String(placement.id));
      await dialog.getByRole('combobox', { name: 'Reviewed published base', exact: true }).selectOption(sha);
      await dialog.getByLabel('Group-safe task', { exact: true }).fill('Review validation');
      await dialog.getByLabel('Private opening prompt', { exact: true }).fill('private-board-canary');
      await dialog.getByRole('checkbox').check();
      await expectAxeClean(page, '[role="dialog"]');
      if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1') await page.screenshot({ path: `../docs/wiki/.attachments/groups-launch-${width}.png`, fullPage: true });
      const launched = page.waitForResponse(response => response.url().endsWith(base + '/sessions') && response.request().method() === 'POST');
      await dialog.getByRole('button', { name: 'Launch group session', exact: true }).click();
      const response = await launched;
      expect(response.status(), await response.text()).toBe(201);
      sessionId = (await response.json()).id;
      await expect(dialog.getByText('Session launched.', { exact: false })).toBeVisible();
      await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('board');
      const row = dialog.getByRole('article', { name: /Work by .*: Review validation/ });
      await expect(row).toBeVisible();
      await expect(dialog).not.toContainText('private-board-canary');
      await row.getByLabel('Claim relative path', { exact: true }).fill('src/api/');
      await row.getByRole('button', { name: 'Claim path', exact: true }).click();
      await expect(row.getByText('src/api/', { exact: true })).toBeVisible();
      await row.getByRole('button', { name: 'Refresh context', exact: true }).click();
      await expect(row.getByRole('region', { name: 'Refreshed board context' })).toContainText('Review validation');
      await expectAxeClean(page, '[role="dialog"]');
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
      await page.screenshot({ path: environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1' ? `../docs/wiki/.attachments/groups-board-${width}.png` : testInfo.outputPath(`board-${width}.png`), fullPage: true });
      await row.getByRole('button', { name: 'Release claim', exact: true }).click();
      await expect(row.getByRole('button', { name: 'Release claim', exact: true })).toHaveCount(0);
      await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('settings');
      await expect(dialog).toContainText('not a monetary cap');
      if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1' && width === 1440) await page.screenshot({ path: '../docs/wiki/.attachments/groups-usage.png', fullPage: true });
      const currentGroup = await (await page.request.get(`/api/groups/${group.id}`)).json();
      expect((await page.request.post(`/api/groups/${group.id}/policy`, { headers, data: { revision: currentGroup.revision, concurrent: 1, daily_launches: 1 } })).status()).toBe(200);
      const next = crypto.randomUUID().replaceAll('-', '').slice(0, 12);
      expect((await page.request.post(base + '/exchange/checkout', { headers, data: { placement_id: placement.id, session_id: next, commit: sha } })).status()).toBe(200);
      expect((await page.request.post(base + '/sessions', { headers, data: { checkout_id: next, task: 'Review follow-up', name: 'Follow-up', provider: 'claude', trust_source: true } })).status()).toBe(409);
      await dialog.getByRole('combobox', { name: 'Group view', exact: true }).selectOption('inbox');
      await expect(dialog.getByText('Launch quota reached.', { exact: false })).toBeVisible({ timeout: 15000 });
      await dialog.getByRole('combobox', { name: 'Assign operation', exact: true }).selectOption(String(person.id));
      await dialog.getByRole('button', { name: 'Save assignee', exact: true }).click();
      await expect(dialog.getByText(`Assigned to: ${state.username}`, { exact: false })).toBeVisible();
      if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1' && width === 1440) await page.screenshot({ path: '../docs/wiki/.attachments/groups-operations.png', fullPage: true });
      await expectAxeClean(page, '[role="dialog"]');
      await dialog.getByRole('button', { name: 'Resolve operation', exact: true }).click();
      await expect(dialog.getByText('Resolved', { exact: true })).toBeVisible();
      await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    } finally {
      try {
        if (sessionId) {
          expect((await member.request.post(`/api/sessions/${sessionId}/stop`, { headers: sessionHeaders })).ok()).toBe(true);
        }
      } finally {
        await member.close();
        rmSync(scratch, { recursive: true, force: true });
      }
    }
  });
}
