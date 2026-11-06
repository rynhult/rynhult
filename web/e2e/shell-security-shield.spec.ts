import { expect, type Page } from '@playwright/test';
import { test } from './shell-state';
import { mkdirSync } from 'node:fs';
import { securityUrl, type SecurityPolicy, type SecurityScope } from '../src/lib/security';
import { AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info, PASSWORD, totp, verifyAgain } from './helpers';

test.use({ storageState: AUTH_FILE, presentationChanges: ['layouts', 'ui_shell'], viewport: { width: 1440, height: 900 } });
async function enable(page: Page) {
  await verifyAgain(page);
  const flags = await (await page.request.get('/api/admin-features')).json();
  expect((await page.request.put('/api/admin-features/new_shell', { headers: await csrfHeaders(page), data: { enabled: true, revision: flags.revision } })).ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
}
const cases = [
  { id: 'on', words: 'Current protections On', icon: 'shield-check' },
  { id: 'unrestricted', words: 'Unrestricted', icon: 'shield-alert' },
  { id: 'older', words: '22 sessions on older settings', icon: 'shield-check' },
  { id: 'custom', words: 'Custom security · 1 off', icon: 'shield-alert' },
  { id: 'pending', words: 'Folder protection pending', icon: 'shield-alert' },
  { id: 'varies', words: 'Security varies by person', icon: 'shield-alert' },
  { id: 'loading', words: 'Security state loading', icon: 'shield-question-mark' },
  { id: 'unknown', words: 'Security state unavailable', icon: 'shield-question-mark' },
  { id: 'offline', words: 'Offline', icon: 'shield-off' },
] as const;
type Sample = typeof cases[number]['id'];
function samplePolicy(policy: SecurityPolicy, sample: Sample) {
  // Presentation fixtures only: real policy replies retain their scopes/metadata.
  // No security setting is changed on the fixture server.
  const switches = Object.fromEntries(Object.keys(policy.switches).map(key => [key, sample !== 'unrestricted']));
  if (sample === 'custom') switches.force_push = false;
  return { ...policy, switches, mode: sample === 'unrestricted' ? 'unrestricted' : sample === 'custom' ? 'custom' : 'default',
    older_sessions: sample === 'older' ? Array.from({ length: 22 }, (_, i) => `older-${i}`) : [],
    varying_switches: sample === 'varies' ? ['tool_install_check'] : [],
    enforcement: { ...policy.enforcement, allowed_folders: sample === 'pending' ? 'pending' : 'applied' } };
}
// Read complete real replies before routing. A navigation may cancel browser
// polling, so presentation fixtures must not depend on route.fetch during reloads.
async function policySamples(page: Page, project?: { id: number; host?: string; account?: string }) {
  const auth = await (await page.request.get('/api/auth/state')).json();
  const scopes: SecurityScope[] = [{ kind: 'instance' }, { kind: 'account', machine: 'local', account: auth.account }];
  if (project) scopes.push({ kind: 'project', project_id: project.id },
    { kind: 'account', machine: project.host || 'local', account: project.account || auth.account });
  const policies = new Map<string, SecurityPolicy>();
  for (const scope of scopes) {
    const url = securityUrl(scope);
    const response = await page.request.get(url); expect(response.ok()).toBeTruthy();
    policies.set(url, await response.json());
  }
  return (url: string) => {
    const request = new URL(url);
    const policy = policies.get(request.pathname + request.search);
    expect(policy, `prefetched policy for ${request.pathname + request.search}`).toBeDefined();
    return policy!;
  };
}
async function shot(page: Page, name: string) {
  const folder = `${process.env.ACC_E2E_BASE}/shield-shots`;
  mkdirSync(folder, { recursive: true });
  await page.screenshot({ path: `${folder}/${name}.png`, animations: 'disabled' });
}

for (const theme of ['light', 'dark'] as const) test(`owner security shield states at 1440 ${theme}`, async ({ page }) => {
  test.setTimeout(180000);
  await enable(page); await page.emulateMedia({ colorScheme: theme });
  const policies = await policySamples(page);
  let sample: Sample = 'on';
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/security/effective*', async route => {
    const current = sample;
    if (current === 'loading') { await held; await route.abort(); return; }
    if (current === 'unknown' || current === 'offline') { await route.fulfill({ status: 503, json: { detail: 'Policy unavailable' } }); return; }
    await route.fulfill({ json: samplePolicy(policies(route.request().url()), current) });
  });
  await page.route('**/api/health', route => sample === 'offline'
    ? route.fulfill({ status: 503, json: { detail: 'Offline' } }) : route.continue());
  try {
    for (const value of cases) {
      sample = value.id;
      await page.goto('/projects');
      const shield = page.locator('#shell-header').getByRole('link', { name: /^Security:/ });
      await expect(shield).toBeVisible();
      await expect(shield).toHaveAccessibleName(new RegExp(value.words));
      await expect(shield.locator(`svg.lucide-${value.icon}`)).toBeVisible();
      await expect(shield).toHaveAttribute('href', /\/settings\?security_scope=account&machine=local&account=.+#security$/);
      const rect = (await shield.boundingBox())!;
      expect(rect.width).toBeLessThanOrEqual(48); expect(rect.height).toBeGreaterThanOrEqual(32);
      if (sample === 'older') {
        await expect(shield.getByText('22', { exact: true })).toBeVisible();
        expect((await shield.locator('.shell-security-count').boundingBox())!.height).toBeLessThanOrEqual(16);
      }
      else await expect(shield.locator('.shell-security-count')).toHaveCount(0);
      if (sample === 'unrestricted') {
        const paint = await shield.evaluate(el => {
          const probe = document.createElement('span'); el.append(probe);
          probe.style.color = 'var(--wait)'; const warning = getComputedStyle(probe).color;
          probe.style.color = 'var(--status-ink)'; const ink = getComputedStyle(probe).color; probe.remove();
          return { background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color, warning, ink };
        });
        expect(paint.background).toBe(paint.warning); expect(paint.color).toBe(paint.ink);
      }
      await shield.hover();
      const tooltip = page.getByRole('tooltip');
      await expect(tooltip).toContainText(value.words);
      await expect(tooltip).toContainText('on local');
      await tooltip.hover(); await expect(tooltip).toBeVisible();
      await page.mouse.move(0, 850); await expect(tooltip).toHaveCount(0);
      await shield.focus(); await expect(tooltip).toContainText(value.words);
      await expect(shield).toHaveAttribute('aria-describedby', await tooltip.getAttribute('id') as string);
      await expectAxeClean(page); await shot(page, `${value.id}-${theme}`);
      await page.keyboard.press('Escape'); await expect(tooltip).toHaveCount(0); await expect(shield).toBeFocused();
      if (sample === 'on') {
        await page.keyboard.press('Enter');
        await expect(page).toHaveURL(/\/settings\?security_scope=account.*#security$/);
        await expect(page.getByRole('heading', { name: 'Security', exact: true })).toBeVisible();
        await expect(page.getByRole('dialog', { name: 'Security protections' })).toHaveCount(0);
      }
    }
  } finally { release(); await page.unrouteAll({ behavior: 'wait' }); }
});

test('cached Unrestricted and older-session warnings stay readable when policy and connection fail', async ({ page }) => {
  test.slow(); await enable(page);
  const policies = await policySamples(page);
  let failed = false; let offline = false;
  await page.route('**/api/security/effective*', async route => {
    if (failed) { await route.fulfill({ status: 503, json: { detail: 'Policy unavailable' } }); return; }
    await route.fulfill({ json: { ...samplePolicy(policies(route.request().url()), 'unrestricted'), older_sessions: ['older-1'] } });
  });
  await page.route('**/api/health', route => offline ? route.fulfill({ status: 503, json: { detail: 'Offline' } }) : route.continue());
  try {
    await page.goto('/projects');
    const shield = page.locator('#shell-header').getByRole('link', { name: /^Security:/ });
    await expect(shield).toHaveAccessibleName(/Unrestricted · 1 session on older settings/);
    failed = true;
    await expect(shield).toHaveAccessibleName(/Security state unavailable · last known Unrestricted/, { timeout: 20000 });
    await expect(shield.locator('svg.lucide-shield-question-mark')).toBeVisible();
    await shield.focus(); await expect(page.getByRole('tooltip')).toContainText('last known count shown');
    await page.keyboard.press('Escape'); offline = true;
    await expect(shield).toHaveAccessibleName(/Offline · last known Unrestricted/, { timeout: 15000 });
    await expect(shield.locator('svg.lucide-shield-off')).toBeVisible();
    for (const theme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: theme });
      const paint = await shield.evaluate(el => {
        const probe = document.createElement('span'); el.append(probe); probe.style.color = 'var(--status-ink)';
        const ink = getComputedStyle(probe).color; probe.remove(); return { color: getComputedStyle(el).color, ink };
      });
      expect(paint.color).toBe(paint.ink);
      await shield.focus(); await expectAxeClean(page); await shot(page, `stale-offline-${theme}`);
      await page.keyboard.press('Escape'); await page.locator('#shell-header').focus();
    }
  } finally { await page.unrouteAll({ behavior: 'wait' }); }
});

