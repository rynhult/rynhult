import { expect, test } from '@playwright/test';
import { expectAxeClean } from './helpers';

test('axe checks settled theme colors rather than an in-flight crossfade', async ({ page }) => {
  await page.setContent(`<!doctype html><html lang="en"><head><title>Theme audit fixture</title>
    <style>button { color: #ededef; background: #111114; font-size: 16px;
      transition: color 2s linear, background-color 2s linear; }
    .light button { color: #18181b; background: #fff; }</style></head>
    <body><main><h1>Output controls</h1><button>Copy recent output</button></main></body></html>`);
  await expect(page.getByRole('button')).toHaveCSS('background-color', 'rgb(17, 17, 20)');
  await page.evaluate(() => document.documentElement.classList.add('light'));
  await page.waitForFunction(() => {
    const red = Number(getComputedStyle(document.querySelector('button')!).backgroundColor.match(/\d+/)![0]);
    return red > 100 && red < 190;
  });
  await expectAxeClean(page);
});
