import { expect, test, type Page } from './fixtures';
import { AUTH_FILE, expectAxeClean, wikiShotPath } from './helpers';

test.use({ storageState: AUTH_FILE });
const stamp = '2026-11-01T18:00:00Z';
const base = { verified: true, detail: null, session_id: null, session_name: null, project: null,
  host: null, acked_at: null, resolved_at: null, publish_id: null, created_at: stamp, updated_at: stamp };
function fixtures() {
  return [
    { ...base, id: 1, kind: 'needs_input', severity: 'warn', title: 'Choose the restore scope', source: 'claude_hook',
      session_id: 'restore-session', session_name: 'Restore preview', project: 'cedar-archive', host: 'cedar-node' },
    { ...base, id: 2, kind: 'tool_request', severity: 'warn', title: 'Install the reviewed tool', source: 'tool request',
      tool: { id: 'tool-approval', host: 'cedar-node', account: 'demo-agent', manager: 'uv', name: 'sample',
        version: '1.0', status: 'pending_approval', operation: 'install' } },
    { ...base, id: 3, kind: 'guardrail_blocked', severity: 'info', title: 'Branch operation blocked', source: 'guardrails' },
    { ...base, id: 4, kind: 'stop_failure', severity: 'critical', title: 'Stop needs reconciliation', source: 'broker',
      session_id: 'stop-session', session_name: 'Validation', host: 'cedar-node', project: 'cedar-archive',
      detail: 'Stop requested; the machine has not confirmed it.' },
    { ...base, id: 5, kind: 'info', severity: 'info', title: 'Results ready', source: 'test' },
    { ...base, id: 6, kind: 'info', severity: 'info', title: 'Results ready', source: 'test' },
    { ...base, id: 7, kind: 'machine_offline', severity: 'warn', title: 'Machine is offline', source: 'presence',
      persistent: 'machine_offline', host: 'cedar-node' },
  ];
}
async function arrange(page: Page, member = false) {
  const auth = await (await page.request.get('/api/auth/state')).json();
  await page.route('**/api/auth/state', route => route.fulfill({ json: { ...auth,
    username: member ? 'casey' : auth.username, role: member ? 'member' : 'owner',
    instance: { ...auth.instance, features: { ...auth.instance?.features, new_shell: true } } } }));
  await page.route('**/api/preferences/ui-shell', route => route.fulfill({ json: { enabled: true, person_id: member ? 'fixture-member' : 'fixture-owner' } }));
  let items = fixtures();
  const posts: string[] = [];
  await page.route('**/api/attention**', async route => {
    const request = route.request();
    if (request.method() !== 'GET') {
      posts.push(new URL(request.url()).pathname);
      const id = Number(new URL(request.url()).pathname.split('/')[3]);
      items = items.filter(item => item.id !== id);
      await route.fulfill({ json: {} });
    } else await route.fulfill({ json: items });
  });
  return { posts };
}
async function browse(page: Page, width: number) {
  const rail = page.getByRole('navigation', { name: width === 390 ? 'Phone rail' : 'Rail', exact: true });
  await rail.getByRole('button', { name: /^Inbox(?:,|$)/ }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  // URL changes before the lazy destination commits and closes the old drawer.
  await expect(page.getByRole('main').getByRole('heading', { name: 'Inbox', exact: true, level: 1 })).toBeVisible();
  if (width === 390) await page.getByRole('navigation', { name: 'Phone rail' }).getByRole('button', { name: 'More', exact: true }).click();
  return width === 390 ? page.getByRole('dialog', { name: 'Browse', exact: true }) : page.getByRole('complementary', { name: 'Context sidebar' });
}

for (const width of [1440, 390]) for (const colorScheme of ['light', 'dark'] as const) for (const member of [false, true]) {
  test(`Inbox categories, context and safe actions · ${width} ${colorScheme} ${member ? 'member' : 'owner'}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 }); await page.emulateMedia({ colorScheme });
    const { posts } = await arrange(page, member);
    await page.goto('/projects');
    const sidebar = await browse(page, width);
    await expect(page).toHaveURL(/\/inbox$/); // The rail opens Inbox; categories remain sub-navigation.
    for (const name of ['Approvals · 1', 'Blocked work · 2', 'Failures · 1', 'Information · 2'])
      await expect(sidebar.getByRole('link', { name, exact: true })).toBeVisible();
    await sidebar.getByRole('link', { name: 'Decisions · 1', exact: true }).click();
    await expect(page).toHaveURL(/\/inbox\?filter=decisions/);
    const main = page.getByRole('main');
    await expect(main.getByRole('heading', { name: 'Decisions', exact: true })).toBeVisible();
    await expect(main.getByText('Choose the restore scope', { exact: true }).first()).toBeVisible();
    await expect(main.getByText('cedar-archive', { exact: true }).first()).toBeVisible();
    await expect(main.getByText('Restore preview', { exact: true }).first()).toBeVisible();
    await expect(main.getByText('cedar-node', { exact: true }).first()).toBeVisible();
    await expect(main.getByRole('link', { name: 'Open related work', exact: true })).toHaveAttribute('href', '/sessions/restore-session');
    await expect(main.getByRole('link', { name: 'Return to work', exact: true })).toHaveAttribute('href', '/projects');
    await expectAxeClean(page);
    await page.screenshot({ path: testInfo.outputPath('inbox.png'), fullPage: true });
    await main.getByRole('button', { name: 'More actions', exact: true }).click();
    await page.getByRole('menuitem', { name: /^Dismiss notice/ }).click();
    await expect.poll(() => posts).toEqual(['/api/attention/1/ack']);
    expect(posts.every(path => !path.endsWith('/approve'))).toBe(true);
    // A stale selection is removed, never kept as cached private metadata.
    await expect(main.getByText('Choose the restore scope', { exact: true })).toHaveCount(0);
  });
}

test('one authoritative approval, duplicate wording stays separate, snooze is not approval', async ({ page }) => {
  const { posts } = await arrange(page);
  const decisions: string[] = [];
  await page.route('**/api/tools/requests/tool-approval/approve', route => {
    decisions.push(route.request().url());
    return route.fulfill({ status: 409, json: { detail: 'This request changed. Review it again.' } });
  });
  await page.goto('/inbox?filter=information');
  const main = page.getByRole('main');
  await expect(main.getByRole('button', { name: /Results ready/ })).toHaveCount(2);
  await page.goto('/inbox?filter=approvals&item=2');
  await main.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(main.getByText('This request changed. Review it again.', { exact: true })).toBeVisible();
  expect(decisions).toHaveLength(1);
  await page.goto('/inbox?filter=blocked&item=7');
  await main.getByRole('button', { name: 'Snooze 1 hour' }).click();
  await expect.poll(() => posts).toEqual(['/api/attention/7/snooze']);
  expect(decisions).toHaveLength(1);
});

test('empty, first-load error and stale data are distinct; selection survives a failed read', async ({ page }) => {
  await arrange(page);
  let unavailable = false;
  await page.route('**/api/attention**', route => unavailable
    ? route.fulfill({ status: 503, json: { detail: 'Inbox temporarily unavailable.' } })
    : route.fulfill({ json: fixtures() }));
  await page.goto('/inbox?filter=failures&item=4');
  const main = page.getByRole('main');
  await expect(main.getByText('Stop requested; the machine has not confirmed it.', { exact: true })).toBeVisible();
  unavailable = true;
  await expect(main.getByText(/Showing the last data received/)).toBeVisible({ timeout: 15000 });
  await expect(main.getByText('Stop requested; the machine has not confirmed it.', { exact: true })).toBeVisible();
  await page.reload();
  await expect(main.getByRole('alert')).toContainText("That service isn't available right now. Try again in a moment.");
  await expect(main.getByRole('alert')).toBeVisible();
  await expect(main.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  await expect(main.getByText('Nothing needs you right now.', { exact: true })).toHaveCount(0);
});

test('pending reads are not empty and large lists stay bounded; exact item survives refresh', async ({ page }) => {
  await arrange(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let empty = false;
  await page.route('**/api/attention**', async route => {
    await pending;
    await route.fulfill({ json: empty ? [] : Array.from({ length: 180 }, (_, index) => ({ ...base,
      id: 1000 + index, kind: 'info', severity: 'info', source: 'test', title: `Notice ${index + 1}` })) });
  });
  await page.goto('/inbox?filter=information');
  const main = page.getByRole('main');
  await expect(main.getByText('Nothing needs you right now.', { exact: true })).toHaveCount(0);
  await expect(main.getByRole('button', { name: 'Refresh Inbox' })).toBeDisabled();
  release();
  const rows = main.getByRole('list', { name: 'Inbox items' }).getByRole('button');
  await expect(rows).toHaveCount(20);
  await main.getByRole('button', { name: 'Next items', exact: true }).click();
  await rows.first().click();
  await expect(page).toHaveURL(/item=1020/);
  await expect(main.getByRole('heading', { name: 'Notice 21', exact: true })).toBeFocused();
  await main.getByRole('button', { name: 'Refresh Inbox' }).click();
  await expect(main.getByRole('heading', { name: 'Notice 21', exact: true })).toBeVisible();
  empty = true;
  await main.getByRole('button', { name: 'Refresh Inbox' }).click();
  await expect(main.getByText('Nothing needs you right now.', { exact: true })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'Notice 21', exact: true })).toHaveCount(0);
});

test('related work uses fixed exact team and host views, not text supplied by the agent', async ({ page }) => {
  await arrange(page);
  const items = [
    { ...base, id: 10, kind: 'team_worklist', severity: 'info', source: 'orchestrator', title: 'Review requirements', session_id: 'team-lead' },
    { ...base, id: 11, kind: 'team_expired', severity: 'warn', source: 'orchestrator', title: 'Team expired', session_id: 'team-lead' },
    { ...base, id: 12, kind: 'machine_offline', severity: 'warn', source: 'presence', title: 'https://example.com/untrusted', host: 'cedar node' },
  ];
  await page.route('**/api/attention**', route => route.fulfill({ json: items }));
  for (const [id, target] of [[10, '/sessions/team-lead?tab=team#worklist'], [11, '/sessions/team-lead?tab=team#overview'], [12, '/host?machine=cedar%20node']]) {
    await page.goto(`/inbox?item=${id}`);
    await expect(page.getByRole('main').getByRole('link', { name: 'Open related work', exact: true })).toHaveAttribute('href', String(target));
  }
});

test('History preserves its filter, dismiss outcome and return address', async ({ page }) => {
  await arrange(page);
  await page.route('**/api/attention**', route => route.fulfill({ json: new URL(route.request().url()).searchParams.get('scope') === 'all'
    ? [{ ...fixtures()[4], acked_at: stamp }] : fixtures() }));
  await page.goto('/projects');
  const sidebar = await browse(page, 1440);
  await sidebar.getByRole('link', { name: 'History', exact: true }).click();
  const main = page.getByRole('main');
  await expect(page.getByRole('complementary').getByRole('link', { name: 'History', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(main.getByRole('radio', { name: 'History', exact: true })).toBeChecked();
  await expect(main.getByRole('button', { name: /Results ready.*Dismissed/ })).toBeVisible();
  await expect(main.getByRole('button', { name: 'Dismiss notice', exact: true })).toHaveCount(0);
  await expect(main.getByRole('link', { name: 'Return to work', exact: true })).toHaveAttribute('href', '/projects');
  await main.getByRole('radio', { name: 'Open', exact: true }).click();
  await expect(main.getByRole('button', { name: /Results ready.*Dismissed/ })).toHaveCount(0);
});

test('phone setup moves from Overview to Information and its badge clears on dismissal', async ({ page }) => {
  await arrange(page);
  let dismissed = false;
  await page.route('**/api/attention**', async route => {
    if (route.request().method() === 'POST') {
      dismissed = true;
      await route.fulfill({ json: { ok: true } });
    } else await route.fulfill({ json: dismissed ? [] : [{ ...base, id: 9001, kind: 'phone_setup', severity: 'info',
      title: 'Connect your phone', source: 'phone setup',
      detail: 'Use Rynhult from your phone, with notifications when a session needs you.' }] });
  });
  await page.route('**/api/preferences/phone-banner', route => route.fulfill({ json: { dismissed: false } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Connect your phone' })).toHaveCount(0);
  await expect(page.getByText('Use Rynhult from your phone', { exact: false })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Inbox, 1 open', exact: true })).toBeVisible();
  const sidebar = await browse(page, 1440);
  await expect(sidebar.getByRole('link', { name: 'Information · 1', exact: true })).toBeVisible();
  await sidebar.getByRole('link', { name: 'Information · 1', exact: true }).click();
  const main = page.getByRole('main');
  await expect(main.getByRole('link', { name: 'Connect your phone', exact: true })).toHaveAttribute('href', '/guide/User-Guide/Connect-Your-Phone');
  await main.getByRole('button', { name: 'Dismiss notice', exact: true }).click();
  await expect(sidebar.getByRole('link', { name: 'Information · 0', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Inbox', exact: true })).toBeVisible();
  await page.reload();
  await expect(main.getByRole('button', { name: 'Connect your phone', exact: true })).toHaveCount(0);
});

// Current rail status and error controls replace the retired classic status banners.
for (const width of [1440, 390]) test(`wiki compact Inbox status and outage at ${width}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
  await arrange(page);
  await page.goto('/');
  const rail = page.getByRole('navigation', { name: width === 390 ? 'Phone rail' : 'Rail', exact: true });
  await expect(rail.getByRole('button', { name: 'Inbox, 7 open', exact: true })).toBeVisible();
  await expect(page.getByRole('main').getByRole('heading', { name: 'Overview', exact: true, level: 1 })).toBeVisible();
  await expect(page.getByText('Loading page…', { exact: true })).toHaveCount(0);
  const size = width === 390 ? 'phone' : 'desktop';
  await page.screenshot({ path: wikiShotPath(testInfo, `inbox-status-${size}.png`) });
  await page.route('**/api/attention**', route => route.fulfill({ status: 503, json: { detail: 'Inbox temporarily unavailable.' } }));
  await page.reload();
  await expect(rail.getByRole('button', { name: /Inbox.*unavailable/i })).toBeVisible();
  await rail.getByRole('button', { name: /^Inbox/ }).click();
  const main = page.getByRole('main');
  await expect(main.getByRole('alert')).toBeVisible();
  await expect(main.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  await page.screenshot({ path: wikiShotPath(testInfo, `inbox-unavailable-${size}.png`) });
});
