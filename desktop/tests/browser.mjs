// Supplemental Chromium coverage. This deliberately makes no WebKitGTK/AT-SPI claim.
import { chromium, expect } from '../../web/node_modules/@playwright/test/index.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
  await context.addCookies(config.cookies.map(c => ({ ...c, domain: new URL(config.origin).hostname })));
  const page = await context.newPage();
  await page.goto(config.origin);
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('link', { name: 'Projects', exact: true }).first()).toBeVisible();
  const focused = [];
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab');
    focused.push(await page.evaluate(() => ({ tag: document.activeElement.tagName,
      name: document.activeElement.getAttribute('aria-label') || document.activeElement.textContent?.trim().slice(0, 80) })));
  }
  if (focused.every(item => item.tag === 'BODY')) throw new Error('Keyboard cannot reach controls');
  const projects = page.getByRole('link', { name: 'Projects', exact: true }).first();
  await projects.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects/);
  await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
  await page.waitForLoadState('networkidle');
  const snapshot = await page.locator('body').ariaSnapshot();
  if (!snapshot.includes('Projects')) throw new Error('Projects absent from accessibility tree');
  await page.screenshot({ path: config.screenshot });
  process.stdout.write(JSON.stringify({ focused, snapshot, urlPath: new URL(page.url()).pathname }));
} finally {
  await browser.close();
}
