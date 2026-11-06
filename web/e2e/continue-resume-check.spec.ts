import { expect, test, type Page, type Route } from './fixtures';
import { AUTH_FILE, expectAxeClean, info } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['layouts'] });
const pendingRoutes = new WeakMap<Page, Set<Promise<void>>>();
async function intercept(page: Page, pattern: string, handler: (route: Route) => Promise<void>) {
  const pending = pendingRoutes.get(page) ?? new Set<Promise<void>>();
  pendingRoutes.set(page, pending);
  await page.route(pattern, async route => {
    const work = Promise.resolve().then(() => handler(route));
    pending.add(work);
    try { await work; } finally { pending.delete(work); }
  });
}
test.afterEach(async ({ page }) => {
  // Complete callbacks before removing interception, as well as before context disposal.
  const pending = pendingRoutes.get(page);
  while (pending?.size) await Promise.all([...pending]);
  await page.unrouteAll({ behavior: 'wait' });
});
const refused = 'This team has stopped. Review its time limit and resume the team first.';
const raced = 'The reviewed settings changed. Start a new session with the current settings.';
async function arrange(page: Page, member = false, shell = true) {
  const csrf = (await page.context().cookies()).find(c => /acc_csrf$/.test(c.name))!.value;
  const response = await page.request.post('/api/projects', { headers: { 'x-acc-csrf': csrf }, data: {
    slug: `resume-${Date.now()}`, name: 'Cedar archive', workspace_path: info().workspace } });
  expect(response.ok()).toBeTruthy(); const project = await response.json();
  const auth = await (await page.request.get('/api/auth/state')).json();
  await intercept(page, '**/api/auth/state', route => route.fulfill({ json: { ...auth,
    role: member ? 'member' : 'owner', username: member ? 'casey' : auth.username,
    instance: { ...auth.instance, features: { ...auth.instance?.features, new_shell: true } } } }));
  await intercept(page, '**/api/preferences/ui-shell', route => route.fulfill({ json: { enabled: shell, person_id: member ? 'fixture-member' : 'fixture-owner' } }));
  const session = { id: 'stopped-session', name: 'Restore preview', project_id: project.id, project: project.slug,
    provider: 'claude', desired_state: 'stopped', observed_state: 'stopped', management: 'acc',
    run_as: 'demo-agent', host: 'local', cwd: project.workspace_path, created_at: new Date().toISOString(),
    security_older_settings: true };
  let rows = [session]; const reads: string[] = []; const posts: string[] = [];
  let eligible = false; let reason = refused;
  await intercept(page, '**/api/sessions', route => route.fulfill({ json: rows }));
  await intercept(page, '**/api/sessions/*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const id = new URL(route.request().url()).pathname.split('/').at(-1);
    await route.fulfill({ json: { ...session, ...rows.find(row => row.id === id) } });
  });
  await intercept(page, '**/api/security/effective*', async route => {
    const response = await route.fetch();
    await route.fulfill({ json: { ...await response.json(), older_sessions: [session.id] } });
  });
  await intercept(page, '**/api/sessions/*/resume-check', async route => {
    reads.push(new URL(route.request().url()).pathname.split('/')[3]);
    await route.fulfill({ json: { eligible, reason: eligible ? null : reason } });
  });
  await intercept(page, '**/api/sessions/*/resume', route => {
    posts.push(route.request().method()); eligible = false; reason = raced;
    return route.fulfill({ status: 409, json: { detail: raced } });
  });
  return { project, session, reads, posts, eligible: () => { eligible = true; },
    rows: (value: typeof rows) => { rows = value; } };
}
for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) for (const member of [false, true]) {
  test(`Resume eligibility before click · ${width} ${colorScheme} ${member ? 'member' : 'owner'}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    const state = await arrange(page, member);
    await page.goto(`/projects/${state.project.id}`);
    const block = page.getByRole('region', { name: 'Sessions', exact: true });
    const resume = block.getByRole('button', { name: 'Resume', exact: true });
    // Eligibility is deliberately lazy; the combined phone page may start above this row.
    await resume.scrollIntoViewIfNeeded();
    await expect(block.getByText(refused, { exact: true })).toBeVisible();
    await expect(resume).toBeDisabled(); await expect(resume).toHaveAttribute('title', refused);
    await expect(block.getByText('older settings', { exact: true })).toBeVisible();
    expect(state.posts).toEqual([]); expect(state.reads).toEqual([state.session.id]);
    await expect(block.getByRole('link', { name: 'Continue Restore preview', exact: true })).toHaveAttribute('href', '/sessions/stopped-session');
    await expectAxeClean(page);
    state.eligible();
    await block.getByRole('button', { name: 'Check again', exact: true }).click();
    await expect(resume).toBeEnabled();
    expect(state.posts).toEqual([]);
    await resume.click();
    await expect(block.getByText(raced, { exact: true }).first()).toBeVisible();
    await expect(resume).toBeDisabled(); await expect(resume).toHaveAttribute('title', raced);
    expect(state.posts).toEqual(['POST']);
  });
}

test('offline and unavailable checks disable Resume while Continue stays usable', async ({ page }) => {
  const state = await arrange(page);
  state.rows([{ ...state.session, machine_offline: true } as typeof state.session]);
  await page.goto(`/projects/${state.project.id}`);
  const block = page.getByRole('region', { name: 'Sessions' });
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toHaveAttribute('title', /Machine offline/);
  await expect(block.getByRole('link', { name: 'Continue Restore preview', exact: true })).toHaveAttribute('href', '/sessions/stopped-session');
  expect(state.reads).toEqual([]); expect(state.posts).toEqual([]);
  state.rows([state.session]);
  await intercept(page, '**/api/sessions/*/resume-check', route => route.fulfill({ status: 503, json: { detail: 'Account service unavailable.' } }));
  await page.reload();
  await expect(block.getByText('Resume eligibility is unavailable. Check again.', { exact: true })).toBeVisible();
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
  await expect(block.getByRole('link', { name: 'Continue Restore preview', exact: true })).toBeVisible();
  expect(state.posts).toEqual([]);
});

test('checks are lazy, limited to visible authorized rows and at most four in flight', async ({ page }) => {
  const state = await arrange(page, true);
  state.rows([{ ...state.session, id: 'external-session', management: 'external' }, ...Array.from({ length: 80 }, (_, index) => ({ ...state.session, id: `stopped-${index}`, name: `Stopped ${index}` })),
    { ...state.session, id: 'foreign-project', project_id: state.project.id + 1 },
    { ...state.session, id: 'archived-session', desired_state: 'archived', observed_state: 'archived' },
    { ...state.session, id: 'live-session', observed_state: 'active', desired_state: 'running' }]);
  const active = new Set<ReturnType<Route['request']>>();
  let peak = 0; const checked: string[] = [];
  // Pagination cancels old rows. Their delayed mock handlers may still be
  // running, but canceled browser requests no longer consume a network slot.
  page.on('requestfailed', request => active.delete(request));
  await intercept(page, '**/api/sessions/*/resume-check', async route => {
    active.add(route.request()); peak = Math.max(peak, active.size); checked.push(new URL(route.request().url()).pathname.split('/')[3]);
    try {
      await new Promise(resolve => setTimeout(resolve, 150));
      await route.fulfill({ json: { eligible: true, reason: null } });
    } finally { active.delete(route.request()); }
  });
  await page.goto(`/projects/${state.project.id}`);
  const block = page.getByRole('region', { name: 'Sessions' });
  await block.getByRole('button', { name: 'Resume', exact: true }).first().scrollIntoViewIfNeeded();
  await expect(block.getByRole('button', { name: 'Resume', exact: true }).first()).toBeEnabled();
  expect(peak).toBeLessThanOrEqual(4);
  expect(checked.length).toBeLessThan(20); // Offscreen rows have not been read.
  expect(checked.every(id => Number(id.split('-')[1]) < 20)).toBe(true);
  await expect.poll(() => active.size).toBe(0); // Drain live first-page reads before paging; teardown also awaits canceled handlers.
  await block.getByRole('button', { name: 'Next sessions', exact: true }).click();
  await expect.poll(() => checked.some(id => Number(id.split('-')[1]) >= 20)).toBe(true);
  await expect.poll(() => active.size).toBe(0);
  expect(peak).toBeLessThanOrEqual(4); expect(state.posts).toEqual([]);
});

test('standard shell retains the existing Resume action without eligibility reads', async ({ page }) => {
  const state = await arrange(page, false, false);
  await page.goto('/sessions?group=stopped');
  await expect(page.getByRole('navigation', { name: 'Main', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  expect(state.reads).toEqual([]); expect(state.posts).toEqual([]);
});

test('eligibility is cached briefly across a remount then refreshed after expiry', async ({ page }) => {
  const state = await arrange(page); state.eligible();
  // Warm the lazy directory in this document before freezing timers. This case
  // measures eligibility freshness across real observer remounts, not cold imports.
  await page.goto('/projects');
  await expect(page.getByRole('heading', { name: 'All projects', level: 1, exact: true })).toBeVisible();
  const at = Date.now();
  await page.clock.install({ time: new Date(at) });
  await page.getByRole('textbox', { name: 'Search projects', exact: true }).fill(state.project.slug);
  await page.getByRole('region', { name: 'Available projects' }).locator(`a[href="/projects/${state.project.id}"]`).click();
  const block = page.getByRole('region', { name: 'Sessions' });
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  // Let initial queries/render notifications settle, then freeze both Date and timers.
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 1000));
  expect(state.reads).toEqual([state.session.id]);
  const leave = async () => {
    await page.getByRole('complementary', { name: 'Context sidebar' }).getByRole('link', { name: 'Back to all projects', exact: true }).click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole('heading', { name: 'All projects', level: 1, exact: true })).toBeVisible();
    await expect(block).toHaveCount(0); // Back must remount the observer, not cancel an uncommitted navigation.
  };
  await leave(); await page.goBack();
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  expect(state.reads).toEqual([state.session.id]);
  await leave();
  await page.clock.runFor(6000); // Expire freshness while the Continue observer is unmounted.
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resume-check'));
  await page.goBack();
  await (await refreshed).finished();
  await page.clock.runFor(100); // Flush the fetched query’s scheduled notifications.
  await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeEnabled();
  expect(state.reads.length).toBe(2);
  await page.clock.runFor(10000); // Exercise the visible-row polling timer, rather than only changing Date.
  await expect.poll(() => state.reads.length).toBe(3);
  expect(state.posts).toEqual([]);
});

test('pending and malformed eligibility responses fail closed without resuming', async ({ page }) => {
  const state = await arrange(page);
  let release!: () => void;
  const wait = new Promise<void>(resolve => { release = resolve; });
  await intercept(page, '**/api/sessions/*/resume-check', async route => {
    await wait; await route.fulfill({ json: { eligible: 'true', reason: null } });
  });
  try {
    await page.goto(`/projects/${state.project.id}`);
    const block = page.getByRole('region', { name: 'Sessions' });
    await expect(block.getByText('Checking whether this session can resume…', { exact: true })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
    release();
    await expect(block.getByText('Resume eligibility is unavailable. Check again.', { exact: true })).toBeVisible();
    await expect(block.getByRole('button', { name: 'Resume', exact: true })).toBeDisabled();
    expect(state.posts).toEqual([]);
  } finally {
    release(); // Also unblock the handler if an assertion fails before the normal release.
  }
});
