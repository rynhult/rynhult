import { expect, test } from '@playwright/test';
import { AUTH_FILE, demoSessions, expectAxeClean } from './helpers';

test.use({ storageState: AUTH_FILE });
const fileText = 'first\n' + 'long generated content '.repeat(200) + '\n[redacted]';

for (const width of [390, 1440]) for (const theme of ['dark', 'light'] as const) {
  test(`Setup disclosures preserve choices and exact copies at ${width}px in ${theme}`, async ({ page, context }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.emulateMedia({ colorScheme: theme });
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const row = demoSessions()[0];
    const files = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`plugin/generated/file-${i}.md`, `file ${i}`]));
    files['very-long-generated-path/demo.txt'] = fileText;
    const inputs = { provider: 'claude', permission_mode: 'bypassPermissions', profile: 'Frontend',
      task: 'Example task', skills: ['review'], mcps: ['playwright'] };
    await page.route(`**/api/sessions/${row.id}`, r => r.fulfill({ json: { ...row, host: 'build-box',
      launch_spec: { inputs, files, digest: 'example123456789' }, runs: [], editor: null } }));
    await page.goto(`/sessions/${row.id}?tab=spec`);
    const panel = page.getByRole('tabpanel');
    const exact = panel.locator('summary').filter({ hasText: /^Exact inputs/ });
    const generated = panel.locator('summary').filter({ hasText: /^Generated files/ });
    await expect(exact).toBeVisible();
    await expect(generated).toContainText('(31)');
    await expect(panel.getByText('Runs without asking', { exact: true })).toBeVisible();
    await expect(panel.getByText('build-box', { exact: true })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Copy launch inputs' })).toBeHidden();
    await expect(panel.getByRole('combobox', { name: 'Generated file', exact: true })).toBeHidden();
    await expectAxeClean(page);
    await exact.focus();
    await page.keyboard.press('Enter');
    await panel.getByRole('button', { name: 'Copy launch inputs' }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(JSON.stringify(inputs, null, 2));
    await exact.click();
    await generated.focus();
    await page.keyboard.press('Space');
    const select = panel.getByRole('combobox', { name: 'Generated file', exact: true });
    await expect(select.getByRole('option')).toHaveCount(31);
    await select.selectOption('very-long-generated-path/demo.txt');
    const region = panel.getByRole('region', { name: 'very-long-generated-path/demo.txt', exact: true });
    await expect(region).toHaveText(fileText);
    const pane = await region.evaluate(el => ({ width: el.clientWidth, frame: el.parentElement!.clientWidth }));
    expect(pane.width).toBeGreaterThan(pane.frame * .8);
    await region.focus();
    await page.keyboard.press('End');
    await expect(region).toBeFocused();
    await expect.poll(() => region.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    await panel.getByRole('button', { name: 'Copy very-long-generated-path/demo.txt', exact: true }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(fileText);
    await expect(panel.getByRole('button', { name: /^Copy plugin\/generated/ })).toHaveCount(0);
    const filter = panel.getByRole('searchbox', { name: 'Filter generated files' });
    await filter.fill('demo.txt');
    await expect(select.getByRole('option')).toHaveCount(1);
    await generated.click();
    await generated.click();
    await expect(filter).toHaveValue('demo.txt');
    await expect(select).toHaveValue('very-long-generated-path/demo.txt');
    await filter.fill('missing-example');
    await expect(panel.getByRole('status', { name: 'Generated files results' })).toContainText('No matching files');
    await expect(panel.getByRole('button', { name: 'Copy very-long-generated-path/demo.txt' })).toBeHidden();
    await filter.fill('');
    await expect(select).toHaveValue('very-long-generated-path/demo.txt');
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: undefined }));
    await panel.getByRole('button', { name: 'Copy very-long-generated-path/demo.txt' }).click();
    await expect(region).toBeFocused();
    expect(await page.evaluate(() => window.getSelection()?.toString())).toBe(fileText);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expectAxeClean(page);
  });
}

test('Setup names an empty generated-file result without hiding launch inputs', async ({ page }) => {
  const row = demoSessions()[0];
  await page.route(`**/api/sessions/${row.id}`, r => r.fulfill({ json: { ...row,
    launch_spec: { inputs: {}, files: {}, digest: 'example' }, runs: [], editor: null } }));
  await page.goto(`/sessions/${row.id}?tab=spec`);
  await page.getByRole('tabpanel').locator('summary').filter({ hasText: /^Generated files/ }).click();
  await expect(page.getByRole('status', { name: 'Generated files results' })).toContainText('No generated files');
  await expect(page.getByRole('combobox', { name: 'Generated file', exact: true })).toHaveCount(0);
  await page.getByRole('tabpanel').locator('summary').filter({ hasText: /^Exact inputs/ }).click();
  await expect(page.getByRole('button', { name: 'Copy launch inputs' })).toBeVisible();
});
