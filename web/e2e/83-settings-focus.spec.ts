import { test } from './shell-state';
import { expect } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, selectSettingsSection, settingsSectionIds } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) test.describe(`${width}px Settings`, () => {
  test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, hasTouch: width === 390, isMobile: width === 390 });
  for (const theme of ['dark', 'light'] as const) {
    test(`Settings chooser keeps keyboard focus through consecutive choices ${width} ${theme}`, async ({ page }) => {
      test.fixme(true, 'Classic native Settings chooser and its arrow-key cycling are retired. Original assertions retained for Josh; scoped link navigation is tested below.');
      await page.addInitScript(t => localStorage.setItem('acc-theme', t), theme);
      await page.goto('/settings');
      const chooser = page.getByRole('combobox', { name: 'Settings section', exact: true });
      for (let tab = 0; tab < 120 && !await chooser.evaluate(el => el === document.activeElement); tab++)
        await page.keyboard.press('Tab');
      await expect(chooser).toBeFocused();
      for (const section of ['sign-in-security', 'password', 'sign-ins']) {
        await page.keyboard.press('ArrowDown');
        await expect(chooser).toHaveValue(section);
        await expect(chooser).toBeFocused();
        await expect(chooser).toHaveCSS('outline-style', 'solid');
        await expect(page.locator(`#${section}`)).toBeVisible();
      }
      await page.keyboard.press('Tab');
      await expect(chooser).not.toBeFocused();
      await page.goBack();
      await expect(page.locator('#password')).toBeFocused();
      await expectAxeClean(page);
    });

    test(`focused Settings preserves drafts, links and keyboard access ${width} ${theme}`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: theme });
      await page.goto('/settings');
      await expect(page.locator('#passkeys')).toBeVisible();
      await expect(page.locator('#password')).toBeHidden();
      const draft = page.getByRole('textbox', { name: 'Passkey name', exact: true });
      await draft.fill('Travel laptop draft');
      await selectSettingsSection(page, 'password');
      await expect(page.locator('#password')).toBeFocused();
      await expect(page.locator('#passkeys')).toBeHidden();
      await page.goBack();
      await expect(page.locator('#passkeys')).toBeFocused();
      await expect(draft).toHaveValue('Travel laptop draft');
      // The classic All sections link / native End choice is retired; current scoped links retain the form.
      await selectSettingsSection(page, 'container-limits');
      await expect(page.locator('#container-limits')).toBeFocused();
      await page.keyboard.press('Tab');
      await expectAxeClean(page);
      await page.goto('/settings#telemetry');
      await expect(page.locator('#telemetry')).toBeVisible();
      await expect(page.locator('#usage-and-bug-reports')).toBeVisible();
      await page.reload();
      await expect(page.locator('#telemetry')).toBeVisible();
      await page.goto('/settings#dns-certificates');
      await expect(page.locator('#dns-certificates')).toBeVisible();
      await expect(page.locator('#dns')).toBeVisible();
      await expectAxeClean(page);
    });
  }
});

test('warnings and late errors remain visible when their section is not selected', async ({ page }) => {
  let answer!: () => void;
  const delayed = new Promise<void>(resolve => { answer = resolve; });
  await page.route('**/api/push', async route => {
    await delayed;
    await route.fulfill({ status: 403, json: { detail: 'Notification settings need review' } });
  });
  await page.goto('/settings#notifications');
  await selectSettingsSection(page, 'passkeys');
  answer();
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention).toContainText('Notification settings need review');
  await expect(page.locator('#notifications').getByRole('alert')).toBeVisible();
  await attention.locator('summary').click();
  await attention.getByRole('link', { name: 'Notifications', exact: true }).click();
  await expect(page.locator('#notifications').getByRole('alert')).toBeVisible();
});

test('member section choices exclude owner capabilities, including hostile fragments', async ({ page }) => {
  await page.route('**/api/auth/state', route => route.fulfill({ json: {
    authenticated: true, setup_required: false, username: 'sam', role: 'member', account: 'sam-agent',
    instance: { host: 'acc-host', private_ca: false },
  } }));
  await page.goto('/settings#people');
  await expect(page.getByRole('complementary', { name: 'Context sidebar', exact: true }).getByRole('region', { name: 'Account settings', exact: true })).toBeVisible();
  await expect(page.locator('#people')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Installation settings', exact: true })).toHaveCount(0);
  await expect(page.locator('#passkeys')).toBeVisible();
  await expectAxeClean(page);
});

test('device revocation pending and renewal warnings stay visible without exposing HTML', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const hostileLabel = 'Travel <script>alert(1)</script>' + 'x'.repeat(300);
  await page.route('**/api/devices?mine=true', route => route.fulfill({ json: {
    pairing_enabled: false, policy_pending: true, devices: [
      { id: 'revoked', person: 'alex', label: hostileLabel, kind: 'browser', status: 'revoked', expires_at: '2030-01-01T00:00:00Z', last_seen_at: null },
      { id: 'expiring', person: 'alex', label: 'Laptop', kind: 'browser', status: 'active', expires_at: new Date(Date.now() + 86400000).toISOString(), last_seen_at: null },
    ],
  } }));
  await page.goto('/settings');
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention).toContainText('Revocation pending');
  await expect(attention).toContainText('renewal reminder');
  await expect(page.locator('#access-devices')).toBeHidden();
  await expect(attention.locator('script')).toHaveCount(0);
  expect(await page.getByRole('main').evaluate(main => main.scrollWidth - main.clientWidth)).toBeLessThanOrEqual(0);
  await attention.locator('summary').click();
  await attention.getByRole('link', { name: 'Access devices', exact: true }).click();
  await expect(page.getByRole('button', { name: `Remove ${hostileLabel}`, exact: true })).toBeVisible();
});

