import { expect, test } from './fixtures';
import { AUTH_FILE, demoSessions } from './helpers';
import * as sessionBits from '../src/components/session-bits';
import type { SessionSummary } from '../src/lib/types';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useMachineReadings } from '../src/lib/queries';

test.use({ storageState: AUTH_FILE });

test('machine readings cache stays private across viewer and role changes', () => {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  const ownerReading = { label: 'Owner machine', connected: true,
    light: { cpu_percent: 17.5, memory_percent: 42, collected_at: 1800000000 } };
  function Reading() { return createElement('span', null, useMachineReadings().data?.label ?? 'No machine data'); }
  const render = () => renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(Reading)));
  try {
    client.setQueryData(['auth'], { username: 'alex', role: 'owner' });
    render();
    // Populate the actual owner's query, rather than duplicating its key builder.
    for (const query of client.getQueryCache().getAll()) {
      if (query.queryKey[0] === 'machine-readings') client.setQueryData(query.queryKey, ownerReading);
    }
    expect(render()).toContain('Owner machine');
    client.setQueryData(['auth'], { username: 'dana', role: 'member' });
    expect(render()).toContain('No machine data');
    client.setQueryData(['auth'], { username: 'alex', role: 'member' });
    expect(render()).toContain('No machine data');
    client.setQueryData(['auth'], { username: 'alex', role: 'owner' });
    expect(render()).toContain('Owner machine');
  } finally { client.clear(); }
});

test('live sessions index preserves project membership and input order', () => {
  expect(sessionBits).toHaveProperty('liveSessionsByProject', expect.any(Function));
  const base = demoSessions()[0];
  const sessions = [
    { ...base, id: 'first', project_id: 2, observed_state: 'idle' },
    { ...base, id: 'second', project_id: 1, observed_state: 'active' },
    { ...base, id: 'stopped', project_id: 2, observed_state: 'stopped' },
    { ...base, id: 'third', project_id: 2, observed_state: 'waiting_input' },
    { ...base, id: 'loose', project_id: null, observed_state: 'active' },
  ] as SessionSummary[];
  const indexed = sessionBits.liveSessionsByProject(sessions);
  expect([...indexed].map(([project, rows]) => [project, rows.map(row => row.id)]))
    .toEqual([[2, ['first', 'third']], [1, ['second']]]);
  expect(sessionBits.liveSessionsByProject([]).size).toBe(0);
});

const projects = Array.from({ length: 50 }, (_, index) => ({
  id: 88001 + index, slug: `cedar-${String(index + 1).padStart(3, '0')}`,
  name: `Cedar ${String(index + 1).padStart(3, '0')}`, host: 'local', account: 'alex-agent',
  owner: 'alex', repo: null, workspace_path: `/tmp/cedar-${index + 1}`,
  import_mode: 'managed', trusted: true, archived: false, compose_autostart: false,
}));

