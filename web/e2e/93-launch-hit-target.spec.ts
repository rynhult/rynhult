import { expect, test } from '@playwright/test';
import { AUTH_FILE, gitProject } from './helpers';

test.use({ storageState: AUTH_FILE, viewport: { width: 390, height: 470 }, isMobile: true, hasTouch: true });

test('Launch stays a non-click-through target while agent discovery is pending', async ({ page }) => {
  await gitProject(page, 'launch-hit-target');
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/providers/registry*', async route => {
    await pending;
    await route.continue();
  });
  try {
    await page.reload();
    await page.getByRole('button', { name: 'New session' }).first().click();
    const dialog = page.getByRole('dialog');
    const launch = dialog.getByRole('button', { name: /^(Trust and )?Launch$/ });
    await expect(launch).toBeDisabled();
    await launch.scrollIntoViewIfNeeded();
    expect(await launch.evaluate(button => {
      const r = button.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !!hit && (hit === button || button.contains(hit));
    })).toBe(true);
    await expect(dialog.getByRole('status')).toContainText('Checking available agents');
  } finally {
    release();
  }
});
