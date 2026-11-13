import { test, expect } from './fixtures';
import AxeBuilder from '@axe-core/playwright';
import { mkdirSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { AUTH_FILE, expectAxeClean, csrfHeaders, info, PASSWORD, totp } from './helpers';
test.use({ storageState: AUTH_FILE, presentationChanges: ['theme'] });
function packageBytes(id: string, name = 'Example', version = '1.0.0') {
  return execFileSync(process.env.ACC_E2E_PYTHON || '/srv/acc/app/.venv/bin/python', ['-c',
    `import sys;sys.path[:0]=['../src','../tests'];from test_themes import archive,manifest;sys.stdout.buffer.write(archive(manifest(id=${JSON.stringify(id)},name=${JSON.stringify(name)},version=${JSON.stringify(version)},tokens={'light':{'bg':'#ffffff'},'dark':{}})))`], { env: { ...process.env, PYTHONPATH: '../src' } });
}
for (const width of [1440, 390]) for (const appearance of ['light','dark'] as const) test(`theme review preview apply reset ${width} ${appearance}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ colorScheme: appearance, reducedMotion: 'reduce' });
  await page.goto('/catalog');
  await page.getByRole('tab', { name: 'Themes', exact: true }).click();
  const id = `test-theme-${width}-${appearance}`;
  await page.getByLabel('Theme package (.zip)').setInputFiles({ name: 'theme.zip', mimeType: 'application/zip', buffer: packageBytes(id) });
  await page.getByRole('button', { name: 'Review theme package', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Review theme import', exact: true })).toBeVisible();
  await expect(page.getByText('Publisher not verified', { exact: true })).toBeVisible();
  expect(await page.locator('html').getAttribute('data-community-theme')).toBeNull();
  const frame = page.frameLocator('iframe[title="Theme preview"]');
  await expect(frame.getByText('Terminal palette stays built in.')).toBeVisible();
  await expect(page.locator('iframe')).toHaveAttribute('sandbox', '');
  // Preview intentionally denies script execution and same-origin access. Scan
  // host review chrome without injecting axe into that opaque sandbox; source
  // palettes/markup have separate deterministic and browser assertions.
  const scan = await new AxeBuilder({ page }).exclude('iframe').options({ iframes: false }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
  expect(scan.violations).toEqual([]);
  await page.screenshot({ path: test.info().outputPath(`theme-preview-${width}-${appearance}.png`) });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice).toBeNull();
  await page.getByRole('button', { name: 'Review theme package', exact: true }).click();
  await page.getByRole('button', { name: 'Add theme to Catalog', exact: true }).click();
  await expect(page.getByText('Theme added. Preview it below before applying.')).toBeVisible();
  expect(await page.locator('html').getAttribute('data-community-theme')).toBeNull();
  await page.getByRole('article', { name: `Theme ${id} 1.0.0`, exact: true }).getByRole('button', { name: 'Preview Example 1.0.0', exact: true }).click();
  await page.getByRole('button', { name: 'Apply to my account', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-community-theme', '');
  await expect(page.getByText('Active theme: Example')).toBeVisible();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-community-theme', '');
  await page.getByRole('tab', { name: 'Themes', exact: true }).click();
  expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice.package_id).toBe(id);
  await page.goto('/catalog?appearance=builtin');
  await expect(page.locator('html')).not.toHaveAttribute('data-community-theme');
  expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice.package_id).toBe(id);
  await page.getByRole('tab', { name: 'Themes', exact: true }).click();
  await page.getByRole('button', { name: 'Reset account theme', exact: true }).click();
  await expect(page.locator('html')).not.toHaveAttribute('data-community-theme');
  await expectAxeClean(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const bad = await page.request.post('/api/catalog/themes/review', { headers: { ...await csrfHeaders(page), 'Content-Type': 'application/zip' }, data: Buffer.from('not a zip') });
  expect(bad.status()).toBe(422);
});

test('member can choose an approved theme but cannot import, and logout restores built-ins', async ({ page, browser }) => {
  const account = `theme-${randomUUID().slice(0,6)}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invite = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invite.ok()).toBe(true);
  const token = (await invite.json()).link.split('/').pop();
  const member = await browser.newContext();
  try {
    const headers = { Origin: info().origin };
    const begun = await member.request.post('/api/auth/invite/begin', { headers, data: { token } });
    const joined = await member.request.post('/api/auth/invite/complete', { headers, data: { token, username: `theme-${randomUUID().slice(0,8)}`, password: PASSWORD, code: totp((await begun.json()).totp_secret) } });
    expect(joined.ok()).toBe(true);
    // The invited person uses the same frame as the owner.

    const view = await member.newPage(); await view.goto('/catalog');
    await view.getByRole('tab', { name: 'Themes', exact: true }).click();
    await expect(view.getByLabel('Theme package (.zip)')).toHaveCount(0);
    await expect(view.getByText('Active theme: Built-in Rynhult')).toBeVisible();
    await view.getByRole('button', { name: 'Preview Example 1.0.0', exact: true }).last().click();
    await view.getByRole('button', { name: 'Apply to my account', exact: true }).click();
    await expect(view.locator('html')).toHaveAttribute('data-community-theme', '');
    expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice).toBeNull();
    // Restore while still authenticated; changing preference does not replace the mounted header.

    await view.getByRole('button', { name: /^Account:/ }).click();
    await view.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(view.locator('html')).not.toHaveAttribute('data-community-theme');
    expect((await (await member.request.get('/api/auth/state')).json()).authenticated).toBe(false);
  } finally {
    try {
      }
    finally { await member.close(); }
  }
});

for (const appearance of ['light', 'dark'] as const) test(`maximum theme labels fit phone ${appearance}`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.emulateMedia({ colorScheme: appearance, reducedMotion: 'reduce' });
  const name = 'N'.repeat(80), version = '1.0.0-' + 'v'.repeat(74);
  const id = `long-theme-${appearance}`;
  const fits = async () => {
    expect(await page.locator('main').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  };
  await page.goto('/catalog');
  await page.getByRole('tab', { name: 'Themes', exact: true }).click();
  await page.getByLabel('Theme package (.zip)').setInputFiles({ name: 'theme.zip', mimeType: 'application/zip', buffer: packageBytes(id, name, version) });
  await page.getByRole('button', { name: 'Review theme package', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.getByRole('button', { name: 'Add theme to Catalog', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const article = page.getByRole('article', { name: `Theme ${id} ${version}`, exact: true });
  await expect(article).toBeVisible();
  await fits();
  const preview = article.getByRole('button');
  const bounds = await preview.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await preview.click();
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await preview.click();
  await page.getByRole('button', { name: 'Apply to my account', exact: true }).click();
  await expect(page.getByText(`Active theme: ${name}`, { exact: true })).toBeVisible();
  await fits();
  await page.getByRole('button', { name: 'Reset account theme', exact: true }).click();
  await expect(page.getByText('Active theme: Built-in Rynhult', { exact: true })).toBeVisible();
});
