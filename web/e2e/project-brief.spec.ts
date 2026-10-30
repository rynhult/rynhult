import { expect, test } from '@playwright/test';
import { AUTH_FILE, expectAxeClean, gitProject, info, wikiShotPath, createProjectFromDialog } from './helpers';
import { appendFileSync, readFileSync, existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

test.use({ storageState: AUTH_FILE });

test('New project keeps the build description and starts a saved brief', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  const dialog = page.getByRole('dialog', { name: 'New project', exact: true });
  const description = dialog.getByRole('textbox', { name: 'What do you want to build? (starts your project brief)', exact: true });
  await expect(description).toHaveAttribute('maxlength', '2000');
  await description.fill('A private room booking board');
  await dialog.getByRole('radio', { name: 'Clone repository', exact: true }).click();
  await expect(description).toHaveValue('A private room booking board');
  await dialog.getByRole('radio', { name: 'Empty folder', exact: true }).click();
  await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill(`Booking ${randomUUID().slice(0, 8)}`);
  const { id } = await createProjectFromDialog(page, dialog);
  const saved = await (await page.request.get(`/api/projects/${id}/brief`)).json();
  expect(saved.current.submitted.product).toBe('A private room booking board');
  expect(saved.current.actor).toBe('alex');
  await page.goto(`/projects/${id}#project-brief`);
  await expect(page.getByText('Saved as version 1', { exact: true })).toBeVisible();
});