test('every section can be focused, with form validation and drafts preserved', async ({ page }) => {
  await page.goto('/settings');
  const ids = await settingsSectionIds(page);
  expect(ids).toContain('people');
  expect(ids.length).toBeGreaterThan(10);
  for (const id of ids) {
    await selectSettingsSection(page, id);
    await expect(page.locator(`[data-settings-section][id="${id}"]`)).toBeVisible();
    await expect(page.locator('[data-settings-section]:visible')).toHaveCount(1);
  }
  await selectSettingsSection(page, 'password');
  const password = page.getByLabel('New password', { exact: true });
  await password.fill('draft');
  await selectSettingsSection(page, 'passkeys');
  await selectSettingsSection(page, 'password');
  await expect(password).toHaveValue('draft');
  await expect(page.locator('#password').getByRole('button', { name: 'Change password', exact: true })).toBeDisabled();
  await expect(page.locator('#password')).toBeVisible();
});

test('native validation reveals and focuses an invalid control in a hidden form', async ({ page }) => {
  await page.route('**/api/devices?mine=true', route => route.fulfill({ json: {
    pairing_enabled: true, policy_pending: false, devices: [],
  } }));
  await page.goto('/settings#access-devices');
  const input = page.getByRole('textbox', { name: 'Device name', exact: true });
  await expect(input).toBeVisible();
  await selectSettingsSection(page, 'passkeys');
  await page.locator('#access-devices form').evaluate((form: HTMLFormElement) => form.requestSubmit());
  await expect(page.locator('#access-devices')).toBeVisible();
  await expect(input).toBeFocused();
  await expect(page).toHaveURL(/#access-devices$/);
});

test('unverified certificate warnings retain their explanation and navigation', async ({ page }) => {
  await page.route('**/api/certificate', route => route.fulfill({ json: { kind: 'unverified', primary: 'acc.example.lan' } }));
  await page.goto('/settings');
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention).toContainText('Not verified');
  await expect(attention).toContainText('Not publicly trusted, and no root is configured');
  await attention.locator('summary').click();
  await attention.getByRole('link', { name: 'Certificate', exact: true }).click();
  await expect(page.locator('#certificate')).toContainText('Not publicly trusted, and no root is configured');
});

for (const status of [422, 503]) test(`switching sections retains a pending Security review, its ${status} failure and another form draft`, async ({ page }) => {
  const detail = status === 422 ? 'Security review needs attention' : 'Security preview needs review';
  let finish!: () => void;
  let saving = false;
  const delayed = new Promise<void>(resolve => { finish = resolve; });
  await page.route('**/api/security/changes/preview', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    saving = true;
    await delayed;
    await route.fulfill({ status, json: { detail } });
  });
  try {
    await page.goto('/settings#security');
    const security = page.locator('#security');
    const protection = security.getByRole('switch', { name: 'Block force pushes', exact: true });
    const before = await protection.isChecked();
    await protection.setChecked(!before);
    await security.getByRole('button', { name: 'Review changes', exact: true }).click();
    await expect.poll(() => saving).toBe(true);
    await selectSettingsSection(page, 'password');
    const draft = page.getByLabel('New password', { exact: true });
    await draft.fill('My retained draft');
    finish();
    await expect(security.getByRole('alert')).toContainText(detail);
    await expect(security).toBeVisible();
    await expect(protection).toBeChecked({ checked: !before });
    await expect(draft).toHaveValue('My retained draft');
  } finally { finish(); }
});

test('empty notification recipients link to the focused notification controls', async ({ page }) => {
  await page.route('**/api/push', route => route.fulfill({ json: { public_key: '', devices: [] } }));
  await page.goto('/settings#devices');
  await page.getByRole('link', { name: 'Turn on notifications', exact: true }).click();
  await expect(page).toHaveURL(/#notifications$/);
  await expect(page.locator('#notifications')).toBeVisible();
  await expect(page.locator('#notifications')).toBeFocused();
  await expectAxeClean(page);
});

test('Overview budget details opens the Budgets section, not the default one', async ({ page }) => {
  const resets = Math.floor(Date.now() / 1000) + 3600;
  await page.route('**/api/observations', route => route.fulfill({ json: [{ key: 'claude.rate_limits', observed_at: resets - 3600,
    value: { five_hour: { used_percentage: 38, resets_at: resets } } }] }));
  await page.route('**/api/budgets', route => route.fulfill({ json: { windows: [], rules: {}, extra: 0,
    status: { 'claude.five_hour': { allowed: 80, used: 38, over: false, action: 'warn' } } } }));
  await page.goto('/');
  await page.getByRole('link', { name: /Budget details \(80%\)/ }).click();
  await expect(page).toHaveURL(/\/settings#budgets$/);
  await expect(page.locator('#budgets')).toBeVisible();
  await expect(page.locator('#passkeys')).toBeHidden();
});
