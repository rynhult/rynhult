import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) test(`config-folder keys refuse double submits and leave a recoverable form at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await page.goto('/settings#keys');
  const names = ['CODEX_HOME', 'COPILOT_HOME', 'CURSOR_CONFIG_DIR', 'XDG_CONFIG_HOME', 'NPM_CONFIG_NODE_OPTIONS', 'OPENCODE_CONFIG_CONTENT'];
  for (const variable of names) {
    await page.getByRole('button', { name: 'Add key', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Add a key', exact: true });
    await expect(dialog.getByRole('alert')).toHaveCount(0);
    await dialog.getByLabel('Name', { exact: true }).fill('Synthetic configuration key');
    await dialog.getByRole('combobox', { name: 'Variable', exact: true }).selectOption('custom');
    await dialog.getByRole('textbox', { name: 'Custom variable', exact: true }).fill(variable);
    await dialog.getByLabel('Value', { exact: true }).fill('synthetic-config-value');
    let release!: () => void;
    let requests = 0;
    const pending = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/vault/keys', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      requests++;
      await pending;
      await route.continue();
    });
    try {
      await dialog.getByRole('button', { name: 'Add key', exact: true }).dblclick();
      await expect.poll(() => requests).toBe(1);
      release();
      await expect(dialog.getByRole('alert')).toContainText('own variables');
      await expect(dialog.getByLabel('Name', { exact: true })).toHaveValue('Synthetic configuration key');
      await expect(dialog.getByRole('textbox', { name: 'Custom variable', exact: true })).toHaveValue(variable);
      await page.mouse.click(1, 1); // a backdrop click must not discard a form
      await expect(dialog).toBeVisible();
      await expectAxeClean(page, '[role="dialog"]');
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
      expect(requests).toBe(1);
    } finally {
      release();
      await page.unroute('**/api/vault/keys');
    }
  }
  await page.getByRole('button', { name: 'Add key', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a key', exact: true });
  await expect(dialog.getByLabel('Name', { exact: true })).toHaveValue('');
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await page.keyboard.press('Escape'); // explicit dismissal is supported
  await expect(dialog).toBeHidden();
  await page.goto('/projects');
  await page.goBack();
  await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
  const keys = await (await page.request.get('/api/vault/keys')).json();
  expect(keys.some((key: { variable: string }) => names.includes(key.variable))).toBe(false);
});