for (const origin of ['project', 'grant'] as const) for (const choice of ['save', 'without', 'back'] as const) {
  test(`unsaved ${origin} brief offers an explicit ${choice} choice before team launch`, async ({ page }, ti) => {
    const { id, headers } = await gitProject(page, `brief-launch-${origin}`);
    const productText = 'Private room bookings with accessible controls';
    await page.goto(`/projects/${id}${origin === 'project' ? '#project-brief' : ''}`);
    if (origin === 'project') await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill(productText);
    await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
    await dialog.getByRole('button', { name: /^Advanced/ }).click();
    await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
    const task = dialog.getByRole('textbox', { name: 'Task (optional)', exact: true });
    await expect(task).toHaveValue(/no accepted brief/);
    const personalTask = 'Plan the booking workflow without a saved brief.';
    if (origin === 'grant' && choice === 'without') await task.fill(personalTask);
    if (origin === 'grant') {
      await dialog.getByText('Add a project brief', { exact: true }).click();
      await dialog.getByRole('textbox', { name: 'Product or feature', exact: true }).fill(productText);
      if (choice === 'save') {
        await dialog.getByText('References with captions (0/20)', { exact: true }).click();
        await dialog.getByRole('textbox', { name: 'Reference caption', exact: true }).fill('Booking flow sketch');
        await dialog.getByRole('textbox', { name: 'What to copy, keep, change or avoid', exact: true }).fill('Keep the large booking controls.');
        await dialog.getByRole('textbox', { name: 'Reference link', exact: true }).fill('https://example.com/booking-sketch');
        await dialog.getByRole('button', { name: 'Add reference', exact: true }).click();
        await expect(dialog.getByText('REF-1: Booking flow sketch', { exact: true })).toBeVisible();
      }
    }
    let launches = 0;
    page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') launches++; });
    await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
    const step = dialog.getByRole('heading', { name: 'Save this brief as version 1 and pin it to the team?', exact: true });
    await expect(step).toBeVisible();
    await expect(step).toBeFocused();
    await expect(dialog).toContainText(productText);
    expect(launches).toBe(0);
    expect(await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    })).toBe(true);
    await expectAxeClean(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (origin === 'grant' && choice === 'save' && ['brief-1440-dark', 'brief-390-dark'].includes(ti.project.name)) {
      await dialog.screenshot({ path: wikiShotPath(ti, `project-brief-save-launch${ti.project.name === 'brief-390-dark' ? '-phone' : ''}.png`) });
    }
    const action = choice === 'back' ? 'Back' : choice === 'save' ? 'Save and launch' : 'Launch without the brief';
    for (let tab = 0; tab < (choice === 'back' ? 1 : choice === 'without' ? 2 : 3); tab++) await page.keyboard.press('Tab');
    await expect(dialog.getByRole('button', { name: action, exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    if (choice === 'back') {
      await expect(step).toHaveCount(0);
      await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeFocused();
      await dialog.getByText('Add a project brief', { exact: true }).click();
      await expect(dialog.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue(productText);
      expect(launches).toBe(0);
      expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(0);
      return;
    }
    await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
    const session = new URL(page.url()).pathname.split('/').at(-1)!;
    try {
      expect(launches).toBe(1);
      const pinned = await (await page.request.get(`/api/teams/${session}/brief`)).json();
      const launched = await (await page.request.get(`/api/sessions/${session}`)).json();
      const firstMessage = launched.launch_spec.inputs.initial_prompt;
      if (choice === 'save') {
        expect(firstMessage).toContain('pinned project brief v1');
        expect(firstMessage).not.toContain(productText);
      } else if (origin === 'grant') expect(firstMessage).toBe(personalTask);
      else expect(firstMessage).toContain('no accepted brief');
      expect(pinned?.version ?? 0).toBe(choice === 'save' ? 1 : 0);
      expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(choice === 'save' ? 1 : 0);
      if (choice === 'save') {
        expect(pinned.submitted.product).toBe(productText);
        if (origin === 'grant') expect(pinned.references[0].caption).toBe('Booking flow sketch');
      } else {
        await page.goto(`/projects/${id}#project-brief`);
        await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue(productText);
        await expect(page.getByText('Draft, not saved', { exact: true })).toBeVisible();
      }
    } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
  });
}

for (const origin of ['project', 'grant'] as const) test(`a ${origin} draft still requires consent when browser storage is full`, async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-storage-full');
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => { throw new DOMException('Synthetic full storage', 'QuotaExceededError'); };
  });
  await page.goto(`/projects/${id}${origin === 'project' ? '#project-brief' : ''}`);
  if (origin === 'project') await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Keep this idea in memory');
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  if (origin === 'grant') {
    await dialog.getByText('Add a project brief', { exact: true }).click();
    await dialog.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Keep this idea in memory');
  }
  let prematureLaunches = 0;
  await page.route('**/api/sessions', route => {
    if (route.request().method() !== 'POST') return route.continue();
    prematureLaunches++;
    return route.fulfill({ status: 503, json: { detail: 'Unexpected premature launch' } });
  });
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'Save this brief as version 1 and pin it to the team?', exact: true })).toBeVisible();
  expect(prematureLaunches).toBe(0);
  await page.unroute('**/api/sessions');
  await dialog.getByRole('button', { name: 'Save and launch', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
  const session = new URL(page.url()).pathname.split('/').at(-1)!;
  try {
    expect((await (await page.request.get(`/api/teams/${session}/brief`)).json()).submitted.product).toBe('Keep this idea in memory');
  } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('a failed brief save never launches a team and Back keeps the draft', async ({ page }) => {
  const { id } = await gitProject(page, 'brief-launch-failure');
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  await dialog.getByText('Add a project brief', { exact: true }).click();
  await dialog.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Keep this booking idea');
  await page.route(`**/api/projects/${id}/brief`, route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic save outage' } }) : route.continue());
  let launches = 0;
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') launches++; });
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  await dialog.getByRole('button', { name: 'Save and launch', exact: true }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  expect(launches).toBe(0);
  expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(0);
  await dialog.getByRole('button', { name: 'Back', exact: true }).click();
  await dialog.getByText('Add a project brief', { exact: true }).click();
  await expect(dialog.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('Keep this booking idea');
});

for (const action of ['retry', 'back'] as const) test(`saved launch failure can ${action} without saving another version or losing acknowledgement`, async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-launch-retry');
  const initial = await page.request.post(`/api/projects/${id}/brief`, { headers,
    data: { version: 0, fields: { product: 'Existing bookings', run_mode: 'prepare' } } });
  expect(initial.ok()).toBeTruthy();
  const proposal = await page.request.post(`/api/projects/${id}/brief/import`, { headers, data: { markdown: (await initial.json()).markdown } });
  expect(proposal.ok()).toBeTruthy();
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Revised private bookings');
  await page.getByRole('dialog', { name: 'There is a proposed brief from your documents; review it first?' }).getByRole('button', { name: 'Continue', exact: true }).click();
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'Save this brief as version 3 and pin it to the team?', exact: true })).toBeVisible();
  await page.route('**/api/sessions', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic launch outage' } }) : route.continue());
  await dialog.getByRole('button', { name: 'Save and launch', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Brief version 3 is saved');
  expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(3);
  await page.unroute('**/api/sessions');
  if (action === 'back') {
    await dialog.getByRole('button', { name: 'Back', exact: true }).click();
    await dialog.getByText('Add a project brief', { exact: true }).click();
    await expect(dialog.getByText('Saved as version 3', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.getByText('Saved as version 3', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Projects', exact: true }).first().click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    return;
  }
  await dialog.getByRole('button', { name: 'Launch with saved brief', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
  const session = new URL(page.url()).pathname.split('/').at(-1)!;
  try {
    expect((await (await page.request.get(`/api/teams/${session}/brief`)).json()).version).toBe(3);
    expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).next_version).toBe(4);
  } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('Launch without the brief skips an older saved version and retains its newer draft', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-skip-existing');
  expect((await page.request.post(`/api/projects/${id}/brief`, { headers,
    data: { version: 0, fields: { product: 'Original bookings', run_mode: 'prepare' } } })).ok()).toBeTruthy();
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('New booking scope, not saved');
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  await expect(dialog.getByRole('combobox', { name: 'Project brief version', exact: true })).toHaveValue('1');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  await dialog.getByRole('button', { name: 'Launch without the brief', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
  const session = new URL(page.url()).pathname.split('/').at(-1)!;
  try {
    expect(await (await page.request.get(`/api/teams/${session}/brief`)).json()).toBeNull();
    expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(1);
    await page.goto(`/projects/${id}#project-brief`);
    await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('New booking scope, not saved');
    await expect(page.getByText('Draft, not saved', { exact: true })).toBeVisible();
  } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('brief validation explains Save and restored drafts never claim to be saved', async ({ page }, info) => {
  const { id } = await gitProject(page, 'brief-unsaved');
  await page.goto(`/projects/${id}#project-brief`);
  const product = page.getByRole('textbox', { name: 'Product or feature', exact: true });
  const save = page.getByRole('button', { name: 'Save project brief', exact: true });
  await expect(page.getByText('Add what you want to build in Product or feature to save', { exact: true })).toBeVisible();
  await expect(save).toBeDisabled();
  await save.press('Enter');
  await expect(product).toBeFocused();
  await page.getByRole('textbox', { name: 'People and problem', exact: true }).fill('Neighbours need a room.');
  await expect(page.getByText('Draft, not saved', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Draft, not saved', { exact: true })).toBeVisible();
  await expect(page.getByText(/Unsaved draft restored/)).toBeVisible();
  await expect(product.locator('..')).toContainText('(required)');
  expect((await (await page.request.get(`/api/projects/${id}/brief`)).json()).current_version).toBe(0);
  if (info.project.name === 'brief-1440-dark') await page.screenshot({ path: wikiShotPath(info, 'project-brief-draft.png'), fullPage: true });
  await product.fill('Private room booking board');
  const views = page.getByRole('tab', { name: 'Files', exact: true });
  await views.click();
  const warning = page.getByRole('dialog', { name: 'Leave with an unsaved project brief?', exact: true });
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(product).toHaveValue('Private room booking board');
  await views.click();
  await warning.getByRole('button', { name: 'Leave without saving', exact: true }).click();
  await expect(product).toHaveCount(0);
  await page.goto(`/projects/${id}#project-brief`);
  await expect(product).toHaveValue('Private room booking board');
  await save.click();
  await expect(page.getByText('Saved as version 1', { exact: true })).toBeVisible();
  await expect(page.getByText('Draft, not saved', { exact: true })).toHaveCount(0);
  await views.click();
  await expect(warning).toHaveCount(0);
});

for (const selection of ['latest', 'older', 'none'] as const) test(`grant displays latest saved version by default and launches with ${selection}`, async ({ page }, info) => {
  const { id, headers } = await gitProject(page, 'brief-visible-pin');
  for (const version of [0, 1]) {
    const saved = await page.request.post(`/api/projects/${id}/brief`, { headers,
      data: { version, fields: { product: `Booking board ${version + 1}`, run_mode: 'build' } } });
    expect(saved.ok(), await saved.text()).toBeTruthy();
  }
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  const choice = dialog.getByRole('combobox', { name: 'Project brief version', exact: true });
  await expect(choice).toHaveValue('2');
  await expect(dialog.getByText('This team will pin saved project brief version 2.', { exact: true })).toBeVisible();
  if (info.project.name === 'brief-1440-dark' && selection === 'latest') await dialog.screenshot({ path: wikiShotPath(info, 'project-brief-grant.png') });
  const expected = selection === 'latest' ? 2 : selection === 'older' ? 1 : 0;
  if (selection !== 'latest') await choice.selectOption(String(expected));
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await expect(dialog.getByText(expected ? `Project brief: saved version ${expected}.` : 'Project brief: none pinned.', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await expect(choice).toHaveValue(String(expected));
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  const sent = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions');
  await dialog.getByRole('button', { name: 'Launch', exact: true }).click();
  const granted = (await sent).postDataJSON().orchestrator.brief_version;
  await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
  const session = new URL(page.url()).pathname.split('/').at(-1)!;
  try {
    expect(granted).toBe(expected);
    const pinned = await (await page.request.get(`/api/teams/${session}/brief`)).json();
    expect(pinned?.version ?? 0).toBe(expected);
    if (selection !== 'none') return;
    await page.goto(`/sessions/${session}?tab=team#overview`);
    await expect(page.getByRole('heading', { name: 'No pinned project brief', exact: true })).toBeVisible();
    const teamChoice = page.getByRole('combobox', { name: 'Project brief version', exact: true });
    await expect(teamChoice).toHaveValue('2');
    await page.getByRole('button', { name: 'Apply selected brief version', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Pinned project brief · version 2', exact: true })).toBeVisible();
    await teamChoice.selectOption('0');
    await page.getByRole('button', { name: 'Apply selected brief version', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'No pinned project brief', exact: true })).toBeVisible();
  } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('grant distinguishes a tab-only draft from no project brief', async ({ page }) => {
  const { id } = await gitProject(page, 'brief-draft-grant');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'People and problem', exact: true }).fill('Draft only, no product yet.');
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  await expect(dialog.getByText('This project has only an unsaved draft in this tab. The team cannot read it.', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('link', { name: 'Save the project brief', exact: true })).toHaveAttribute('href', `/projects/${id}#project-brief`);
  await dialog.getByRole('link', { name: 'Save the project brief', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole('button', { name: 'Discard draft', exact: true }).click();
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  await expect(dialog.getByText('This project has no saved brief.', { exact: true })).toBeVisible();
});

test('Team explains a local draft and can pin it only after the person saves and applies it', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-team-draft');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Private booking board');
  const launched = await page.request.post('/api/sessions', { headers, data: {
    project_id: id, provider: 'claude', worktree: true, name: 'Booking lead',
    orchestrator: { brief_version: 0, agents: [{ agent: 'claude', profile: null }], machines: ['local'],
      at_once: 2, total: 4, depth: 1, hours: 2 },
  } });
  expect(launched.ok(), await launched.text()).toBeTruthy();
  const session = (await launched.json()).id;
  try {
    await page.goto(`/sessions/${session}?tab=team#overview`);
    await expect(page.getByRole('heading', { name: 'No pinned project brief', exact: true })).toBeVisible();
    await expect(page.getByText('This project has only an unsaved draft in this tab. The team cannot read it.', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Save the project brief', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Product or feature', exact: true })).toHaveValue('Private booking board');
    await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
    await expect(page.getByText('Saved as version 1', { exact: true })).toBeVisible();
    await page.goto(`/sessions/${session}?tab=team#overview`);
    await expect(page.getByRole('heading', { name: 'No pinned project brief', exact: true })).toBeVisible();
    await expect(page.getByRole('combobox', { name: 'Project brief version', exact: true })).toHaveValue('1');
    await page.getByRole('button', { name: 'Apply selected brief version', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Pinned project brief · version 1', exact: true })).toBeVisible();
    await expectAxeClean(page);
  } finally { expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy(); }
});

test('a failed brief read blocks grant launch until its visible version can be reviewed', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-grant-error');
  const saved = await page.request.post(`/api/projects/${id}/brief`, { headers,
    data: { version: 0, fields: { product: 'Private booking board', run_mode: 'build' } } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  await page.route(`**/api/projects/${id}/brief`, route => route.fulfill({ status: 503, json: { detail: 'Synthetic brief outage' } }));
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  // Keep the outage until both the picker and lazy brief form have mounted and failed.
  // Otherwise the lazy observer can recover automatically after unroute, removing Retry mid-click.
  await expect(dialog.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(2);
  await expect(dialog.getByRole('button', { name: 'Retry', exact: true }).first()).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeDisabled();
  await page.unroute(`**/api/projects/${id}/brief`);
  await dialog.getByRole('button', { name: 'Retry', exact: true }).first().click();
  await expect(dialog.getByRole('combobox', { name: 'Project brief version', exact: true })).toHaveValue('1');
  await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
});

test('a saved brief cannot regress to stale query data before its refresh finishes', async ({ page }) => {
  const { id } = await gitProject(page, 'brief-refresh');
  await page.goto(`/projects/${id}#project-brief`);
  const product = page.getByRole('textbox', { name: 'Product or feature', exact: true });
  await product.fill('Private room bookings');
  await page.getByRole('textbox', { name: 'People and problem', exact: true }).fill('People need private bookings.');
  await page.getByRole('combobox', { name: 'Run mode', exact: true }).selectOption('build');
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let savedRequest = false;
  await page.route(`**/api/projects/${id}/brief`, async route => {
    if (route.request().method() === 'POST') savedRequest = true;
    if (route.request().method() === 'GET' && savedRequest) await gate;
    await route.continue();
  });
  try {
    const refreshed = page.waitForRequest(request => savedRequest && request.method() === 'GET'
      && new URL(request.url()).pathname === `/api/projects/${id}/brief`);
    await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
    await refreshed;
    await expect(page.getByRole('status').filter({ hasText: 'Brief version 1 saved.' })).toBeVisible();
    await expect(page.getByText('Editing from version 1. Saving keeps every earlier version.', { exact: true })).toBeVisible();
    await expect(product).toHaveValue('Private room bookings');
    await expect(page.getByRole('textbox', { name: 'People and problem', exact: true })).toHaveValue('People need private bookings.');
    await expect(page.getByRole('combobox', { name: 'Run mode', exact: true })).toHaveValue('build');
    await product.fill('Reviewed private bookings');
    release();
    await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Brief version 2 saved.' })).toBeVisible();
    await expect(product).toHaveValue('Reviewed private bookings');
  } finally { release(); }
});

test('a person saves incomplete scope, private captioned references and immutable history', async ({ page }, info) => {
  const { id, folder } = await gitProject(page, 'brief');
  await page.goto(`/projects/${id}#project-brief`);
  await expect(page.getByRole('heading', { name: 'Project brief', exact: true })).toBeVisible();
  for (const label of ['References with captions (0/20)', 'Markdown import and export', 'Version history']) {
    const disclosure = page.getByText(label, { exact: true });
    await expect.poll(async () => (await disclosure.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Example appointment planner');
  await page.getByLabel('People and problem', { exact: true }).fill('People need an accessible way to arrange appointments.');
  await page.getByLabel('Uncertainty', { exact: true }).fill("I don't know, recommend something");
  await page.getByText('References with captions (0/20)', { exact: true }).press('Enter');
  await page.getByLabel('Reference caption', { exact: true }).fill('Private spacing study');
  await page.getByLabel('What to copy, keep, change or avoid', { exact: true }).fill('Keep spacing; avoid its navigation.');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5r8AAAAASUVORK5CYII=', 'base64');
  await page.getByLabel('Or upload PNG, JPG, WebP or PDF (10 MiB maximum)', { exact: true }).setInputFiles({ name: 'spacing.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('button', { name: 'Add reference', exact: true }).click();
  await expect(page.getByText('REF-1: Private spacing study', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 1 saved.' })).toBeVisible();
  expect(existsSync(`${folder}/docs/PROJECT-BRIEF.md`)).toBeTruthy();
  const markdown = readFileSync(`${folder}/docs/PROJECT-BRIEF.md`, 'utf8');
  expect(markdown).toContain('Example appointment planner');
  expect(markdown).not.toContain('Private spacing study');
  await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Example planner, revised');
  await page.getByRole('button', { name: 'Save project brief', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 2 saved.' })).toBeVisible();
  await page.getByText('Markdown import and export', { exact: true }).click();
  await page.getByLabel('Import Markdown as a proposed version', { exact: true }).fill(markdown);
  await page.getByRole('button', { name: 'Propose imported Markdown', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Proposed version 3; conflicts: product' })).toBeVisible();
  await page.getByText('Version history', { exact: true }).click();
  await expect(page.getByText('Conflicts with current brief: product', { exact: true })).toBeVisible();
  await expect(page.getByText('repository import (not accepted)', { exact: false })).toBeVisible();
  const take = page.getByRole('group', { name: /Repository text, not yours until you accept it/ });
  await expect(take.getByRole('button', { name: 'Accept the chosen fields from version 3' })).toBeDisabled();
  await take.getByRole('checkbox', { name: 'Product or feature', exact: true }).check();
  await expectAxeClean(page);
  const horizontal = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  expect(horizontal).toBeFalsy();
  if (info.project.name === 'brief-1440-dark') {
    await page.getByRole('heading', { name: 'Project brief', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: wikiShotPath(info, 'project-brief.png'), fullPage: true });
  }
  await take.getByRole('button', { name: 'Accept 1 field from version 3' }).click();
  await expect(page.getByText(/^Version 4 · saved · /)).toBeVisible();
});

test('team grant offers the existing project brief form', async ({ page }) => {
  const { id } = await gitProject(page, 'brief-grant');
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(id));
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  const team = dialog.getByRole('radio', { name: /helper|team|orchestrat/i });
  await expect(team).toBeVisible();
  await team.check();
  await dialog.getByText('Add a project brief', { exact: true }).click();
  await expect(dialog.getByRole('textbox', { name: 'Product or feature', exact: true })).toBeVisible();
  await expect(dialog.getByRole('combobox', { name: 'Brief run mode', exact: true })).toHaveValue('prepare');
  await expect(dialog.getByRole('combobox', { name: 'Team run mode', exact: true })).toBeVisible();
  await expectAxeClean(page);
});

test('a saved brief supplies the team run mode instead of a conflicting grant choice', async ({ page }) => {
  const { id, headers } = await gitProject(page, 'brief-mode');
  const saved = await page.request.post(`/api/projects/${id}/brief`, { headers, data: {
    version: 0, fields: { product: 'Example feature', run_mode: 'prepare' },
  } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  await page.goto(`/projects/${id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(id));
  await dialog.getByRole('button', { name: /^Advanced/ }).click();
  await dialog.getByRole('radio', { name: /helper|team|orchestrat/i }).check();
  const mode = dialog.getByRole('combobox', { name: 'Team run mode', exact: true });
  await expect(mode).toHaveValue('prepare');
  await expect(mode).toBeDisabled();
  await dialog.getByText('Add a project brief', { exact: true }).click();
  await dialog.getByRole('combobox', { name: 'Brief run mode', exact: true }).selectOption('build');
  await expect(mode).toHaveValue('prepare');  // Unsaved form edits confer no new authority.
  await dialog.getByRole('button', { name: 'Save project brief', exact: true }).click();
  await expect(mode).toHaveValue('prepare'); // Saving a build brief never widens a narrower team grant.
  await expect(mode).toBeEnabled();
  await mode.selectOption('build');
  await expect(mode).toHaveValue('build');
});

test('document upload retains per-file errors and a cited proposal accepts only chosen fields', async ({ page }, ti) => {
  const { id, headers } = await gitProject(page, 'document-brief');
  await page.goto(`/projects/${id}#project-brief`);
  await page.getByText('Start from documents', { exact: true }).click();
  await page.getByLabel('Source documents').setInputFiles([
    { name: 'rooms.md', mimeType: 'text/markdown', buffer: Buffer.from('# Scope\nA room booking board') },
    { name: 'broken.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('broken') },
  ]);
  await expect(page.getByText(/rooms.md · Text ready/)).toBeVisible();
  await expect(page.getByText(/broken.docx · Extraction failed/)).toBeVisible();
  await expect(page.getByText('Draft the brief from these documents', { exact: true })).toBeVisible();
  await page.locator('details').filter({ has: page.getByText('Start from documents', { exact: true }) }).screenshot({ path: wikiShotPath(ti, `project-brief-documents-${ti.project.name.includes('1440') ? 'desktop' : 'phone'}.png`) });
  const documents = (await (await page.request.get(`/api/projects/${id}/brief`)).json()).documents;
  expect(documents).toHaveLength(2);
  const launched = await page.request.post('/api/sessions', { headers, data: { project_id: id, provider: 'claude', name: 'Document lead',
    worktree: true, orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 1, total: 2, depth: 1, hours: 1 } } });
  expect(launched.ok(), await launched.text()).toBeTruthy();
  const lead = (await launched.json()).id;
  const requestId = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${lead}/events.jsonl`, JSON.stringify({ acc_session: lead,
    hook: { hook_event_name: 'AccTeam', request: { id: requestId, action: 'brief_propose_documents', base_version: 0,
      fields: { product: 'Room booking board', design: 'Calm layout' },
      citations: { product: [{ reference: documents[0].reference_id, location: 'section 1' }],
        design: [{ reference: documents[0].reference_id, location: 'section 1' }] } } } }) + '\n');
  const replyFile = `${info().run_dir}/${lead}/team/${requestId}.json`;
  await expect.poll(() => existsSync(replyFile)).toBeTruthy();
  expect(JSON.parse(readFileSync(replyFile, 'utf8')).ok).toBeTruthy();
  expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBeTruthy();
  await page.reload();
  await page.getByText('Version history', { exact: true }).click();
  await expect(page.getByText(/Product or feature: rooms.md.*section 1/)).toBeVisible();
  await expectAxeClean(page);
  await page.locator('details').filter({ has: page.getByText('Version history', { exact: true }) }).screenshot({ path: wikiShotPath(ti, `project-brief-document-proposal-${ti.project.name.includes('1440') ? 'desktop' : 'phone'}.png`) });
  await page.getByRole('checkbox', { name: 'Product or feature', exact: true }).check();
  await page.getByRole('button', { name: /Accept 1 field from version/ }).click();
  const current = (await (await page.request.get(`/api/projects/${id}/brief`)).json()).current;
  expect(current.submitted.product).toBe('Room booking board');
  expect(current.submitted.design).toBeUndefined();
});

test('New project starts from private documents', async ({ page }) => {
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  const dialog = page.getByRole('dialog', { name: 'New project', exact: true });
  await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill(`Docs ${randomUUID().slice(0, 8)}`);
  await dialog.getByText('Start from documents', { exact: true }).click();
  await dialog.getByLabel('Source documents').setInputFiles({ name: 'scope.txt', mimeType: 'text/plain', buffer: Buffer.from('Book rooms') });
  const { id } = await createProjectFromDialog(page, dialog);
  const brief = await (await page.request.get(`/api/projects/${id}/brief`)).json();
  expect(brief.documents[0].caption).toBe('scope.txt');
  expect(brief.current_version).toBe(0);
});
