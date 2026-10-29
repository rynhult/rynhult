import { test, expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { AUTH_FILE, routeDemoSessions, expectAxeClean, wikiShotPath } from './helpers';

// Artificial fixtures only; the suite owns the server and its temporary database.
test.use({ storageState: AUTH_FILE });
for (const theme of ['dark', 'light'] as const) for (const width of [1440, 390]) {
  test(`brand review ${theme} ${width}`, async ({ page }) => {
    test.setTimeout(90000);
    await page.emulateMedia({ colorScheme: theme });
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await routeDemoSessions(page);
    // Keep the review comparable with the fresh stage-1 capture, regardless of earlier flow tests.
    await page.route('**/api/projects', route => route.fulfill({ json: [] }));
    await page.route('**/api/multiruns', route => route.fulfill({ json: [] }));
    await page.route('**/api/attention*', route => route.fulfill({ json: [] }));
    await page.route('**/api/preferences/phone-banner', route => route.fulfill({ json: { dismissed: false } }));
    await page.route('**/api/nodes', async route => {
      const nodes = await (await route.fetch()).json();
      await route.fulfill({ json: nodes.filter((node: { name: string }) => ['lab-box', 'studio-mac'].includes(node.name)).map((node: object) => ({ ...node, role: 'always-on', connected: true, status: 'connected', offline_alert: false })) });
    });
    const phase = process.env.RYNHULT_REVIEW_PHASE;
    const out = `../.acc/artifacts/${phase ?? 'after'}`;
    if (phase) mkdirSync(out, { recursive: true });
    for (const [url, name] of [['/', 'overview'], ['/sessions', 'sessions'], ['/inbox', 'inbox']]) {
      await page.goto(url);
      await page.waitForLoadState('networkidle');
      await expect(page.locator('h1').first()).toBeVisible();
      if (phase) await page.screenshot({ animations: 'disabled', path: `${out}/${name}-${theme}-${width}.png` });
      if (phase !== 'before') await expectAxeClean(page);
    }
    // Public login, without signing out other tests' shared account.
    await page.route('**/api/auth/state', async route => {
      const data = await (await route.fetch()).json();
      await route.fulfill({ json: { ...data, authenticated: false } });
    });
    await page.goto('/login');
    await page.waitForLoadState('networkidle');
    await expect(page.getByLabel('Username')).toBeVisible();
    if (phase) await page.screenshot({ animations: 'disabled', path: `${out}/login-${theme}-${width}.png` });
    if (phase !== 'before') {
      await expect(page.getByText('One place to supervise your coding agents.', { exact: true })).toBeVisible();
      await expect(page.getByText('Your agents. Your machines. Your call.', { exact: true })).toBeVisible();
      await expectAxeClean(page);
      if (process.env.ACC_UPDATE_WIKI_SCREENSHOTS === '1' && theme === 'dark' && width === 1440) {
        await page.screenshot({ animations: 'disabled', path: wikiShotPath(test.info(), 'login.png') });
      }
    }
  });
}

for (const theme of ['dark', 'light'] as const) for (const severity of ['critical', 'warn']) {
  test(`Inbox count remains readable on solid ${severity} in ${theme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: theme });
    const now = new Date().toISOString();
    await page.route('**/api/attention*', route => route.fulfill({ json: [{
      id: 9001, kind: 'ci_failed', severity, title: 'Example build needs attention', source: 'agent', verified: true, detail: null,
      session_id: null, session_name: null, project: null, created_at: now, updated_at: now, acked_at: null, resolved_at: null, publish_id: null,
    }] }));
    await page.goto('/');
    const badge = page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: /Inbox/ }).locator('span.tnum');
    await expect(badge).toHaveText('1');
    const contrast = await badge.evaluate(el => {
      const style = getComputedStyle(el);
      const luminance = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(v => {
        const c = v / 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
      }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const a = luminance(style.color), b = luminance(style.backgroundColor);
      return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
    await expectAxeClean(page);
  });
}
