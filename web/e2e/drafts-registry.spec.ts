import { expect, type Page } from './fixtures';
import { test } from './shell-state';
import { AUTH_FILE, gitProject, openAdvanced } from './helpers';

test.use({ storageState: AUTH_FILE, });
// Identity-change navigation can leave an auth intercept in flight at the final
// assertion. Finish those handlers before Playwright disposes their responses.
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });

async function reloadWarning(page: Page) {
  // A real user interaction supplies the browser's sticky activation requirement.
  const dialog = page.waitForEvent('dialog');
  // Chromium leaves a dismissed reload's navigation promise pending. Do not
  // await it; bound it and handle its rejection while asserting the actual dialog.
  void page.reload({ waitUntil: 'domcontentloaded', timeout: 5000 }).catch(() => null);
  const warning = await dialog;
  expect(warning.type()).toBe('beforeunload');
  await warning.dismiss();
}

for (const kind of ['new-project', 'retained-new-project', 'new-session', 'new-session-options', 'register-folder', 'brief', 'files', 'security', 'remote-access', 'publishing', 'publishing-address'] as const) {
  test(`reload warns for dirty ${kind}`, async ({ page }) => {
    if (kind === 'brief' || kind === 'files') {
      const { id } = await gitProject(page, `draft-${kind}`);
      await page.goto(`/projects/${id}${kind === 'files' ? '?tab=files&file=app.py' : '#project-brief'}`);
      if (kind === 'brief') await page.getByRole('textbox', { name: 'Product or feature', exact: true }).fill('Unsaved test brief');
      else await page.getByRole('textbox', { name: 'File content' }).fill('unsaved file edits');
    } else if (kind === 'security') {
      await page.goto('/settings#security');
      await page.getByRole('combobox', { name: 'Security mode', exact: true }).selectOption('unrestricted');
    } else if (kind === 'remote-access') {
      await page.goto('/settings/remote-access');
      await page.getByRole('textbox', { name: 'Public hostname' }).fill('remote.example.com');
    } else if (kind === 'publishing-address') {
      await page.goto('/settings#publishing');
      await page.getByRole('combobox', { name: 'Address access' }).selectOption('internal');
    } else if (kind === 'publishing') {
      await page.route('**/api/publish/zones', route => route.fulfill({ json: { configured: true, zones: [{ id: 'demo', name: 'example.com' }], public_ip: '192.0.2.10', internal_ip: '10.0.0.10' } }));
      await page.goto('/publishing');
      await page.getByRole('combobox', { name: 'Access', exact: true }).selectOption('public');
    } else {
      await page.goto('/projects');
      if (kind.startsWith('new-session')) {
        await page.getByRole('button', { name: 'New session', exact: true }).first().click();
        const form = page.getByRole('region', { name: 'New session', exact: true });
        if (kind === 'new-session-options') {
          await openAdvanced(form);
          await form.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('codex');
        } else await form.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Unsaved task');
        await form.getByRole('button', { name: 'Back to workspace', exact: true }).click();
      } else if (kind === 'register-folder') {
        await page.goto('/projects?import=existing');
        await page.getByRole('dialog', { name: 'Register a folder', exact: true }).getByRole('textbox', { name: 'Name', exact: true }).fill('Unsaved registration');
      } else {
        await page.getByRole('button', { name: 'New project', exact: true }).first().click();
        const form = page.getByRole('dialog', { name: 'New project', exact: true });
        await form.getByRole('textbox', { name: 'Name', exact: true }).fill('Unsaved project');
        if (kind === 'retained-new-project') await form.getByRole('button', { name: 'Cancel', exact: true }).click();
      }
    }
    await reloadWarning(page);
    // Test cleanup explicitly accepts closing its disposable dirty tab.
    page.on('dialog', dialog => dialog.accept());
  });
}

