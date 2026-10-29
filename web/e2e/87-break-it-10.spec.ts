import { test, expect, type Page } from '@playwright/test';
import { AUTH_FILE, csrfHeaders, expectScannableQr, resetPairingThrottle, verifyAgain } from './helpers';

// Break-it pass 10 (docs/reviews/2026-10-27-break-it-10.md): phone pairing and remote access settings, used carelessly
// on a phone-sized screen.
test.use({ storageState: AUTH_FILE, viewport: { width: 390, height: 844 } });

let before: Record<string, unknown> | undefined;
let devicesBefore = new Set<string>();

test.beforeEach(async ({ page }) => {
  const state = await (await page.request.get('/api/remote-access')).json();
  before = { pairing_enabled: state.pairing_enabled, machine_identities: state.machine_identities,
    public_hostname: state.public_hostname, private_origin: state.private_origin || 'https://acc.example.lan',
    certificate_days: state.certificate_days };
  devicesBefore = new Set((await (await page.request.get('/api/devices')).json()).devices.map((d: { id: string }) => d.id));
  await resetPairingThrottle(page);
});

test.afterEach(async ({ page }) => {
  if (!before) return;
  await verifyAgain(page);
  const devices = (await (await page.request.get('/api/devices')).json()).devices as { id: string }[];
  for (const device of devices.filter(d => !devicesBefore.has(d.id))) {
    expect((await page.request.delete(`/api/devices/${device.id}`, { headers: await csrfHeaders(page) })).ok()).toBe(true);
  }
  expect((await page.request.put('/api/remote-access', { headers: await csrfHeaders(page), data: before })).ok()).toBe(true);
});

async function noSideways(page: Page, where: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${where} scrolls sideways at 390px`).toBeLessThanOrEqual(0);
}

async function preparePairing(page: Page) {
  await page.goto('/settings/remote-access');
  await expect(page.getByRole('heading', { name: 'Remote access', exact: true, level: 1 })).toBeVisible();
  await noSideways(page, 'Remote access');
  await verifyAgain(page);
  await page.goto('/settings/remote-access');
  await page.getByLabel('Public hostname').fill('remote.example.com');
  await page.getByLabel('Private recovery address').fill('https://acc.example.lan');
  await page.getByLabel('Allow LAN device pairing').check();
  await page.getByRole('button', { name: 'Save pairing settings' }).click();
  await expect(page.getByText('Pairing settings saved. Public routing was not changed.')).toBeVisible();
  await noSideways(page, 'Remote access after saving');
}

test('closing a pairing on a phone ends its link at once', async ({ page, browser }) => {
  await preparePairing(page);
  await page.getByLabel('Device name', { exact: true }).fill('Careless phone');
  await page.getByRole('button', { name: 'Pair device', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Pair device' });
  const qr = dialog.getByRole('img', { name: 'One-use device pairing QR code' });
  await expect(qr).toBeVisible();
  // A broken image is still a visible element: check it decoded (break-it 10, B10-3).
  expect(await qr.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme });
    await expectScannableQr(qr, `pairing QR, ${colorScheme}`);
  }
  await noSideways(page, 'the pairing dialog');
  const box = (await dialog.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  const link = await dialog.getByLabel('One-use pairing link').inputValue();
  await dialog.getByRole('button', { name: 'Close pairing' }).click();
  await expect(dialog).toBeHidden();
  // The person changed their mind; the link they may have shared or photographed must not work afterwards.
  const receiver = await browser.newContext();
  try {
    const phone = await receiver.newPage();
    await expect.poll(async () => {
      await phone.goto(new URL('/pair-device#' + link.split('#')[1], page.url()).href);
      await phone.getByRole('button', { name: 'Request pairing' }).click();
      return (await phone.getByRole('alert').textContent()) ?? '';
    }, { timeout: 15_000 }).toContain('expired or was already used');
  } finally {
    await receiver.close();
  }
});

test('leaving the page with Back while pairing ends the link too', async ({ page, browser }) => {
  await preparePairing(page);
  await page.getByLabel('Device name', { exact: true }).fill('Impatient phone');
  await page.getByRole('button', { name: 'Pair device', exact: true }).click();
  const link = await page.getByRole('dialog', { name: 'Pair device' }).getByLabel('One-use pairing link').inputValue();
  // Tap Overview in the phone's tab bar, behind the open dialog's back: leaving the page unmounts the dialog, as an
  // in-app Back does.
  await page.evaluate(() => (document.querySelector('nav a[href="/"]') as HTMLAnchorElement).click());
  await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
  const receiver = await browser.newContext();
  try {
    const phone = await receiver.newPage();
    await expect.poll(async () => {
      await phone.goto(new URL('/pair-device#' + link.split('#')[1], page.url()).href);
      await phone.getByRole('button', { name: 'Request pairing' }).click();
      return (await phone.getByRole('alert').textContent()) ?? '';
    }, { timeout: 15_000 }).toContain('expired or was already used');
  } finally {
    await receiver.close();
  }
});
