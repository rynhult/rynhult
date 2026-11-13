import { test, expect } from './fixtures';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const failure of ['network', 'server'] as const) test(`receiving device resumes status polling after a temporary ${failure} failure`, async ({ page }) => {
  await page.setViewportSize({ width: failure === 'network' ? 1440 : 390, height: 844 });
  let polls = 0;
  await page.route('**/api/device-pairing/preview', route => route.fulfill({ json: { installation: 'https://acc.example.lan', installation_name: 'Test installation', person: 'alex', label: 'Phone', desktop_code: '654321' } }));
  await page.route('**/api/device-pairing/claim', route => route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } }));
  await page.route('**/api/device-pairing/status', route => {
    polls++;
    return polls === 1 ? failure === 'network' ? route.abort('internetdisconnected')
      : route.fulfill({ status: 503, json: { detail: 'Temporary test outage' } })
      : route.fulfill({ json: { status: 'ready', available: true } });
  });

  await page.goto('/pair-device#' + 't'.repeat(43));
  await page.getByLabel('The installation, person and desktop match code match my trusted screen').check();
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeEnabled({ timeout: 12000 });
  await expect(page.getByRole('alert')).toBeHidden();
});

test('expired pairing disables a previously available download and stops polling', async ({ page }) => {
  let polls = 0;
  await page.route('**/api/device-pairing/preview', route => route.fulfill({ json: { installation: 'https://acc.example.lan', installation_name: 'Test installation', person: 'alex', label: 'Phone', desktop_code: '654321' } }));
  await page.route('**/api/device-pairing/claim', route => route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } }));
  await page.route('**/api/device-pairing/status', route => {
    polls++;
    return polls === 1 ? route.fulfill({ json: { status: 'ready', available: true } })
      : route.fulfill({ status: 410, json: { detail: 'This pairing expired. Start again on the trusted device.' } });
  });
  await page.goto('/pair-device#' + 't'.repeat(43));
  await page.getByLabel('The installation, person and desktop match code match my trusted screen').check();
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeEnabled({ timeout: 10000 });
  await expect(page.getByRole('alert')).toContainText('expired', { timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Download identity once' })).toBeDisabled();
  await page.waitForTimeout(6500);
  expect(polls).toBe(2);
});

for (const failure of ['network', 'server'] as const) test(`pairing preview retries a temporary ${failure} failure before allowing claim`, async ({ page }) => {
  let previews = 0;
  let claims = 0;
  await page.route('**/api/device-pairing/preview', route => {
    previews++;
    if (previews === 1) return failure === 'network' ? route.abort('internetdisconnected')
      : route.fulfill({ status: 503, json: { detail: 'Temporary preview outage' } });
    return route.fulfill({ json: { installation: 'https://acc.example.lan', installation_name: 'Test installation', person: 'alex', label: 'Phone', desktop_code: '654321' } });
  });
  await page.route('**/api/device-pairing/claim', route => {
    claims++;
    expect(route.request().postDataJSON().phone_confirmed).toBe(true);
    return route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } });
  });
  await page.goto('/pair-device#' + 't'.repeat(43));
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Request pairing', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry pairing preview' }).click();
  await expect(page.getByText(/Installation:/)).toContainText('Test installation');
  await expect(page.getByRole('button', { name: 'Request pairing', exact: true })).toBeDisabled();
  await page.getByLabel('The installation, person and desktop match code match my trusted screen').check();
  await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
  await expect(page.getByRole('paragraph').filter({ hasText: 'Waiting for owner confirmation.' })).toBeVisible();
  expect(previews).toBe(2);
  expect(claims).toBe(1);
});

test('a claim response arriving after pagehide cannot restore a receipt', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let claims = 0;
  let polls = 0;
  await page.route('**/api/device-pairing/preview', route => route.fulfill({ json: {
    installation: 'https://acc.example.lan', installation_name: 'Test installation', person: 'alex', label: 'Phone', desktop_code: '654321',
  } }));
  await page.route('**/api/device-pairing/claim', async route => {
    claims++;
    await gate;
    await route.fulfill({ json: { receipt: 'r'.repeat(43), matching_code: '123456' } });
  });
  await page.route('**/api/device-pairing/status', route => { polls++; return route.fulfill({ json: { available: true } }); });
  try {
    await page.goto('/pair-device#' + 't'.repeat(43));
    await page.getByLabel('The installation, person and desktop match code match my trusted screen').check();
    await page.getByRole('button', { name: 'Request pairing', exact: true }).click();
    await expect.poll(() => claims).toBe(1);
    // Model a document being frozen for browser history while its claim is still in flight.
    await page.evaluate(() => dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })));
    const received = page.waitForResponse(response => response.url().endsWith('/api/device-pairing/claim'));
    release();
    await received;
    await expect(page.getByText(/A refreshed page cannot recover/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request pairing', exact: true })).toHaveCount(0);
    await expect(page.locator('code')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Download identity once' })).toHaveCount(0);
    // Wait past the normal polling delay: no stale receipt may start a status request.
    await page.waitForTimeout(3500);
    expect(polls).toBe(0);
  } finally {
    release();
  }
});
