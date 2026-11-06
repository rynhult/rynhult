import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { expect, test } from './fixtures';
import { AUTH_FILE, AXE_PAGES, csrfHeaders, gitProject, info } from './helpers';
import { containmentTests, containmentSize, containmentMessage } from './ux-final-containment';
containmentTests();

test.use({ storageState: AUTH_FILE });
for (const theme of ['dark', 'light'] as const) test(`stateful data stays contained in ${theme}`, async ({ page }, testInfo) => {
  test.setTimeout(180_000);
  await page.emulateMedia({ colorScheme: theme });
  await page.goto('/sessions');
  const headers = await csrfHeaders(page);
  const projects: { id: number; slug: string }[] = [];
  const sessions: string[] = [];
  const schedules: string[] = [];
  const teams: string[] = [];
  const groups: string[] = [];
  const toolRequests: string[] = [];
  const notices: number[] = [];
  const post = async (path: string, data: object = {}) => {
    const response = await page.request.post(path, { headers, data });
    expect(response.ok(), `${path}: ${await response.text()}`).toBe(true);
    return response.json();
  };
  try {
    const users = await (await page.request.get('/api/users')).json();
    const owner = users.users.find((user: { username: string }) => user.username === 'alex');
    for (let i = 0; i < 4; i++) {
      const project = await gitProject(page, `containment-${i}`, {
        name: `Phone ${i} ${'UnbrokenProjectName'.repeat(8)}`,
        notes: `Review /srv/example/${'nested-path/'.repeat(12)}README.md`,
      });
      projects.push(project);
      execFileSync('git', ['-C', project.folder, 'branch', '-m', `feature/${'long-branch-'.repeat(8)}${i}`]);
      for (let j = 0; j < 2; j++) {
        const session = await post('/api/sessions', { project_id: project.id, provider: 'claude',
          name: `${i}${j}${'LongSessionName'.repeat(3)}`.slice(0, 40),
          ...(i === 0 && j === 0 ? { worktree: true, orchestrator: {
            agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 2, total: 4, depth: 1, hours: 2,
          } } : {}) });
        sessions.push(session.id);
        if (i === 0 && j === 0) {
          teams.push(session.id);
          const team = async (action: string, args: object) => {
            const id = randomUUID().replaceAll('-', '');
            appendFileSync(`${info().run_dir}/${session.id}/events.jsonl`, JSON.stringify({ acc_session: session.id,
              hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } } }) + '\n');
            const reply = `${info().run_dir}/${session.id}/team/${id}.json`;
            await expect.poll(() => existsSync(reply), { timeout: 20_000 }).toBe(true);
            expect(JSON.parse(readFileSync(reply, 'utf8')).ok).toBe(true);
          };
          await team('plan_update', { reason: 'Phone regression fixture', idea: 'Review phone containment.',
            scope: { in: ['Layout'], out: [], parked: [] }, phases: [{ key: 'layout', title: 'Check layout' }] });
          await team('plan_ready', { reason: 'Bounded synthetic task' });
          for (let task = 0; task < 6; task++) await team('assign', {
            goal: `Review ${task} /srv/example/${'unbroken-path-'.repeat(10)}README.md`, task_type: 'implementation',
            acceptance: ['Content stays readable'], requirement_ids: [],
            need: { role: 'general', classification_reason: 'Synthetic phone layout check' },
          });
        }
      }
      const schedule = await post('/api/schedules', { launch: { project_id: project.id, provider: 'claude',
        name: `${i}${'ScheduledReview'.repeat(3)}`.slice(0, 40) },
        timing: { kind: 'daily', time: '09:00' }, timezone: 'America/Chicago', enabled: false });
      schedules.push(schedule.id);
      const group = await post('/api/groups', { name: `Team ${i} ${'LongTeamName'.repeat(5)}`, maintainer_id: owner.id });
      groups.push(group.id);
    }
    // Earlier stateful specs leave actionable Inbox rows, not only sessions.
    // Keep this request pending: fake providers never install anything here.
    const toolName = `containment-${'long-package-name-'.repeat(6)}`;
    appendFileSync(`${info().run_dir}/${sessions[1]}/events.jsonl`, JSON.stringify({ provider: 'acc',
      hook: { hook_event_name: 'AccRequest', request: { type: 'tool', manager: 'npm', name: toolName,
        reason: 'Exercise pending Inbox content on a phone.', action: 'install' } } }) + '\n');
    await expect.poll(async () => (await (await page.request.get('/api/attention')).json())
      .some((item: { title: string }) => item.title.includes(toolName))).toBe(true);
    const attention = await (await page.request.get('/api/attention')).json();
    toolRequests.push(...attention.filter((item: { tool?: { name: string } }) => item.tool?.name === toolName)
      .map((item: { tool: { id: string } }) => item.tool.id));
    // Invalid coordinates are refused by the real request parser. The resulting
    // notice includes the session name, as do failures left by earlier specs.
    appendFileSync(`${info().run_dir}/${sessions[1]}/events.jsonl`, JSON.stringify({ provider: 'acc',
      hook: { hook_event_name: 'AccRequest', request: { type: 'tool', manager: 'npm',
        name: 'InvalidPackageName', reason: 'Phone refusal-notice regression.', action: 'install' } } }) + '\n');
    await expect.poll(async () => {
      const rows = await (await page.request.get('/api/attention')).json();
      const refused = rows.filter((item: { session_id: string; title: string }) =>
        item.session_id === sessions[1] && item.title.includes('tool request refused'));
      notices.push(...refused.map((item: { id: number }) => item.id));
      return refused.length;
    }).toBeGreaterThan(0);
    const alternatives = await post('/api/multiruns', { project_id: projects[0].id,
      name: 'LongUnbrokenAlternativeComparisonName123', initial_prompt: 'Review the phone layout.',
      runs: [{ provider: 'claude' }, { provider: 'claude' }] });
    sessions.push(...alternatives.runs.flatMap((run: { session: { id: string } | null }) => run.session ? [run.session.id] : []));
    expect(alternatives.runs.every((run: { session: object | null }) => run.session !== null), 'Both alternatives launched').toBe(true);
    // Retain an ordinary launcher draft alongside the server-side resources.
    await page.goto('/projects');
    await page.getByRole('button', { name: 'New session', exact: true }).first().click();
    const launcher = page.getByRole('dialog', { name: 'New session', exact: true });
    await launcher.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Review ' + 'long/path/'.repeat(20));
    await launcher.getByRole('button', { name: 'Close', exact: true }).click();
    page.on('dialog', dialog => dialog.accept());
    const failures: object[] = [];
    for (const width of [320, 390]) {
      await page.setViewportSize({ width, height: 844 });
      for (const path of [...AXE_PAGES, '/groups', '/guide/User-Guide/Install', `/sessions/${teams[0]}?tab=team`, `/projects/${projects[0].id}`]) {
        await page.goto(path);
        await page.waitForLoadState('networkidle');
        const size = await containmentSize(page);
        const message = containmentMessage(path, width, size);
        if (size.root > width || size.main > size.client) failures.push({ path, width, ...size });
        expect.soft(size.root, message).toBeLessThanOrEqual(width);
        expect.soft(size.main, message).toBeLessThanOrEqual(size.client);
      }
    }
    await testInfo.attach('containment-overflow', { body: JSON.stringify(failures, null, 2), contentType: 'application/json' });
  } finally {
    const cleanupErrors: string[] = [];
    const cleanup = async (action: () => Promise<unknown>) => {
      try { await action(); } catch (error) { cleanupErrors.push(String(error)); }
    };
    for (const id of toolRequests) await cleanup(() => post(`/api/tools/requests/${id}/reject`));
    for (const id of new Set(notices)) await cleanup(() => post(`/api/attention/${id}/ack`));
    for (const id of schedules) {
      await cleanup(() => post(`/api/schedules/${id}/cancel`));
      await cleanup(async () => expect((await page.request.delete(`/api/schedules/${id}`, { headers })).ok()).toBe(true));
    }
    for (const id of teams) await cleanup(() => post(`/api/sessions/${id}/team/stop`));
    for (const id of sessions) await cleanup(() => post(`/api/sessions/${id}/stop`));
    for (const project of projects) await cleanup(() => post(`/api/projects/${project.id}/delete`, { confirm: project.slug }));
    for (const id of groups) await cleanup(async () => {
      const group = await (await page.request.get(`/api/groups/${id}`)).json();
      await post(`/api/groups/${id}/archive`, { revision: group.revision });
    });
    expect(cleanupErrors, 'All containment fixtures cleaned up').toEqual([]);
  }
});
