import { wikiShotPath } from "./helpers";
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { AUTH_FILE, info } from './helpers';

test.use({ storageState: AUTH_FILE });

test('owner previews and restores a builtin, then all, preserving custom content', async ({ page }, testInfo) => {
  await page.goto('/catalog');
  const cookies = await page.context().cookies();
  const csrf = cookies.find((c) => c.name === 'acc_csrf')!.value;
  const added = await page.request.post('/api/catalog/skills', {
    headers: { 'X-ACC-CSRF': csrf, Origin: info().origin },
    data: { id: 'restore-custom', content: '---\nname: Personal example\ndescription: Custom instructions\n---\nKeep this skill.\n', profiles: [] },
  });
  expect(added.status()).toBe(200);
  const beforeProfiles = (await added.json()).profiles;
  await page.getByRole('button', { name: 'Restore built-ins', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Source', exact: true }).selectOption('installed');
  await dialog.getByRole('button', { name: 'Preview', exact: true }).dblclick();
  await expect(dialog.getByText('skill/frontend-design', { exact: true })).toBeVisible();
  await expect(dialog.getByText('unchanged', { exact: true }).first()).toBeVisible();
  const a11y = await new AxeBuilder({ page }).include('[role="dialog"]').withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(a11y.violations).toEqual([]);
  await dialog.screenshot({ path: wikiShotPath(testInfo, 'catalog-restore-preview.png') });
  await dialog.getByRole('button', { name: 'Restore skill/frontend-design', exact: true }).dblclick();
  await expect(dialog.getByRole('status')).toContainText('Restored 1');
  await dialog.getByRole('button', { name: 'Preview', exact: true }).dblclick();
  await dialog.getByLabel('Accept flagged risks').check();
  await dialog.getByRole('button', { name: 'Restore all', exact: true }).dblclick();
  await expect(dialog.getByRole('status')).toContainText('Restored');
  const after = await (await page.request.get('/api/catalog')).json();
  expect(after.skills.find((item: { id: string }) => item.id === 'restore-custom').origin).toBe('local');
  expect(after.profiles).toEqual(beforeProfiles);
  await dialog.getByRole('button', { name: 'Close', exact: true }).last().click();
  await page.getByRole('tab', { name: /Skills/ }).click();
  await expect(page.getByText('restored built-in', { exact: true }).first()).toBeVisible();
});

test('release preview renders text diff, collision choice, refusal and errors', async ({ page }) => {
  await page.route('**/api/catalog/restore/preview', route => route.fulfill({ json: {
    token: 'preview', source: 'latest', version: '9.0.0', items: [
      { key: 'skill/demo', id: 'demo', kind: 'skill', status: 'changed', collision: true, refused: '',
        diff: '--- current/SKILL.md\n+++ 9.0.0/SKILL.md\n-Old\n+<script>New</script>', flags: [], excluded_profiles: ['frontend'] },
      { key: 'skill/future', id: 'future', kind: 'skill', status: 'new', collision: false,
        refused: 'unknown provider; update Rynhult', diff: '', flags: [], excluded_profiles: [] },
    ],
  } }));
  await page.route('**/api/catalog/restore/apply', route => route.fulfill({ status: 409, json: { detail: 'catalog changed; preview again' } }));
  await page.goto('/catalog');
  await page.getByRole('button', { name: 'Restore built-ins', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Source', exact: true }).selectOption('latest');
  await dialog.getByRole('button', { name: 'Preview', exact: true }).dblclick();
  await expect(dialog).toContainText('9.0.0');
  await expect(dialog).toContainText('unknown provider');
  await expect(dialog.getByRole('button', { name: 'Restore skill/demo', exact: true })).toBeDisabled();
  await dialog.getByLabel('Alternate ID for skill/demo').fill('builtin-demo');
  await dialog.getByText('View diff', { exact: true }).click();
  await expect(dialog.locator('pre')).toContainText('+<script>New</script>');
  await expect(dialog.getByRole('region', {name:'Restore diff'})).toHaveAttribute('tabindex','0');
  await dialog.getByRole('button', { name: 'Restore skill/demo', exact: true }).click();
  await expect(dialog).toContainText('catalog changed; preview again');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});
