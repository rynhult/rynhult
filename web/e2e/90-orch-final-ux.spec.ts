import { expect, test, type Locator, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { expandTeamPanels, AUTH_FILE, csrfHeaders, expectAxeClean, gitProject, info } from './helpers';

test.use({ storageState: AUTH_FILE });

async function lead(page: Page, briefMode?: 'prepare' | 'build') {
  const project = await gitProject(page, 'final-ux');
  if (briefMode) {
    const saved = await page.request.post(`/api/projects/${project.id}/brief`, { headers: project.headers,
      data: { version: 0, fields: { product: 'Private room bookings', run_mode: briefMode } } });
    expect(saved.ok(), await saved.text()).toBeTruthy();
  }
  const response = await page.request.post('/api/sessions', { headers: project.headers, data: {
    project_id: project.id, provider: 'claude', worktree: true, name: 'Booking lead',
    orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'],
      at_once: 2, total: 4, depth: 1, hours: 2 },
  } });
  expect(response.ok(), await response.text()).toBeTruthy();
  return { ...project, session: (await response.json()).id as string };
}

test('selection settings link opens only the intended section and places keyboard focus there', async ({ page }) => {
  const { session, headers } = await lead(page);
  try {
    await page.goto(`/sessions/${session}?tab=team#overview`);
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    await page.getByRole('link', { name: 'Edit selection allow-lists in Settings', exact: true }).press('Enter');
    await expect(page.getByRole('combobox', { name: 'Settings section', exact: true })).toHaveValue('selection-allow-lists');
    await expect(page.locator('#selection-allow-lists')).toBeFocused();
    await expect(page.locator('#passkeys')).toBeHidden();
    await expectAxeClean(page);
  } finally {
    expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy();
  }
});

test('a failed plan refresh labels retained scope as stale instead of silently appearing current', async ({ page }, ti) => {
  const { session, headers } = await lead(page);
  try {
    const saved = await page.request.patch(`/api/teams/${session}/plan`, { headers, data: {
      version: 0, idea: 'Private room bookings', scope: { in: ['Booking form'], out: [], parked: [] },
    } });
    expect(saved.ok(), await saved.text()).toBeTruthy();
    await page.goto(`/sessions/${session}?tab=team#overview`);
    const idea = page.getByRole('region', { name: 'Idea', exact: true });
    await expect(idea).toContainText('Private room bookings');
    await page.route(`**/api/teams/${session}/plan`, route =>
      route.fulfill({ status: 503, json: { detail: 'Synthetic plan read failure' } }));
    const stale = page.getByRole('status').filter({ hasText: 'Showing the last data received; refresh failed:' });
    await expect(stale).toBeVisible({ timeout: 20_000 });
    await expect(idea).toContainText('Private room bookings');
    await page.getByRole('button', { name: 'Edit plan', exact: true }).press('Enter');
    await expect(stale).toBeVisible();
    await expectAxeClean(page);
    await page.screenshot({ path: ti.outputPath('plan-refresh-error.png'), fullPage: true });
    await page.unroute(`**/api/teams/${session}/plan`);
    await expect(stale).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByRole('form', { name: 'Edit the plan' }).getByRole('textbox', { name: 'Idea', exact: true }))
      .toHaveValue('Private room bookings');
  } finally {
    expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy();
  }
});