test('storage failure keeps current draft and announces a local-save failure', async ({ page }) => {
  const { id } = await gitProject(page, 'draft-storage');
  await page.addInitScript(() => {
    Storage.prototype.setItem = function(key: string) {
      if (key.startsWith('acc-draft:')) throw new DOMException('Storage full', 'QuotaExceededError');
    };
  });
  await page.goto(`/projects/${id}#project-brief`);
  const input = page.getByRole('textbox', { name: 'Product or feature', exact: true });
  await input.fill('Keep the latest edit in memory');
  await expect(page.getByRole('status').filter({ hasText: "Couldn't save this draft locally" })).toBeVisible();
  await expect(input).toHaveValue('Keep the latest edit in memory');
  await reloadWarning(page);
  await expect(input).toHaveValue('Keep the latest edit in memory');
  page.on('dialog', dialog => dialog.accept());
});

for (const kind of ['now', 'reply', 'discussion', 'item', 'plan'] as const) {
  test(`reload warns for dirty team ${kind}`, async ({ page }) => {
    const { id, headers } = await gitProject(page, `draft-${kind}`);
    const created = await page.request.post('/api/sessions', { headers, data: {
      project_id: id, provider: 'claude', name: 'Draft fixture lead', worktree: true,
      orchestrator: { agents: [{ agent: 'claude', profile: null }], machines: ['local'], at_once: 1, total: 2, depth: 1, hours: 1 },
    } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const lead = (await created.json()).id;
    try {
      await page.route(`**/api/teams/${lead}/worklist`, route => route.fulfill({ json: {
        now: 'Review the proposal', now_revision: 1, now_updated_at: null, now_updated_by: null, revision: 1, idle_minutes: 0,
        items: [{ id: 986, position: 0, kind: kind === 'discussion' ? 'decision' : 'question', title: 'Example question', steps: '', state: 'open', revision: 1,
          options: null, recommendation: null, default: null, in_effect: null, answer: null, answered_by: null,
          answered_at: null, note: null, source: 'lead', owner_edited: false, links: [], done_at: null, blocks: [] }],
      } }));
      await page.route(`**/api/teams/${lead}/plan`, route => route.fulfill({ json: {
        state: 'ready', run_mode: 'prepare', idea: 'An example plan', version: 1, ready_at: null,
        scope: { in: [], out: [], parked: [] }, phases: [], person_edits: {}, open_questions: [], mermaid: 'flowchart TD', history: [],
      } }));
      await page.goto(`/sessions/${lead}?tab=team#${kind === 'plan' ? 'overview' : 'worklist'}`);
      if (kind === 'plan') {
        await page.getByRole('button', { name: 'Edit plan', exact: true }).click();
        expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
        await page.getByRole('textbox', { name: 'Idea', exact: true }).fill('Edited plan');
      } else if (kind === 'now') {
        await page.getByRole('region', { name: 'Now' }).getByRole('button', { name: 'Edit', exact: true }).click();
        expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
        await page.getByLabel('Now summary').fill('Unsaved summary');
      } else if (kind === 'item') await page.getByRole('textbox', { name: 'New item title' }).fill('Unsaved list item');
      else if (kind === 'discussion') {
        await page.getByRole('button', { name: 'Discuss', exact: true }).click();
        await page.getByRole('textbox', { name: 'Message to lead' }).fill('Unsaved discussion');
      } else await page.getByRole('textbox', { name: /Your answer|Answer/ }).fill('Unsaved answer');
      await reloadWarning(page);
      page.on('dialog', dialog => dialog.accept());
    } finally {
      expect((await page.request.post(`/api/sessions/${lead}/team/stop`, { headers })).ok()).toBeTruthy();
    }
  });
}

test('a changed sign-in destroys old mounted drafts before the next person sees editors', async ({ page }) => {
  let otherPerson = false;
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch();
    const state = await response.json();
    await route.fulfill({ response, json: { ...state, username: otherPerson ? 'blair' : state.username } });
  });
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  const form = page.getByRole('dialog', { name: 'New project', exact: true });
  await form.getByRole('textbox', { name: 'Name', exact: true }).fill('Private previous-person draft');
  await form.getByRole('button', { name: 'Cancel', exact: true }).click();
  const changed = page.waitForEvent('framenavigated', { predicate: frame => frame === page.mainFrame() });
  otherPerson = true;
  await changed; // the shared auth polling observes the cookie/identity boundary
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await expect(page.getByRole('dialog', { name: 'New project', exact: true }).getByRole('textbox', { name: 'Name', exact: true })).toHaveValue('');
});