test('S-4: arriving from another route lands on the Security heading after Settings loads', async ({ page }) => {
  await enable(page); await page.goto('/projects');
  await page.locator('#shell-header').getByRole('link', { name: /^Security:/ }).click();
  await expect(page).toHaveURL(/#security$/);
  const heading = page.getByRole('heading', { name: 'Security', exact: true });
  await expect(heading).toBeVisible();
  await expect.poll(async () => (await heading.boundingBox())!.y).toBeLessThan(200);
});

test('S-1: an offline project machine keeps the current policy state and says which thing is offline', async ({ page }) => {
  test.slow(); await enable(page);
  const project = await gitProject(page, 'cedar-machine');
  await page.route('**/api/projects', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: (await response.json()).map((row: { id: number }) => row.id === project.id ? { ...row, machine_offline: true } : row) });
  });
  await page.goto(`/projects/${project.id}`);
  const shield = page.locator('#shell-header').getByRole('link', { name: /^Security:/ });
  await expect(shield).toHaveAccessibleName(/^Security: Current protections On · .* · machine offline$/);
  await expect(shield.locator('svg.lucide-shield-check')).toBeVisible();
  await expect(shield).toHaveAttribute('data-state', 'on');
  await page.unrouteAll({ behavior: 'wait' });
});

