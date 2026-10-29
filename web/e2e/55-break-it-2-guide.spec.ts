import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('the Groups guide describes shipped private session launches consistently', async ({ page }) => {
  await page.goto('/guide/User-Guide/Groups');
  const article = page.getByRole('main');
  await expect(article.getByRole('heading', { name: 'Groups', exact: true })).toBeVisible();
  await expect(article).toContainText('Launch group session');
  await expect(article).not.toContainText('Group session launches are not available in this version');
});