test('a dirty remote policy survives its view unmount without applying it', async ({ page }) => {
  let writes = 0;
  page.on('request', request => { if (request.method() !== 'GET' && new URL(request.url()).pathname === '/api/remote-access') writes++; });
  await page.goto('/settings/remote-access');
  await page.getByRole('textbox', { name: 'Public hostname' }).fill('retained.example.com');
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await page.goBack();
  await expect(page.getByRole('textbox', { name: 'Public hostname' })).toHaveValue('retained.example.com');
  expect(writes).toBe(0);
  await reloadWarning(page);
  page.on('dialog', dialog => dialog.accept());
});

// Security review round 1: each probe asserts the native browser boundary.
test('DR-1 a refused credential check never removes the dirty Security guard', async ({ page }) => {
  await page.route('**/api/security/changes/preview', route => route.fulfill({ status: 403, json: { detail: 'Verify your sign-in again.' } }));
  await page.goto('/settings#security');
  await page.getByRole('combobox', { name: 'Security mode', exact: true }).selectOption('unrestricted');
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  const verify = page.getByRole('dialog', { name: 'Verify your sign-in', exact: true });
  await verify.getByLabel('Verification password').fill('incorrect-example-password');
  await verify.getByLabel('Verification authenticator code', { exact: true }).fill('000000');
  const refused = page.waitForResponse(response => response.url().endsWith('/api/auth/passkeys/reauth'));
  await verify.getByRole('button', { name: 'Verify with password and authenticator' }).click();
  expect((await refused).status()).toBe(401);
  await verify.getByRole('button', { name: 'Cancel', exact: true }).click();
  await reloadWarning(page);
  page.on('dialog', dialog => dialog.accept());
});

test('DR-2 expiry during File save keeps the leave prompt and edited buffer', async ({ page }) => {
  const { id } = await gitProject(page, 'expiry-draft');
  await page.goto(`/projects/${id}?tab=files&file=app.py`);
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill('keep edits after expired Save');
  await page.route(`**/api/files/project/${id}`, route => route.request().method() === 'POST'
    ? route.fulfill({ status: 401, json: { detail: 'Sign in again.' } }) : route.continue());
  const warning = page.waitForEvent('dialog');
  await page.getByRole('button', { name: 'Save', exact: true }).click({ noWaitAfter: true });
  const dialog = await warning;
  expect(dialog.type()).toBe('beforeunload'); await dialog.dismiss();
  await expect(editor).toContainText('keep edits after expired Save');
  page.on('dialog', dialog => dialog.accept());
});

