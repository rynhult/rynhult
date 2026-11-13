import { test, expect } from './fixtures';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['setup_level', 'layouts'] });

// All journeys use the production frame; the retired preference API is absent.
test.describe('release layout defaults', () => {
  test.use({ });

  test('the new layout is unconditional and Settings has no layout choice', async ({ page, isolatedOwner }) => {
    void isolatedOwner;

    expect((await page.request.get('/api/preferences/ui-shell')).status()).toBe(404);
    await page.goto('/settings#layout');
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Use the classic layout' })).toHaveCount(0);
    await expect(page.locator('a[href="/settings#layout"]')).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
  });

  test('Features has no New layout toggle', async ({ page, isolatedOwner }) => {
    void isolatedOwner;
    const state = await (await page.request.get('/api/admin-features')).json();
    expect(state.features.some((feature: { id: string }) => feature.id === 'new_shell')).toBe(false);
    await page.goto('/settings#admin-features');
    await expect(page.getByRole('heading', { name: 'Admin features', exact: true })).toBeVisible();
    await expect(page.getByText('New layout', { exact: true })).toHaveCount(0);
  });
});

test('compact first-run prompt links to Settings and dismissal follows the person to another browser', async ({ page, browser }) => {
  await page.goto('/');
  const prompt = page.getByRole('complementary', { name: 'Choose what you see' });
  await expect(prompt).toBeVisible();
  await expect(prompt.getByRole('link', { name: 'Settings → Setup level' })).toHaveAttribute('href', '/settings#setup-level');
  expect((await prompt.boundingBox())!.height).toBeLessThan(56);
  await prompt.getByRole('link').click();
  await expect(page.getByRole('combobox', { name: 'Setup level', exact: true })).toBeVisible();
  await page.goto('/');
  await prompt.getByRole('button', { name: 'Dismiss setup prompt' }).click();
  await expect(prompt).toHaveCount(0);
  await page.reload();
  await expect(prompt).toHaveCount(0);
  const other = await browser.newContext({ storageState: AUTH_FILE });
  try {
    const another = await other.newPage();
    await another.goto(page.url());
    await expect(another.getByRole('navigation', { name: 'Rail', exact: true })).toBeVisible();
    await expect(another.getByRole('complementary', { name: 'Choose what you see' })).toHaveCount(0);
  } finally { await other.close(); }
  expect((await (await page.request.get('/api/preferences/setup-level')).json()).level).toBeNull();
});
