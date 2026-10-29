import { expect, test } from '@playwright/test';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const phone of [false, true]) {
  test(`long Host page keeps its shell in view (${phone ? 'phone safe areas' : 'desktop'})`, async ({ page }) => {
    if (phone) await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/host');
    await expect(page.getByRole('heading', { name: 'Host', exact: true })).toBeVisible();
    // Nonzero insets model an installed app drawing under the status bar/home indicator.
    if (phone) await page.addStyleTag({ content: 'header { padding-top: 47px !important } nav[aria-label="Primary"] { padding-bottom: 34px !important }' });
    const main = page.getByRole('main');
    await main.evaluate(el => {
      const longContent = document.createElement('div');
      longContent.style.height = '4000px';
      longContent.textContent = 'Long Host content';
      el.append(longContent);
      el.scrollTop = el.scrollHeight;
    });
    await main.hover();
    await page.mouse.wheel(0, 100000);
    await page.evaluate(() => window.scrollTo(0, 100000));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(await page.evaluate(() => innerHeight));
    await expect(main).toBeInViewport();
    expect(await main.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(1);
    if (phone) {
      await expect(page.getByRole('navigation', { name: 'Primary', exact: true })).toBeInViewport({ ratio: 1 });
      await page.getByRole('button', { name: 'Menu', exact: true }).click();
      await expect(page.getByRole('dialog')).toBeInViewport({ ratio: 1 });
    } else await expect(page.locator('aside')).toBeInViewport({ ratio: 1 });
  });
}