test('gate evidence exposes the full tested revision and unavailable legacy bindings without implying freshness', async ({ page }, ti) => {
  const { id } = await gitProject(page, 'binding-ux');
  const response = await page.request.get(`/api/gates?project=${id}`);
  expect(response.ok(), await response.text()).toBeTruthy();
  const snapshot = await response.json();
  const at = new Date().toISOString();
  snapshot.jobs = [{
    id: 'a'.repeat(32), branch: 'acc/booking', base: 'main', head: 'b'.repeat(40),
    base_sha: 'c'.repeat(40), commands_digest: 'd'.repeat(64), runner_version: 'test/gate-1',
    state: 'passed', stale: 'base moved', suite: 'related', priority: 'check', results: {},
    created_at: at, finished_at: at, requested_by: 'alex', attempt: 1, slot: null, waiting: null, reason: null,
  }, {
    id: 'e'.repeat(32), branch: 'acc/legacy', base: 'main', state: 'passed', stale: null,
    suite: 'related', priority: 'check', results: {}, created_at: at, finished_at: at,
    requested_by: 'alex', attempt: 1, slot: null, waiting: null, reason: null,
  }];
  await page.route(`**/api/gates?project=${id}`, route => route.fulfill({ json: snapshot }));
  await page.goto(`/projects/${id}#project-gates`);
  const jobs = page.getByRole('list', { name: 'Gate jobs', exact: true });
  const current = jobs.getByRole('listitem').filter({ hasText: 'acc/booking' });
  const legacy = jobs.getByRole('listitem').filter({ hasText: 'acc/legacy' });
  await current.getByText('Verification binding', { exact: true }).press('Enter');
  for (const value of ['b'.repeat(40), 'c'.repeat(40), 'd'.repeat(64), 'test/gate-1']) {
    await expect(current.getByText(value, { exact: true })).toBeVisible();
  }
  await expect(current).toContainText('stale: base moved');
  await legacy.getByText('Verification binding', { exact: true }).press('Enter');
  await expect(legacy).toContainText('current commit unconfirmed');
  await expect(legacy.getByText('Not recorded', { exact: true })).toHaveCount(4);
  await current.getByText('Verification binding', { exact: true }).press('Enter');
  await legacy.getByText('Verification binding', { exact: true }).press('Enter');
  await expect(current.getByText('stale: base moved. Queue a fresh gate before integrating.', { exact: true })).toBeVisible();
  await expect(legacy.getByText('passed, current commit unconfirmed', { exact: true })).toBeVisible();
  await current.getByText('Verification binding', { exact: true }).press('Enter');
  await legacy.getByText('Verification binding', { exact: true }).press('Enter');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expectAxeClean(page);
  await jobs.screenshot({ path: ti.outputPath('gate-binding.png') });
});

async function tabTo(page: Page, target: Locator) {
  await expect(target).toBeVisible();
  await expect(target).toBeEnabled();
  await page.bringToFront();
  await expect.poll(() => page.evaluate(() => document.hasFocus())).toBe(true);
  // Trial checks visibility, stability and overlays without clicking or assigning focus.
  const skipLink = await target.evaluate(element => element.matches('a[href="#main"]'));
  if (!skipLink) await target.click({ trial: true });
  // Preserve section hashes: activating #main there would change the selected project section.
  if ((!new URL(page.url()).hash || new URL(page.url()).hash === '#main')
    && await target.evaluate(element => !element.closest('[role=dialog]') && !!element.closest('main')
    && !document.querySelector('main')?.contains(document.activeElement))) {
    const skip = page.getByRole('link', { name: 'Skip to content', exact: true });
    if (await skip.count()) {
      await tabTo(page, skip);
      await page.keyboard.press('Enter');
      await expect.poll(() => page.getByRole('main').evaluate(element => element === document.activeElement)).toBe(true);
    }
  }
  let previous = '';
  await expect.poll(async () => {
    const current = await target.evaluate(element => {
      const region = element.closest('[role=dialog], [role=region], [role=tabpanel], main') ?? element;
      return [...region.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex], [contenteditable=true]')]
        .filter(item => item.tabIndex >= 0 && !item.matches(':disabled') && !item.closest('[inert], [hidden]')
          && item.getClientRects().length && getComputedStyle(item).visibility !== 'hidden')
        .map(item => [item.tagName, item.getAttribute('aria-label'), item.getAttribute('name'),
          item.textContent, item.tabIndex]);
    });
    const signature = JSON.stringify(current);
    const stable = signature === previous;
    previous = signature;
    return stable;
  }, { message: 'Target region has a stable keyboard focus order' }).toBe(true);
  const handle = await target.elementHandle();
  if (!handle) throw new Error('Keyboard target detached after readiness checks');
  const order = () => handle.evaluate(element => {
    if (!element.isConnected) throw new Error('Keyboard target detached during traversal');
    const root = element.closest('[role=dialog]') ?? document.body;
    const stops = [...root.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex], [contenteditable=true]')]
      .filter(item => item.tabIndex >= 0 && !item.matches(':disabled') && !item.closest('[inert], [hidden]')
        && item.getClientRects().length && getComputedStyle(item).visibility !== 'hidden')
      .sort((a, b) => (a.tabIndex || Infinity) - (b.tabIndex || Infinity));
    const active = stops.indexOf(document.activeElement as HTMLElement);
    const target = stops.indexOf(element as HTMLElement);
    const forward = active < 0 ? target + 1 : (target - active + stops.length) % stops.length;
    const backward = active < 0 ? stops.length - target : (active - target + stops.length) % stops.length;
    return { count: stops.length, reverse: backward < forward, focused: element === document.activeElement };
  });
  try {
    const budget = (await order()).count;
    for (let n = 0; n <= budget; n++) {
      const current = await order();
      if (current.focused) {
        await expect.poll(() => handle.evaluate(element => element === document.activeElement && document.hasFocus())).toBe(true);
        return;
      }
      const before = await page.evaluateHandle(() => document.activeElement);
      try {
        await page.keyboard.press(current.reverse ? 'Shift+Tab' : 'Tab');
        await expect.poll(() => before.evaluate(element => element === document.body || element !== document.activeElement),
          { message: 'Tab advances keyboard focus' }).toBe(true);
      } finally { await before.dispose(); }
    }
    await expect.poll(() => handle.evaluate(element => element === document.activeElement),
      { message: `Target is reachable within the ${budget} live tab stops` }).toBe(true);
  } finally { await handle.dispose(); }
}

