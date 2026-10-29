import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

test('choosing a local workspace before the catalog loads still selects the research profile', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  try {
    await page.route('**/api/catalog', async route => { await gate; await route.continue(); });
    await page.goto('/');
    await page.getByRole('button', { name: 'New session', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('radio', { name: 'Local workspace', exact: true }).click();
    await dialog.getByLabel('Topic', { exact: true }).fill('Example research');
    const previews: string[] = [];
    page.on('request', request => {
      if (request.url().endsWith('/api/sessions/preview') && request.method() === 'POST') previews.push(request.postDataJSON().profile);
    });
    release();
    await expect(dialog.getByRole('combobox', { name: 'Profile', exact: true })).toHaveValue('researcher');
    await expect.poll(() => previews).toContain('researcher');
  } finally {
    release();
  }
});
