import { test, expect } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) test(`an open status board follows system theme changes without a refresh at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await page.addInitScript(() => localStorage.removeItem('acc-theme'));
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/board');
  await expect(page.getByRole('heading', { name: 'Status board', exact: true })).toBeVisible();
  await expect(page.locator('html')).not.toHaveClass(/\bdark\b/);
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveClass(/\bdark\b/);
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('html')).not.toHaveClass(/\bdark\b/);
});

for (const theme of ['dark', 'light'] as const) test(`standalone routes keep explicit ${theme} when the system changes`, async ({ page }) => {
  await page.addInitScript(theme => localStorage.setItem('acc-theme', theme), theme);
  for (const path of ['/board', '/pair-device']) {
    await page.emulateMedia({ colorScheme: theme });
    await page.goto(path);
    await expect(page.locator('h1')).toBeVisible();
    await page.emulateMedia({ colorScheme: theme === 'dark' ? 'light' : 'dark' });
    await expect(page.locator('html')).toHaveClass(theme === 'dark' ? /\bdark\b/ : /^(?!.*\bdark\b)/);
  }
});