test('keyboard traversal reaches a target beyond 100 tab stops on an initially inactive page', async ({ page, context }) => {
  await page.goto('/projects');
  await page.setContent(`<nav>${Array.from({ length: 120 }, (_, n) => `<a href="#${n}">Earlier ${n}</a>`).join('')}</nav>
    <main><button>Keyboard destination</button></main>
    <footer>${Array.from({ length: 120 }, (_, n) => `<a href="#later-${n}">Later ${n}</a>`).join('')}</footer>`);
  const other = await context.newPage();
  try {
    await other.bringToFront();
    const target = page.getByRole('button', { name: 'Keyboard destination', exact: true });
    await tabTo(page, target);
    await expect.poll(() => target.evaluate(element => document.hasFocus() && document.activeElement === element)).toBe(true);
  } finally { await other.close(); }
});

test('keyboard traversal skips accumulated navigation through the real keyboard skip-link action', async ({ page }) => {
  await page.goto('/projects');
  await page.setContent(`<a id="skip" href="#main">Skip to content</a>
    <nav>${Array.from({ length: 400 }, (_, n) => `<a href="#${n}">Earlier ${n}</a>`).join('')}</nav>
    <main id="main" tabindex="0"><button id="destination">Keyboard destination</button></main>`);
  const visited: string[] = [];
  await page.exposeFunction('recordKeyboardFocus', (id: string) => visited.push(id));
  await page.evaluate(() => document.addEventListener('focusin', event => {
    const id = (event.target as HTMLElement).id;
    if (id) (window as unknown as { recordKeyboardFocus: (id: string) => void }).recordKeyboardFocus(id);
  }));
  await tabTo(page, page.getByRole('button', { name: 'Keyboard destination', exact: true }));
  await expect.poll(() => visited).toEqual(['skip', 'main', 'destination']);
});

async function teamReply(session: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll('-', '');
  appendFileSync(`${info().run_dir}/${session}/events.jsonl`, JSON.stringify({
    acc_session: session, hook: { hook_event_name: 'AccTeam', request: { id, action, ...args } },
  }) + '\n');
  const path = `${info().run_dir}/${session}/team/${id}.json`;
  await expect.poll(() => existsSync(path), { timeout: 20_000 }).toBeTruthy();
  return JSON.parse(readFileSync(path, 'utf8'));
}

