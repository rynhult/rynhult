import { expect } from '@playwright/test';
import { test } from './shell-state';
import { AUTH_FILE, expectAxeClean, gitProject } from './helpers';

test.use({ storageState: AUTH_FILE });
for (const width of [1440, 390]) test.describe(`${width}px context spacing`, () => {
  test.use({ viewport: { width, height: 900 }, hasTouch: width === 390, isMobile: width === 390 });
  test('title and stats share the collapse band; lists stay compact with usable targets', async ({ page }, testInfo) => {
    test.slow(); // Six destinations and two accessibility scans per viewport.
    const project = await gitProject(page, 'context-spacing');
    await page.route('**/api/attention*', route => route.fulfill({ json: [] }));
    await page.route('**/api/board', route => route.fulfill({ json: { generated_at: Date.now() / 1000, projects: [], machines: [
      { name: 'local', label: 'Cedar main', local: true, connected: true, light: null, alerts: [] },
      { name: 'edge', label: 'Maple node', local: false, connected: false, light: null, alerts: [] },
    ] } }));
    for (const [path, title] of [['/inbox', 'Inbox'], ['/hosts', 'Hosts'], ['/projects', 'Projects'], [`/projects/${project.id}`, project.slug], ['/settings', 'Settings'], ['/providers', 'Tools']]) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
      const panel = width === 390 ? page.getByRole('dialog', { name: 'Browse', exact: true }) : page.getByRole('complementary', { name: 'Context sidebar' });
      const content = panel.locator('.shell-sidebar-content');
      const heading = content.getByRole('heading', { name: title, exact: true, level: 2 });
      await expect(heading).toBeVisible();
      if (path === `/projects/${project.id}`) await expect(panel.getByRole('region', { name: `${project.slug} context`, exact: true }).getByRole('button', { name: `Pin ${project.slug}`, exact: true })).toBeVisible();
      const top = (await content.boundingBox())!.y;
      const box = (await heading.boundingBox())!;
      expect.soft(box.y - top, `${path}: no empty title band`).toBeLessThanOrEqual(12);
      if (width === 1440) {
        const collapse = (await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).boundingBox())!;
        expect.soft(box.y, `${path}: title overlaps collapse row`).toBeLessThan(collapse.y + collapse.height);
        expect.soft(box.x + box.width, `${path}: title avoids collapse target`).toBeLessThanOrEqual(collapse.x);
      }
      if (path === '/inbox') {
        const stats = content.getByText('0 open items shown', { exact: true });
        await expect(stats).toBeVisible();
        expect.soft((await stats.boundingBox())!.y - top).toBeLessThanOrEqual(32);
        await expect(content.getByText('Browsing keeps your work open.', { exact: false })).toBeVisible();
      }
      const links = content.locator('a.shell-link:visible');
      // The static Projects/Hosts title can precede the queried list, especially
      // after the stateful family has accumulated many projects. Measure rows
      // only once the list has rendered, rather than snapshotting a zero count.
      await expect(links.first(), `${path}: context links loaded`).toBeVisible();
      for (const link of (await links.all()).slice(0, 8)) {
        const bounds = (await link.boundingBox())!;
        expect.soft(bounds.height, `${path}: compact link ${await link.textContent()}`).toBeLessThanOrEqual(width === 390 ? 48 : 40);
        expect.soft(bounds.height, `${path}: usable link target`).toBeGreaterThanOrEqual(width === 390 ? 44 : 36);
      }
      if (path === '/inbox') await page.screenshot({ path: testInfo.outputPath('inbox-context.png'), fullPage: true });
      if (path === '/inbox' || path === '/hosts') await expectAxeClean(page);
    }
  });
});
