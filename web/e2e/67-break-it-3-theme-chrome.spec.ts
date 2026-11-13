import { test, expect } from './fixtures';
import { AUTH_FILE } from './helpers';

test.use({ storageState: AUTH_FILE });

for (const width of [1440, 390]) for (const choice of ['dark', 'light'] as const) {
  test(`browser chrome follows explicit ${choice} rather than the opposite system theme at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.emulateMedia({ colorScheme: choice === 'dark' ? 'light' : 'dark' });
    await page.addInitScript(value => localStorage.setItem('acc-theme', value), choice);
    for (const path of ['/', '/board', '/pair-device']) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      await expect(page.locator('html')).toHaveClass(choice === 'dark' ? /dark/ : /^(?!.*dark).*$/);
      await expect.poll(() => page.evaluate(() => {
        const meta = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')]
          .find(item => !item.media || matchMedia(item.media).matches);
        return meta?.content === getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
      })).toBe(true);
    }
  });
}
