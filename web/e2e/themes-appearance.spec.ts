import { expect, test } from './fixtures';
import AxeBuilder from '@axe-core/playwright';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, csrfHeaders, expectAxeClean, info, PASSWORD, totp } from './helpers';
test.use({ storageState: AUTH_FILE });
for (const appearance of ['light', 'dark'] as const) test(`Account Appearance owner and member ${appearance}`, async ({ page, browser }) => {
  await page.emulateMedia({ colorScheme: appearance, reducedMotion: 'reduce' });
  const id = `appearance-${appearance}`;
  const archive = execFileSync(process.env.ACC_E2E_PYTHON!, ['-c', `import sys;sys.path[:0]=['../src','../tests'];from test_themes import archive,manifest;sys.stdout.buffer.write(archive(manifest(id='${id}',name='Cedar theme')))`]);
  const headers = await csrfHeaders(page);
  const reviewed = await page.request.post('/api/catalog/themes/review', { headers: { ...headers, 'Content-Type': 'application/zip' }, data: archive });
  expect(reviewed.ok(), await reviewed.text()).toBeTruthy();
  const installed = await page.request.post('/api/catalog/themes/install', { headers, data: { token: (await reviewed.json()).token } });
  expect(installed.ok(), await installed.text()).toBeTruthy();
  await page.goto('/settings#appearance');
  await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();
  await expect(page.getByLabel('Theme package (.zip)')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Manage themes in Catalog' })).toHaveAttribute('href', '/catalog?tab=themes');
  const check = async (view: import('@playwright/test').Page, role: string) => {
    const card = view.getByRole('article', { name: `Theme ${id} 1.0.0`, exact: true });
    await expect(card).toBeVisible();
    const before = await (await view.request.get('/api/catalog/themes/preference')).json();
    await card.getByRole('button', { name: 'Preview Cedar theme 1.0.0', exact: true }).click();
    await expect(view.getByRole('dialog', { name: 'Preview account theme' })).toBeVisible();
    await expect(view.locator('iframe[title="Theme preview"]')).toHaveAttribute('sandbox', '');
    const scan = await new AxeBuilder({ page: view }).exclude('iframe').options({ iframes: false }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
    expect(scan.violations).toEqual([]);
    await view.getByRole('button', { name: 'Cancel', exact: true }).click();
    expect((await (await view.request.get('/api/catalog/themes/preference')).json()).revision).toBe(before.revision);
    await card.getByRole('button', { name: 'Preview Cedar theme 1.0.0', exact: true }).click();
    await view.getByRole('button', { name: 'Apply to my account', exact: true }).click();
    await expect(view.getByText('Active theme: Cedar theme', { exact: true })).toBeVisible();
    await expect(view.locator('html')).toHaveAttribute('data-community-theme', '');
    expect((await (await view.request.get('/api/catalog/themes/preference')).json()).choice.package_id).toBe(id);
    if (role === 'member') expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice).toBeNull();
    await view.reload();
    await expect(view.getByText('Active theme: Cedar theme', { exact: true })).toBeVisible();
    const control = view.getByRole('combobox', { name: 'Appearance mode', exact: true });
    await control.selectOption(appearance === 'light' ? 'dark' : 'light');
    await expect(view.locator('html')).toHaveClass(appearance === 'light' ? /dark/ : /^(?!.*dark)/);
    await control.selectOption('system');
    await view.getByRole('button', { name: /^Account:/ }).click();
    await view.getByRole('button', { name: 'Theme: System', exact: true }).click();
    await expect(control).toHaveValue('light');
    await control.selectOption('system');
    if (appearance === 'dark') await expect(view.locator('html')).toHaveClass(/dark/);
    else await expect(view.locator('html')).not.toHaveClass(/dark/);
    await expectAxeClean(view);
    if (process.env.ACC_UPDATE_APPEARANCE_SCREENSHOTS === '1') await view.screenshot({ path: `../docs/handoffs/themes-appearance/${role}-${appearance}.png`, fullPage: true });
    await view.getByRole('button', { name: 'Reset account theme' }).click();
    await expect(view.locator('html')).not.toHaveAttribute('data-community-theme');
  };
  await check(page, 'owner');
  await page.getByRole('link', { name: 'Manage themes in Catalog' }).click();
  await expect(page.getByRole('tab', { name: 'Themes', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Theme package (.zip)')).toBeVisible();
  const account = `appearance-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers, data: { role: 'member', account } });
  expect(invited.ok(), await invited.text()).toBeTruthy(); const token = (await invited.json()).link.split('/').pop();
  const member = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: appearance, reducedMotion: 'reduce' });
  try {
    const begun = await member.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
    const joined = await member.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `blair-${Date.now()}`, password: PASSWORD, code: totp((await begun.json()).totp_secret) } });
    expect(joined.ok(), await joined.text()).toBeTruthy();
    // The invited person uses the same frame as the owner.

    const view = await member.newPage(); await view.goto('/settings#appearance');
    await expect(view.getByLabel('Theme package (.zip)')).toHaveCount(0);
    await expect(view.getByRole('link', { name: 'Manage themes in Catalog' })).toHaveCount(0);
    await check(view, 'member');
    expect((await (await page.request.get('/api/catalog/themes/preference')).json()).choice).toBeNull();
    const refused = await member.request.post('/api/catalog/themes/review', { headers: { ...await csrfHeaders(view), 'Content-Type': 'application/zip' }, data: archive });
    expect(refused.status()).toBe(403);
  } finally {
    try {  }
    finally { await member.close(); }
  }
});
