import { expect, test, type Page } from '@playwright/test';
import { AUTH_FILE, gitProject } from './helpers';

// RC accessibility audit (beyond axe): what a screen reader hears, and that every tab list is named.
test.use({ storageState: AUTH_FILE });

/** Live regions (not hidden, not aria-live=off) whose text includes `text`: each one is a separate announcement. */
const liveRegionsWith = (page: Page, text: string) => page.evaluate((wanted) =>
  [...document.querySelectorAll('[aria-live],[role=status],[role=alert],[role=log]')]
    .filter(e => e.getAttribute('aria-live') !== 'off' && !e.closest('[hidden]') && (e.textContent ?? '').includes(wanted)).length, text);

test('Settings announces a late error once, a late warning elsewhere once, and nothing for a section change', async ({ page }) => {
  let answer!: () => void;
  const delayed = new Promise<void>(resolve => { answer = resolve; });
  await page.route('**/api/push', async route => {
    await delayed;
    await route.fulfill({ status: 403, json: { detail: 'Notification settings need review' } });
  });
  await page.goto('/settings#notifications');
  const chooser = page.getByRole('combobox', { name: 'Settings section', exact: true });
  await chooser.selectOption('passkeys');
  answer();
  const attention = page.getByRole('region', { name: 'Settings needing attention' });
  await expect(attention).toContainText('Notification settings need review');
  // Errored sections stay shown and their own alerts speak (one failed /api/push read shows in both Notifications
  // cards); the summary repeating them was one more announcement of the same error.
  const alerts = await page.getByRole('alert').filter({ hasText: 'Notification settings need review' }).count();
  expect(alerts).toBeGreaterThan(0);
  await expect.poll(() => liveRegionsWith(page, 'Notification settings need review')).toBe(alerts);

  // A warning that arrives later in a hidden section is announced once, by the Settings announcer.
  let warn = false;
  await page.route('**/api/updates', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), ...(warn ? { blocked: 'Updates paused for an example review' } : {}) } });
  });
  const announcer = page.getByTestId('settings-announcer');
  await expect(announcer).toHaveText('');
  warn = true;
  await expect(announcer).toContainText('Updates: Updates paused for an example review', { timeout: 15000 });
  await expect.poll(() => liveRegionsWith(page, 'Updates paused for an example review')).toBe(1);

  // Choosing sections reshuffles the summary; that is navigation, not news.
  const said = await announcer.textContent();
  for (const section of ['password', 'sign-ins', 'passkeys']) {
    await chooser.selectOption(section);
    await expect(page.locator(`#${section}`)).toBeVisible();
  }
  await expect(announcer).toHaveText(said ?? '');
});

test('every tab list on the project page has a name, and the selected tab and its panel point at each other', async ({ page }) => {
  const { id } = await gitProject(page, 'a11y-tabs');
  await page.goto(`/projects/${id}`);
  await expect(page.getByRole('tablist', { name: 'Project views', exact: true })).toBeVisible();
  await expect(page.getByRole('tablist', { name: 'Project sections', exact: true })).toBeVisible();
  const lists = await page.evaluate(() => [...document.querySelectorAll('[role=tablist]')].map(list => {
    const tab = list.querySelector('[role=tab][aria-selected=true]')!;
    const panel = document.getElementById(tab.getAttribute('aria-controls') ?? '');
    return { named: !!(list.getAttribute('aria-label') || list.getAttribute('aria-labelledby')),
      wired: panel?.getAttribute('role') === 'tabpanel' && panel.getAttribute('aria-labelledby') === tab.id };
  }));
  expect(lists.length).toBe(2);
  expect(lists).toEqual(lists.map(() => ({ named: true, wired: true })));
});

test('Host announces a machine check the person asked for, not the page loading', async ({ page }) => {
  await page.goto('/host');
  const announcer = page.getByTestId('machine-check-announcer');
  await expect(announcer).toHaveText('');
  await page.getByRole('button', { name: 'Check machines now', exact: true }).click();
  await expect(announcer).toHaveText(/^Checked \d+ machines?: \d+ responding/);
  // Opening Host again shows the last result without reading the whole list out.
  await page.reload();
  await expect(page.getByText(/Connected and responding/).first()).toBeVisible();
  await expect(announcer).toHaveText('');
  expect(await liveRegionsWith(page, 'Connected and responding')).toBe(0);
});

test('Providers announces that Check now finished', async ({ page }) => {
  await page.goto('/providers');
  const announcer = page.getByTestId('providers-check-announcer');
  await expect(announcer).toHaveText('');
  await page.getByRole('button', { name: 'Check now', exact: true }).click();
  await expect(announcer).toHaveText('Sign-ins and usage checked.');
});
