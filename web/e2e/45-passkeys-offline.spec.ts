import { expect, test } from './fixtures';
import { AUTH_FILE, gotoAppPage } from './helpers';

test.use({ storageState: AUTH_FILE });

test('passkey changes are disabled while Rynhult is offline', async ({ page }) => {
  await page.route('**/api/health', route => route.fulfill({ status: 503, json: { detail: 'offline' } }));
  await gotoAppPage(page, '/settings');
  await expect(page.getByRole('heading', { name: 'Passkeys', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create recovery code', exact: true })).toBeDisabled({ timeout: 15_000 });
  await page.getByLabel('Passkey name', { exact: true }).fill('Alex laptop');
  await expect(page.getByRole('button', { name: 'Add passkey', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Verify with a passkey', exact: true })).toBeDisabled();
});

for (const role of ['active', 'standby'] as const) {
  test(`offline Settings recovers to an HA ${role}`, async ({ page }) => {
    let recovered = false;
    await page.route('**/api/health', route => route.fulfill(recovered ? {
      json: { ok: true, ha: { enabled: true, role, epoch: 7, database: 'ok', automatic_failover: false } },
    } : { status: 503, json: { detail: 'offline' } }));
    await gotoAppPage(page, '/settings');
    await expect(page.getByRole('heading', { name: 'Passkeys', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Create recovery code', exact: true })).toBeDisabled();
    recovered = true;
    if (role === 'active') {
      await expect(page.getByRole('button', { name: 'Create recovery code', exact: true })).toBeEnabled({ timeout: 15_000 });
      await expect(page.getByRole('link', { name: 'Active main · epoch 7' })).toBeVisible();
    } else {
      await expect(page.getByRole('heading', { name: 'High availability' })).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText('Standby · read-only', { exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Create recovery code', exact: true })).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'New session', exact: true })).toHaveCount(0);
    }
  });
}

test('offline health polling keeps Settings mounted and changes disabled', async ({ page }) => {
  let checks = 0;
  await page.route('**/api/health', route => {
    checks++;
    return route.fulfill({ status: 503, json: { detail: 'offline' } });
  });
  await gotoAppPage(page, '/settings');
  const name = page.getByLabel('Passkey name', { exact: true });
  await expect(name).toBeVisible();
  await name.fill('Alex laptop');
  const initialChecks = checks;
  await expect.poll(() => checks, { timeout: 15_000 }).toBeGreaterThan(initialChecks);
  await expect(name).toHaveValue('Alex laptop');
  await expect(page.getByRole('button', { name: 'Create recovery code', exact: true })).toBeDisabled();
});

test('recovery verification offers a passkey before consuming the one-action window', async ({ page }) => {
  let generations = 0;
  await page.route('**/api/auth/passkeys/recovery-reauth', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/auth/passkeys/recovery-codes', route => {
    generations++;
    return route.fulfill({ json: { codes: ['demo-recovery-code'] } });
  });
  await gotoAppPage(page, '/settings#sign-in-security');
  await page.getByLabel('I signed in with a recovery code and lost my authenticator').check();
  await page.getByLabel('Recovery verification password', { exact: true }).fill('demo-password');
  await page.getByLabel('Different unused recovery code', { exact: true }).fill('demo-unused-code');
  await page.getByRole('button', { name: 'Verify to recover sign-in', exact: true }).click();
  const add = page.getByRole('button', { name: 'Add a passkey', exact: true });
  await expect(add).toBeVisible();
  expect(generations).toBe(0);
  await expect(page.getByText('Either action ends this recovery window.', { exact: false })).toBeVisible();
  const regenerate = page.getByRole('button', { name: 'Regenerate codes and end recovery window' });
  const actions = page.getByRole('button').filter({ hasText: /^(Add a passkey|Regenerate codes and end recovery window)$/ });
  await expect(actions).toHaveText(['Add a passkey', 'Regenerate codes and end recovery window']);
  await regenerate.click();
  await expect(page.getByText('demo-recovery-code', { exact: true })).toBeVisible();
  expect(generations).toBe(1);
  await expect(add).toHaveCount(0);
});
