// Focused download-page browser regression; run against a product-only scratch build.
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../web/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const { default: AxeBuilder } = require('@axe-core/playwright');
const [base, shots] = process.argv.slice(2);
if (!base) throw new Error('usage: node site/test/download-layout.mjs BASE_URL [SCREENSHOT_DIR]');
if (shots) mkdirSync(shots, { recursive: true });
const browser = await chromium.launch();
try {
  for (const theme of ['light', 'dark']) {
    for (const width of [320, 390, 1440]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: theme, reducedMotion: 'reduce' });
      const page = await context.newPage();
      await page.goto(`${base}/download/`, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      const checksums = page.getByRole('region', { name: /^SHA-256 for / });
      assert.equal(await checksums.count(), 4, 'each checksum has its own labelled, focusable region');
      for (const checksum of await checksums.all()) {
        await checksum.focus();
        assert.ok(await checksum.evaluate(el => document.activeElement === el), 'checksum is keyboard focusable');
        const size = await checksum.evaluate(el => {
          const cell = el.closest('td').getBoundingClientRect();
          const box = el.getBoundingClientRect();
          return { hash: el.textContent.trim(), overflow: getComputedStyle(el).overflowX,
            contained: box.left >= cell.left && box.right <= cell.right + 1 };
        });
        assert.match(size.hash, /^[a-f0-9]{64}$/);
        assert.equal(size.overflow, 'auto');
        assert.ok(size.contained, 'hash region stays inside its cell');
        if (await checksum.evaluate(el => el.scrollWidth > el.clientWidth)) {
          await checksum.press('ArrowRight');
          await expect.poll(() => checksum.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
        }
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no page overflow');
      if (width === 1440) {
        const description = await page.locator('.release-downloads tbody td:nth-child(2)').first().boundingBox();
        assert.ok(description.width >= 250, 'desktop descriptions have room to read');
      }
      const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
      assert.deepEqual(axe.violations.map(v => v.id), [], 'download accessibility');
      const table = page.getByRole('region', { name: 'Release files', exact: true });
      if (width < 640) {
        await table.focus();
        await table.press('ArrowRight');
        await expect.poll(() => table.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
      }
      await page.evaluate(() => {
        document.activeElement.blur();
        document.querySelectorAll('.download-checksum, .table-scroll').forEach(el => { el.scrollLeft = 0; });
        scrollTo(0, 0);
      });
      if (shots && width !== 320) await page.screenshot({ path: `${shots}/download-${width}-${theme}.png`, fullPage: true });
      console.log(`ok /download/ ${width} ${theme}`);
      await context.close();
    }
  }
} finally { await browser.close(); }
