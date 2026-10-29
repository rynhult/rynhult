import { test, expect } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectAxeClean, verifyAgain, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
test('public git backup consent defaults off, persists, rejects stale tabs and works on a phone', async ({ page, context }, testInfo) => {
  const name = 'demo-roamer';
  await page.goto('/host');
  const headers = await csrfHeaders(page);
  expect((await page.request.post('/api/nodes', { headers, data: { name, role: 'always_on' } })).status()).toBe(201);
  const other = await context.newPage();
  const label = 'Allow git backups over the public address';
  try {
    await page.reload();
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await expect(page.getByRole('checkbox', { name: label })).not.toBeChecked();
    await other.goto('/host');
    await other.getByRole('button', { name: `Machine settings for ${name}` }).click();
    // Opting in needs a freshly verified owner sign-in (opting out doesn't).
    await verifyAgain(page);
    await page.getByRole('checkbox', { name: label }).focus();
    await page.keyboard.press('Space');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await expect(page.getByText('Public git backup allowed', { exact: true })).toBeVisible();
    const conflict = other.waitForResponse(r => r.url().endsWith(`/api/nodes/${name}/policy`));
    await other.getByRole('button', { name: 'Save', exact: true }).click();
    expect((await conflict).status()).toBe(409);
    await expect(other.getByRole('dialog').getByRole('alert')).toContainText(/changed/i);
    await page.getByRole('button', { name: `Machine settings for ${name}` }).click();
    await expect(page.getByRole('checkbox', { name: label })).toBeChecked();
    await expectAxeClean(page, '[role="dialog"]');
    await page.screenshot({ path: wikiShotPath(testInfo, 'nodes-public-git-backup.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('checkbox', { name: label }).scrollIntoViewIfNeeded();
    await expectAxeClean(page, '[role="dialog"]');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: wikiShotPath(testInfo, 'nodes-public-git-backup-phone.png') });
    await page.getByRole('checkbox', { name: label }).focus();
    await page.keyboard.press('Space');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    const nodes = await (await page.request.get('/api/nodes')).json();
    expect(nodes.find((n: { name: string }) => n.name === name).public_git_backup).toBe(false);
  } finally {
    await other.close();
    expect((await page.request.post(`/api/nodes/${name}/remove`, { headers })).ok()).toBe(true);
  }
});