test('project shield keeps the reviewed project destination and legacy layout keeps its full badge', async ({ page }) => {
  test.slow(); await enable(page);
  const project = await gitProject(page, 'cedar-shield');
  const policies = await policySamples(page, project);
  await page.route('**/api/security/effective*', async route => {
    const policy = policies(route.request().url());
    await route.fulfill({ json: samplePolicy(policy, policy.scope.kind === 'project' ? 'unrestricted' : 'on') });
  });
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto(`/projects/${project.id}`);
    const shield = page.locator('#shell-header').getByRole('link', { name: /^Security:.*Unrestricted/ });
    await expect(shield).toHaveAttribute('href', `/settings?security_scope=project&project_id=${project.id}#security`);
    await shield.click(); await expect(page).toHaveURL(new RegExp(`security_scope=project&project_id=${project.id}#security$`));
  }
  await page.unrouteAll({ behavior: 'wait' });
  await page.route('**/api/security/effective*', async route => {
    await route.fulfill({ json: samplePolicy(policies(route.request().url()), 'unrestricted') });
  });
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: false } })).ok()).toBeTruthy();
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto('/projects');
    const badge = page.getByRole('button', { name: /^Unrestricted/ });
    await expect(badge).toContainText('Unrestricted');
    await expect(page.getByRole('link', { name: /^Security:/ })).toHaveCount(0);
    await badge.click(); await expect(page.getByRole('dialog', { name: 'Security protections' })).toBeVisible();
    await page.keyboard.press('Escape');
  }
  await page.unrouteAll({ behavior: 'wait' });
});

test('member header has no security shield or policy inventory requests in either theme', async ({ page, context }) => {
  test.slow(); await enable(page);
  const account = `shield-${Date.now()}-agent`;
  mkdirSync(`${info().state}/fixture-accounts/${account}`, { recursive: true });
  const invited = await page.request.post('/api/users/invites', { headers: await csrfHeaders(page), data: { role: 'member', account } });
  expect(invited.ok()).toBeTruthy(); const token = (await invited.json()).link.split('/').pop();
  await context.clearCookies();
  const begun = await page.request.post('/api/auth/invite/begin', { headers: { Origin: info().origin }, data: { token } });
  expect((await page.request.post('/api/auth/invite/complete', { headers: { Origin: info().origin }, data: { token, username: `robin-${Date.now()}`, password: PASSWORD, code: totp((await begun.json()).totp_secret) } })).ok()).toBeTruthy();
  expect((await page.request.put('/api/preferences/ui-shell', { headers: await csrfHeaders(page), data: { enabled: true } })).ok()).toBeTruthy();
  const reads: string[] = [];
  page.on('request', r => { if (new URL(r.url()).pathname === '/api/security/effective') reads.push(r.url()); });
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme }); await page.goto('/projects');
    await expect(page.locator('#shell-header')).toBeVisible();
    await expect(page.getByRole('link', { name: /^Security:/ })).toHaveCount(0);
    await expectAxeClean(page); await shot(page, `member-${theme}`);
  }
  expect(reads).toEqual([]);
});