async function team(session: string, action: string, args: object = {}) {
  const reply = await teamReply(session, action, args);
  expect(reply.ok, JSON.stringify(reply)).toBeTruthy();
  return reply.result;
}

test('effective run mode accepts only person widening, allows lead narrowing and labels the brief suggestion', async ({ page }, ti) => {
  const { session, headers } = await lead(page, 'prepare');
  try {
    await page.goto(`/sessions/${session}?tab=team#overview`);
    const effective = (mode: string) => page.getByRole('heading', { name: `Effective run mode: ${mode}`, exact: true });
    await expect(effective('Prepare')).toBeVisible();
    const refused = await teamReply(session, 'plan_update', { run_mode: 'build', reason: 'Implementation requested by lead' });
    expect(refused.ok).toBe(false);
    expect(refused.error).toContain('person');
    expect(refused.error).toContain('Run mode');
    await expect(effective('Prepare')).toBeVisible();
    expect((await page.request.post(`/api/sessions/${session}/stop`, { headers })).ok()).toBeTruthy();
    await expect.poll(async () => {
      const current = await (await page.request.get(`/api/sessions/${session}`)).json();
      return current.desired_state === 'stopped' && !current.team_stop_pending;
    }).toBe(true);
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    await tabTo(page, page.getByRole('combobox', { name: 'Run mode', exact: true }));
    await page.keyboard.press('End');
    await tabTo(page, page.getByRole('button', { name: 'Save run mode', exact: true }));
    await page.keyboard.press('Enter');
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expandTeamPanels(page);
    await expect(effective('Build')).toBeVisible();
    await expect(page.getByText('The brief suggested Prepare.', { exact: true })).toBeVisible();
    const history = page.getByText(/^Plan history \(/);
    await tabTo(page, history);
    await page.keyboard.press('Enter');
    const versions = history.locator('..').getByRole('listitem');
    await expect(versions.filter({ hasText: 'Person chose build mode' })).toContainText('alex');
    await effective('Build').scrollIntoViewIfNeeded();
    await expectAxeClean(page);
    await page.screenshot({ path: ti.outputPath('effective-build-suggested-prepare.png') });
    expect((await page.request.post(`/api/sessions/${session}/resume`, { headers })).ok()).toBeTruthy();
    await team(session, 'plan');
    await team(session, 'plan_update', { run_mode: 'prepare', reason: 'Lead needs more discovery' });
    await expect(effective('Prepare')).toBeVisible();
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    await expect(page.getByRole('combobox', { name: 'Run mode', exact: true })).toHaveValue('prepare');
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expandTeamPanels(page);
    await history.click();
    await expect(versions.filter({ hasText: 'Lead needs more discovery' })).toContainText('the lead');
    const stopped = await (await page.request.get(`/api/sessions/${session}`)).json();
    expect(stopped.desired_state).toBe('stopped');
    expect(stopped.launch_spec.inputs.permission_mode).toBe('plan');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expectAxeClean(page);
  } finally {
    expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy();
  }
});

test('new project keyboard journey saves a screenshot brief, prepares scope and starts a separate build grant', async ({ page }, ti) => {
  test.setTimeout(150_000);
  const name = `Booking ${randomUUID().slice(0, 8)}`;
  await page.goto('/projects');
  await tabTo(page, page.getByRole('button', { name: 'New project', exact: true }).first());
  await page.keyboard.press('Enter');
  const projectDialog = page.getByRole('dialog', { name: 'New project', exact: true });
  await expect(projectDialog).toBeVisible();
  await expect.poll(() => projectDialog.getByRole('textbox', { name: 'Name', exact: true })
    .evaluate(element => element === document.activeElement)).toBe(true);
  await page.keyboard.insertText(name);
  await tabTo(page, projectDialog.getByRole('button', { name: 'Create project', exact: true }));
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/\d+$/);
  const id = Number(new URL(page.url()).pathname.split('/').pop());
  const headers = await csrfHeaders(page);
  const projects = await (await page.request.get('/api/projects')).json();
  const project = projects.find((row: { id: number }) => row.id === id);
  // New managed projects already have the bootstrap commit needed for a team worktree.
  expect(execFileSync('git', ['-C', project.workspace_path, 'show', 'HEAD:README.md'], { encoding: 'utf8' })).toContain(name);
  await page.goto(`/projects/${id}#project-brief`);
  const product = page.getByRole('textbox', { name: 'Product or feature', exact: true });
  await expect(page.getByText('Editing from version 0. Saving keeps every earlier version.', { exact: true })).toBeVisible();
  await tabTo(page, product); await page.keyboard.insertText('A private room booking web app');
  const references = page.getByText('References with captions (0/20)', { exact: true });
  await tabTo(page, references); await page.keyboard.press('Enter');
  await expect(references.locator('..')).toHaveAttribute('open', '');
  await tabTo(page, page.getByRole('textbox', { name: 'Reference caption', exact: true }));
  await page.keyboard.insertText('Booking screen sketch');
  await page.keyboard.press('Tab');
  await expect.poll(() => page.getByRole('textbox', { name: 'What to copy, keep, change or avoid', exact: true })
    .evaluate(element => element === document.activeElement)).toBe(true);
  await page.keyboard.insertText('Keep the simple booking flow; avoid tiny controls.');
  await page.getByLabel('Or upload PNG, JPG, WebP or PDF (10 MiB maximum)', { exact: true }).setInputFiles({
    name: 'booking.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5r8AAAAASUVORK5CYII=', 'base64'),
  });
  await tabTo(page, page.getByRole('button', { name: 'Add reference', exact: true }));
  await page.keyboard.press('Enter');
  await expect(page.getByText('REF-1: Booking screen sketch', { exact: true })).toBeVisible();
  await tabTo(page, page.getByRole('button', { name: 'Save project brief', exact: true }));
  await page.keyboard.press('Enter');
  await expect(page.getByRole('status').filter({ hasText: 'Brief version 1 saved.' })).toBeVisible();
  await expectAxeClean(page);
  const owned: string[] = [];
  async function launch(mode: 'prepare' | 'build') {
    await page.goto(`/projects/${id}`);
    await tabTo(page, page.getByRole('main').getByRole('button', { name: 'New session', exact: true }));
    await page.keyboard.press('Enter');
    const dialog = page.getByRole('dialog', { name: 'New session', exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(id));
    await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
    await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    await tabTo(page, dialog.getByRole('button', { name: /^Advanced/ }));
    await page.keyboard.press('Enter');
    await tabTo(page, dialog.getByRole('radio', { name: /Once/ }));
    await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown');
    await expect(dialog.getByRole('radio', { name: /With helpers/ })).toBeChecked();
    const choice = dialog.getByRole('combobox', { name: 'Team run mode', exact: true });
    if (mode === 'prepare') await expect(choice).toHaveValue('prepare');
    else {
      await tabTo(page, choice); await page.keyboard.press('End');
      await expect(choice).toHaveValue('build');
    }
    await tabTo(page, dialog.getByRole('button', { name: 'Launch', exact: true }));
    await expect(dialog.getByRole('button', { name: 'Launch', exact: true })).toBeInViewport();
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/sessions' && r.request().method() === 'POST');
    await page.keyboard.press('Enter');
    const made = await response; expect(made.ok(), await made.text()).toBeTruthy();
    const session = (await made.json()).id as string; owned.push(session);
    await page.goto(`/sessions/${session}?tab=team#overview`);
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    await expect(page.getByRole('heading', { name: `Run mode: ${mode === 'prepare' ? 'Prepare' : 'Build'}`, exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expandTeamPanels(page);
    return session;
  }
  try {
    const prepare = await launch('prepare');
    await expect(page.getByText('The lead has not interpreted this brief yet.', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    await expect(page.getByText('No assignments recorded.', { exact: true })).toBeVisible();
    await expectAxeClean(page);
    expect((await page.request.post(`/api/sessions/${prepare}/team/stop`, { headers })).ok()).toBeTruthy();
    await page.goto(`/projects/${id}#project-brief`);
    await tabTo(page, page.getByRole('combobox', { name: 'Run mode', exact: true }));
    await page.keyboard.press('End');
    await tabTo(page, page.getByRole('button', { name: 'Save project brief', exact: true }));
    await page.keyboard.press('Enter');
    await expect(page.getByRole('status').filter({ hasText: 'Brief version 2 saved.' })).toBeVisible();
    const build = await launch('build');
    await team(build, 'brief');
    await team(build, 'brief_interpret', { brief_version: 2, text: 'Synthetic interpretation, not a vision qualification.',
      references: { 'REF-1': { status: 'readable', note: 'Synthetic fixture image; no real provider inspected it.' } } });
    await team(build, 'brief_draft', { brief_version: 2,
      requirements: [{ text: 'Book a room privately', acceptance: 'Related booking tests pass' }], constraints: [] });
    await team(build, 'plan_update', { reason: 'Small agreed web app', idea: 'Private room bookings',
      scope: { in: ['Booking form'], out: ['Public directory'], parked: [] },
      phases: [{ key: 'booking', title: 'Booking form', requirement_ids: ['R-1'] }] });
    await team(build, 'worklist_add', { kind: 'question', title: 'What should the confirmation say?' });
    await page.reload();
    await page.getByRole('tab', { name: 'Working list', exact: true }).click();
    const answer = page.getByRole('textbox', { name: 'Answer "What should the confirmation say?"', exact: true });
    await tabTo(page, answer); await page.keyboard.insertText('Your private booking is confirmed.');
    await tabTo(page, page.getByRole('listitem', { name: 'What should the confirmation say?', exact: true })
      .getByRole('button', { name: 'Answer', exact: true }));
    await page.keyboard.press('Enter');
    await expect(answer).toHaveCount(0);
    const proposal = page.getByRole('listitem').filter({ hasText: 'Book a room privately' })
      .filter({ has: page.getByRole('button', { name: 'Accept change', exact: true }) });
    // Decision options are one-tap buttons; Change opens a person-written answer.
    await tabTo(page, proposal.getByRole('button', { name: 'Accept change', exact: true }));
    const accepted = page.waitForResponse(response => response.request().method() === 'PATCH'
      && new URL(response.url()).pathname.startsWith(`/api/teams/${build}/worklist/items/`)
      && response.request().postDataJSON().answer === 'accept');
    await page.keyboard.press('Enter');
    const savedAnswer = await accepted;
    expect(savedAnswer.ok(), await savedAnswer.text()).toBeTruthy();
    expect((await savedAnswer.json()).answer).toBe('accept');
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expandTeamPanels(page);
    await expect(page.getByRole('region', { name: 'Requirements', exact: true })).toContainText('active');
    await team(build, 'plan_ready', { reason: 'Bounded synthetic scope' });
    const assignment = await team(build, 'assign', { goal: 'Build the booking form', task_type: 'implementation',
      acceptance: ['Related booking tests pass'], requirement_ids: ['R-1'],
      need: { role: 'fullstack', classification_reason: 'Booking form needs the granted fullstack profile' } });
    const picked = await team(build, 'select', { assignment: assignment.id, pin: { agent: 'claude' } });
    expect(picked.chosen, JSON.stringify(picked)).not.toBeNull();
    expect(picked.candidates[picked.chosen].profile).toBe('fullstack');
    const helper = await team(build, 'start', { assignment: assignment.id, selection: picked.id, name: 'Booking helper' });
    await team(helper.id, 'report', { status: 'done', summary: 'Synthetic booking evidence saved.' });
    await page.getByRole('tab', { name: 'Settings', exact: true }).click();
    await expandTeamPanels(page);
    const assignments = page.getByRole('region', { name: 'Assignments', exact: true });
    await expect(assignments).toContainText('Synthetic booking evidence saved.');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expectAxeClean(page);
    await assignments.scrollIntoViewIfNeeded();
    await page.screenshot({ path: ti.outputPath('new-user-build-team.png') });
  } finally {
    for (const session of owned) expect((await page.request.post(`/api/sessions/${session}/team/stop`, { headers })).ok()).toBeTruthy();
  }
});
