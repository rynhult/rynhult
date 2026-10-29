import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';
test.use({ storageState: AUTH_FILE });

test('qualification is panel information, while pending cleanup still needs action', async ({ page }) => {
  let pending: string[] = [];
  await page.route('**/api/admin-features', route => route.fulfill({ json: { revision: 1, cleanup_pending: pending, features: [
    { id: 'containers', label: 'Containers per session', description: 'Optional isolation', enabled: false,
      preview: true, sensitive: true, locked: false, qualified: false },
  ] } }));
  await page.goto('/settings#password');
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention.getByText('Needs qualification', { exact: true })).toBeHidden();
  await expect(attention.getByText('Needs verification', { exact: true })).toBeHidden();
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('admin-features');
  await expect(page.locator('#admin-features').getByText('Needs qualification', { exact: true })).toBeVisible();
  await expect(page.locator('#admin-features').getByRole('region', { name: 'Previews', exact: true }).getByText('Needs verification', { exact: true })).toBeVisible();
  pending = ['Container cleanup needs your attention.'];
  await page.reload();
  await expect(page.locator('#admin-features').getByRole('alert')).toContainText(pending[0]);
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('password');
  await expect(attention).toContainText(pending[0]);
  const link = attention.getByRole('link', { name: 'Admin features', exact: true });
  await expect(link).toHaveCSS('text-decoration-line', 'none');
  await expect(link).toHaveClass(/border/);
  await link.click();
  await expect(page).toHaveURL(/#admin-features$/);
  await expectAxeClean(page);
});

test('an Updates error stays in its own panel and the attention link opens it', async ({ page }) => {
  await page.route('**/api/updates', route => route.fulfill({ status: 503, json: { detail: 'Update check unavailable: retry in Updates.' } }));
  await page.goto('/settings#password');
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention).toContainText("That service isn't available right now. Try again in a moment.");
  await expect(page.locator('#updates')).toBeHidden();
  const link = attention.getByRole('link', { name: 'Updates', exact: true });
  await expect(link).toHaveClass(/border/);
  await link.click();
  await expect(page.locator('#updates')).toBeVisible();
  await expect(page.locator('#updates')).toContainText("That service isn't available right now. Try again in a moment.");
  await page.getByRole('combobox', { name: 'Settings section', exact: true }).selectOption('passkeys');
  await expect(page.locator('#updates')).toBeHidden();
  await expectAxeClean(page);
});

test('Settings navigation uses button styling and retains deep links', async ({ page }) => {
  await page.goto('/settings#remote-access');
  const link = page.locator('#remote-access').getByRole('link', { name: 'Remote access', exact: true });
  await expect(link).toHaveClass(/border/);
  await expect(link).toHaveCSS('text-decoration-line', 'none');
  await expect(link).toHaveAttribute('href', '/settings/remote-access');
  await link.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/settings\/remote-access$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
