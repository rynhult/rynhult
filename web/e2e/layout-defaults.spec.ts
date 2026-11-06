import { test, expect, chooseLayout } from './fixtures';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE, signedInLayout: 'new', extraHTTPHeaders: { 'x-acc-e2e-layout': 'release' }, presentationChanges: ['ui_shell', 'setup_level', 'layouts'] });

// The disposable server honors this test-only header to use production semantics.
test.describe('release layout defaults', () => {
  test.use({ signedInLayout: 'unchosen' });

  test('saved classic choices use the new layout and Settings has no layout choice', async ({ page, isolatedOwner }) => {
    void isolatedOwner;
    await chooseLayout(page.context(), false);
    expect((await (await page.request.get('/api/preferences/ui-shell')).json()).enabled).toBe(true);
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