test('DR-3 an explicit project preset wins over another retained session draft', async ({ page }) => {
  test.fixme(true, 'Classic-only project-page launch action is absent; global New session retains its draft. Original per-project preset assertions retained for Josh.');
  const first = await gitProject(page, 'preset-first');
  const second = await gitProject(page, 'preset-second');
  await page.goto(`/projects/${first.id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  let form = page.getByRole('region', { name: 'New session', exact: true });
  await form.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('task only for the first project');
  await form.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await page.getByRole('link', { name: new RegExp(second.slug) }).first().click();
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  form = page.getByRole('region', { name: 'New session', exact: true });
  await expect(form.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(second.id));
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  await form.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Projects', exact: true }).click();
  await page.getByRole('link', { name: new RegExp(first.slug) }).first().click();
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  await expect(form.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue(String(first.id));
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('task only for the first project');
  page.on('dialog', dialog => dialog.accept());
});

test('DR-4 an untouched New session settles defaults without registering a draft', async ({ page }) => {
  await gitProject(page, 'clean-dialog');
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New session', exact: true }).first().click();
  const form = page.getByRole('region', { name: 'New session', exact: true });
  await openAdvanced(form);
  await expect(form.getByRole('combobox', { name: 'Project', exact: true })).not.toHaveValue('0');
  await form.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
});

test('DR-5 role transition reloads and new mounted editors register again', async ({ page }) => {
  let member = false;
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch(); const state = await response.json();
    await route.fulfill({ response, json: { ...state, role: member ? 'member' : state.role } });
  });
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await page.getByRole('dialog', { name: 'New project', exact: true }).getByRole('textbox', { name: 'Name', exact: true }).fill('before role change');
  const warning = page.waitForEvent('dialog'); member = true;
  const prompt = await warning; expect(prompt.type()).toBe('beforeunload');
  const loaded = page.waitForEvent('framenavigated', { predicate: frame => frame === page.mainFrame() });
  await prompt.accept(); await loaded;
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await page.getByRole('dialog', { name: 'New project', exact: true }).getByRole('textbox', { name: 'Name', exact: true }).fill('after role change');
  await reloadWarning(page); page.on('dialog', dialog => dialog.accept());
});

test('DR-7 schedule drafts are separate from the launcher and can be discarded', async ({ page }) => {
  await gitProject(page, 'schedule-draft');
  await page.goto('/projects');
  await page.getByRole('button', { name: 'New session', exact: true }).first().click();
  const launcher = page.getByRole('region', { name: 'New session', exact: true });
  await launcher.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('launcher only');
  await launcher.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await page.getByRole('navigation', { name: 'Rail', exact: true }).getByRole('button', { name: 'Tools', exact: true }).click();
  await page.getByRole('complementary', { name: 'Context sidebar' }).getByRole('link', { name: 'Schedules', exact: true }).click();
  await page.getByRole('button', { name: 'New schedule', exact: true }).click();
  const schedule = page.getByRole('dialog', { name: 'New schedule', exact: true });
  await expect(schedule.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  await schedule.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('schedule only');
  await schedule.getByRole('button', { name: 'Discard draft', exact: true }).click();
  await page.getByRole('dialog', { name: 'Discard this draft?', exact: true }).getByRole('button', { name: 'Discard draft', exact: true }).click();
  await expect(schedule).not.toBeVisible();
  await page.getByRole('button', { name: 'New schedule', exact: true }).click();
  await expect(schedule.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  await schedule.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('button', { name: 'New session', exact: true }).first().click();
  await expect(launcher.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('launcher only');
  page.on('dialog', dialog => dialog.accept());
});

for (const family of ['new-project', 'register-folder', 'brief', 'files', 'security', 'remote-access', 'publishing-address', 'publishing', 'schedule'] as const) {
  test(`DR-4 clean ${family} does not block reload`, async ({ page }) => {
    if (family === 'brief' || family === 'files') {
      const { id } = await gitProject(page, `clean-${family}`);
      await page.goto(`/projects/${id}${family === 'files' ? '?tab=files&file=app.py' : '#project-brief'}`);
      await expect(page.getByRole('textbox', { name: family === 'files' ? 'File content' : 'Product or feature', exact: true })).toBeVisible();
    } else if (family === 'security') {
      await page.goto('/settings#security'); await expect(page.getByRole('combobox', { name: 'Security mode', exact: true })).toBeEnabled();
    } else if (family === 'remote-access') {
      await page.goto('/settings/remote-access'); await expect(page.getByRole('textbox', { name: 'Public hostname' })).toBeVisible();
    } else if (family === 'publishing-address') {
      await page.goto('/settings#publishing'); await expect(page.getByRole('combobox', { name: 'Address access' })).toBeVisible();
    } else if (family === 'publishing') {
      await page.route('**/api/publish/zones', route => route.fulfill({ json: { configured: true, zones: [{ id: 'demo', name: 'example.com' }], public_ip: '192.0.2.10', internal_ip: '10.0.0.10' } }));
      await page.goto('/publishing'); await expect(page.getByRole('combobox', { name: 'Zone', exact: true })).toHaveValue('example.com');
    } else if (family === 'schedule') {
      await page.goto('/schedules'); await page.getByRole('button', { name: 'New schedule', exact: true }).click();
      await page.getByRole('dialog', { name: 'New schedule', exact: true }).getByRole('button', { name: 'Cancel', exact: true }).click();
    } else {
      await page.goto(family === 'register-folder' ? '/projects?import=existing' : '/projects');
      if (family === 'new-project') await page.getByRole('button', { name: 'New project', exact: true }).first().click();
      const form = page.getByRole('dialog', { name: family === 'new-project' ? 'New project' : 'Register a folder', exact: true });
      await form.getByRole('button', { name: 'Cancel', exact: true }).click();
    }
    expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
  });
}

test('DR-2 auth polling expiry also preserves the leave prompt', async ({ page }) => {
  let expired = false;
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch(); const state = await response.json();
    await route.fulfill({ response, json: expired ? { ...state, authenticated: false, username: null } : state });
  });
  const { id } = await gitProject(page, 'auth-expiry');
  await page.goto(`/projects/${id}?tab=files&file=app.py`);
  const editor = page.getByRole('textbox', { name: 'File content' });
  await editor.fill('keep edits after auth polling expiry');
  const warning = page.waitForEvent('dialog'); expired = true;
  const prompt = await warning; expect(prompt.type()).toBe('beforeunload'); await prompt.dismiss();
  await expect(editor).toContainText('keep edits after auth polling expiry');
  page.on('dialog', dialog => dialog.accept());
});

test('DR-7 discarding a retained existing schedule returns to its server values', async ({ page }) => {
  const { id } = await gitProject(page, 'existing-schedule');
  await page.route('**/api/schedules', route => route.fulfill({ json: [{ id: 'draft-fixture', owner: 'alex',
    launch: { project_id: id, provider: 'claude', name: 'Example scheduled task', initial_prompt: 'Saved schedule task', worktree: true },
    timing: { kind: 'daily', time: '09:00' }, timezone: 'UTC', enabled: false, missed: 'once', next_run_at: null, runs: [] }] }));
  await page.goto('/schedules');
  const row = page.getByRole('article', { name: 'Example scheduled task', exact: true });
  await row.getByRole('button', { name: 'Edit', exact: true }).click();
  const form = page.getByRole('dialog', { name: 'Edit schedule', exact: true });
  await form.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Unsaved schedule change');
  await form.getByRole('button', { name: 'Cancel', exact: true }).click();
  await row.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('Unsaved schedule change');
  await form.getByRole('button', { name: 'Discard draft', exact: true }).click();
  await page.getByRole('dialog', { name: 'Discard this draft?', exact: true }).getByRole('button', { name: 'Discard draft', exact: true }).click();
  await row.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('Saved schedule task');
  await form.getByRole('button', { name: 'Cancel', exact: true }).click();
});

test('DR-10 explicit sign-out clears scoped and legacy plaintext tab drafts', async ({ page, isolatedOwner }) => {
  // Exercise real login/logout with a test-owned authenticator. A fresh wall
  // clock step alone cannot prevent parallel tests replaying the owner's code.
  const { PASSWORD, totp, info } = await import('./helpers');
  const signedIn = await page.request.post('/api/auth/login', { headers: { Origin: info().origin },
    data: { username: isolatedOwner.username, password: PASSWORD, code: totp(isolatedOwner.secret) } });
  expect(signedIn.ok(), await signedIn.text()).toBeTruthy();
  await page.goto('/projects');
  await page.evaluate(() => {
    sessionStorage.setItem('acc-draft:brief:legacy', 'legacy private fixture');
    sessionStorage.setItem('acc-draft:v2:fixture', 'scoped private fixture');
    sessionStorage.setItem('unrelated-preference', 'keep');
  });
  await page.getByRole('button', { name: /^Account:/ }).click();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  // The router can reach /login before an in-flight 401 finishes its document
  // redirect. Retry across that navigation, retaining both cleanup assertions.
  await expect(async () => {
    expect(await page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith('acc-draft:')))).toEqual([]);
    expect(await page.evaluate(() => sessionStorage.getItem('unrelated-preference'))).toBe('keep');
  }).toPass({ timeout: 5000 });
});

for (const boundary of ['person', 'role'] as const) test(`DR-13 hashed Security ${boundary} transition reloads the document`, async ({ page }) => {
  let changed = false;
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch(); const state = await response.json();
    await route.fulfill({ response, json: changed ? { ...state, ...(boundary === 'person' ? { username: 'blair', person_id: 'fixture-blair' } : { role: 'member' }) } : state });
  });
  await page.goto('/settings#security');
  await page.getByRole('combobox', { name: 'Security mode', exact: true }).selectOption('unrestricted');
  // Opening the URL-backed launcher supplies the auth poll; the new identity reopens a clean form.
  await page.getByRole('button', { name: 'New session', exact: true }).first().click();
  const loaded = page.waitForEvent('domcontentloaded');
  if (boundary === 'role') {
    const warning = page.waitForEvent('dialog'); changed = true;
    const prompt = await warning; expect(prompt.type()).toBe('beforeunload'); await prompt.accept();
  } else changed = true;
  await loaded;
  const cleanLaunch = page.getByRole('region', { name: 'New session', exact: true });
  await expect(cleanLaunch.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  await cleanLaunch.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await expect(cleanLaunch).toHaveCount(0);
  if (boundary === 'person') await expect(page.getByRole('combobox', { name: 'Security mode', exact: true })).not.toHaveValue('unrestricted');
});

test('DR-14 Stay after expiry keeps polling and removes drafts on a later different sign-in', async ({ page }) => {
  let phase: 'original' | 'expired' | 'other' = 'original'; let polls = 0;
  await page.route('**/api/auth/state', async route => {
    polls++; const response = await route.fetch(); const state = await response.json();
    await route.fulfill({ response, json: phase === 'expired' ? { ...state, authenticated: false, username: null }
      : phase === 'other' ? { ...state, username: 'blair' } : state });
  });
  await page.goto('/projects#keep');
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  const form = page.getByRole('dialog', { name: 'New project', exact: true });
  await form.getByRole('textbox', { name: 'Name', exact: true }).fill('Private before expiry');
  const warning = page.waitForEvent('dialog'); phase = 'expired';
  const prompt = await warning; expect(prompt.type()).toBe('beforeunload'); await prompt.dismiss();
  await expect(form.getByRole('textbox', { name: 'Name', exact: true })).toHaveValue('Private before expiry');
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(true);
  let repeatedWarnings = 0;
  page.on('dialog', dialog => { repeatedWarnings++; void dialog.dismiss(); });
  const afterStay = polls;
  await expect.poll(() => polls, { timeout: 15000 }).toBeGreaterThan(afterStay);
  expect(repeatedWarnings).toBe(0);
  const loaded = page.waitForEvent('domcontentloaded'); phase = 'other'; await loaded;
  await page.getByRole('button', { name: 'New project', exact: true }).first().click();
  await expect(page.getByRole('dialog', { name: 'New project', exact: true }).getByRole('textbox', { name: 'Name', exact: true })).toHaveValue('');
});

test('DR-15 a clean registration has no removal warning or leave guard when removeItem is blocked', async ({ page }) => {
  await page.addInitScript(() => {
    const remove = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function(key) { if (key.startsWith('acc-draft:')) throw new DOMException('Blocked', 'SecurityError'); return remove.call(this, key); };
  });
  await page.goto('/projects?import=existing');
  await expect(page.getByRole('dialog', { name: 'Register a folder', exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: "Couldn't remove the local draft" })).toHaveCount(0);
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
});

test('DR-16 changing a preset target moves its draft and successful launch leaves no guard', async ({ page }) => {
  test.fixme(true, 'Classic-only project-page launch action is absent. Original target-move and consumed-draft assertions retained for Josh.');
  const first = await gitProject(page, 'move-from'); const second = await gitProject(page, 'move-to');
  await page.goto(`/projects/${first.id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  const form = page.getByRole('region', { name: 'New session', exact: true });
  await form.getByRole('textbox', { name: 'Task (optional)', exact: true }).fill('Task moved to Maple');
  await form.getByRole('combobox', { name: 'Project', exact: true }).selectOption(String(second.id));
  await form.getByRole('button', { name: 'Launch', exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+$/);
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
  await page.goto(`/projects/${first.id}`);
  await page.getByRole('main').getByRole('button', { name: 'New session', exact: true }).click();
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
});
