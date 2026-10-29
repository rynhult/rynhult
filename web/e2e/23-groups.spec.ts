import { environmentValue } from "../../src/acc/env.mjs";
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, info, totp } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owner opts in, reviews group roles and archives a group', async ({ page }) => {
  await page.goto('/groups');
  await expect(page.getByRole('heading', { name: 'Groups', exact: true })).toBeVisible();
  await page.getByLabel('Group name', { exact: true }).fill('Demo team');
  const maintainer = page.getByRole('combobox', { name: 'Initial maintainer', exact: true });
  await expect(maintainer.locator('option').filter({ hasText: /^alex$/ })).toHaveCount(1);
  await maintainer.selectOption({ label: 'alex' });
  await page.getByRole('button', { name: 'Create group', exact: true }).click();
  const group = page.getByRole('article', { name: 'Demo team' });
  await expect(group.getByText('Active', { exact: true })).toBeVisible();
  await group.getByRole('button', { name: 'Manage people', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Demo team · People' });
  await expect(dialog.getByText('alex', { exact: true })).toBeVisible();
  await expect(dialog.getByLabel('Role for alex')).toHaveValue('maintainer');
  expect((await new AxeBuilder({ page }).include('[role="dialog"]').analyze()).violations).toEqual([]);
  if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1') await page.screenshot({ path: '../docs/wiki/.attachments/groups-people.png', fullPage: true });
  await dialog.getByRole('button', { name: 'Close', exact: true }).click();
  await group.getByRole('button', { name: 'Projects', exact: true }).click();
  const projects = page.getByRole('dialog', { name: 'Demo team · Projects' });
  await projects.getByLabel('Project name', { exact: true }).fill('Demo API');
  await projects.getByLabel('Project slug', { exact: true }).fill('group-demo-api');
  await projects.getByRole('combobox', { name: 'Project owner', exact: true }).selectOption({ label: 'alex' });
  await projects.getByRole('button', { name: 'Create group project', exact: true }).click();
  await expect(projects.getByText('Private checkouts', { exact: true })).toBeVisible();
  if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1') await page.screenshot({ path: '../docs/wiki/.attachments/groups-projects.png', fullPage: true });
  await projects.getByRole('button', { name: 'Close', exact: true }).click();
  expect((await new AxeBuilder({ page }).include('main').analyze()).violations).toEqual([]);
  if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1') await page.screenshot({ path: '../docs/wiki/.attachments/groups.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  if (environmentValue("ACC_UPDATE_GROUP_SHOTS") === '1') await page.screenshot({ path: '../docs/wiki/.attachments/groups-phone.png', fullPage: true });
  await group.getByRole('button', { name: 'Archive group', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Archive group', exact: true }).click();
  await expect(group.getByText('Archived', { exact: true })).toBeVisible();
});

test('a maintainer manages existing people without gaining instance privileges, then loses access on removal', async ({ page, browser }) => {
  await page.goto('/groups');
  const csrf = (await page.context().cookies()).find(c => c.name === 'acc_csrf')!.value;
  const headers = { 'X-ACC-CSRF': csrf, Origin: info().origin };
  const roster = await (await page.request.get('/api/users')).json();
  const account = roster.accounts.find((candidate: string) => candidate !== roster.default_account
    && !roster.users.some((user: { account: string }) => user.account === candidate));
  expect(account, 'a fixture agent account is available for the new member').toBeTruthy();
  const invited = await page.request.post('/api/users/invites', { headers, data: { role: 'member', account } });
  expect(invited.status(), await invited.text()).toBe(201);
  const token = (await invited.json()).link.split('/').pop();
  const guest = await browser.newContext({ baseURL: info().origin });
  try {
    const begin = await guest.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    const joined = await guest.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin },
      data: { token, username: 'samgroup', password: 'artificial group password', code: totp((await begin.json()).totp_secret) } });
    expect(joined.status()).toBe(200);
    const users = (await (await page.request.get('/api/users')).json()).users;
    const sam = users.find((u: { username: string }) => u.username === 'samgroup');
    const alex = users.find((u: { username: string }) => u.username === 'alex');
    const created = await page.request.post('/api/groups', { headers, data: { name: 'Example maintainers', maintainer_id: sam.id } });
    expect(created.status()).toBe(201);
    const group = await created.json();
    const teammate = await guest.newPage();
    await teammate.goto('/groups');
    await expect(teammate.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Groups', exact: true })).toBeVisible();
    await expect(teammate.getByRole('button', { name: 'Create group', exact: true })).toHaveCount(0);
    const row = teammate.getByRole('article', { name: group.name });
    await row.getByRole('button', { name: 'Manage people', exact: true }).click();
    const people = teammate.getByRole('dialog', { name: `${group.name} · People` });
    const alexRow = people.getByRole('form', { name: 'Membership for alex', exact: true });
    await alexRow.getByRole('button', { name: 'Add person', exact: true }).click();
    await expect(alexRow.getByRole('button', { name: 'Save role', exact: true })).toBeVisible();
    await people.getByRole('button', { name: 'Close', exact: true }).click();
    expect((await guest.request.get('/api/users')).status()).toBe(403);
    expect((await guest.request.get('/api/audit')).status()).toBe(403);
    let revision = (await (await page.request.get(`/api/groups/${group.id}`)).json()).revision;
    expect((await page.request.post(`/api/groups/${group.id}/members/${alex.id}`, { headers, data: { revision, role: 'maintainer' } })).status()).toBe(200);
    revision = (await (await page.request.get(`/api/groups/${group.id}`)).json()).revision;
    expect((await page.request.post(`/api/groups/${group.id}/members/${sam.id}`, { headers, data: { revision, role: 'remove' } })).status()).toBe(200);
    await teammate.reload();
    await expect(teammate.getByRole('article', { name: group.name })).toHaveCount(0);
    expect((await guest.request.get(`/api/groups/${group.id}`)).status()).toBe(404);
  } finally {
    await guest.close();
  }
});
