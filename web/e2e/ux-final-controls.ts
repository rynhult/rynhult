import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, gitProject, openAdvanced } from './helpers';
export function finalControlTests() {
test.use({ storageState: AUTH_FILE });
for (const theme of ['dark', 'light'] as const) {
  test(`Guide commands are copyable, contained and keyboard readable in ${theme}`, async ({ page, context }) => {
    await page.emulateMedia({ colorScheme: theme });
    await page.setViewportSize({ width: 320, height: 844 });
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/guide/User-Guide/Install');
    const article = page.locator('article');
    const copy = article.getByRole('button', { name: /Copy/ }).first();
    await expect(copy).toBeVisible();
    const region = article.locator('pre').first();
    await expect(region).toHaveAttribute('tabindex', '0');
    const raw = await region.textContent();
    await copy.click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(raw);
    expect(await page.locator('main').evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
    await expectAxeClean(page);
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) } }));
    await copy.click();
    await expect(article.getByText(/Clipboard unavailable/)).toBeVisible();
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }));
    await copy.click();
    await expect(article.getByText(/Clipboard unavailable/)).toBeVisible();
  });
}
test('machine radios wrap and select with arrows using one tab stop', async ({ page }) => {
  await page.route('**/api/nodes', r => r.fulfill({ json: Array.from({ length: 8 }, (_, i) => ({ id: `node-${i}`, name: `demo-long-machine-${i}`, connected: true, status: 'active', info: {}, accounts: [], offline_threshold_seconds: 60 })) }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/sessions');
  const group = page.getByRole('radiogroup', { name: 'Show', exact: true });
  const first = group.getByRole('radio').first();
  await first.focus();
  await page.keyboard.press('ArrowRight');
  await expect(group.getByRole('radio').nth(1)).toBeChecked();
  await expect(group.getByRole('radio').nth(1)).toBeFocused();
  expect(await group.getByRole('radio').evaluateAll(es => es.filter(e => e.tabIndex === 0).length)).toBe(1);
  expect(await page.locator('main').evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto('/host');
  await expect(page.getByRole('combobox', {name:'Machine', exact:true})).toHaveCount(1);
  expect(await page.locator('main').evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
});

test('expanded launch copies commands and files without page overflow', async ({ page, context }, testInfo) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.route('**/api/sessions/preview', r => r.fulfill({ json: { command: ['claude', '--print', 'hello'], cwd: '/srv/demo', files: { 'demo.txt': 'example\n' + 'x'.repeat(600) }, digest: 'demo', trusted: true, import_mode: 'none' } }));
  await page.setViewportSize({ width: 390, height: 844 });
  const { id } = await gitProject(page, `final-preview-${testInfo.project.name}-${testInfo.repeatEachIndex}`);
  await page.getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(id));
  await openAdvanced(dialog);
  await dialog.getByRole('button', { name: 'Copy launch command', exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('cd /srv/demo\nclaude --print hello');
  // Provider defaults can update the preview while the dialog first loads. Wait for that real data to settle.
  await page.waitForLoadState('networkidle');
  await dialog.locator('summary').filter({ hasText: 'demo.txt' }).click();
  await expect(dialog.getByRole('region', { name: 'demo.txt', exact: true })).toHaveAttribute('tabindex', '0');
  await expectAxeClean(page);
  await dialog.getByRole('button', { name: 'Copy demo.txt', exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('example\n' + 'x'.repeat(600));
  await expect(dialog.getByRole('region', { name: 'demo.txt', exact: true })).toHaveAttribute('tabindex', '0');
  await expectAxeClean(page);
});

}