for (const shell of ['old']) for (const view of ['Cards', 'List']) {
  test(`runtime reads follow the viewport in ${shell} shell ${view}`, async ({ page }) => {
    test.fixme(true, "These original tests require per-project live container probes. The compact list shows configured autostart; current directory no-fanout qualification remains below.");
    await page.clock.install();
    await page.route('**/api/auth/state', async route => {
      const response = await route.fetch();
      const state = await response.json();
      await route.fulfill({ response, json: { ...state,
        instance: { ...state.instance, features: { ...state.instance.features } } } });
    });

    await page.route('**/api/projects', route => route.fulfill({ json: projects }));
    await page.route('**/api/sessions', route => route.fulfill({ json: [] }));
    const calls = new Map<number, number>();
    await page.route('**/api/projects/*/runtime', route => {
      const id = Number(new URL(route.request().url()).pathname.split('/')[3]);
      calls.set(id, (calls.get(id) ?? 0) + 1);
      return route.fulfill({ json: { containers: [{ State: 'running', Service: 'web', Status: 'Up' }],
        compose: { approved: true, digest: 'artificial-compose', diff: '' } } });
    });
    await page.goto('/projects');
    await expect(page.getByRole('navigation', { name: shell === 'new' ? 'Rail' : 'Main', exact: true })).toBeVisible();
    await page.getByRole('radio', { name: view, exact: true }).click();
    const main = page.getByRole('main');
    const row = (name: string) => view === 'List'
      ? main.getByRole('row').filter({ has: page.getByRole('link', { name, exact: true }) })
      : main.getByRole('link', { name, exact: true }).locator('xpath=ancestor::section[1]');
    await expect(row('Cedar 001')).toContainText('1/1 containers up');
    await page.waitForLoadState('networkidle');
    expect(calls.has(88050)).toBe(false);
    expect(calls.size).toBeLessThan(20);
    const initial = calls.get(88001)!;
    await page.clock.fastForward(16000);
    await expect.poll(() => calls.get(88001)).toBeGreaterThan(initial);
    await main.getByRole('link', { name: 'Cedar 050', exact: true }).scrollIntoViewIfNeeded();
    await expect(row('Cedar 050')).toContainText('1/1 containers up');
    await page.waitForLoadState('networkidle');
    const offscreen = calls.get(88001)!;
    const visible = calls.get(88050)!;
    await page.clock.fastForward(16000);
    await expect.poll(() => calls.get(88050)).toBeGreaterThan(visible);
    expect(calls.get(88001)).toBe(offscreen);
  });
}

test('visible project rows load runtime before horizontally scrolling to Autostart', async ({ page }) => {
  test.fixme(true, 'The compact list uses stacked phone rows and configured Runtime without inline autostart changes. Original horizontal-scroll assertions retained; current behavior is covered in shell-project-pages.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/projects', route => route.fulfill({ json: projects }));
  await page.route('**/api/sessions', route => route.fulfill({ json: [] }));
  let reads = 0;
  await page.route('**/api/projects/*/runtime', route => {
    if (new URL(route.request().url()).pathname === '/api/projects/88001/runtime') reads++;
    return route.fulfill({ json: { containers: [{ State: 'running', Service: 'web', Status: 'Up' }],
      compose: { approved: true, digest: 'artificial-compose', diff: '' } } });
  });
  await page.goto('/projects');
  await page.getByRole('radio', { name: 'List', exact: true }).click();
  await page.waitForLoadState('networkidle');
  reads = 0;
  await page.reload(); // Persisted List choice, without the prior Cards query cache.
  const first = page.getByRole('row').filter({ has: page.getByRole('link', { name: 'Cedar 001', exact: true }) });
  await expect(first).toBeInViewport();
  const autostart = first.getByRole('checkbox', { name: 'Autostart' });
  await expect(autostart).not.toBeInViewport();
  await expect.poll(() => reads).toBeGreaterThan(0);
  await expect(autostart).toBeEnabled();
});

