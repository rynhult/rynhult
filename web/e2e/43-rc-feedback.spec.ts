import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
const destination = 'https://github.com/rynhult/rynhult/issues/new';
for (const version of ['0.15.0-rc.1', '0.15.0-beta.2', '0.15.0', '0.15.0+build-rc.1', undefined]) {
  test(`RC feedback follows the running version: ${version ?? 'unknown'}`, async ({ page }, testInfo) => {
    await page.route('**/api/auth/state', async route => {
      const response = await route.fetch();
      const state = await response.json();
      await route.fulfill({ response, json: { ...state, instance: { ...state.instance, version } } });
    });
    await page.goto('/settings#usage-and-bug-reports');
    await expect(page.getByRole('heading', { name: 'Usage and bug reports', exact: true })).toBeVisible();
    await expect(page.getByLabel('Offer bug reports for review')).not.toBeChecked();
    const feedback = page.getByRole('region', { name: 'RC feedback', exact: true });
    if (version === '0.15.0-rc.1' || version === '0.15.0-beta.2') {
      await expect(feedback).toBeVisible();
      await expect(feedback).toContainText('No telemetry opt-in is needed.');
      const link = feedback.getByRole('link', { name: 'Report an RC bug on GitHub', exact: true });
      await expect(link).toHaveAttribute('href', destination);
      await expect(feedback).not.toContainText(/invitation|private repository/i);
      await expect(feedback).toContainText('Never include secrets or logs containing tokens.');
      await expect(link).toHaveAttribute('rel', /noreferrer/);
      await expect(feedback.getByRole('link', { name: 'support@rynhult.dev' })).toHaveAttribute('href', 'mailto:support@rynhult.dev');
      await page.setViewportSize({ width: 390, height: 844 });
      await page.emulateMedia({ colorScheme: 'light' });
      await feedback.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await link.focus();
      await expect(link).toBeFocused();
      await expectAxeClean(page, '#telemetry');
      if (version === '0.15.0-rc.1' && testInfo.project.name === 'desktop') {
        await feedback.screenshot({ path: wikiShotPath(testInfo, 'rc-feedback-phone.png') });
      }
    } else {
      await expect(feedback).toHaveCount(0);
      await expect(page.locator(`a[href="${destination}"]`)).toHaveCount(0);
    }
  });
}

test('RC feedback stays available when telemetry is turned off in Admin features', async ({ page }) => {
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    const state = await response.json();
    await route.fulfill({ response, json: { ...state, instance: { ...state.instance, version: '0.15.0-rc.1', features: { ...state.instance.features, telemetry: false } } } });
  });
  await page.goto('/settings#usage-and-bug-reports');
  await expect(page.getByText('Turned off in Admin features: nothing new is shared')).toBeVisible();
  const feedback = page.getByRole('region', { name: 'RC feedback', exact: true });
  await expect(feedback.getByRole('link', { name: 'Report an RC bug on GitHub', exact: true })).toHaveAttribute('href', destination);
  await expectAxeClean(page, '#telemetry');
});