test('Overview and Projects keep live counts when session data changes', async ({ page }) => {
  await page.clock.install();
  const base = demoSessions()[0];
  let sessions = [
    { ...base, id: 'one', project_id: 88001, project: 'cedar-001', observed_state: 'active' },
    { ...base, id: 'two', project_id: 88001, project: 'cedar-001', observed_state: 'idle' },
    { ...base, id: 'three', project_id: 88002, project: 'cedar-002', observed_state: 'active' },
    { ...base, id: 'four', project_id: 88001, project: 'cedar-001', observed_state: 'stopped' },
  ];
  await page.route('**/api/projects', route => route.fulfill({ json: projects.slice(0, 3) }));
  await page.route('**/api/projects/*/runtime', route => route.fulfill({ json: { containers: [], note: 'no compose file' } }));
  await page.route('**/api/sessions', route => route.fulfill({ json: sessions }));
  await page.route('**/api/source-views/directory?*', route => route.fulfill({ json: {
    items: projects.slice(0, 3).map(project => ({ ...project, project_id: project.id, repositories: [], locations: [],
      audience_mismatch: false, membership_count: 0, registration_count: 0, counts: { repositories: 0, locations: 0 },
      counts_scope: 'visible', read_only: true, truncated: { repositories: false, locations: false } })),
    total: 3, offset: 0, limit: 20, next_offset: null, facets: { source_owners: [], hosts: [], scope: 'visible_page' },
  } }));
  await page.goto('/projects');
  const firstRow = page.getByRole('table', { name: 'Project directory', exact: true }).getByRole('row').filter({ hasText: 'Cedar 001' });
  await expect(firstRow).toContainText('2 active');
  await page.goto('/');
  const glance = page.getByRole('heading', { name: 'Projects', exact: true }).locator('xpath=ancestor::section[1]');
  await expect(glance.locator('a[href="/projects/88001"]').getByTitle('2 live session(s)')).toHaveText('2');
  await expect(glance.locator('a[href="/projects/88002"]').getByTitle('1 live session(s)')).toHaveText('1');
  await expect(glance.locator('a[href="/projects/88003"] [title$=" live session(s)"]')).toHaveCount(0);
  sessions = sessions.map(session => session.id === 'one' ? { ...session, observed_state: 'stopped' } : session);
  await page.clock.fastForward(3100);
  await expect(glance.locator('a[href="/projects/88001"]').getByTitle('1 live session(s)')).toHaveText('1');
  await page.goto('/projects');
  await expect(firstRow).toContainText('1 active');
});

// The paged directory supports compact List and source Cards without runtime probes.
test('new source directory bounds rows, refreshes activity and never fans out runtime reads', async ({ page }) => {
  await page.clock.install();
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch(); const state = await response.json();
    await route.fulfill({ response, json: { ...state, instance: { ...state.instance,
      features: { ...state.instance.features } } } });
  });

  await page.route('**/api/projects', route => route.fulfill({ json: projects }));
  const base = demoSessions()[0];
  let sessions = [{ ...base, id: 'directory-live', project_id: 88001, observed_state: 'active' }];
  await page.route('**/api/sessions', route => route.fulfill({ json: sessions }));
  let runtimeReads = 0;
  await page.route('**/api/projects/*/runtime', route => { runtimeReads++; return route.fulfill({ json: { containers: [] } }); });
  await page.route('**/api/source-views/directory?*', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') || 0);
    return route.fulfill({ json: { items: projects.slice(offset, offset + 20).map(project => ({
      ...project, project_id: project.id, repositories: [], locations: [], audience_mismatch: false,
      membership_count: 0, registration_count: 0, counts: { repositories: 0, locations: 0 },
      counts_scope: 'visible', read_only: true, truncated: { repositories: false, locations: false },
    })), total: 50, offset, limit: 20, next_offset: offset + 20 < 50 ? offset + 20 : null,
    facets: { source_owners: [], hosts: [], scope: 'visible_page' } } });
  });
  await page.goto('/projects');
  const table = page.getByRole('table', { name: 'Project directory', exact: true });
  await expect(table.locator('tbody tr')).toHaveCount(20);
  await expect(table.getByRole('row').filter({ hasText: 'Cedar 001' })).toContainText('1 active');
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(table.getByRole('link', { name: 'Cedar 021', exact: true })).toBeVisible();
  await expect(table.locator('tbody tr')).toHaveCount(20);
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(table.getByRole('link', { name: 'Cedar 050', exact: true })).toBeVisible();
  await expect(table.locator('tbody tr')).toHaveCount(10);
  await expect(page.getByRole('button', { name: 'Next', exact: true })).toBeDisabled();
  await page.clock.fastForward(16000);
  expect(runtimeReads).toBe(0);
  sessions = [];
  await page.getByRole('button', { name: 'Previous', exact: true }).click();
  await page.getByRole('button', { name: 'Previous', exact: true }).click();
  await page.clock.fastForward(3100);
  await expect(table.getByRole('row').filter({ hasText: 'Cedar 001' })).toContainText('Idle');
  expect(runtimeReads).toBe(0);
});
