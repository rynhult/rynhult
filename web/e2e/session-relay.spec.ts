import './relay-rc-recovery';
import { test, expect, type Page, type Request } from './fixtures';
import { AUTH_FILE, sessionWithChanges, csrfHeaders, openAdvanced } from './helpers';
import type { RelayReceipt } from '../src/lib/sessionRelayTypes';
import { operationDigest } from '../src/lib/sessionRelayDigest';
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { BASE } from './fixture-paths';
import { nativeRelayBridge } from './relay-native-bridge';
import { prefixAdmissionGuard } from './relay-frame-admission';
import { environmentValue } from '../../src/acc/env.mjs';
test.use({ storageState: AUTH_FILE });
const made: string[] = [];
test.afterEach(async ({ page }) => { if (!page.isClosed()) await page.unrouteAll({ behavior: 'wait' }); for (const id of made.splice(0)) await page.request.post(`/api/sessions/${id}/stop`, { headers: await csrfHeaders(page) }); });
async function proveNativeClaudeChatReceipt(page: Page, session: string, text: string, count: number,
  beforeCompletion?: (receipt: RelayReceipt) => Promise<void>) {
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  let submits = 0;
  const observe = (request: import('@playwright/test').Request) => {
    if (request.method() === 'POST' && request.url().endsWith(`/api/sessions/${session}/relay`)
        && request.postDataJSON()?.method === 'ry/turn.submit') submits++;
  };
  page.on('request', observe);
  try {
    await panel.getByRole('textbox', { name: 'Message', exact: true }).fill(text);
    const response = page.waitForResponse(value => value.url().endsWith(`/api/sessions/${session}/relay`)
      && value.request().postDataJSON()?.method === 'ry/turn.submit');
    await panel.getByRole('button', { name: 'Send', exact: true }).click();
    const original: RelayReceipt = (await (await response).json()).result;
    expect(original.method).toBe('turn.submit'); expect(original.operationId).toMatch(/^[a-f0-9]{32}$/);
    if (beforeCompletion) await beforeCompletion(original);
    let receipt: RelayReceipt = original;
    await expect.poll(async () => {
      const observed = await page.request.get(`/api/sessions/${session}/relay/operations/${original.operationId}`);
      expect(observed.ok(), await observed.text()).toBe(true);
      receipt = await observed.json(); return receipt.state;
    }, { timeout: 20_000 }).toBe('accepted');
    expect(receipt).toMatchObject({ operationId: original.operationId, method: original.method,
      sessionId: original.sessionId, runId: original.runId, payloadDigest: original.payloadDigest, actor: original.actor });
    // Native completion and browser rendering are separate observations. The old
    // aggregate5s poll could stop sampling just before an accepted snapshot arrived.
    await expect(panel.getByRole('list', { name: 'Operation receipts' }).getByText('turn.submit: accepted', { exact: true }))
      .toHaveCount(count, { timeout: 10_000 });
    expect(submits).toBe(1);
    console.log('Native Chat receipt:', JSON.stringify({ initial: original.state, final: receipt.state, sameIdentity: true, submits }));
    return receipt;
  } finally { page.off('request', observe); }
}
async function fake(page: Page, provider: 'codex' | 'claude', enabled = true, optedIn = true) {
  const id = await sessionWithChanges(page, `relay-${randomUUID().slice(0, 8)}`); made.push(id);
  await page.route('**/api/auth/state', async route => {
    const response = await route.fetch(); const data = await response.json();
    data.instance.features = { ...data.instance.features, session_relay: enabled };
    await route.fulfill({ response, json: data });
  });
  await page.route(`**/api/sessions/${id}`, async route => {
    const response = await route.fetch(); await route.fulfill({ response, json: { ...await response.json(), provider, session_relay: optedIn } });
  });
  await page.route('**/api/sessions', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch(); const rows = await response.json();
    await route.fulfill({ response, json: rows.map((s: { id: string }) => s.id === id ? { ...s, session_relay: optedIn, provider } : s) });
  });
  const snapshot = { sessionId: id, runId: 'fake-run', historyEpoch: 1, watermark: 0, controlRevision: 1, ownershipRevision: 7, configurationRevision: 1,
    capabilities: { text: 'qualified', 'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified' }, history: 'unsupported', pendingDecisions: [], receipts: [] as RelayReceipt[],
    selector: { shared_live: provider === 'codex', owner: 'terminal', may_control: true, blockers: [] as string[], switch_state: 'owned', idle_verified: true } };
  const methods: string[] = [];
  await page.route('**/api/session-relay/preference', route => route.fulfill({ json: { view: 'terminal', revision: 0 } }));
  await page.route(`**/api/sessions/${id}/relay`, async route => {
    const command = route.request().postDataJSON(); methods.push(command.method);
    let result: unknown = snapshot;
    switch (command.method) {
      case 'ry/initialize': result = { major: 1, minor: 0, features: ['queue', 'decisions', 'switch'], connectionToken: 'fake-connection' }; break;
      case 'ry/session.attach': result = { attachmentId: 'fake-attachment', snapshot }; break;
      case 'ry/operation.get': result = snapshot.receipts.find(receipt => receipt.operationId === command.params.operationId); break;
      case 'ry/events.read': result = { events: [], cursor: 'fake-cursor' }; break;
      case 'ry/operation.register': result = { operationId: randomUUID(), existing: false, receipt: { state: 'queued' } }; break;
      case 'ry/switch.prepare': result = { planId: 'plan', planDigest: 'sha256:' + 'a'.repeat(64), revision: 2 }; break;
      case 'ry/switch.commit': snapshot.selector.owner = 'chat'; snapshot.controlRevision++; result = { state: 'accepted' }; break;
    }
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result } });
  });
  return { id, snapshot, methods };
}
test('controlled admission rejects HTTP error and malformed prefix acknowledgements immediately', async () => {
  const accepted = prefixAdmissionGuard();
  expect(accepted.check({ result: { partial: false, watermark: { admitted: 1 } } }, 1)).toBe(true);
  expect(() => accepted.throwIfFailed()).not.toThrow();
  for (const reply of [{ error: { data: { code: 'OFFLINE' } } },
    { result: { partial: true, watermark: { admitted: 1 } } },
    { result: { partial: false, watermark: { admitted: 2 } } },
    { error: null, result: { partial: false, watermark: { admitted: 1 } } }, null]) {
    const guard = prefixAdmissionGuard();
    expect(guard.check(reply, 1)).toBe(false);
    await expect(guard.failed).rejects.toThrow('Native typing frame 1 was not admitted');
    expect(() => guard.throwIfFailed()).toThrow('was not admitted');
  }
});
test('Claude waits for first Chat without switching or replaying its draft', async ({ page }) => {
  const data = await fake(page, 'claude');
  data.snapshot.selector.owner = 'chat';
  const execution = { view: 'chat', phase: 'owned', mine: true, available: true, inputEpoch: 1, waitingForFirstChat: true };
  Object.assign(data.snapshot, { execution });
  await page.route(`**/api/sessions/${data.id}/relay/ownership?*`, route => route.fulfill({ json: { execution } }));
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const terminal = panel.getByRole('button', { name: 'Terminal', exact: true });
  await expect(terminal).toBeDisabled();
  await expect(panel.getByText('Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.')).toBeVisible();
  const input = panel.getByRole('textbox', { name: 'Message', exact: true });
  await expect(input).toBeEditable(); await input.fill('Unsent first Chat draft');
  execution.waitingForFirstChat = false;
  await expect(terminal).toBeEnabled({ timeout: 10_000 });
  await expect(input).toHaveValue('Unsent first Chat draft');
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(data.methods.filter(method => /operation.register|switch\.|turn.submit|terminal.input/.test(method))).toEqual([]);
});
test('crash recovery opens a blank new session and leaves old evidence readable', async ({ page }) => {
  const data = await fake(page, 'claude');
  Object.assign(data.snapshot, { l1: { phase: 'recovery_required', revision: 7, recoveryAvailable: true },
    exitOutcome: 'unknown', retainedInputs: [{ sourceRunId: 'old-native-run', view: 'terminal', text: 'Old evidence, never replay', readOnly: true }] });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('textbox', { name: 'Previous Terminal draft (read-only)' })).toHaveValue('Old evidence, never replay');
  const creates: Request[] = [];
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') creates.push(request); });
  await panel.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'New session', exact: true })).toBeVisible();
  await expect(page.locator('[data-launch-task]')).toHaveValue('');
  expect(creates).toHaveLength(0);
  expect(data.methods.filter(method => /switch.recover|operation.register|turn.submit|terminal.input/.test(method))).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(panel.getByRole('textbox', { name: 'Previous Terminal draft (read-only)' })).toHaveValue('Old evidence, never replay');
});
test('Claude retained native draft stays read-only and never populates fresh Chat input', async ({ page }) => {
  const data = await fake(page, 'claude');
  data.snapshot.selector.owner = 'chat';
  Object.assign(data.snapshot, { retainedInputs: [{ sourceRunId: 'old-native-run', view: 'terminal',
    text: 'Keep this old unsent native draft <script>neverRun()</script>', readOnly: true }] });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const retained = panel.getByRole('textbox', { name: 'Previous Terminal draft (read-only)' });
  await expect(retained).toBeVisible(); await expect(retained).not.toBeEditable();
  await expect(retained).toHaveValue('Keep this old unsent native draft <script>neverRun()</script>');
  await retained.click(); await page.keyboard.type('Never replay'); await page.keyboard.press('Enter');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('');
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  expect(data.methods.filter(method => ['ry/turn.submit', 'ry/operation.register', 'ry/terminal.input'].includes(method))).toEqual([]);
});
test('proven answer outcome stays visible with original attribution in both panes without another answer', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot, { decisionOutcomes: [{ decisionId: 'answered-native-question', runId: 'fake-run',
    actionDigest: 'sha256:' + 'a'.repeat(64), description: 'Artificial proven native decision', revision: 3,
    expiresAtMs: Date.now() + 60000, status: 'denied', winner: { kind: 'person', principal: 'alex', channel: 'chat' },
    choices: [{ id: 'cancel', label: 'Cancel turn', effect: 'deny' }] }] });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByText('Denied by alex', { exact: true })).toBeVisible();
  await expect(panel.getByRole('region', { name: 'Review decision', exact: true })).toHaveCount(0);
  await expect(panel.getByRole('region', { name: 'Decision outcome', exact: true })).toContainText('Denied by alex');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByText('Denied by alex', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Cancel turn', exact: true })).toHaveCount(0);
  expect(data.methods.some(method => ['ry/decision.reply', 'ry/operation.register', 'ry/terminal.input'].includes(method))).toBe(false);
});
test('public L1 switches only on a click and retires Chat draft read-only with fresh input', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.owner = 'chat';
  const l1 = { owner: 'chat', phase: 'owned', revision: 115, available: true };
  Object.assign(data.snapshot, { l1 });
  let target = 'terminal'; const prepares: number[] = []; const commits: string[] = []; const mutations: string[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON(); let result: unknown = data.snapshot;
    switch (command.method) {
      case 'ry/initialize': result = { major: 1, minor: 0, features: ['switch'] }; break;
      case 'ry/session.attach': result = { attachmentId: 'public-l1-tab', snapshot: data.snapshot }; break;
      case 'ry/events.read': result = { events: [], cursor: 'cursor' }; break;
      case 'ry/operation.register': result = { operationId: randomUUID(), receipt: { state: 'queued' } }; break;
      case 'ry/switch.prepare': prepares.push(command.params.expectedRevision); target = command.params.target; result = { phase: 'prepared', planId: 'opaque-plan', planDigest: 'sha256:' + 'a'.repeat(64), revision: ++l1.revision }; break;
      case 'ry/switch.commit': commits.push(command.params.planId); l1.owner = target; l1.revision++; data.snapshot.runId = randomUUID(); result = { phase: 'owned', changed: true, runId: data.snapshot.runId }; break;
      case 'ry/turn.submit': case 'ry/terminal.input': mutations.push(command.method); break;
    }
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  expect(prepares).toEqual([]);
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Keep this original Chat draft');
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  const old = panel.getByRole('textbox', { name: 'Previous Chat draft (read-only)' });
  await expect(old).toHaveValue('Keep this original Chat draft'); await expect(old).not.toBeEditable();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('');
  await expect(old).toHaveValue('Keep this original Chat draft');
  expect(prepares).toEqual([115, 117]); expect(commits).toEqual(['opaque-plan', 'opaque-plan']); expect(mutations).toEqual([]);
});
test('public L1 run change retires an observers unsent Chat draft without sending', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.owner = 'chat';
  const l1 = { owner: 'chat', phase: 'owned', revision: 20, available: true }; Object.assign(data.snapshot, { l1 });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Previous window keeps this');
  data.snapshot.runId = 'externally-replaced-run'; l1.owner = 'terminal'; l1.revision++;
  await expect(panel.getByRole('textbox', { name: 'Previous Chat draft (read-only)' })).toHaveValue('Previous window keeps this');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('');
  expect(data.methods.some(method => method.startsWith('ry/switch.') || ['ry/turn.submit', 'ry/terminal.input'].includes(method))).toBe(false);
});
for (const [enabled, optedIn] of [[false, false], [true, false]]) test(`legacy path: flag ${enabled}, opt-in ${optedIn}`, async ({ page }) => {
  const data = await fake(page, 'codex', enabled, optedIn); await page.goto(`/sessions/${data.id}`);
  await expect(page.getByRole('tab', { name: 'Terminal', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Shared session', exact: true })).toHaveCount(0);
  expect(data.methods).toEqual([]);
});
for (const provider of ['codex', 'claude'] as const) test(`${provider}: selector preserves draft and switches only on a click`, async ({ page }) => {
  const data = await fake(page, provider); await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const chat = panel.getByRole('button', { name: 'Chat', exact: true }); await expect(chat).toBeEnabled();
  expect(data.methods.some(m => m.startsWith('ry/switch.'))).toBe(false);
  await chat.click(); await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
  await panel.getByRole('textbox', { name: 'Message' }).fill('Unsent artificial draft');
  if (provider === 'codex') {
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click(); await chat.click();
    expect(data.methods.some(m => m.startsWith('ry/switch.'))).toBe(false);
  } else {
    expect(data.methods.filter(m => m === 'ry/switch.prepare')).toHaveLength(1);
    expect(data.methods.filter(m => m === 'ry/switch.commit')).toHaveLength(1);
  }
  await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveValue('Unsent artificial draft');
});
test('Claude busy refusal stays inline and never switches later', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.blockers = ['active_turn'];
  await page.goto(`/sessions/${data.id}`); const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('The agent is working');
  data.snapshot.selector.blockers = []; await page.waitForTimeout(2200);
  expect(data.methods.some(m => m.startsWith('ry/switch.'))).toBe(false);
});

test('new-session opt-in is unchecked and refuses an unqualified launch', async ({ page }) => {
  const data = await fake(page, 'claude');
  const detail = await (await page.request.get(`/api/sessions/${data.id}`)).json();
  await page.goto(`/projects/${detail.project_id}`);
  await page.getByRole('button', { name: /^New session/ }).first().click();
  const launch = page.getByRole('region', { name: 'New session', exact: true });
  const opt = launch.getByRole('checkbox', { name: /Use shared Chat and Terminal/ });
  await expect(opt).not.toBeChecked();
  let body: Record<string, unknown> = {}; let launches = 0;
  await page.route('**/api/sessions/preview', async route => {
    body = route.request().postDataJSON(); await route.continue();
  });
  await page.route('**/api/sessions', async route => {
    if (route.request().method() === 'POST') launches++;
    await route.fallback();
  });
  await opt.check();
  await expect.poll(() => body.session_relay).toBe(true);
  await expect(launch.getByRole('button', { name: 'Launch', exact: true })).toBeDisabled();
  await expect(launch).toContainText(/Session relay (?:is turned off|launch is unavailable)/);
  expect(launches).toBe(0);
  expect(body.automation).toBeUndefined();
});
test('Grid uses the shared selector without launching on open', async ({ page }) => {
  const data = await fake(page, 'codex'); await page.goto(`/grid?ids=${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
  expect(data.methods.some(m => m.startsWith('ry/switch.'))).toBe(false);
});
test('uncertain receipt prevents another Send and explains the outcome', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.receipts.push({ operationId: 'uncertain-1', sessionId: data.id, runId: 'fake-run', method: 'turn.submit', payloadDigest: 'sha256:' + 'a'.repeat(64), actor: { kind: 'person', principal: 'Example person', channel: 'chat' }, state: 'uncertain', revision: 1 });
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    if (route.request().postDataJSON().method !== 'ry/operation.get') return route.fallback();
    await route.fulfill({ json: { result: data.snapshot.receipts[0] } });
  });
  await page.goto(`/sessions/${data.id}`); const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toBeEnabled(); await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByText('Delivery unknown.', { exact: false })).toBeVisible();
  await panel.getByRole('textbox', { name: 'Message' }).fill('Keep this draft');
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
});

test('flag rollback preserves enrolled history without legacy input fallback', async ({ page }) => {
  const data = await fake(page, 'codex', false, true); await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(page.locator('.xterm')).toHaveCount(0);
  expect(data.methods.some(m => m === 'ry/operation.register')).toBe(false);
});


test('real hub: unqualified retained reads attach observers without native input or a provider launch', async ({ page }) => {
  const id = await sessionWithChanges(page, `hub-${randomUUID().slice(0, 8)}`, 'codex'); made.push(id);
  const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  await expect.poll(async () => (await page.request.post(`/e2e-test/session-relay/${id}`, {
    headers: { 'x-e2e-isolation': fixture.isolation_token },
  })).status()).toBe(200);
  // No hub, preference, enrollment or auth-state browser mocks in this case.
  await page.goto(`/sessions/${id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel).toBeVisible();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Artificial retained hub message.');
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(panel.getByText(/shared Terminal connection is unavailable/)).toBeVisible();
  const reference = await page.request.get(`/api/sessions/${id}/terminal-reference`);
  expect(reference.status()).toBe(409);
});

test('exclusive Codex shows the holder and keeps the other view read-only without automatic take-over', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot, { writer: { view: 'terminal', holder: { kind: 'person', principal: 'Example colleague', channel: 'chat' },
    revision: 1, active: true, available: false, mine: false } });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByLabel('Input holder')).toContainText('Example colleague holds input in Terminal');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveAttribute('readonly', '');
  await expect(panel.getByRole('button', { name: /^Send/ })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Take input in Chat', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveValue('');
  expect(data.methods.some(method => method === 'ry/control.take' || method.startsWith('ry/switch.'))).toBe(false);
});

test('exclusive Codex Chat send binds its displayed control revision', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.controlRevision = 7;
  Object.assign(data.snapshot, { writer: { view: 'chat', holder: { kind: 'person', principal: 'Example person', channel: 'chat' },
    revision: 7, active: true, available: false, mine: true } });
  let revision: unknown; let ifBusy: unknown;
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/turn.submit') return route.fallback();
    revision = command.params.expectedControlRevision; ifBusy = command.params.ifBusy;
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: { state: 'accepted' } } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('textbox', { name: 'Message' }).fill('Artificial writer draft');
  await panel.getByRole('button', { name: /^Send/ }).click();
  await expect.poll(() => revision).toBe(7);
  expect(ifBusy).toBe('reject');
});

test('public writer take-over requires a person click and reason and uses the current attachment', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.controlRevision = 7;
  Object.assign(data.snapshot, { terminalMediated: false, writer: { view: 'terminal', holder: { kind: 'person', principal: 'Example colleague', channel: 'chat' },
    revision: 7, active: true, available: true, mine: false } });
  const takes: Record<string, unknown>[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/writer.take') return route.fallback();
    takes.push(command.params);
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id,
      result: { state: 'accepted', method: 'writer.take', operationId: command.params.operationId } } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByLabel('Input holder')).toContainText('Example colleague');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const take = panel.getByRole('button', { name: 'Take input in Chat', exact: true });
  await expect(take).toHaveAttribute('title', /previous draft stays visible and read-only.*new input starts empty/);
  await expect(take).toBeDisabled();
  expect(takes).toEqual([]);
  await panel.getByRole('textbox', { name: 'Reason for taking input' }).fill('Example person continues the task');
  await expect(take).toBeEnabled();
  expect(takes).toEqual([]);
  await take.click();
  await expect.poll(() => takes.length).toBe(1);
  expect(takes[0]).toMatchObject({ view: 'chat', expectedRevision: 7, attachmentId: 'fake-attachment', reason: 'Example person continues the task' });
  await expect(panel.getByRole('textbox', { name: 'Reason for taking input' })).toHaveValue('');
  expect(data.methods.some(method => method === 'ry/control.take' || method === 'ry/terminal.input')).toBe(false);
});

test('shared Terminal reads stay live with mediation false and never claim legacy input', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot, { terminalMediated: false, writer: { view: 'chat', holder: { kind: 'person', principal: 'Example colleague', channel: 'chat' },
    revision: 1, active: true, available: false, mine: false } });
  let reads = 0; let revoked = false;
  const methods: string[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    methods.push(route.request().postDataJSON().method); await route.fallback();
  });
  await page.route(`**/api/sessions/${data.id}/relay/terminal?**`, async route => {
    const query = new URL(route.request().url()).searchParams;
    expect(query.get('attachmentId')).toBe('fake-attachment');
    expect(query.get('expectedRunId')).toBe('fake-run');
    reads++;
    await route.fulfill(revoked ? { status: 403, json: { detail: 'Read revoked' } }
      : { json: { runId: 'fake-run', truncated: false,
        text: `Artificial native screen ${reads}\n<script>window.__relayInjected = true</script>` } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const screen = panel.getByRole('log', { name: 'Native Terminal screen', exact: true });
  await expect(screen).toContainText('Artificial native screen');
  await expect.poll(() => reads).toBeGreaterThan(1);
  await expect(screen).toContainText('<script>window.__relayInjected = true</script>');
  expect(await page.evaluate(() => '__relayInjected' in window)).toBe(false);
  await expect(panel).toContainText('Read-only Terminal.');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(screen).toContainText('Artificial native screen');
  expect(methods.some(method => method === 'ry/control.claim' || method === 'ry/terminal.input'
    || method === 'ry/writer.take' || method.startsWith('ry/switch.'))).toBe(false);
  await expect(page.locator('.xterm')).toHaveCount(0);
  revoked = true;
  await expect(screen).toBeEmpty();
  await expect(panel).toContainText('The shared Terminal connection is unavailable.');
});


test('idle take-over preserves the old Chat draft read-only and starts fresh input without replay', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.controlRevision = 7;
  Object.assign(data.snapshot, { writer: { view: 'chat', holder: { kind: 'person', principal: 'Example person', channel: 'chat' },
    revision: 7, active: true, available: true, mine: true } });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Keep this exact unsent draft');
  data.snapshot.controlRevision = 8;
  Object.assign(data.snapshot.writer!, { view: 'terminal', revision: 8, mine: false });
  await expect(panel.getByRole('textbox', { name: 'Previous draft', exact: true })).toHaveValue('Keep this exact unsent draft');
  await expect(panel.getByRole('textbox', { name: 'Previous draft', exact: true })).toHaveAttribute('readonly', '');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveAttribute('readonly', '');
  data.snapshot.controlRevision = 9;
  Object.assign(data.snapshot.writer!, { view: 'chat', revision: 9, mine: true });
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toHaveAttribute('readonly', '');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('');
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Fresh input');
  await expect(panel.getByRole('textbox', { name: 'Previous draft', exact: true })).toHaveValue('Keep this exact unsent draft');
  expect(data.methods.some(method => method === 'ry/turn.submit' || method === 'ry/terminal.input' || method === 'ry/writer.take')).toBe(false);
});

test('a delayed old Send response cannot erase identical text in a fresh writer composer', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.controlRevision = 7;
  const writer = { view: 'chat', holder: { kind: 'person', principal: 'Example person', channel: 'chat' },
    revision: 7, active: true, available: true, mine: true };
  Object.assign(data.snapshot, { writer });
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let sent = false;
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/turn.submit') return route.fallback();
    const receipt: RelayReceipt = { operationId: command.params.operationId, sessionId: data.id, runId: 'fake-run',
      method: 'turn.submit', payloadDigest: 'sha256:' + 'a'.repeat(64), actor: { kind: 'person', principal: 'Example person', channel: 'chat' },
      state: 'accepted', revision: 1 };
    // Provider/main have settled; only this HTTP response is delayed. Polling
    // can observe the durable receipt and a legitimate later idle take-over.
    data.snapshot.receipts.push(receipt); sent = true;
    await held;
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: receipt } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const message = panel.getByRole('textbox', { name: 'Message', exact: true });
  await message.fill('Identical text in a different client');
  await panel.getByRole('button', { name: /^Send/ }).click();
  await expect.poll(() => sent).toBe(true);
  data.snapshot.controlRevision = 8;
  Object.assign(writer, { revision: 8, holder: { kind: 'person', principal: 'Fresh lease', channel: 'chat' } });
  await expect(panel.getByLabel('Input holder')).toContainText('Fresh lease');
  await expect(message).toHaveValue('');
  await message.fill('Identical text in a different client');
  release();
  await expect(panel.getByRole('button', { name: /^Send/ })).toBeEnabled();
  await expect(message).toHaveValue('Identical text in a different client');
});

for (const enabled of [true, false]) test(`Terminal holder can review native permission with preview ${enabled}`, async ({ page }) => {
  const data = await fake(page, 'codex', enabled);
  Object.assign(data.snapshot.capabilities, { 'decision.tool_approval': 'qualified' });
  Object.assign(data.snapshot, { writer: { view: 'terminal', holder: { kind: 'person', principal: 'Example person', channel: 'chat' },
    revision: 1, active: true, available: true, mine: true }, pendingDecisions: [{ decisionId: 'native-permission', runId: 'fake-run',
      actionDigest: 'sha256:' + 'b'.repeat(64), description: 'Fixture command requires a person decision.', revision: 0,
      expiresAtMs: Date.now() + 60_000, status: 'pending', choices: [
        { id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'cancel', label: 'Cancel turn', effect: 'deny' }] }] });
  const answers: Record<string, unknown>[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/decision.reply') return route.fallback();
    answers.push(command.params);
    Object.assign(data.snapshot, { pendingDecisions: [{ decisionId: 'native-permission', runId: 'fake-run',
      actionDigest: 'sha256:' + 'b'.repeat(64), description: 'Fixture command requires a person decision.', revision: 2,
      expiresAtMs: Date.now() + 60_000, status: 'outcome_unknown',
      winner: { kind: 'person', principal: 'Example person', channel: 'chat' }, choices: [] }] });
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: { state: 'uncertain', method: 'decision.reply', operationId: command.params.operationId } } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const review = panel.getByRole('region', { name: 'Review decision', exact: true });
  await expect(review).toBeVisible();
  const allow = review.getByRole('button', { name: 'Allow once', exact: true });
  if (enabled) await expect(allow).toBeEnabled(); else await expect(allow).toBeDisabled();
  await review.getByRole('button', { name: 'Cancel turn', exact: true }).click();
  await expect.poll(() => answers.length).toBe(1);
  expect(answers[0]).toMatchObject({ decisionId: 'native-permission', choiceId: 'cancel', expectedRevision: 0, actionDigest: 'sha256:' + 'b'.repeat(64) });
  await expect(review).toContainText('outcome is unknown');
  expect(data.methods.some(method => method === 'ry/writer.take' || method === 'ry/control.claim' || method === 'ry/terminal.input' || method.startsWith('ry/switch.'))).toBe(false);
});

test('unnegotiated native decisions remain read-only despite a fabricated capability', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot.capabilities, { 'decision.tool_approval': 'qualified' });
  Object.assign(data.snapshot, { pendingDecisions: [{ decisionId: 'unavailable-native-permission', runId: 'fake-run',
    actionDigest: 'sha256:' + 'c'.repeat(64), description: 'Artificial unqualified native command.', revision: 0,
    expiresAtMs: Date.now() + 60_000, status: 'pending', choices: [
      { id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'cancel', label: 'Cancel turn', effect: 'deny' }] }] });
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/initialize') return route.fallback();
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: { major: 1, minor: 0, features: [], connectionToken: 'fake-connection' } } });
  });
  await page.goto(`/sessions/${data.id}`);
  const review = page.getByRole('region', { name: 'Review decision', exact: true });
  await expect(review).toBeVisible();
  await expect(review.getByRole('button', { name: 'Allow once', exact: true })).toBeDisabled();
  await expect(review.getByRole('button', { name: 'Cancel turn', exact: true })).toBeDisabled();
  expect(data.methods.some(method => method === 'ry/decision.reply')).toBe(false);
});

// Artificial HTTP contract proofs: these do not qualify the real native/browser join.
async function terminalInputFixture(page: Page, mismatch?: 'actor' | 'epoch', provider: 'codex' | 'claude' = 'codex') {
  const data = await fake(page, provider);
  const holder = { kind: 'person', principal: 'Example person', channel: 'chat' };
  Object.assign(data.snapshot.capabilities, { 'terminal.input': 'qualified' });
  const execution = { view: 'terminal', phase: 'owned', holder, available: true, mine: true, inputEpoch: 2 };
  Object.assign(data.snapshot, { terminalMediated: false, ...(provider === 'claude' ? { execution } :
    { writer: { view: 'terminal', holder, revision: 1, active: true, available: true, mine: true } }) });
  await page.route(`**/api/sessions/${data.id}/relay/ownership?**`, route => route.fulfill({ json: {
    execution, selector: data.snapshot.selector, ownershipRevision: data.snapshot.ownershipRevision } }));
  const writes: Record<string, unknown>[] = []; let claims = 0; let processed = 0;
  let acceptedClaim: RelayReceipt | undefined;
  let block: Promise<void> | undefined; let failWrite = false; let failScreen = false;
  let nextRead: Promise<void> | undefined; let readHeld = false;
  let claimWait: Promise<void> | undefined;
  await page.route(`**/api/sessions/${data.id}/relay/terminal?**`, async route => {
    const held = nextRead; nextRead = undefined;
    if (held) { readHeld = true; await held; return route.fulfill({ status: 409, json: { detail: 'Artificial old read failure' } }); }
    return failScreen ? route.fulfill({ status: 409, json: { detail: 'Artificial unavailable screen' } }) : route.fulfill({ json: { runId: 'fake-run', text: 'Artificial native screen', truncated: false } });
  });
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON(); let result: unknown;
    switch (command.method) {
      case 'ry/initialize': result = { major: 1, minor: 0, features: ['queue', 'decisions', 'switch', 'terminal', 'writer'], connectionToken: 'fake-connection' }; break;
      case 'ry/operation.get': {
        const receipt = data.snapshot.receipts.find(item => item.operationId === command.params.operationId);
        if (!receipt) return route.fallback();
        result = receipt; break;
      }
      case 'ry/control.claim': {
        claims++; const epoch = provider === 'claude' ? execution.inputEpoch : data.snapshot.controlRevision;
        expect(command.params.expectedRevision).toBe(data.snapshot.controlRevision);
        if (provider === 'claude') data.snapshot.controlRevision = epoch;
        if (claimWait) await claimWait;
        const { operationId, ...claimPayload } = command.params;
        acceptedClaim = { state: 'accepted', method: 'control.claim', sessionId: data.id, runId: 'fake-run', actor: holder,
          operationId, revision: 2, payloadDigest: await operationDigest('control.claim', claimPayload) };
        result = { receipt: acceptedClaim,
          controlRevision: epoch, holder, inputGrant: 'a'.repeat(64), inputEpoch: mismatch === 'epoch' ? epoch + 1 : epoch };
        if (mismatch === 'actor') (result as { receipt: { actor: { principal: string } } }).receipt.actor = { ...holder, principal: 'Wrong person' };
        break;
      }
      case 'ry/terminal.input': {
        writes.push(command.params);
        if (failWrite) return route.fulfill({ json: { error: { message: 'Artificial lost write answer' } } });
        const epoch = command.params.inputEpoch; const sequence = command.params.frameSeq;
        if (block) await block;
        result = { partial: false, watermark: { epoch, admitted: sequence, processed } }; break;
      }
      case 'ry/terminal.drain': result = { epoch: data.snapshot.controlRevision, admitted: writes.length, processed }; break;
      default: return route.fallback();
    }
    await route.fulfill({ json: { result } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const connect = panel.getByRole('button', { name: 'Connect Terminal input', exact: true });
  await expect(connect).toBeEnabled();
  return { ...data, panel, connect, writes, execution, acceptedClaim: () => acceptedClaim, claims: () => claims, process: (value: number) => { processed = value; },
    holdClaim: (value: Promise<void>) => { claimWait = value; },
    hold: (value: Promise<void> | undefined) => { block = value; }, fail: () => { failWrite = true; }, screenFailure: (value: boolean) => { failScreen = value; }, holdRead: (value: Promise<void>) => { nextRead = value; }, readHeld: () => readHeld };
}
test('Terminal input requires a click and causal processing before another submission', async ({ page, _drainRoutes }) => {
  const data = await terminalInputFixture(page);
  expect(data.claims()).toBe(0); expect(data.writes).toEqual([]);
  await data.connect.click(); const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeVisible(); await typing.press('a'); await typing.press('Enter');
  await expect.poll(() => data.writes.length).toBe(2);
  expect(data.writes.map(frame => atob(frame.data as string))).toEqual(['a', '\r']);
  await expect(typing).toHaveValue('a'); await expect(typing).toBeDisabled();
  await typing.dispatchEvent('keydown', { key: 'b' }); expect(data.writes.length).toBe(2);
  data.process(2); await expect(typing).toHaveValue(''); await expect(data.panel.getByText('Keyboard connected.', { exact: true })).toBeVisible();
  await expect(typing).toBeFocused(); await page.keyboard.type('b'); await expect.poll(() => data.writes.length).toBe(3);
  expect(data.claims()).toBe(1); expect(data.writes[2].frameSeq).toBe(3);
});
test('Terminal take-over permanently retains buffered old draft and starts a fresh generation', async ({ page }) => {
  const data = await terminalInputFixture(page); let release!: () => void;
  data.hold(new Promise<void>(resolve => { release = resolve; }));
  await data.connect.click(); const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  await page.keyboard.type('a'); await expect.poll(() => data.writes.length).toBe(1);
  await page.keyboard.type('bc');
  await expect(typing).toHaveValue('abc');
  // A separately admitted holder transition, delivered through the ordinary snapshot read.
  data.snapshot.controlRevision = 2;
  await expect(data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true })).toHaveValue('abc');
  await expect(data.connect).toBeEnabled(); data.hold(undefined); release();
  await data.connect.click(); await expect(typing).toHaveValue('');
  await typing.press('z'); await expect.poll(() => data.writes.length).toBe(2);
  expect(data.writes.map(frame => atob(frame.data as string))).toEqual(['a', 'z']);
  expect(data.writes.map(frame => [frame.inputEpoch, frame.frameSeq])).toEqual([[1, 1], [2, 1]]);
  const old = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
  await expect(old).toHaveAttribute('readonly', ''); await old.press('x'); await expect(old).toHaveValue('abc');
  expect(data.claims()).toBe(2);
});
test('Terminal unsupported native controls and paste never reach the input transport', async ({ page }) => {
  const data = await terminalInputFixture(page); await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true }); await expect(typing).toBeVisible();
  for (const key of ['/', '!', '?', 'Escape', 'ArrowUp']) await typing.press(key);
  await typing.dispatchEvent('paste');
  await expect(data.panel.getByText('Paste is unavailable in this input profile. Use Chat for this text.', { exact: true })).toBeVisible();
  expect(data.writes).toEqual([]); await expect(typing).toHaveValue('');
});

test('Terminal uncertain delivery retains the draft without reconnect or frame retry', async ({ page }) => {
  const data = await terminalInputFixture(page); await data.connect.click(); data.fail();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeVisible(); await typing.press('a');
  const old = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
  await expect(old).toHaveValue('a'); await expect(data.panel.getByText('Input delivery is unknown. Review the native session; nothing will be replayed.', { exact: true })).toBeVisible();
  await old.press('b'); await page.waitForTimeout(1200);
  expect(data.writes.length).toBe(1); expect(data.claims()).toBe(1); await expect(data.connect).toHaveCount(0);
});
test('Terminal screen loss permanently retires input even when the same read recovers', async ({ page }) => {
  const data = await terminalInputFixture(page); await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeVisible(); await typing.press('a'); await expect.poll(() => data.writes.length).toBe(1);
  data.screenFailure(true); const old = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
  await expect(old).toHaveValue('a'); data.screenFailure(false);
  await expect(data.panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Artificial native screen');
  await expect(data.connect).toHaveCount(0); await old.press('b'); expect(data.writes.length).toBe(1); expect(data.claims()).toBe(1);
});

for (const mismatch of ['actor', 'epoch'] as const) test(`Terminal rejects a mismatched ${mismatch} in an accepted claim without using its grant`, async ({ page }) => {
  const data = await terminalInputFixture(page, mismatch); await data.connect.click();
  await expect(data.panel.getByText('Input connection is unresolved. Take input again when safe; nothing will be replayed.', { exact: true })).toBeVisible();
  const old = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
  await expect(old).toHaveValue(''); await old.press('a');
  await expect(data.connect).toHaveCount(0); expect(data.claims()).toBe(1); expect(data.writes).toEqual([]);
});

for (const scenario of ['continuous', 'reconnect', 'second-person', 'compact', 'repeated', 'grid', 'normal-end', 'abnormal-end', 'unknown-end', 'normal-read-end', 'normal-process-end', 'terminal-interrupt', 'terminal-interrupt-slow-reader', 'terminal-interrupt-lost', 'terminal-interrupt-detach', 'terminal-interrupt-logout', 'terminal-interrupt-cookie-fence', 'terminal-interrupt-stop-fence', 'terminal-interrupt-process-lost']) test(`offline native: person new-session opt-in joins browser Chat and Terminal on one native thread (${scenario})`, { tag: '@relay-native' }, async ({ page, otherOwner, playwright }) => {
  const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  test.skip(!fixture.relay_native_enabled, 'Requires the explicitly enabled mandatory offline native fixture');
  test.setTimeout(150_000);
  const keeperRequests = page.request;
  if (scenario === 'terminal-interrupt-cookie-fence') page = await otherOwner.newPage();
  let nativeWriteHeld = false;
  const headers = { ...await csrfHeaders(page), 'x-e2e-isolation': fixture.isolation_token };
  let session: string | undefined;
  let secondPage: Page | undefined;
  let stopTiming: (() => void) | undefined;
  const expectedRequests = scenario.startsWith('terminal-interrupt') ? 2 : scenario.endsWith('-end') ? 1 : ['second-person', 'repeated'].includes(scenario) ? 4 : 3;
  const chatResponse = `Production fixture response ${scenario === 'repeated' ? 4 : 3}.`;
  if (scenario === 'compact') await page.setViewportSize({ width: 390, height: 844 });
  try {
    const setup = await page.request.post('/e2e-test/relay-native/prepare', { headers });
    expect(setup.ok(), await setup.text()).toBe(true);
    const { projectId } = await setup.json();
    // Real auth state, preview, public launch, enrollment and relay APIs: no routes mocked.
    await page.goto(`/projects/${projectId}`);
    await page.getByRole('button', { name: /^New session/ }).first().click();
    const launch = page.getByRole('region', { name: 'New session', exact: true });
    // The provider choice is outside Advanced; default launch options remain real.
    await launch.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('codex');
    await openAdvanced(launch);
    await launch.getByRole('checkbox', { name: /^Own worktree/ }).uncheck();
    const opt = launch.getByRole('checkbox', { name: /Use shared Chat and Terminal/ });
    await expect(opt).not.toBeChecked();
    const previewed = page.waitForResponse(response => response.url().endsWith('/api/sessions/preview') && response.request().postDataJSON()?.session_relay === true);
    await opt.check();
    const preview = await previewed; expect(preview.ok(), await preview.text()).toBe(true);
    await expect(launch.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
    const launched = page.waitForResponse(response => response.url().endsWith('/api/sessions') && response.request().method() === 'POST');
    await launch.getByRole('button', { name: 'Launch', exact: true }).click();
    const response = await launched;
    expect(response.ok(), await response.text()).toBe(true);
    const created = await response.json(); session = created.id;
    expect(session).toBeTruthy();
    const qualified = await page.request.post(`/e2e-test/relay-native/qualify/${session}`, { headers });
    expect(qualified.ok(), await qualified.text()).toBe(true);
    const initial = await qualified.json();
    expect(initial.views.every((view: { fresh: boolean }) => view.fresh)).toBe(true);
    const panel = page.getByRole('region', { name: 'Shared session', exact: true });
    await expect(panel).toBeVisible();
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    const message = panel.getByRole('textbox', { name: 'Message', exact: true });
    await expect(message).toHaveAttribute('readonly', '');
    await panel.getByRole('textbox', { name: 'Reason for taking input' }).fill('Person chooses initial Chat input');
    const takeStarted = performance.now();
    const taken = page.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
      && response.request().postDataJSON()?.method === 'ry/writer.take' && response.request().postDataJSON()?.params.view === 'chat');
    const [, takeResponse] = await Promise.all([panel.getByRole('button', { name: 'Take input in Chat', exact: true }).click(), taken]);
    const takeReply = await takeResponse.json();
    expect(takeReply.error, JSON.stringify(takeReply)).toBeUndefined(); expect(takeReply.result.state).toBe('accepted');
    test.info().annotations.push({ type: 'relay_initial_take_timeline', description: JSON.stringify({ admissionMs: performance.now() - takeStarted }) });
    await expect(message).toBeEditable(); await message.fill('First production Chat');
    const responseStarted = performance.now();
    const submitted = page.waitForResponse(response => response.url().endsWith('/relay') && response.request().method() === 'POST' && response.request().postDataJSON()?.method === 'ry/turn.submit');
    await panel.getByRole('button', { name: 'Send', exact: true }).click();
    const sent = await submitted; const answer = await sent.json(); expect(answer.error, JSON.stringify(answer)).toBeUndefined();
    const admissionMs = performance.now() - responseStarted;
    await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 1.', { timeout: 20_000 });
    test.info().annotations.push({ type: 'relay_latency', description: JSON.stringify({ admissionMs, responseMs: performance.now() - responseStarted }) });
    if (scenario.endsWith('-end')) {
      const kind = ['normal-read-end', 'normal-process-end'].includes(scenario) ? 'normal' : scenario.slice(0, -4);
      await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
      const screen = panel.getByRole('log', { name: 'Native Terminal screen' });
      await expect(screen).toContainText('First production Chat');
      // Use a fresh control connection: this single fault must not depend on
      // the browser API pool's idle keepalive lifetime or retry a mutation.
      const control = await playwright.request.newContext({ baseURL: new URL(page.url()).origin,
        storageState: await page.context().storageState(), extraHTTPHeaders: headers });
      try {
        const ended = await control.post(`/e2e-test/relay-native/end/${kind}`);
        expect(ended.ok(), await ended.text()).toBe(true);
      } finally { await control.dispose(); }
      await expect.poll(async () => {
        const response = await page.request.get(`/api/sessions/${session}/relay/events`);
        const events = (await response.json()).events;
        return events.filter((event: { payload: Record<string, unknown> }) => event.payload.exitOutcome).at(-1)?.payload.exitOutcome;
      }).toBe(kind);
      await expect(panel.getByText(kind === 'normal' ? 'Session ended normally.' : kind === 'abnormal' ? 'Session ended unexpectedly. Explicit recovery is required.' : 'Execution connection ended; native exit outcome is unknown.', { exact: true })).toBeVisible();
      await expect(panel.getByRole('button', { name: 'Take input in Terminal', exact: true })).toBeDisabled();
      if (kind === 'unknown') {
        await expect(screen).toHaveText('');
        await expect(panel.getByText('The shared Terminal connection is unavailable.', { exact: true })).toBeVisible();
      } else await expect(screen).toContainText('First production Chat');
      await page.reload();
      await expect(panel).toBeVisible();
      await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
      if (kind === 'unknown') await expect(screen).toHaveText('');
      else await expect(screen).toContainText('First production Chat');
      await expect(panel.getByRole('textbox', { name: 'Terminal typing', exact: true })).toHaveCount(0);
      await panel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 1.');
      await expect(message).toHaveAttribute('readonly', '');
      const endedRun = (await (await page.request.get(`/api/sessions/${session}/relay`)).json()).runId;
      const choice = panel.getByRole('combobox', { name: 'Retained run', exact: true });
      await expect(choice).toBeVisible(); await choice.selectOption(endedRun);
      const past = panel.getByRole('region', { name: 'Past run history', exact: true });
      await expect(past.getByRole('log', { name: 'Past run conversation' })).toContainText('Production fixture response 1.');
      if (kind !== 'unknown') await expect(past.getByRole('log', { name: 'Native Terminal screen' })).toContainText('First production Chat');
      await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveCount(0);
      if (scenario === 'normal-read-end') {
        const readRequest = await page.waitForRequest(request => request.url().includes(`/api/sessions/${session}/relay/terminal?`));
        const readHeaders = await readRequest.allHeaders();
        const proofHeaders = { 'X-ACC-Relay-Connection': readHeaders['x-acc-relay-connection'], 'x-e2e-retained-proof': fixture.isolation_token };
        for (const action of ['redact-release', 'epoch-release']) {
          const armed = await page.request.post('/e2e-test/relay-native/retained/hold', { headers });
          expect(armed.ok(), await armed.text()).toBe(true);
          const pending = page.request.get(readRequest.url(), { headers: proofHeaders });
          await expect.poll(async () => (await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json()).held).toBe(true);
          const released = await page.request.post(`/e2e-test/relay-native/retained/${action}`, { headers });
          expect(released.ok(), await released.text()).toBe(true);
          const returned = await pending;
          const body = await returned.text();
          expect(body).not.toContain('First production Chat');
          if (action === 'redact-release') { expect(returned.status()).toBe(200); expect(body).toContain('[REDACTED]'); }
          else expect([404, 409]).toContain(returned.status());
        }
        const readState = await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json();
        expect(readState.nativeActive).toBe(0); expect(readState.nativeRequests).toBe(1);
        test.info().annotations.push({ type: 'retained_read_races', description: 'actual-native-final-screen:redacted-after-await,epoch-refused-after-await' });
      }
      if (scenario === 'normal-process-end') {
        const source = await page.request.get('/e2e-test/relay-native/cold-observer', { headers });
        expect(source.ok(), await source.text()).toBe(true);
        const oldRead = await page.waitForRequest(request => request.url().includes(`/api/sessions/${session}/relay/terminal?`));
        const oldHeaders = await oldRead.allHeaders();
        const oldUrl = new URL(oldRead.url());
        writeFileSync(`${BASE}/retained-process.json`, JSON.stringify({ session, run: endedRun, source: await source.json(),
          oldRead: { path: oldUrl.pathname + oldUrl.search, connection: oldHeaders['x-acc-relay-connection'] } }));
      }
      await choice.selectOption(''); await expect(message).toHaveAttribute('readonly', '');
      return;
    }
    if (['reconnect', 'grid'].includes(scenario)) {
      // A new document negotiates a new observer attachment; reads must neither
      // restart the provider nor reclaim input or replay the accepted Chat turn.
      if (scenario === 'grid') await page.goto(`/grid?ids=${session}`);
      else await page.reload();
      await expect(panel).toBeVisible();
      await panel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 1.');
      await expect(message).toHaveAttribute('readonly', '');
      const rejoined = await page.request.post(`/e2e-test/relay-native/qualify/${session}`, { headers });
      expect(rejoined.ok(), await rejoined.text()).toBe(true);
      const joined = await rejoined.json();
      expect(joined.nativeId).toBe(initial.nativeId); expect(joined.owner).toBe(initial.owner);
      const state = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
      expect(state.native.requests).toBe(1);
    }
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    await panel.getByRole('textbox', { name: 'Reason for taking input' }).fill('Person chooses native Terminal');
    await panel.getByRole('button', { name: 'Take input in Terminal', exact: true }).click();
    await panel.getByRole('button', { name: 'Connect Terminal input', exact: true }).click();
    const typing = panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
    await expect(typing).toBeVisible(); await expect(typing).toBeEnabled();
    await expect(typing).toBeFocused();
    if (scenario.startsWith('terminal-interrupt')) {
      const prepared = await page.request.post(`/e2e-test/relay-native/interrupt/${scenario === 'terminal-interrupt-process-lost' ? 'prepare-native-lost' : scenario.endsWith('-lost') ? 'prepare-lost' : 'prepare'}`, { headers });
      expect(prepared.ok(), await prepared.text()).toBe(true);
    }
    if (scenario === 'terminal-interrupt-slow-reader') {
      const slow = await page.request.post(`/e2e-test/relay-native/interrupt/slow-reader`, { headers });
      expect(slow.ok(), await slow.text()).toBe(true);
      const observed = await slow.json();
      test.info().annotations.push({ type: 'native_reader_stall', description: JSON.stringify(observed) });
      expect(observed.cycles).toHaveLength(20);
      expect(observed.cycles.every((cycle: { socket_open: boolean; heartbeat_timeout: boolean; native_alive: boolean }) => cycle.socket_open && !cycle.heartbeat_timeout && cycle.native_alive)).toBe(true);
      expect(observed.same_peer && observed.same_process && observed.socket_open).toBe(true);
    }
    await expect(typing).toBeFocused();
    const controlled = scenario === 'terminal-interrupt-detach' || scenario === 'terminal-interrupt-logout';
    const networkDetail: { sequence: number; characters: number; requestStart: number; responseStart: number; responseEnd: number }[] = [];
    const prefixAcks: { characters: number; endEpoch: number; networkMs: number }[] = [];
    let prefixCompleted = 0, prefixFrames = 0, lastPrefixNetworkEnd = 0, enterSequence = 0, enterRequestedAt = 0;
    function frame(request: Request) {
      if (request.method() !== 'POST' || !request.url().endsWith(`/api/sessions/${session}/relay`)) return undefined;
      const command = request.postDataJSON();
      return command?.method === 'ry/terminal.input' ? command.params : undefined;
    }
    const prefixGuard = prefixAdmissionGuard();
    const prefixChecks: Promise<void>[] = [];
    function finished(request: Request) {
      const params = frame(request);
      if (params && params.data !== 'DQ==') {
        prefixChecks.push((async () => {
          try {
            const response = await request.response();
            if (!prefixGuard.check(await response?.json(), params.frameSeq)) return;
            prefixCompleted += atob(params.data).length; prefixFrames++;
            const timing = request.timing();
            lastPrefixNetworkEnd = timing.startTime + timing.responseEnd;
            prefixAcks.push({ characters: atob(params.data).length, endEpoch: lastPrefixNetworkEnd, networkMs: timing.responseEnd });
            networkDetail.push({ sequence: params.frameSeq, characters: atob(params.data).length,
              requestStart: timing.requestStart, responseStart: timing.responseStart, responseEnd: timing.responseEnd });
          } catch { prefixGuard.check(undefined, params.frameSeq); }
        })());
      }
    }
    function requested(request: Request) {
      const params = frame(request);
      if (params?.data === 'DQ==') {
        enterSequence = params.frameSeq; enterRequestedAt = performance.now();
      }
    }
    if (controlled) {
      await page.evaluate(() => {
        const target = window as Window & { relayTypingEpochs?: number[] }; target.relayTypingEpochs = [];
        document.addEventListener('keydown', event => {
          if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey)
            target.relayTypingEpochs!.push(performance.timeOrigin + performance.now());
        }, true);
      });
      page.on('requestfinished', finished); page.on('request', requested);
      stopTiming = () => { page.off('requestfinished', finished); page.off('request', requested); };
    }
    await page.keyboard.type('Production Terminal input');
    await expect(typing).toHaveValue('Production Terminal input');
    if (controlled) {
      prefixGuard.throwIfFailed();
      const pendingPrefixAtEnter = 'Production Terminal input'.length - prefixCompleted;
      const enterStarted = performance.now();
      const admitted = page.waitForResponse(response => frame(response.request())?.data === 'DQ==').then(async response => {
        await response.finished();
        const reply = await response.json();
        expect(reply.error, JSON.stringify(reply)).toBeUndefined(); expect(reply.result.partial).toBe(false);
        expect(reply.result.watermark.admitted).toBe(enterSequence);
        return { at: performance.now(), networkMs: response.request().timing().responseEnd, startEpoch: response.request().timing().startTime };
      });
      const processed = page.waitForResponse(async response => {
        if (!enterSequence || !response.url().endsWith(`/api/sessions/${session}/relay`)
          || response.request().postDataJSON()?.method !== 'ry/terminal.drain') return false;
        const value = await response.json();
        return value.result?.processed >= enterSequence;
      }).then(() => performance.now());
      const [, ack, consumed] = await Promise.race([Promise.all([page.keyboard.press('Enter'), admitted, processed]), prefixGuard.failed]);
      await Promise.all(prefixChecks);
      expect(prefixCompleted).toBe('Production Terminal input'.length);
      const keyEpochs = await page.evaluate(() => (window as Window & { relayTypingEpochs?: number[] }).relayTypingEpochs!);
      expect(keyEpochs).toHaveLength(prefixCompleted);
      let key = 0;
      const keyToAdmissionMs = prefixAcks.flatMap(frame => Array.from({ length: frame.characters }, () => frame.endEpoch - keyEpochs[key++]));
      expect(keyToAdmissionMs.every(value => value >= 0)).toBe(true);
      test.info().annotations.push({ type: 'relay_typing_timeline', description: JSON.stringify({
        characters: prefixCompleted, frames: prefixFrames, keyToAdmissionMs,
        frameHttpMs: prefixAcks.map(frame => frame.networkMs), networkDetail,
        burstToAdmissionMs: lastPrefixNetworkEnd - keyEpochs[0],
      }) });
      expect(enterSequence).toBe(prefixFrames + 1);
      test.info().annotations.push({ type: 'relay_terminal_timeline', description: JSON.stringify({
        prefixFrames, prefixCharacters: prefixCompleted, pendingPrefixAtEnter, prefixResponseToEnterStartMs: ack.startEpoch - lastPrefixNetworkEnd,
        enterQueueAndQuietMs: enterRequestedAt - enterStarted, enterHttpAdmissionMs: ack.networkMs,
        enterToAdmissionMs: ack.at - enterStarted, enterToProcessingObservedMs: consumed - enterStarted,
      }) });
      stopTiming!(); stopTiming = undefined;
    } else await page.keyboard.press('Enter');
    if (scenario.startsWith('terminal-interrupt')) {
      const interrupt = panel.getByRole('button', { name: 'Interrupt Terminal turn', exact: true });
      await expect(interrupt).toBeEnabled({ timeout: 20_000 });
      const before = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
      expect(before.native.modelHeld).toBe(true);
      if (scenario === 'terminal-interrupt-stop-fence') {
        const held = await page.request.post('/e2e-test/relay-native/interrupt/hold-write', { headers });
        expect(held.ok(), await held.text()).toBe(true); nativeWriteHeld = true;
        const commanded = page.waitForRequest(request => request.url().endsWith(`/api/sessions/${session}/relay`)
          && request.postDataJSON()?.method === 'ry/turn.interrupt');
        await interrupt.focus(); await page.keyboard.press('Space');
        const nativeCommand = await commanded; const command = nativeCommand.postDataJSON();
        await expect.poll(async () => (await (await page.request.post('/e2e-test/relay-native/interrupt/write-state', { headers })).json()).waiting).toBe(true);
        // Actual public Stop commits its intent before waiting for the broker's
        // dispatch lock. Do not serialize it behind the held interrupt request.
        const stopping = page.request.post(`/api/sessions/${session}/stop`, { headers: await csrfHeaders(page) });
        await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session}`)).json()).desired_state).toBe('stopped');
        const release = await page.request.post('/e2e-test/relay-native/interrupt/release-write', { headers });
        expect(release.ok(), await release.text()).toBe(true); nativeWriteHeld = false;
        const stopped = await stopping; expect(stopped.ok(), await stopped.text()).toBe(true);
        const interruptResponse = await nativeCommand.response();
        expect([0, 200]).toContain(interruptResponse?.status() ?? 0);
        await expect.poll(async () => (await (await page.request.get('/e2e-test/relay-native/state', { headers })).json()).native.active).toBe(0);
        const observed = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
        expect(observed.interrupt.interrupts).toHaveLength(0); expect(observed.interrupt.writeAdmissions).toHaveLength(0);
        expect(observed.interrupt.refusedGuards).toBe(1); expect(observed.native.requests).toBe(2);
        expect(observed.interrupt.journalReceipts).toEqual([{ operationId: command.params.operationId, state: 'rejected' }]);
        const receipt = await page.request.get(`/api/sessions/${session}/relay/operations/${command.params.operationId}`);
        expect(receipt.status()).toBe(200); expect(['failed', 'uncertain']).toContain((await receipt.json()).state);
        const absent = await page.request.post(`/e2e-test/relay-native/interrupt/receipt-state?operationId=${command.params.operationId}`, { headers });
        expect((await absent.json()).remainingGrants).toBe(0);
        const model = await page.request.post('/e2e-test/relay-native/interrupt/release', { headers }); expect(model.ok()).toBe(true);
        test.info().annotations.push({ type: 'native_public_stop_fence', description: JSON.stringify({
          boundary: 'public-stop-intent-at-actual-native-write-lock', journal: 'rejected', refusedGuards: 1,
          nativeInterrupts: 0, nativeOwners: 0, requests: 2, interruptHttpStatus: interruptResponse?.status() ?? null,
        }) });
        return;
      }
      if (scenario === 'terminal-interrupt-cookie-fence') {
        const held = await page.request.post('/e2e-test/relay-native/interrupt/hold-write', { headers });
        expect(held.ok(), await held.text()).toBe(true); nativeWriteHeld = true;
        const commanded = page.waitForRequest(request => request.url().endsWith(`/api/sessions/${session}/relay`)
          && request.postDataJSON()?.method === 'ry/turn.interrupt');
        await interrupt.focus(); await page.keyboard.press('Space');
        const command = (await commanded).postDataJSON();
        await expect.poll(async () => (await (await page.request.post('/e2e-test/relay-native/interrupt/write-state', { headers })).json()).waiting).toBe(true);
        const signedOut = await page.request.post('/api/auth/logout', { headers: await csrfHeaders(page) });
        expect(signedOut.ok(), await signedOut.text()).toBe(true);
        const release = await page.request.post('/e2e-test/relay-native/interrupt/release-write', { headers });
        expect(release.ok(), await release.text()).toBe(true); nativeWriteHeld = false;
        await expect.poll(async () => (await (await page.request.get('/e2e-test/relay-native/state', { headers })).json()).interrupt.refusedGuards).toBe(1);
        const refused = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
        expect(refused.interrupt.interrupts).toHaveLength(0); expect(refused.interrupt.writeAdmissions).toHaveLength(0);
        expect(refused.interrupt.completions).toHaveLength(0); expect(refused.native.modelHeld).toBe(true);
        expect(refused.clients.owner).toBe(before.clients.owner); expect(refused.clients.views).toHaveLength(before.clients.views.length);
        expect(refused.interrupt.journalReceipts).toEqual([{ operationId: command.params.operationId, state: 'rejected' }]);
        const receiptURL = `/e2e-test/relay-native/interrupt/receipt-state?operationId=${command.params.operationId}`;
        await expect.poll(async () => (await (await page.request.post(receiptURL, { headers })).json()).remainingGrants).toBe(0);
        const receipt = await (await page.request.post(receiptURL, { headers })).json();
        expect(['failed', 'uncertain']).toContain(receipt.state);
        const noAuthority = await page.request.get(`/api/sessions/${session}/relay/events`);
        expect(noAuthority.status()).toBe(401); await expect(panel).toHaveCount(0);
        expect((await keeperRequests.get(`/api/sessions/${session}/relay/events`)).status()).toBe(200);
        const resumed = await page.request.post('/e2e-test/relay-native/interrupt/release', { headers });
        expect(resumed.ok()).toBe(true);
        await expect.poll(async () => (await (await page.request.get('/e2e-test/relay-native/state', { headers })).json()).interrupt.completions.at(-1)?.status,
          { timeout: 20_000 }).toBe('completed');
        const completed = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
        expect(completed.interrupt.interrupts).toHaveLength(0); expect(completed.native.requests).toBe(2);
        expect(completed.clients.owner).toBe(before.clients.owner);
        test.info().annotations.push({ type: 'native_cookie_fence', description: JSON.stringify({
          boundary: 'public-cookie-logout-at-actual-native-write-lock', journal: 'rejected',
          refusedGuards: 1, nativeInterrupts: 0, originalTurn: 'completed', mainReceipt: receipt.state,
        }) });
        return;
      }
      const interrupted = page.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
        && response.request().postDataJSON()?.method === 'ry/turn.interrupt');
      await interrupt.focus(); const interruptStarted = performance.now(); await page.keyboard.press('Space');
      const answer = await (await interrupted).json();
      const interruptAdmissionMs = performance.now() - interruptStarted;
      expect(answer.error, JSON.stringify(answer)).toBeUndefined();
      expect(answer.result.state).toBe(scenario.endsWith('-lost') ? 'uncertain' : 'accepted');
      await expect.poll(async () => (await (await page.request.get('/e2e-test/relay-native/state', { headers })).json()).interrupt.completions.at(-1)?.status).toBe('interrupted');
      if (controlled) test.info().annotations.push({ type: 'relay_interrupt_timeline', description: JSON.stringify({
        admissionMs: interruptAdmissionMs, confirmationObservedMs: performance.now() - interruptStarted,
      }) });
      await expect(panel.getByRole('list', { name: 'Terminal interrupt receipts' })).toContainText(scenario === 'terminal-interrupt-process-lost' ? 'Interrupt outcome unknown.' : 'Interrupt requested.');
      await expect(interrupt).toHaveCount(0);
      const receipt = await (await page.request.get(`/api/sessions/${session}/relay/operations/${answer.result.operationId}`)).json();
      expect(receipt.state).toBe(scenario === 'terminal-interrupt-process-lost' ? 'uncertain' : 'accepted'); expect(receipt.actor).toEqual(answer.result.actor);
      expect(receipt.payloadDigest).toBe(answer.result.payloadDigest);
      const after = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
      expect(after.interrupt.interrupts).toHaveLength(1); expect(after.interrupt.completions).toHaveLength(1);
      expect(after.interrupt.interrupts[0].turnId).toBe(after.interrupt.completions[0].id);
      expect(after.clients.owner).toBe(before.clients.owner); expect(after.clients.views).toHaveLength(before.clients.views.length);
      expect(after.native.requests).toBe(2);
      const released = await page.request.post('/e2e-test/relay-native/interrupt/release', { headers });
      expect(released.ok()).toBe(true);
      if (scenario === 'terminal-interrupt-process-lost') {
        expect(after.interrupt.writeAdmissions).toHaveLength(1);
        expect(after.interrupt.journalReceipts).toEqual([{ operationId: receipt.operationId, state: 'uncertain' }]);
        const control = await playwright.request.newContext({ baseURL: new URL(page.url()).origin,
          storageState: await page.context().storageState(), extraHTTPHeaders: headers });
        try {
          const ended = await control.post('/e2e-test/relay-native/end/normal'); expect(ended.ok(), await ended.text()).toBe(true);
        } finally { await control.dispose(); }
        await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session}/relay`)).json()).exitOutcome).toBe('normal');
        const stored = await page.request.get(`/api/sessions/${session}/relay/operations/${receipt.operationId}`);
        expect(stored.status()).toBe(200); expect(await stored.json()).toEqual(receipt);
        const source = await page.request.get('/e2e-test/relay-native/cold-observer', { headers }); expect(source.ok()).toBe(true);
        const oldRead = await page.waitForRequest(request => request.url().includes(`/api/sessions/${session}/relay/terminal?`));
        const oldHeaders = await oldRead.allHeaders(); const oldUrl = new URL(oldRead.url());
        const snapshot = await (await page.request.get(`/api/sessions/${session}/relay`)).json();
        writeFileSync(`${BASE}/retained-process.json`, JSON.stringify({ session, run: snapshot.runId,
          source: await source.json(), receipt, oldRead: { path: oldUrl.pathname + oldUrl.search,
            connection: oldHeaders['x-acc-relay-connection'] } }));
        test.info().annotations.push({ type: 'native_uncertain_receipt', description: JSON.stringify({
          operationId: receipt.operationId, state: receipt.state, journal: 'uncertain', nativeInterrupts: 1,
          nativeCompletions: 1, requests: 2, noReplay: true,
        }) });
        return;
      }
      if (controlled) {
        const control = await playwright.request.newContext({ baseURL: new URL(page.url()).origin,
          storageState: await page.context().storageState(), extraHTTPHeaders: headers });
        try {
          const ended = await control.post('/e2e-test/relay-native/end/normal');
          expect(ended.ok(), await ended.text()).toBe(true);
        } finally { await control.dispose(); }
        await expect.poll(async () => (await (await page.request.get(`/api/sessions/${session}/relay`)).json()).exitOutcome).toBe('normal');
        const readRequest = await page.waitForRequest(request => request.url().includes(`/api/sessions/${session}/relay/terminal?`));
        const readHeaders = await readRequest.allHeaders();
        const connection = readHeaders['x-acc-relay-connection'];
        const attachmentId = new URL(readRequest.url()).searchParams.get('attachmentId');
        const retained = await page.request.get(readRequest.url(), { headers: { 'X-ACC-Relay-Connection': connection } });
        expect(retained.status()).toBe(200); expect(await retained.text()).toContain('Production Terminal input');
        const armed = await page.request.post('/e2e-test/relay-native/retained/hold', { headers });
        expect(armed.ok(), await armed.text()).toBe(true);
        const pending = page.request.get(readRequest.url(), { headers: {
          'X-ACC-Relay-Connection': connection, 'x-e2e-retained-proof': fixture.isolation_token } });
        await expect.poll(async () => (await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json()).held).toBe(true);
        const detached = await page.request.post(`/api/sessions/${session}/relay`, {
          headers: { ...headers, 'X-ACC-Relay-Connection': connection },
          data: { jsonrpc: '2.0', id: 'detach-held-read', method: 'ry/session.detach', params: {
            sessionId: session, attachmentId, connectionSeq: 2 } } });
        expect(detached.ok()).toBe(true); expect((await detached.json()).result.detached).toBe(true);
        const release = await page.request.post('/e2e-test/relay-native/retained/release', { headers });
        expect(release.ok(), await release.text()).toBe(true);
        const refused = await pending;
        expect([404, 409]).toContain(refused.status()); expect(await refused.text()).not.toContain('Production Terminal input');
        const state = await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json();
        expect(state.nativeActive).toBe(0); expect(state.nativeRequests).toBe(2);
        test.info().annotations.push({ type: 'retained_detach_race', description: 'public-session.detach-after-actual-native-screen-capture-refuses-without-restart' });
        if (scenario === 'terminal-interrupt-logout') {
          // Use the existing isolated second person; never revoke shared setup AUTH_FILE.
          secondPage = await otherOwner.newPage(); await secondPage.goto(`/sessions/${session}`);
          const observerPanel = secondPage.getByRole('region', { name: 'Shared session', exact: true });
          await expect(observerPanel).toBeVisible();
          await observerPanel.getByRole('button', { name: 'Terminal', exact: true }).click();
          await expect(observerPanel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Production Terminal input');
          const freshRead = await secondPage.waitForRequest(request => request.url().includes(`/api/sessions/${session}/relay/terminal?`));
          const freshHeaders = await freshRead.allHeaders();
          const freshConnection = freshHeaders['x-acc-relay-connection'];
          const current = await secondPage.request.get(freshRead.url(), { headers: { 'X-ACC-Relay-Connection': freshConnection } });
          expect(current.status()).toBe(200); expect(await current.text()).toContain('Production Terminal input');
          const dropped = await page.request.post('/e2e-test/relay-native/retained/drop-runtime', { headers });
          expect(dropped.ok(), await dropped.text()).toBe(true);
          expect(await dropped.json()).toMatchObject({ runtimePresent: false, nativeActive: 0, nativeRequests: 2 });
          const retainedCold = await secondPage.request.get(freshRead.url(), { headers: { 'X-ACC-Relay-Connection': freshConnection } });
          expect(retainedCold.status()).toBe(200);
          expect(await retainedCold.text()).toContain('Production Terminal input');
          const coldEvents = await secondPage.request.get(`/api/sessions/${session}/relay/events`);
          expect(coldEvents.status()).toBe(200);
          const coldSnapshot = await secondPage.request.get(`/api/sessions/${session}/relay`);
          expect(coldSnapshot.status()).toBe(200); expect((await coldSnapshot.json()).exitOutcome).toBe('normal');
          const absent = await page.request.post('/e2e-test/relay-native/retained/runtime-state', { headers });
          expect(await absent.json()).toMatchObject({ runtimePresent: false, nativeActive: 0, nativeRequests: 2 });
          test.info().annotations.push({ type: 'retained_cold_runtime', description: 'ended-native-retained-reads-without-main-runtime-no-owner-or-grant' });
          const hold = await page.request.post('/e2e-test/relay-native/retained/hold', { headers });
          expect(hold.ok(), await hold.text()).toBe(true);
          const protectedRead = secondPage.request.get(freshRead.url(), { headers: {
            'X-ACC-Relay-Connection': freshConnection, 'x-e2e-retained-proof': fixture.isolation_token } });
          await expect.poll(async () => (await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json()).held).toBe(true);
          const signedOut = await secondPage.request.post('/api/auth/logout', { headers: await csrfHeaders(secondPage) });
          expect(signedOut.ok(), await signedOut.text()).toBe(true); expect((await signedOut.json()).ok).toBe(true);
          const release = await page.request.post('/e2e-test/relay-native/retained/release', { headers });
          expect(release.ok(), await release.text()).toBe(true);
          const denied = await protectedRead;
          expect(denied.status()).toBe(401); expect(await denied.text()).not.toContain('Production Terminal input');
          const subsequent = await secondPage.request.get(freshRead.url(), { headers: { 'X-ACC-Relay-Connection': freshConnection } });
          expect(subsequent.status()).toBe(401); await expect(observerPanel).toHaveCount(0);
          const originalLogin = await page.request.get(`/api/sessions/${session}/relay/events`);
          expect(originalLogin.status()).toBe(200);
          const finalState = await (await page.request.post('/e2e-test/relay-native/retained/state', { headers })).json();
          expect(finalState.nativeActive).toBe(0); expect(finalState.nativeRequests).toBe(2);
          test.info().annotations.push({ type: 'retained_logout_race', description: 'public-cookie-logout-after-actual-native-capture-refuses401-clears-view-without-restart' });
        }

      }
      return;
    }

    await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Production fixture response 2.', { timeout: 30_000 });
    await expect(typing).toHaveValue(''); await expect(typing).toBeEnabled();
    if (scenario === 'repeated') {
      // Identical text is a deliberate new user action, never a retry or a
      // text-based acknowledgement of the prior operation.
      await typing.pressSequentially('Production Terminal input'); await typing.press('Enter');
      await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Production fixture response 3.', { timeout: 30_000 });
      await expect(typing).toHaveValue(''); await expect(typing).toBeEnabled();
    }
    await typing.pressSequentially('Retain this unsent native draft');
    await expect(typing).toHaveValue('Retain this unsent native draft');
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    await expect(message).toHaveAttribute('readonly', '');
    await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production Terminal input');
    if (scenario === 'repeated') await expect(panel.getByRole('log', { name: 'Conversation' }).locator('p').filter({ hasText: /^user: Production Terminal input$/ })).toHaveCount(2);
    await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 2.');
    await panel.getByRole('textbox', { name: 'Reason for taking input' }).fill('Person continues in fresh Chat');
    await expect(panel.getByRole('button', { name: 'Take input in Chat', exact: true })).toBeEnabled({ timeout: 20_000 });
    const chatTaken = page.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
      && response.request().postDataJSON()?.method === 'ry/writer.take'
      && response.request().postDataJSON()?.params.view === 'chat');
    const [, chatTakeResponse] = await Promise.all([panel.getByRole('button', { name: 'Take input in Chat', exact: true }).click(), chatTaken]);
    const chatTakeReply = await chatTakeResponse.json();
    const { operationId: chatTakeOperation, ...chatTakePayload } = chatTakeResponse.request().postDataJSON().params;
    expect(chatTakeReply.error, JSON.stringify(chatTakeReply)).toBeUndefined();
    expect(chatTakeReply.result).toMatchObject({ state: 'accepted', method: 'writer.take', operationId: chatTakeOperation,
      sessionId: session, runId: chatTakePayload.expectedRunId, payloadDigest: await operationDigest('writer.take', chatTakePayload) });
    await expect(message).toBeEditable(); await expect(message).toHaveValue('');
    await message.fill('Chat after production take'); await panel.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText(chatResponse);
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    const old = panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
    await expect(old).toHaveValue('Retain this unsent native draft'); await expect(old).toHaveAttribute('readonly', '');
    await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Retain this unsent native draft');
    const final = await page.request.post(`/e2e-test/relay-native/qualify/${session}`, { headers });
    const clients = await final.json(); expect(clients.nativeId).toBe(initial.nativeId); expect(clients.owner).toBe(initial.owner);
    expect(clients.views.some((view: { retired: boolean; draft: string }) => view.retired && view.draft === 'Retain this unsent native draft')).toBe(true);
    if (scenario === 'second-person') {
      secondPage = await otherOwner.newPage();
      await secondPage.goto(`/sessions/${session}`);
      const otherPanel = secondPage.getByRole('region', { name: 'Shared session', exact: true });
      await expect(otherPanel).toBeVisible();
      await otherPanel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(otherPanel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 3.');
      await expect(otherPanel.getByRole('textbox', { name: 'Message', exact: true })).toHaveAttribute('readonly', '');
      await otherPanel.getByRole('button', { name: 'Terminal', exact: true }).click();
      await expect(otherPanel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Retain this unsent native draft');
      await otherPanel.getByRole('textbox', { name: 'Reason for taking input' }).fill('Second person chooses fresh Terminal input');
      await otherPanel.getByRole('button', { name: 'Take input in Terminal', exact: true }).click();
      await otherPanel.getByRole('button', { name: 'Connect Terminal input', exact: true }).click();
      const otherTyping = otherPanel.getByRole('textbox', { name: 'Terminal typing', exact: true });
      await expect(otherTyping).toBeVisible(); await expect(otherTyping).toBeEnabled(); await expect(otherTyping).toBeFocused(); await expect(otherTyping).toHaveValue('');
      await otherTyping.pressSequentially('Fresh replacement Terminal input');
      await otherTyping.press('Enter');
      await expect(otherPanel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Production fixture response 4.', { timeout: 30_000 });
      await expect(otherTyping).toHaveValue(''); await expect(otherTyping).toBeEnabled();
      await panel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(message).toHaveAttribute('readonly', '');
      await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Fresh replacement Terminal input');
      await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 4.');
      const holder = otherPanel.getByRole('status', { name: 'Input holder', exact: true });
      await expect(holder).not.toContainText('alex holds input');
      await expect(panel.getByRole('status', { name: 'Input holder', exact: true }).locator('p')).toHaveText(await holder.locator('p').innerText());
      const replaced = await page.request.post(`/e2e-test/relay-native/qualify/${session}`, { headers });
      expect(replaced.ok(), await replaced.text()).toBe(true);
      const identity = await replaced.json();
      expect(identity.nativeId).toBe(initial.nativeId); expect(identity.owner).toBe(initial.owner);
      expect(identity.views.some((view: { retired: boolean; draft: string }) => view.retired && view.draft === 'Retain this unsent native draft')).toBe(true);
    }
    const observed = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
    expect(observed.native.requests).toBe(expectedRequests);
    expect(observed.native.request_markers.every((request: { input_markers: string[] }) => !request.input_markers.includes('Retain this unsent native draft'))).toBe(true);
  } catch (failure) {
    console.error('Offline native join primary failure:', (failure as Error).message);
    console.error('Offline browser statuses:', JSON.stringify(await page.getByRole('status').allTextContents()));
    console.error('Offline keyboard status:', JSON.stringify(await page.getByRole('region', { name: 'Native Terminal', exact: true }).locator('p').allTextContents()));
    const diagnostic = await (await page.request.get('/e2e-test/relay-native/state', { headers })).json();
    console.error('Offline native state:', JSON.stringify({ ...diagnostic.native, socket_records: undefined, clients: diagnostic.clients }));
    throw failure;
  } finally {
    stopTiming?.();
    const requests = page.request;
    if (nativeWriteHeld) {
      await requests.post('/e2e-test/relay-native/interrupt/release-write', { headers });
      nativeWriteHeld = false;
    }
    await secondPage?.close();
    await page.close(); // Stop these owned observers/input clients before reclaiming the native fixture.
    const finished = await requests.post('/e2e-test/relay-native/finish', { headers });
    expect(finished.ok(), await finished.text()).toBe(true);
    const result = await finished.json();
    if (session) {
      expect(result.terminalMediated).toBe(false);
      test.info().annotations.push({ type: 'relay_main_input_profile', description: JSON.stringify(result.mainInputProfile) });
      test.info().annotations.push({ type: 'relay_native_spans', description: JSON.stringify(result.timings) });
      expect(result.requests).toHaveLength(expectedRequests);
      expect(result.requests.every((request: { input_markers: string[] }) => !request.input_markers.includes('Retain this unsent native draft'))).toBe(true);
    }
  }
});

test('persisted native final screen survives a fresh main process without provider allocation', { tag: '@relay-native' }, async ({ page }) => {
  test.skip(!environmentValue('ACC_E2E_SEED_DATA'), 'Requires the explicit second process of the persisted-data rehearsal');
  expect(existsSync(`${BASE}/retained-process.json`)).toBe(true);
  const saved = JSON.parse(readFileSync(`${BASE}/retained-process.json`, 'utf8'));
  const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  const headers = { ...await csrfHeaders(page), 'x-e2e-isolation': fixture.isolation_token };
  const armed = await page.request.post('/e2e-test/relay-native/cold-observer', { headers });
  expect(armed.ok(), await armed.text()).toBe(true);
  const cold = await armed.json();
  expect(cold.pid).not.toBe(saved.source.pid); expect(cold.state).not.toBe(saved.source.state);
  expect(cold).toMatchObject({ runtimePresent: false, nativeFixtureActive: false, runtimeAllocations: [], publicWriterQualified: false });
  const stale = await page.request.get(saved.oldRead.path, { headers: { 'X-ACC-Relay-Connection': saved.oldRead.connection } });
  expect([404, 409]).toContain(stale.status()); expect(await stale.text()).not.toContain('First production Chat');
  await page.goto(`/sessions/${saved.session}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel).toBeVisible();
  await expect(panel.getByText('Session ended normally.', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('First production Chat');
  await expect(panel.getByRole('button', { name: 'Take input in Terminal', exact: true })).toBeDisabled();
  await expect(panel.getByRole('textbox', { name: 'Terminal typing', exact: true })).toHaveCount(0);
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Production fixture response 1.');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveAttribute('readonly', '');
  const choice = panel.getByRole('combobox', { name: 'Retained run', exact: true });
  await choice.selectOption(saved.run);
  const past = panel.getByRole('region', { name: 'Past run history', exact: true });
  await expect(past.getByRole('log', { name: 'Past run conversation' })).toContainText('Production fixture response 1.');
  await expect(past.getByRole('log', { name: 'Native Terminal screen' })).toContainText('First production Chat');
  await page.reload(); await expect(panel).toBeVisible();
  const events = await page.request.get(`/api/sessions/${saved.session}/relay/events`);
  expect(events.status()).toBe(200);
  expect(JSON.stringify(await events.json())).toContain('Production fixture response 1.');
  if (saved.receipt) {
    expect(saved.receipt.state).toBe('uncertain');
    for (let read = 0; read < 2; read++) {
      const observed = await page.request.get(`/api/sessions/${saved.session}/relay/operations/${saved.receipt.operationId}`);
      expect(observed.status()).toBe(200); expect(await observed.json()).toEqual(saved.receipt);
    }
  }
  const after = await page.request.get('/e2e-test/relay-native/cold-observer', { headers });
  expect(await after.json()).toEqual(cold);
  test.info().annotations.push({ type: 'retained_process_restart', description: JSON.stringify({ source: saved.source, cold }) });
  if (saved.receipt) {
    const logout = await page.request.post('/api/auth/logout', { headers: await csrfHeaders(page) }); expect(logout.ok()).toBe(true);
    const denied = await page.request.get(`/api/sessions/${saved.session}/relay/operations/${saved.receipt.operationId}`);
    expect(denied.status()).toBe(401); expect(await denied.text()).not.toContain(saved.receipt.payloadDigest);
    await expect(panel).toHaveCount(0);
    test.info().annotations.push({ type: 'retained_uncertain_receipt', description: JSON.stringify({
      operationId: saved.receipt.operationId, state: 'uncertain', authenticatedReads: 2, loggedOutStatus: 401,
      runtimeAllocations: cold.runtimeAllocations.length,
    }) });
  }
});

test('Terminal physical key sequences retain and forward each supported character', async ({ page }) => {
  const data = await terminalInputFixture(page); await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeVisible(); await typing.pressSequentially('abc');
  await expect(typing).toHaveValue('abc'); await expect.poll(() => data.writes.map(value => atob(value.data as string)).join('')).toBe('abc');
});

test('a delayed old native screen failure cannot retire input in a fresh writer lease', async ({ page }) => {
  const data = await terminalInputFixture(page); let release!: () => void;
  data.holdRead(new Promise<void>(resolve => { release = resolve; }));
  try {
    await expect.poll(data.readHeld).toBe(true);
    data.snapshot.controlRevision = 2;
    data.snapshot.writer!.holder!.principal = 'Fresh fixture holder';
    // Wait for the panel's actual holder snapshot, not a new claim's fixture reply.
    await page.waitForResponse(response => response.url().endsWith('/relay') && response.request().postDataJSON()?.method === 'ry/session.snapshot');
    await expect(data.panel.getByLabel('Input holder')).toContainText('Fresh fixture holder');
    await data.connect.click(); const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
    await expect(typing).toBeVisible(); release();
    await typing.press('a'); await expect(typing).toHaveValue('a');
    await page.waitForTimeout(700); await expect(typing).toBeVisible();
    expect(data.writes.length).toBe(1); expect(data.writes[0].inputEpoch).toBe(2);
  } finally { release(); }
});


test('Terminal claim focuses committed input only after the pending receipt clears', async ({ page, _drainRoutes }) => {
  const data = await terminalInputFixture(page);
  let release!: () => void;
  data.holdClaim(new Promise<void>(resolve => { release = resolve; }));
  try {
    await data.connect.click();
    await expect(data.panel.getByRole('textbox', { name: 'Connecting Terminal input', exact: true })).toBeVisible();
    data.snapshot.receipts.push({ operationId: 'pending-claim', sessionId: data.id, runId: 'fake-run', method: 'control.claim',
      payloadDigest: 'sha256:' + 'a'.repeat(64), actor: { kind: 'person', principal: 'Example person', channel: 'chat' }, state: 'submitting', revision: 1 });
    await expect(data.panel.getByRole('list', { name: 'Operation receipts', includeHidden: true })).toContainText('control.claim: submitting');
    release();
    const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
    await expect(typing).toBeVisible(); await expect(typing).toBeDisabled(); await expect(typing).not.toBeFocused();
    data.snapshot.receipts.splice(0);
    await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
    await page.keyboard.type('abc'); await expect(typing).toHaveValue('abc');
    await expect.poll(() => data.writes.map(value => atob(value.data as string)).join('')).toBe('abc');
    expect(page.url()).not.toContain('new-session=1');
  } finally { release(); }
});

async function proveNativeClaudeStopUnknown(page: Page, session: string, headers: Record<string, string>, source: 'sdk' | 'native_hook', repeat = false) {
  const requests = page.request;
  const before = await (await requests.get(`/api/sessions/${session}/relay`)).json();
  const answers: RelayReceipt[] = before.receipts.filter((row: RelayReceipt) => row.method === 'decision.reply');
  expect(answers).toHaveLength(1);
  const original = answers[0]; expect(original.state).toBe('uncertain');
  const held = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
  expect(held.model_held).toBe(true); expect(held.decision_sources).toEqual([source]);
  const stopped = await requests.post(`/api/sessions/${session}/stop`, { headers });
  expect(stopped.ok(), await stopped.text()).toBe(true);
  const detail = await (await requests.get(`/api/sessions/${session}`)).json();
  expect(detail.last_error ?? '').not.toContain('stop not confirmed');
  await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).active).toBe(0);
  // Releasing the private offline response after verified Stop is not authority
  // to restart, answer again or infer acceptance from an interrupted tool chain.
  expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
  await page.waitForTimeout(1200);
  const receipt: RelayReceipt = await (await requests.get(`/api/sessions/${session}/relay/operations/${original.operationId}`)).json();
  expect(receipt).toMatchObject({ operationId: original.operationId, sessionId: original.sessionId, runId: original.runId,
    method: original.method, payloadDigest: original.payloadDigest, actor: original.actor, state: 'uncertain' });
  const after = await (await requests.get(`/api/sessions/${session}/relay`)).json();
  expect(after.receipts.filter((row: RelayReceipt) => row.method === 'decision.reply')).toHaveLength(1);
  const ended = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
  expect(ended.active).toBe(0); expect(ended.owner_process).toBeNull();
  expect(ended.application_retained).toBe(0); expect(ended.application_observers).toBe(0);
  expect(ended.decision_sources).toEqual([source]); expect(ended.nativeId).toBe(held.nativeId);
  expect(ended.terminalMediated).toBe(false);
  if (repeat) {
    const repeated = await requests.post(`/api/sessions/${session}/stop`, { headers });
    expect(repeated.ok(), await repeated.text()).toBe(true);
    const detail = await (await requests.get(`/api/sessions/${session}`)).json();
    expect(detail.last_error ?? '').not.toContain('stop not confirmed');
    const receipt = await (await requests.get(`/api/sessions/${session}/relay/operations/${original.operationId}`)).json();
    expect(receipt).toMatchObject({ operationId: original.operationId, state: 'uncertain', payloadDigest: original.payloadDigest });
    const after = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(after.active).toBe(0); expect(after.decision_sources).toEqual([source]);
  }
  console.log('Native Stop with unknown answer:', JSON.stringify({ source, sameIdentity: true, receipt: receipt.state, active: ended.active, answers: ended.decision_sources.length }));
}

async function typeNativeClaudeFrames(page: Page, session: string, typing: import('@playwright/test').Locator) {
  const frames: { frameSeq: number; inputEpoch: number; data: string }[] = [];
  function input(request: Request) {
    if (request.method() !== 'POST' || !request.url().endsWith(`/api/sessions/${session}/relay`)) return;
    const command = request.postDataJSON();
    if (command?.method === 'ry/terminal.input') return command.params;
  }
  const capture = (request: Request) => {
    const frame = input(request);
    if (frame) frames.push({ frameSeq: frame.frameSeq, inputEpoch: frame.inputEpoch, data: frame.data });
  };
  page.on('request', capture);
  try {
    const delivered = page.waitForResponse(response => input(response.request())?.data === 'DQ==');
    await expect(typing).toBeFocused(); await page.keyboard.type('abc');
    await expect(typing).toBeFocused(); await page.keyboard.press('Enter');
    const reply = await (await delivered).json();
    expect(reply.error).toBeUndefined();
    expect(frames.length).toBeGreaterThanOrEqual(2); expect(frames.length).toBeLessThanOrEqual(4);
    expect(frames.map(frame => frame.frameSeq)).toEqual(frames.map((_, index) => index + 1));
    expect(new Set(frames.map(frame => frame.inputEpoch)).size).toBe(1);
    const text = frames.map(frame => Buffer.from(frame.data, 'base64').toString('utf8'));
    expect(text.slice(0, -1).join('')).toBe('abc'); expect(text.at(-1)).toBe('\r');
    expect(text.slice(0, -1).every(frame => /^[a-z]+$/.test(frame))).toBe(true);
    expect(reply.result.watermark).toMatchObject({ epoch: frames[0].inputEpoch, admitted: frames.length });
    console.log('Native Claude input frames:', JSON.stringify({ frames: frames.length, contiguous: true, exactBytesOnce: true, separateEnter: true }));
    return reply.result.watermark.admitted as number;
  } finally { page.off('request', capture); }
}

async function reloadNativeClaudeUnknown(page: Page, session: string, headers: Record<string, string>, submittedSequence: number) {
  const requests = page.request;
  const snapshot = await (await requests.get(`/api/sessions/${session}/relay`)).json();
  const answers: RelayReceipt[] = snapshot.receipts.filter((row: RelayReceipt) => row.method === 'decision.reply');
  expect(answers).toHaveLength(1); const original = answers[0]; expect(original.state).toBe('uncertain');
  const before = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
  // The preceding35s barrier already observed fast-observer expiry. Existing
  // ownership polls may briefly schedule a one-shot proof read at any time.
  expect(before.application_retained).toBe(1);
  await page.reload();
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel).toBeVisible();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(panel.getByRole('log', { name: 'Native Terminal screen', exact: true })).toContainText('abc');
  await expect(panel.getByRole('button', { name: 'Connect Terminal input', exact: true })).toHaveCount(0);
  const receipt = await (await requests.get(`/api/sessions/${session}/relay/operations/${original.operationId}`)).json();
  expect(receipt).toMatchObject({ operationId: original.operationId, method: original.method, sessionId: original.sessionId,
    runId: original.runId, actor: original.actor, payloadDigest: original.payloadDigest, state: 'uncertain' });
  const after = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
  expect(after.owner_process).toBe(before.owner_process); expect(after.runId).toBe(before.runId);
  expect(after.nativeId).toBe(before.nativeId); expect(after.enclosures).toBe(before.enclosures);
  expect(after.admitted).toBe(submittedSequence); expect(after.decision_sources).toEqual(['native_hook']);
  return original;
}

async function nativeClaudeBrowser(page: Page, scenario: string, otherOwner?: import('@playwright/test').BrowserContext) {
  const stopUnknown = ['sdk-stop-unknown', 'sdk-stop-allow', 'stop-unknown', 'stop-allow', 'sdk-stop-repeat', 'sdk-stop-repeat-allow', 'stop-repeat', 'stop-repeat-allow'].includes(scenario);
  const sdkLate = ['sdk-late', 'sdk-window', 'sdk-window-allow', 'sdk-stop-unknown', 'sdk-stop-allow', 'sdk-stop-repeat', 'sdk-stop-repeat-allow', 'sdk-cache', 'sdk-cache-allow'].includes(scenario);
  const terminalLate = ['late-permission', 'window-permission', 'window-allow', 'stop-unknown', 'stop-allow', 'stop-repeat', 'stop-repeat-allow', 'reload-window-permission', 'reload-window-allow', 'cache-permission', 'cache-allow'].includes(scenario);
  const allow = ['allow', 'sdk-allow', 'sdk-window-allow', 'window-allow', 'sdk-stop-allow', 'stop-allow', 'reload-window-allow', 'sdk-stop-repeat-allow', 'stop-repeat-allow', 'sdk-cache-allow', 'cache-allow', 'revoke-allow'].includes(scenario);
  const afterWindow = ['sdk-window', 'window-permission', 'sdk-window-allow', 'window-allow', 'reload-window-permission', 'reload-window-allow'].includes(scenario);
  const reloadUnknown = scenario.startsWith('reload-window-');
  const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  test.skip(!fixture.relay_native_enabled, 'Requires the explicitly enabled mandatory offline native fixture');
  test.setTimeout(230_000);
  let headers = { ...await csrfHeaders(page), 'x-e2e-isolation': fixture.isolation_token };
  let requests = page.request;
  let session: string | undefined; let completed = false; let stoppedEarly = false; let secondPage: Page | undefined;
  try {
    if (scenario === 'revoke-allow') secondPage = await otherOwner!.newPage();
    if (scenario === 'compact') await page.setViewportSize({ width: 390, height: 844 });
    const setup = await requests.post('/e2e-test/relay-claude/prepare', { headers });
    expect(setup.ok(), await setup.text()).toBe(true);
    const { projectId } = await setup.json();
    await page.goto(`/projects/${projectId}`);
    await page.getByRole('button', { name: /^New session/ }).first().click();
    const launch = page.getByRole('region', { name: 'New session', exact: true });
    await launch.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('claude');
    await openAdvanced(launch);
    await launch.getByRole('checkbox', { name: /^Own worktree/ }).uncheck();
    await launch.getByRole('checkbox', { name: /Remote Control/ }).uncheck();
    await launch.getByRole('combobox', { name: 'Claude sign-in', exact: true }).selectOption('api_key');
    const opt = launch.getByRole('checkbox', { name: /Use shared Chat and Terminal/ });
    await expect(opt).not.toBeChecked();
    const previewed = page.waitForResponse(response => response.url().endsWith('/api/sessions/preview') && response.request().postDataJSON()?.session_relay === true);
    await opt.check();
    const preview = await previewed; expect(preview.ok(), await preview.text()).toBe(true);
    await expect(launch.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
    const launched = page.waitForResponse(response => response.url().endsWith('/api/sessions') && response.request().method() === 'POST');
    await launch.getByRole('button', { name: 'Launch', exact: true }).click();
    const response = await launched; expect(response.ok(), await response.text()).toBe(true);
    const created = await response.json(); session = created.id;
    const initial = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(initial.sessionId).toBe(session); expect(initial.nativeId).toBe(created.native_id);
    expect(initial.view).toBe('chat'); expect(initial.enclosures).toBe(1);
    const panel = page.getByRole('region', { name: 'Shared session', exact: true });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeDisabled();
    await expect(panel.getByText('Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.', { exact: true })).toBeVisible();
    const waiting = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(waiting.owner_process).toBe(initial.owner_process); expect(waiting.runId).toBe(initial.runId);
    expect(waiting.enclosures).toBe(1); expect(waiting.view).toBe('chat');
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    const message = panel.getByRole('textbox', { name: 'Message', exact: true });
    if (scenario === 'sdk-first-deny') {
      const armed = await requests.post('/e2e-test/relay-claude/permission', { headers });
      expect(armed.ok(), await armed.text()).toBe(true);
    }
    if (scenario === 'warm') {
      await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeDisabled();
      await expect(panel).toContainText('Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.');
      expect(initial.view).toBe('chat'); expect(initial.enclosures).toBe(1);
    }
    if (scenario === 'sdk-first-deny') {
      await message.fill('First isolated fixture turn');
      await panel.getByRole('button', { name: 'Send', exact: true }).click();
    } else {
      await proveNativeClaudeChatReceipt(page, session, 'First isolated fixture turn', 1);
      await expect(panel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch', { timeout: 10_000 });
      if (scenario === 'warm') await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeEnabled();
    }
    if (scenario.startsWith('sdk-')) {
      if (scenario !== 'sdk-first-deny') {
        const armed = await requests.post('/e2e-test/relay-claude/permission', { headers });
        expect(armed.ok(), await armed.text()).toBe(true);
        await message.fill('SDK permission fixture turn');
        await panel.getByRole('button', { name: 'Send', exact: true }).click();
      }
      const review = panel.getByRole('region', { name: 'Review decision', exact: true });
      await expect(review).toBeVisible({ timeout: 20_000 });
      if (sdkLate) expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: afterWindow || stopUnknown ? { held: true, long: true } : { held: true } })).ok()).toBe(true);
      const inbox = await page.context().newPage();
      let answered: unknown;
      let originalAnswer: RelayReceipt | undefined;
      try {
        await inbox.goto('/inbox');
        await expect(inbox.getByRole('region', { name: 'CLI approval details' })).toBeVisible();
        const applied = inbox.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
          && response.request().postDataJSON()?.method === 'ry/decision.reply');
        const registered = inbox.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
          && response.request().postDataJSON()?.method === 'ry/operation.register'
          && response.request().postDataJSON()?.params.method === 'decision.reply');
        await inbox.getByLabel('Native review actions').getByRole('button', {
          name: allow ? 'Allow once' : 'Deny', exact: true }).click();
        const [reply, registration] = await Promise.all([applied, registered]);
        originalAnswer = (await registration.json()).result.receipt;
        const answerReply = await reply.json();
        expect(answerReply.error, JSON.stringify(answerReply)).toBeUndefined();
        answered = answerReply.result;
      } finally { await inbox.close(); }
      if (sdkLate) {
        expect(answered).toEqual({ outcome: 'unknown' });
        if (scenario.includes('cache')) {
          const forgotten = await requests.post('/e2e-test/relay-claude/forget-answers', { headers });
          expect(forgotten.ok(), await forgotten.text()).toBe(true);
          expect(await forgotten.json()).toEqual({ forgot: 1, remaining: 0, durable: 1 });
        }
        if (stopUnknown) {
          await proveNativeClaudeStopUnknown(page, session!, headers, 'sdk', scenario.includes('stop-repeat')); stoppedEarly = completed = true; return;
        }
        if (afterWindow) {
          await page.waitForTimeout(35_000); // exceed the real30s observer lifetime while native final response remains held
          await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).application_observers).toBe(0);
          const held = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
          expect(held.application_retained).toBe(1); expect(held.model_held).toBe(true);
        }
        expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
        await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('decision.reply: accepted', { timeout: 10_000 });
      }
      await expect.poll(async () => panel.getByRole('list', { name: 'Operation receipts' }).getByText('turn.submit: accepted', { exact: true }).count(), { timeout: 20_000 }).toBe(scenario === 'sdk-first-deny' ? 1 : 2);
      // The 3s application-observation window may return unknown before the
      // exact native result. Observe only the original registered receipt;
      // acceptance stays mandatory and no answer is sent again.
      expect(originalAnswer?.operationId).toMatch(/^[a-f0-9]{32}$/);
      const registeredAnswer = originalAnswer!;
      let finalAnswer: RelayReceipt | undefined;
      await expect.poll(async () => {
        const response = await requests.get(`/api/sessions/${session}/relay/operations/${registeredAnswer.operationId}`);
        expect(response.status(), await response.text()).toBe(200);
        finalAnswer = await response.json(); return finalAnswer?.state;
      }, { timeout: 20_000 }).toBe('accepted');
      expect(finalAnswer).toMatchObject({ operationId: registeredAnswer.operationId, sessionId: registeredAnswer.sessionId,
        runId: registeredAnswer.runId, method: 'decision.reply', actor: registeredAnswer.actor, payloadDigest: registeredAnswer.payloadDigest });
      if (!sdkLate) expect(['accepted', 'unknown']).toContain((answered as { outcome: string }).outcome);
      const state = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(state.decision_sources).toEqual(['sdk']);
      expect(state.permission_marker).toBe(allow);
      await expect(panel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch');
      await proveNativeClaudeChatReceipt(page, session!, 'Fresh Chat after SDK permission', scenario === 'sdk-first-deny' ? 2 : 3);
      completed = true;
      return;
    }
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    const connect = panel.getByRole('button', { name: 'Connect Terminal input', exact: true });
    await expect(connect).toBeEnabled({ timeout: 20_000 });
    const terminal = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(terminal.nativeId).toBe(initial.nativeId); expect(terminal.runId).not.toBe(initial.runId);
    expect(terminal.view).toBe('terminal');
    await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('First isolated fixture turn');
    const readerClaim = ['reconnect-reader', 'writer-logout', 'take-busy'].includes(scenario) ? page.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
      && response.request().postDataJSON()?.method === 'ry/control.claim') : undefined;
    await connect.click();
    const oldReader = readerClaim ? await readerClaim : undefined;
    const typing = panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
    await expect(typing).toBeEnabled({ timeout: 20_000 }); await expect(typing).toBeFocused();
    if (scenario === 'take-busy') {
      expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: true, long: true } })).ok()).toBe(true);
    }
    if (scenario === 'permission' || allow || terminalLate) {
      const armed = await requests.post('/e2e-test/relay-claude/permission', { headers });
      expect(armed.ok(), await armed.text()).toBe(true);
    }
    if (scenario === 'read-race') {
      expect((await requests.post('/e2e-test/relay-claude/observation-race', { headers })).ok()).toBe(true);
      await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).read_race_injected, { timeout: 15_000 }).toBe(1);
      // Wait for the injected read response to reach the browser. It must keep
      // this input generation; no reconnect/claim/draft replay repairs the race.
      await page.waitForTimeout(1500);
      await expect(typing).toBeEnabled();
      await expect(panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true })).toHaveCount(0);
    }
    if (scenario === 'guard') expect((await requests.post('/e2e-test/relay-claude/guard', { headers })).ok()).toBe(true);
    const submittedSequence = await typeNativeClaudeFrames(page, session!, typing);
    if (scenario === 'take-busy') {
      await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).model_held,
        { timeout: 15_000 }).toBe(true);
      const held = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(held.idle).toBe(false); expect(held.view).toBe('terminal');
      const snapshot = await (await requests.get(`/api/sessions/${session}/relay`)).json();
      expect(Number.isSafeInteger(snapshot.ownershipRevision)).toBe(true);
      const payload = { sessionId: session, expectedRunId: held.runId, target: 'chat', expectedRevision: snapshot.ownershipRevision };
      const sorted = Object.fromEntries(Object.keys(payload).sort().map(key => [key, payload[key as keyof typeof payload]]));
      const digest = 'sha256:' + createHash('sha256').update(JSON.stringify({ method: 'switch.prepare', params: sorted })).digest('hex');
      const connection = oldReader!.request().headers()['x-acc-relay-connection'];
      const rpc = async (method: string, params: object) => (await requests.post(`/api/sessions/${session}/relay`, {
        headers: { ...headers, 'X-ACC-Relay-Connection': connection },
        data: { jsonrpc: '2.0', id: randomUUID(), method: 'ry/' + method, params } })).json();
      const registration = await rpc('operation.register', { sessionId: session, expectedRunId: held.runId,
        registrationKey: 'native-busy-' + randomUUID(), method: 'switch.prepare', payloadDigest: digest });
      expect(registration.error).toBeUndefined();
      expect(registration.result?.receipt.method).toBe('switch.prepare'); expect(registration.result?.receipt.state).toBe('queued');
      const refused = await rpc('switch.prepare', { ...payload, operationId: registration.result.operationId });
      expect(['BUSY', 'INPUT_CONFLICT']).toContain(refused.error?.data?.code);
      // Explicitly cancel this zero-write registration, never retry it after idle.
      const cancelled = await rpc('operation.cancel', { sessionId: session, operationId: registration.result.operationId,
        expectedRevision: registration.result.receipt.revision });
      expect(cancelled.result).toMatchObject({ operationId: registration.result.operationId, state: 'failed', reason: 'cancelled' });
      const stayed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(stayed.owner_process).toBe(held.owner_process); expect(stayed.runId).toBe(held.runId);
      expect(stayed.nativeId).toBe(initial.nativeId); expect(stayed.enclosures).toBe(1); expect(stayed.view).toBe('terminal');
      console.log('Native busy public switch:', JSON.stringify({ code: refused.error.data.code, zeroWriteCancelled: true, sameOwner: true }));
      expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
    }
    if (scenario === 'permission' || allow || terminalLate) {
      // Keyboard dispatch only queues frames. Start review timing after actual
      // broker admission, independently of the G3 transport latency gate.
      await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).admitted, { timeout: 30_000 }).toBe(submittedSequence);
      // Frame admission does not mean the native permission has reached the
      // hub. Observe that separate boundary before timing browser rendering;
      // otherwise native processing consumes most of the review assertion.
      await expect.poll(async () => {
        const items = await (await requests.get(`/api/attention?session_id=${session}`)).json();
        return items.some((item: { relay_decision?: unknown }) => !!item.relay_decision);
      }, { timeout: 15_000 }).toBe(true);
      const review = panel.getByRole('region', { name: 'Review decision', exact: true });
      await expect(review).toBeVisible({ timeout: 15_000 });
      const inboxItems = await (await requests.get(`/api/attention?session_id=${session}`)).json();
      const nativeReview = inboxItems.find((item: { relay_decision?: unknown }) => item.relay_decision);
      expect(nativeReview).toBeTruthy();
      expect(nativeReview.approval_card.tool_name).toBe('Bash');
      expect(nativeReview.approval_card.command).toContain('relay-native-permission-forbidden');
      expect(nativeReview.permission_id).toBeUndefined();
      if (terminalLate) expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: afterWindow || stopUnknown ? { held: true, long: true } : { held: true } })).ok()).toBe(true);
      const inbox = await page.context().newPage();
      try {
        await inbox.goto('/inbox');
        expect(nativeReview.approval_card.cwd).toContain('native-claude-browser-');
        const card = inbox.getByRole('region', { name: 'CLI approval details', exact: true })
          .filter({ hasText: nativeReview.approval_card.cwd }).filter({ hasText: 'relay-native-permission-forbidden' });
        await expect(card).toBeVisible();
        await expect(card.getByLabel('CLI working directory')).toContainText('native-claude-browser-');
        const appliedRequest = scenario === 'revoke-allow' ? inbox.waitForRequest(request => request.url().endsWith(`/api/sessions/${session}/relay`)
          && request.postDataJSON()?.method === 'ry/decision.reply') : undefined;
        const applied = scenario === 'revoke-allow' ? undefined : inbox.waitForResponse(response => response.url().endsWith(`/api/sessions/${session}/relay`)
          && response.request().postDataJSON()?.method === 'ry/decision.reply');
        if (scenario === 'revoke-allow') {
          expect((await requests.post('/e2e-test/relay-claude/revoke-answer', { headers })).ok()).toBe(true);
        }
        await inbox.getByLabel('Native review actions').getByRole('button', { name: allow ? 'Allow once' : 'Deny', exact: true }).click();
        if (scenario === 'revoke-allow') {
          const operation = (await appliedRequest!).postDataJSON().params.operationId;
          await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).answer_revoked).toBe(true);
          expect((await requests.get(`/api/sessions/${session}/relay`)).status()).toBe(401);
          requests = secondPage!.request; headers = { ...await csrfHeaders(secondPage!), 'x-e2e-isolation': fixture.isolation_token };
          expect((await requests.get(`/api/sessions/${session}/relay/operations/${operation}`)).status()).toBe(404);
          await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).revoked_receipt).toBe('uncertain');
          const denied = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
          expect(denied.answer_revoked).toBe(true); expect(denied.permission_marker).toBe(false);
          expect(denied.decision_metadata.records.some((row: { result?: { is_error?: boolean; tool_use_id?: string } }) =>
            row.result?.is_error === true && row.result.tool_use_id === 'native_permission_fixture')).toBe(true);
          expect(denied.revoked_receipt).toBe('uncertain'); expect(denied.revoked_decision).toBe('outcome_unknown');
          expect(denied.decision_sources).toEqual(['native_hook']); expect(denied.active).toBe(1);
          expect(denied.owner_process).toBe(terminal.owner_process); expect(denied.runId).toBe(terminal.runId);
          expect(denied.nativeId).toBe(initial.nativeId); expect(denied.admitted).toBe(submittedSequence);
          completed = true; return;
        }
        expect((await (await applied!).json()).result).toEqual({ outcome: terminalLate ? 'unknown' : 'accepted' });
        if (terminalLate) {
          await expect(inbox.getByLabel('Native review actions')).toContainText('outcome is unknown');
          if (scenario.includes('cache')) {
            const forgotten = await requests.post('/e2e-test/relay-claude/forget-answers', { headers });
            expect(forgotten.ok(), await forgotten.text()).toBe(true);
            expect(await forgotten.json()).toEqual({ forgot: 1, remaining: 0, durable: 1 });
          }
          if (stopUnknown) {
            await proveNativeClaudeStopUnknown(page, session!, headers, 'native_hook', scenario.includes('stop-repeat')); stoppedEarly = completed = true; return;
          }
          if (afterWindow) {
            await page.waitForTimeout(35_000); // exceed the real30s observer lifetime while native final response remains held
            await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).application_observers).toBe(0);
            const held = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
            expect(held.application_retained).toBe(1); expect(held.model_held).toBe(true);
          }
          const restored = reloadUnknown ? await reloadNativeClaudeUnknown(page, session!, headers, submittedSequence) : undefined;
          expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
          if (restored) {
            let receipt: RelayReceipt | undefined;
            await expect.poll(async () => {
              receipt = await (await requests.get(`/api/sessions/${session}/relay/operations/${restored.operationId}`)).json();
              return receipt?.state;
            }, { timeout: 15_000 }).toBe('accepted');
            expect(receipt).toMatchObject({ operationId: restored.operationId, method: restored.method, sessionId: restored.sessionId,
              runId: restored.runId, actor: restored.actor, payloadDigest: restored.payloadDigest });
            const snapshot = await (await requests.get(`/api/sessions/${session}/relay`)).json();
            expect(snapshot.receipts.filter((row: RelayReceipt) => row.method === 'decision.reply')
              .map((row: RelayReceipt) => row.operationId)).toEqual([restored.operationId]);
            const applied = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
            expect(applied.decision_sources).toEqual(['native_hook']); expect(applied.runId).toBe(restored.runId);
            expect(applied.nativeId).toBe(initial.nativeId); expect(applied.enclosures).toBe(1);
          }
        }
        await inbox.bringToFront(); // attention polling is intentionally foreground-only
        await expect.poll(() => inbox.evaluate(() => document.visibilityState)).toBe('visible');
        await expect(card).toHaveCount(0, { timeout: 10_000 });
      } finally { await inbox.close(); }
      const evidence = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      console.log('Native permission evidence:', JSON.stringify({ rpc_times: evidence.rpc_times, hook_observations: evidence.hook_observations, decision_metadata: evidence.decision_metadata }));
      await expect(panel.getByRole('list', { name: 'Operation receipts', includeHidden: true })).toContainText('decision.reply: accepted');
      await expect(review).toHaveCount(0);
      const denied = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(denied.permission_marker).toBe(allow);
      expect(denied.nativeId).toBe(initial.nativeId);
    }
    if (reloadUnknown) await expect(typing).toHaveCount(0);
    else {
      await expect(typing).toHaveValue('', { timeout: 30_000 });
      await expect(typing).toBeEnabled();
    }
    const processed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(processed.admitted).toBe(submittedSequence); expect(processed.processed).toBe(submittedSequence);
    if (scenario === 'guard') {
      expect(processed.guard_denials).toContain('secrets');
      expect(processed.decision_sources).toEqual([]);
      await expect(panel.getByRole('region', { name: 'Review decision' })).toHaveCount(0);
    }
    await expect(panel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch');
    if (scenario === 'reconnect-reader') {
      const old = oldReader!.request();
      const request = old.postDataJSON();
      const detached = await requests.post(`/api/sessions/${session}/relay`, { headers: {
        ...headers, 'X-ACC-Relay-Connection': old.headers()['x-acc-relay-connection'] }, data: {
        jsonrpc: '2.0', id: 'explicit-reader-detach', method: 'ry/session.detach',
        params: { sessionId: session, attachmentId: request.params.attachmentId, connectionSeq: 2 } } });
      expect((await detached.json()).result).toEqual({ detached: true });
      await page.reload();
      await expect(panel).toBeVisible();
      await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
      await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('abc', { timeout: 15_000 });
      await expect(panel.getByRole('button', { name: 'Connect Terminal input', exact: true })).toHaveCount(0);
      const observed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(observed.owner_process).toBe(processed.owner_process); expect(observed.runId).toBe(processed.runId);
      expect(observed.nativeId).toBe(initial.nativeId);
      expect(observed.admitted).toBe(submittedSequence); expect(observed.processed).toBe(submittedSequence);
    }
    if (scenario === 'writer-logout') {
      const claim = await oldReader!.json(); const old = oldReader!.request(); const attachment = old.postDataJSON().params.attachmentId;
      const loggedOut = await requests.post('/api/auth/logout', { headers });
      expect(loggedOut.ok(), await loggedOut.text()).toBe(true);
      await expect.poll(async () => await typing.count() === 0, { timeout: 10_000 }).toBe(true);
      expect((await requests.get(`/api/sessions/${session}/relay`)).status()).toBe(401);
      // This is a new unsent frame, never replay of a draft or prior keystroke.
      const refused = await requests.post(`/api/sessions/${session}/relay`, { headers: {
        ...headers, 'X-ACC-Relay-Connection': old.headers()['x-acc-relay-connection'] }, data: {
        jsonrpc: '2.0', id: 'revoked-writer-new-frame', method: 'ry/terminal.input', params: {
          sessionId: session, attachmentId: attachment, inputGrant: claim.result.inputGrant,
          inputEpoch: claim.result.inputEpoch, frameSeq: submittedSequence + 1, data: 'eg==' } } });
      expect([401, 403]).toContain(refused.status());
      secondPage = await otherOwner!.newPage();
      requests = secondPage.request; headers = { ...await csrfHeaders(secondPage), 'x-e2e-isolation': fixture.isolation_token };
      const observed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(observed.owner_process).toBe(processed.owner_process); expect(observed.runId).toBe(processed.runId);
      expect(observed.nativeId).toBe(initial.nativeId); expect(observed.active).toBe(1);
      expect(observed.admitted).toBe(submittedSequence); expect(observed.processed).toBe(submittedSequence);
      await secondPage.goto(`/sessions/${session}`);
      const recovery = secondPage.getByRole('region', { name: 'Shared session', exact: true });
      await expect(recovery).toBeVisible();
      await recovery.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(recovery.getByRole('textbox', { name: 'Message', exact: true })).toBeEditable({ timeout: 20_000 });
      await proveNativeClaudeChatReceipt(secondPage, session!, 'Fresh authorized Chat after writer sign-out', 1);
      const returned = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(returned.nativeId).toBe(initial.nativeId); expect(returned.view).toBe('chat');
      completed = true; return;
    }
    if (scenario === 'logout-reader') {
      secondPage = await otherOwner!.newPage();
      await secondPage.goto(`/sessions/${session}`);
      const readerPanel = secondPage.getByRole('region', { name: 'Shared session', exact: true });
      await expect(readerPanel).toBeVisible();
      await readerPanel.getByRole('button', { name: 'Terminal', exact: true }).click();
      const screen = readerPanel.getByRole('log', { name: 'Native Terminal screen', exact: true });
      await expect(screen).toContainText('abc');
      await expect(readerPanel.getByRole('textbox', { name: 'Terminal typing', exact: true })).toHaveCount(0);
      const loggedOut = await secondPage.request.post('/api/auth/logout', { headers: await csrfHeaders(secondPage) });
      expect(loggedOut.ok(), await loggedOut.text()).toBe(true);
      await expect.poll(async () => await screen.count() === 0 || await screen.textContent() === '', { timeout: 10_000 }).toBe(true);
      const denied = await secondPage.request.get(`/api/sessions/${session}/relay`);
      expect(denied.status()).toBe(401);
      expect(await denied.text()).not.toContain('First isolated fixture turn');
      const observed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(observed.owner_process).toBe(processed.owner_process); expect(observed.runId).toBe(processed.runId);
      expect(observed.nativeId).toBe(initial.nativeId); expect(observed.active).toBe(1);
      expect(observed.admitted).toBe(submittedSequence); expect(observed.processed).toBe(submittedSequence);
    }
    if (scenario === 'second-person') {
      const firstSnapshot = await (await requests.get(`/api/sessions/${session}/relay`)).json();
      const firstActor = firstSnapshot.receipts.find((row: RelayReceipt) => row.method === 'turn.submit').actor.principal;
      secondPage = await otherOwner!.newPage();
      await secondPage.goto(`/sessions/${session}`);
      const otherPanel = secondPage.getByRole('region', { name: 'Shared session', exact: true });
      await expect(otherPanel).toBeVisible();
      await expect(otherPanel.getByRole('button', { name: 'Connect Terminal input', exact: true })).toHaveCount(0);
      await otherPanel.getByRole('button', { name: 'Chat', exact: true }).click();
      const otherMessage = otherPanel.getByRole('textbox', { name: 'Message', exact: true });
      await expect(otherMessage).toBeEditable({ timeout: 20_000 }); await expect(otherMessage).toHaveValue('');
      const transferred = await proveNativeClaudeChatReceipt(secondPage, session!, 'First second-person isolated fixture turn', 1);
      expect(transferred.actor.principal).not.toBe(firstActor);
      expect((await requests.get(`/api/sessions/${session}/relay/operations/${transferred.operationId}`)).status()).toBe(404);
      await expect(panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true })).toHaveAttribute('readonly', '');
      await panel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(message).toHaveAttribute('readonly', '');
      await expect(otherPanel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch');
      await otherPanel.getByRole('button', { name: 'Terminal', exact: true }).click();
      const otherConnect = otherPanel.getByRole('button', { name: 'Connect Terminal input', exact: true });
      await expect(otherConnect).toBeEnabled({ timeout: 20_000 }); await otherConnect.click();
      const otherTyping = otherPanel.getByRole('textbox', { name: 'Terminal typing', exact: true });
      await expect(otherTyping).toBeEnabled({ timeout: 20_000 }); await expect(otherTyping).toHaveValue('');
      const otherSubmittedSequence = await typeNativeClaudeFrames(secondPage, session!, otherTyping);
      await expect(otherTyping).toHaveValue('', { timeout: 30_000 }); await expect(otherTyping).toBeEnabled();
      const otherProcessed = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(otherProcessed.admitted).toBe(otherSubmittedSequence); expect(otherProcessed.processed).toBe(otherSubmittedSequence);
      expect(otherProcessed.nativeId).toBe(initial.nativeId);
      await expect(otherPanel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch');
      await otherPanel.getByRole('button', { name: 'Chat', exact: true }).click();
      await expect(otherMessage).toBeEditable({ timeout: 20_000 }); await expect(otherMessage).toHaveValue('');
      const returned = await proveNativeClaudeChatReceipt(secondPage, session!, 'Final second-person isolated fixture turn', 3);
      expect(returned.actor.principal).toBe(transferred.actor.principal);
      const final = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(final.nativeId).toBe(initial.nativeId); expect(final.view).toBe('chat');
      completed = true; return;
    }
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    await expect(panel.getByRole('textbox', { name: 'Message', exact: true, includeHidden: true })).toHaveAttribute('readonly', '');
    await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).view, { timeout: 20_000 }).toBe('chat');
    await expect(message).not.toHaveAttribute('readonly', '', { timeout: 15_000 });
    if (scenario === 'retained-run') {
      const draft = 'Keep current Chat draft while reading the retired native run';
      await message.fill(draft);
      const choice = panel.getByRole('combobox', { name: 'Retained run', exact: true });
      await choice.selectOption(terminal.runId);
      const past = panel.getByRole('region', { name: 'Past run history', exact: true });
      await expect(past.getByRole('log', { name: 'Native Terminal screen' })).toContainText('First isolated fixture turn');
      await expect(panel.getByRole('textbox', { name: 'Terminal typing', exact: true })).toHaveCount(0);
      await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveCount(0);
      const events = await requests.get(`/api/sessions/${session}/relay/events?runId=${terminal.runId}`);
      expect(events.ok(), await events.text()).toBe(true);
      const received = await events.json();
      expect(received.events.length).toBeGreaterThan(0);
      expect(received.events.every((event: { runId: string }) => event.runId === terminal.runId)).toBe(true);
      const runs = await (await requests.get(`/api/sessions/${session}/relay/runs`)).json();
      expect(runs.currentRunId).not.toBe(terminal.runId);
      expect(runs.runs.find((run: { runId: string }) => run.runId === terminal.runId).exitOutcome).toBe('unknown');
      await choice.selectOption('');
      await expect(message).toHaveValue(draft);
      test.info().annotations.push({ type: 'claude_retained_run', description: 'distinct-native-TUI-exit-SDK-resume-durable-screen-received-events-unknown-exit-current-draft-preserved' });
    }
    if (scenario === 'chat-receipt') expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: afterWindow || stopUnknown ? { held: true, long: true } : { held: true } })).ok()).toBe(true);
    const settled = await proveNativeClaudeChatReceipt(page, session!, 'Second isolated fixture turn', 3,
      scenario !== 'chat-receipt' ? undefined : async original => {
        expect(original.state).toBe('uncertain');
        await expect.poll(async () => (await (await requests.get('/e2e-test/relay-claude/state', { headers })).json()).model_held, { timeout: 15_000 }).toBe(true);
        await page.waitForTimeout(5500); // deliberate native response hold beyond the old assertion's deadline
        const held = await (await requests.get(`/api/sessions/${session}/relay/operations/${original.operationId}`)).json();
        expect(held.state).toBe('uncertain'); expect(held.payloadDigest).toBe(original.payloadDigest);
        expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
      });
    if (scenario === 'chat-receipt') {
      const evidence = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
      expect(evidence.sdk_progress.filter((row: { operationId: string; kind: string; phase: string }) =>
        row.operationId === settled.operationId && row.kind === 'operation.accepted' && row.phase === 'done')).toHaveLength(1);
    }
    const final = await (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    expect(final.nativeId).toBe(initial.nativeId); expect(final.view).toBe('chat');
    expect(final.terminalMediated).toBe(false); completed = true;
  } catch (failure) {
    console.error('Offline Claude primary failure:', (failure as Error).message);
    const diagnostic = await requests.get('/e2e-test/relay-claude/state', { headers }).catch(() => undefined);
    console.error('Offline Claude native diagnostic:', diagnostic ? await diagnostic.text() : 'unavailable');
    throw failure;
  } finally {
    if (scenario === 'revoke-allow' && secondPage) {
      requests = secondPage.request; headers = { ...await csrfHeaders(secondPage), 'x-e2e-isolation': fixture.isolation_token };
    }
    if (scenario === 'take-busy' && session) await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } });
    if (session && !stoppedEarly) {
      const stopped = await requests.post(`/api/sessions/${session}/stop`, { headers });
      expect(stopped.ok(), await stopped.text()).toBe(true);
      const detail = await (await requests.get(`/api/sessions/${session}`)).json();
      expect(detail.last_error ?? '').not.toContain('stop not confirmed');
    }
    await secondPage?.close();
    await page.close();
    const finished = await requests.post('/e2e-test/relay-claude/finish', { headers });
    expect(finished.ok(), await finished.text()).toBe(true);
    const result = await finished.json();
    if (session) {
      expect(result.cli_version).toBe('2.1.308 (Claude Code)');
      if (completed) expect(result.tui.empty_tree_records).toHaveLength(scenario === 'second-person' ? 5 : scenario.startsWith('sdk-') ? 1 : stopUnknown || scenario === 'revoke-allow' ? 2 : 3);
      expect(result.tui.empty_tree_records.every((record: { empty: boolean }) => record.empty)).toBe(true);
      expect(result.tui.terminalMediated).toBe(false);
      console.log('Native Claude completion:', JSON.stringify({ scenario, cli_version: result.cli_version,
        cli_digest: result.cli_digest, empty_tree_records: result.tui.empty_tree_records, terminalMediated: false }));
    }
  }
}

for (const scenario of ['warm', 'retained-run', 'read-race', 'reconnect-reader', 'chat-receipt', 'compact', 'permission', 'allow', 'sdk-permission', 'sdk-allow', 'sdk-first-deny', 'sdk-late', 'sdk-window', 'sdk-window-allow', 'late-permission', 'window-permission', 'window-allow', 'sdk-stop-unknown', 'sdk-stop-allow', 'stop-unknown', 'stop-allow', 'reload-window-permission', 'reload-window-allow', 'sdk-stop-repeat', 'sdk-stop-repeat-allow', 'stop-repeat', 'stop-repeat-allow', 'sdk-cache', 'sdk-cache-allow', 'cache-permission', 'cache-allow', 'take-busy', 'guard']) test(`offline native Claude: public person new-session Chat Terminal Chat keeps one native id (${scenario})`, { tag: '@relay-native' }, async ({ page }) => {
  await nativeClaudeBrowser(page, scenario);
});
test('offline native Claude: public person new-session Chat Terminal Chat keeps one native id (second-person)', { tag: '@relay-native' }, async ({ page, otherOwner }) => {
  await nativeClaudeBrowser(page, 'second-person', otherOwner);
});

test('offline native Claude: public reader sign-out clears screen without transferring input (logout-reader)', { tag: '@relay-native' }, async ({ page, otherOwner }) => {
  await nativeClaudeBrowser(page, 'logout-reader', otherOwner);
});

test('offline native Claude: writer sign-out refuses new frame and another person recovers at idle (writer-logout)', { tag: '@relay-native' }, async ({ page, otherOwner }) => {
  const writer = await otherOwner.newPage();
  await nativeClaudeBrowser(writer, 'writer-logout', page.context());
});

test('offline native Claude: revocation after Allow verification denies execution (revoke-allow)', { tag: '@relay-native' }, async ({ page, otherOwner }) => {
  const writer = await otherOwner.newPage();
  await nativeClaudeBrowser(writer, 'revoke-allow', page.context());
});


test('Claude browser input accepts its fresh claim epoch and retires without replay on authority loss', async ({ page }) => {
  const data = await terminalInputFixture(page, undefined, 'claude');
  expect(data.claims()).toBe(0);
  await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  await typing.press('a'); await typing.press('Enter');
  await expect.poll(() => data.writes.length).toBe(2);
  expect(data.writes.every(frame => frame.inputEpoch === 2)).toBe(true);
  data.process(2);
  await expect(typing).toHaveValue(''); await expect(typing).toBeEnabled();
  await page.waitForTimeout(2200); // Panel/screen polling must retain this same grant generation.
  await expect(typing).toBeEnabled(); expect(data.claims()).toBe(1);
  await typing.press('b'); await expect.poll(() => data.writes.length).toBe(3);
  data.execution.available = false;
  const retired = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
  await expect(retired).toHaveValue('b'); await expect(retired).toHaveAttribute('readonly', '');
  await retired.press('c'); await page.waitForTimeout(700);
  expect(data.writes).toHaveLength(3); expect(data.claims()).toBe(1);
});

test('Claude selection uses ownership revision and reattaches only after an accepted switch', async ({ page }) => {
  const data = await fake(page, 'claude');
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    calls.push(route.request().postDataJSON()); await route.fallback();
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Chat', exact: true })).toBeEnabled();
  expect(calls.filter(call => call.method.startsWith('ry/switch.'))).toHaveLength(0);
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect.poll(() => calls.filter(call => call.method === 'ry/session.attach').length).toBe(2);
  expect(calls.filter(call => call.method === 'ry/switch.prepare')).toHaveLength(1);
  expect(calls.find(call => call.method === 'ry/switch.prepare')?.params.expectedRevision).toBe(7);
  expect(calls.filter(call => call.method === 'ry/switch.commit')).toHaveLength(1);
  const clients = calls.filter(call => call.method === 'ry/initialize').map(call => call.params.clientId);
  expect(clients).toHaveLength(2); expect(clients[0]).not.toBe(clients[1]);
  expect(calls.some(call => call.method === 'ry/control.claim')).toBe(false);
});

for (const state of ['submitting', 'uncertain'] as const) test(`Claude pending permission remains reviewable during its exact active turn ${state}`, async ({ page }) => {
  const data = await fake(page, 'claude');
  Object.assign(data.snapshot.capabilities, { 'decision.tool_approval': 'qualified' });
  const execution = { view: 'terminal', phase: 'owned', mine: true, available: true, inputEpoch: 1,
    activeOperationId: 'active-person-turn' };
  Object.assign(data.snapshot, { execution, pendingDecisions: [{ decisionId: 'active-tool', runId: 'fake-run',
    actionDigest: 'sha256:' + 'a'.repeat(64), description: 'Artificial active native permission.', revision: 0,
    expiresAtMs: Date.now() + 60_000, status: 'pending', choices: [{ id: 'decline', label: 'Decline', effect: 'deny' }] }] });
  data.snapshot.receipts.push({ operationId: 'active-person-turn', sessionId: data.id, runId: 'fake-run', method: 'turn.submit',
    payloadDigest: 'sha256:' + 'b'.repeat(64), state, revision: 1, actor: { kind: 'person', principal: 'Example person', channel: 'chat' } });
  await page.route(`**/api/sessions/${data.id}/relay/ownership?**`, route => route.fulfill({ json: { execution,
    selector: data.snapshot.selector, ownershipRevision: data.snapshot.ownershipRevision } }));
  await page.goto(`/sessions/${data.id}`);
  const decline = page.getByRole('region', { name: 'Review decision', exact: true }).getByRole('button', { name: 'Decline', exact: true });
  await expect(decline).toBeEnabled();
  data.snapshot.receipts[0].runId = 'other-run'; await expect(decline).toBeDisabled();
  data.snapshot.receipts[0].runId = 'fake-run'; await expect(decline).toBeEnabled();
  data.snapshot.receipts.push({ ...data.snapshot.receipts[0], operationId: 'unresolved-other-answer', method: 'decision.reply', state: 'uncertain' });
  await expect(decline).toBeDisabled();
  expect(data.methods.includes('ry/decision.reply')).toBe(false);
});

for (const profile of ['current', 'stale-run', 'rollback', 'expired'] as const) test(`Inbox native approval uses shipped card and one hub answer (${profile})`, async ({ page }) => {
  const data = await fake(page, 'claude');
  const decision = { decisionId: 'inbox-native-review', runId: 'fake-run', actionDigest: 'sha256:' + 'a'.repeat(64),
    description: 'Review native tool action', revision: 3, expiresAtMs: Date.now() + (profile === 'expired' ? -1 : 60_000),
    status: 'pending', choices: [{ id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'decline', label: 'Deny', effect: 'deny' }] };
  Object.assign(data.snapshot, { pendingDecisions: [decision], capabilities: { 'decision.tool_approval': 'qualified' } });
  if (profile === 'stale-run') data.snapshot.runId = 'replacement-run';
  const stamp = new Date().toISOString();
  await page.route('**/api/attention*', route => route.fulfill({ json: [{ id: 9991, kind: 'needs_input', severity: 'warn',
    source: 'session_relay', title: 'Review native tool action', session_id: data.id, created_at: stamp, updated_at: stamp,
    acked_at: null, resolved_at: null, relay_decision: decision, relay_decision_available: true, relay_allow_enabled: profile !== 'rollback',
    approval_card: { command: 'echo artificial', cwd: '/tmp/example', diff: null, tool_name: 'Bash', label: 'No specific hint' },
    permission_input: '{"command":"echo artificial"}' }] }));
  const answers: Record<string, unknown>[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method !== 'ry/decision.reply') return route.fallback();
    answers.push(command.params);
    await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: { outcome: 'unknown' } } });
  });
  await page.goto('/inbox');
  const card = page.getByRole('region', { name: 'CLI approval details', exact: true });
  await expect(card.getByLabel('CLI command', { exact: true })).toHaveText('echo artificial');
  await expect(card.getByLabel('CLI working directory')).toHaveText('/tmp/example');
  const actions = page.getByLabel('Native review actions');
  if (profile === 'expired') {
    await expect(actions.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  } else {
    if (profile === 'rollback') await expect(actions.getByRole('button', { name: 'Allow once', exact: true })).toBeDisabled();
    await actions.getByRole('button', { name: 'Deny', exact: true }).click();
    await expect(actions).toContainText(profile === 'stale-run' ? 'needs fresh review' : 'outcome is unknown');
    expect(answers).toHaveLength(profile === 'stale-run' ? 0 : 1);
    if (answers.length) expect(answers[0]).toMatchObject({ expectedRunId: 'fake-run', decisionId: decision.decisionId,
      actionDigest: decision.actionDigest, choiceId: 'decline', expectedRevision: 3 });
    await expect(actions.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  }
  expect(data.methods).not.toContain('ry/control.claim');
  expect(data.methods).not.toContain('ry/terminal.input');
});

test('Claude generic review sends granting choices to the detailed Inbox card', async ({ page }) => {
  const data = await fake(page, 'claude');
  Object.assign(data.snapshot, { execution: { view: 'terminal', phase: 'owned', mine: true, available: true, inputEpoch: 1 },
    capabilities: { 'decision.tool_approval': 'qualified' }, pendingDecisions: [{ decisionId: 'detailed-review', runId: 'fake-run',
      actionDigest: 'sha256:' + 'a'.repeat(64), description: 'Review a native tool action.', revision: 0,
      expiresAtMs: Date.now() + 60_000, status: 'pending', choices: [
        { id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'decline', label: 'Deny', effect: 'deny' }] }] });
  await page.route(`**/api/sessions/${data.id}/relay/ownership?**`, route => route.fulfill({ json: { execution: {
    view: 'terminal', phase: 'owned', mine: true, available: true, inputEpoch: 1 }, selector: data.snapshot.selector } }));
  await page.goto(`/sessions/${data.id}`);
  const review = page.getByRole('region', { name: 'Review decision', exact: true });
  await expect(review.getByRole('button', { name: 'Allow once', exact: true })).toBeDisabled();
  await expect(review.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled();
  await expect(review.getByRole('link', { name: 'Review full tool details in Inbox before allowing.' })).toHaveAttribute('href', '/inbox');
});

test('public interrupt binds the visible turn and control revision and labels requested truthfully', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot, { activeTurnId: 'active-person-turn', writer: {
    view: 'chat', holder: { kind: 'person', principal: 'fixture', channel: 'chat' },
    revision: 1, active: true, available: true, mine: true,
  } });
  Object.assign(data.snapshot.capabilities, { interrupt: 'qualified' });
  const submitted: Record<string, unknown>[] = [];
  const receipt: RelayReceipt = { operationId: randomUUID(), sessionId: data.id, runId: 'fake-run',
    method: 'turn.interrupt', payloadDigest: 'sha256:' + 'a'.repeat(64),
    actor: { kind: 'person', principal: 'fixture', channel: 'chat' }, state: 'queued', revision: 0 };
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method === 'ry/operation.register' && command.params.method === 'turn.interrupt') {
      await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: { operationId: receipt.operationId, receipt } } });
    } else if (command.method === 'ry/turn.interrupt') {
      submitted.push(command.params);
      receipt.state = 'accepted'; receipt.revision++;
      data.snapshot.receipts.push(receipt);
      await route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: receipt } });
    } else await route.fallback();
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const interrupt = panel.getByRole('button', { name: 'Interrupt', exact: true });
  await expect(interrupt).toBeEnabled();
  await interrupt.click();
  await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('Interrupt requested.');
  expect(submitted).toHaveLength(1);
  expect(submitted[0]).toMatchObject({ turnId: 'active-person-turn', expectedControlRevision: 1, expectedRunId: 'fake-run' });
  Object.assign(data.snapshot, { writer: { view: 'chat', revision: 1, active: true, available: true, mine: false } });
  await expect(interrupt).toBeDisabled();
  expect(submitted).toHaveLength(1);
});

test('retained run selection is read-only and preserves the current unsent draft', async ({ page }) => {
  const data = await fake(page, 'codex');
  data.snapshot.runId = '900';
  Object.assign(data.snapshot, { writer: { view: 'chat', revision: 1, active: true, available: true, mine: true } });
  await page.route(`**/api/sessions/${data.id}/relay/runs`, route => route.fulfill({ json: {
    currentRunId: '900', runs: [{ runId: '1', exitOutcome: 'normal' }, { runId: '2', exitOutcome: 'abnormal' }], more: false,
  } }));
  await page.route(`**/api/sessions/${data.id}/relay/events?*`, route => {
    const run = new URL(route.request().url()).searchParams.get('runId');
    return route.fulfill({ json: { events: [{ kind: 'message.upsert', sessionId: data.id,
      payload: { itemId: `past-${run}`, role: 'assistant', text: `Retained message for run ${run}` } }], cursor: `past-${run}`, more: false } });
  });
  await page.route(`**/api/sessions/${data.id}/relay/terminal?*`, route => {
    const run = new URL(route.request().url()).searchParams.get('expectedRunId');
    return route.fulfill({ json: { runId: run, text: `Native screen for run ${run}`, truncated: false } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const draft = panel.getByRole('textbox', { name: 'Message', exact: true });
  await expect(draft).toBeEditable(); await draft.fill('Keep this current unsent draft');
  const choice = panel.getByRole('combobox', { name: 'Retained run', exact: true });
  await choice.selectOption('1');
  const past = panel.getByRole('region', { name: 'Past run history', exact: true });
  await expect(past.getByRole('log', { name: 'Past run conversation' })).toContainText('Retained message for run 1');
  await expect(past.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Native screen for run 1');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveCount(0);
  await choice.selectOption('2');
  await expect(past.getByRole('log', { name: 'Past run conversation' })).toContainText('Retained message for run 2');
  await expect(past.getByRole('log', { name: 'Past run conversation' })).not.toContainText('run 1');
  await choice.selectOption('');
  await expect(draft).toHaveValue('Keep this current unsent draft');
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/writer.take', 'ry/turn.submit', 'ry/control.claim'].includes(method))).toEqual([]);
});

test('late retained pagination cannot overwrite a fresh run selection', async ({ page }) => {
  const data = await fake(page, 'codex'); data.snapshot.runId = '900';
  Object.assign(data.snapshot, { writer: { view: 'chat', revision: 1, active: true, available: true, mine: true } });
  let release!: () => void; let held = false; let fresh = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/sessions/${data.id}/relay/runs`, route => route.fulfill({ json: {
    currentRunId: '900', runs: [{ runId: '1', exitOutcome: 'normal' }, { runId: '2', exitOutcome: 'normal' }], more: false,
  } }));
  await page.route(`**/api/sessions/${data.id}/relay/terminal?*`, async route => {
    const run = new URL(route.request().url()).searchParams.get('expectedRunId');
    const stale = run === '1' && !fresh;
    if (stale) await pending;
    return route.fulfill({ json: { runId: run, text: stale ? 'Stale secret screen' : `Fresh screen ${run}`, truncated: false } });
  });
  await page.route(`**/api/sessions/${data.id}/relay/events?*`, async route => {
    const query = new URL(route.request().url()).searchParams; const run = query.get('runId');
    if (query.get('cursor') === 'old-next') { held = true; await pending; }
    const text = query.get('cursor') ? 'Stale secret from run 1' : run === '2' ? 'Only run 2' : fresh ? '[REDACTED] fresh history' : 'Initial run 1';
    await route.fulfill({ json: { events: [{ kind: 'message.upsert', sessionId: data.id,
      payload: { itemId: text, role: 'assistant', text } }], cursor: 'old-next', more: !fresh && run === '1' } });
  });
  try {
    await page.goto(`/sessions/${data.id}`);
    const panel = page.getByRole('region', { name: 'Shared session', exact: true });
    const select = panel.getByRole('combobox', { name: 'Retained run', exact: true });
    await select.selectOption('1');
    const conversation = panel.getByRole('log', { name: 'Past run conversation' });
    await expect(conversation).toContainText('Initial run 1');
    await panel.getByRole('button', { name: 'Load more retained history' }).click();
    await expect.poll(() => held).toBe(true);
    fresh = true; data.snapshot.historyEpoch++;
    await expect(conversation).toContainText('[REDACTED] fresh history');
    await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toHaveText('Fresh screen 1');
    await select.selectOption('2'); await expect(conversation).toHaveText('assistant: Only run 2');
    release(); await page.waitForTimeout(100);
    await expect(conversation).toHaveText('assistant: Only run 2');
    await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toHaveText('Fresh screen 2');
    expect(data.methods.some(method => ['ry/turn.submit', 'ry/writer.take', 'ry/control.claim'].includes(method))).toBe(false);
  } finally { release(); }
});

for (const mode of ['accepted', 'unknown', 'other-tab', 'preview-off'] as const) test(`Terminal interrupt uses exact registered target without input bytes (${mode})`, async ({ page }) => {
  const data = await fake(page, 'codex', mode !== 'preview-off');
  Object.assign(data.snapshot.capabilities, { interrupt: 'qualified' });
  Object.assign(data.snapshot, { activeTurnId: 'terminal-active-operation', writer: {
    view: 'terminal', revision: 1, active: true, available: true, mine: mode !== 'other-tab' } });
  const registered: Record<string, unknown>[] = [], sent: Record<string, unknown>[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method === 'ry/operation.register' && command.params.method === 'turn.interrupt') {
      registered.push(command.params);
      return route.fulfill({ json: { jsonrpc: '2.0', id: command.id,
        result: { operationId: 'terminal-cancel', receipt: { state: 'queued' } } } });
    }
    if (command.method === 'ry/operation.get') return route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: data.snapshot.receipts[0] } });
    if (command.method !== 'ry/turn.interrupt') return route.fallback();
    sent.push(command.params);
    const receipt = { operationId: 'terminal-cancel', method: 'turn.interrupt', state: mode === 'unknown' ? 'uncertain' : 'accepted',
      revision: 1, actor: { kind: 'person', principal: 'Fixture person', channel: 'chat' } };
    Object.assign(data.snapshot, { receipts: [receipt] });
    return route.fulfill({ json: { jsonrpc: '2.0', id: command.id, result: receipt } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const interrupt = panel.getByRole('button', { name: 'Interrupt Terminal turn', exact: true });
  if (['other-tab', 'preview-off'].includes(mode)) { await expect(interrupt).toBeDisabled(); return; }
  await expect(interrupt).toBeEnabled(); await interrupt.focus(); await page.keyboard.press('Space');
  await expect.poll(() => sent.length).toBe(1); expect(registered).toHaveLength(1);
  expect(sent[0]).toMatchObject({ expectedRunId: 'fake-run', turnId: 'terminal-active-operation', expectedControlRevision: 1, operationId: 'terminal-cancel' });
  await expect(panel.getByRole('list', { name: 'Terminal interrupt receipts' })).toContainText(mode === 'unknown' ? 'Interrupt outcome unknown.' : 'Interrupt requested.');
  if (mode === 'unknown') {
    await expect(interrupt).toBeDisabled(); await page.waitForTimeout(2500);
    await expect(interrupt).toBeDisabled(); expect(sent).toHaveLength(1);
  }
  expect(data.methods.some(method => ['ry/control.claim', 'ry/terminal.input', 'ry/writer.take', 'ry/turn.submit'].includes(method))).toBe(false);
});


for (const moveFocus of [false, true]) test(`Terminal autofocus survives a late pending snapshot; respects person focus ${moveFocus}`, async ({ page, _drainRoutes }) => {
  const data = await terminalInputFixture(page);
  await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  data.snapshot.receipts.push({ operationId: 'late-claim', sessionId: data.id, runId: 'fake-run', method: 'control.claim',
    payloadDigest: 'sha256:' + 'a'.repeat(64), actor: { kind: 'person', principal: 'Example person', channel: 'chat' }, state: 'submitting', revision: 1 });
  await expect(typing).toBeDisabled();
  const other = data.panel.getByRole('button', { name: 'Chat', exact: true });
  if (moveFocus) await other.focus();
  data.snapshot.receipts.splice(0);
  await expect(typing).toBeEnabled();
  if (moveFocus) { await expect(other).toBeFocused(); await expect(typing).not.toBeFocused(); }
  else {
    await expect(typing).toBeFocused();
    await page.keyboard.type('abc'); await expect(typing).toHaveValue('abc');
    await expect.poll(() => data.writes.map(value => atob(value.data as string)).join('')).toBe('abc');
  }
});

for (const location of ['session', 'grid'] as const) test(`Claude ${location}: restored view and read reconnect never request ownership`, async ({ page }) => {
  const data = await fake(page, 'claude');
  const launches: string[] = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && /^\/api\/sessions(?:$|\/[^/]+\/(?:start|resume|restart)$)/.test(path)) launches.push(path);
  });
  await page.route('**/api/session-relay/preference', route => route.fulfill({ json: { view: 'chat', revision: 3 } }));
  const clientIds: unknown[] = [];
  let reset = false;
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method === 'ry/initialize') clientIds.push(command.params.clientId);
    if (command.method === 'ry/session.snapshot' && reset) {
      reset = false;
      return route.fulfill({ json: { error: { message: 'Read binding expired', data: { code: 'NOT_FOUND_OR_DENIED' } } } });
    }
    return route.fallback();
  });
  await page.goto(location === 'grid' ? `/grid?ids=${data.id}` : `/sessions/${data.id}?tab=terminal`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
  await expect.poll(() => data.methods.filter(m => m === 'ry/session.attach').length).toBe(1);
  reset = true;
  await expect.poll(() => clientIds.length, { timeout: 15000 }).toBe(2);
  await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeEnabled();
  await expect.poll(() => data.methods.filter(m => m === 'ry/session.attach').length).toBe(2);
  expect(clientIds[0]).toBeTruthy();
  expect(clientIds[1]).toBe(clientIds[0]);
  expect(launches).toEqual([]);
  expect(data.methods.filter(m => m.startsWith('ry/switch.') || m === 'ry/operation.register' || m === 'ry/writer.take' || m === 'ry/control.claim')).toEqual([]);
});

const nativeSelectorRepeats = environmentValue('ACC_RELAY_NATIVE_REPEATS') === '20' ? 20 : 1;
for (let nativeRepeat = 1; nativeRepeat <= nativeSelectorRepeats; nativeRepeat++) test(`native retained owner ${nativeRepeat}: Grid deep-link preference reconnect never launch or claim`, { tag: '@relay-native' }, async ({ page }, testInfo) => {
  const data = await fake(page, 'codex');
  const bridge = await nativeRelayBridge(data.id);
  const launches: string[] = []; const methods: string[] = []; const clientIds: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /^\/api\/sessions(?:$|\/[^/]+\/(?:start|resume|restart)$)/.test(new URL(request.url()).pathname)) launches.push(request.url());
  });
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const request = route.request(); const command = request.postDataJSON();
    methods.push(command.method);
    if (command.method === 'ry/initialize') clientIds.push(command.params.clientId);
    const reply = await bridge.call('POST', new URL(request.url()).pathname, command, request.headers());
    await route.fulfill({ status: reply.status, json: reply.body });
  });
  await page.route('**/api/session-relay/preference', async route => {
    const request = route.request();
    const reply = await bridge.call(request.method(), '/api/session-relay/preference', request.postDataJSON());
    await route.fulfill({ status: reply.status, json: reply.body });
  });
  async function navigate(path: string) {
    await page.evaluate(path => { history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')); }, path);
  }
  try {
    expect((await bridge.call('PUT', '/api/session-relay/preference', { view: 'chat', expectedRevision: 0 })).status).toBe(200);
    await page.goto(`/sessions/${data.id}?tab=terminal`);
    const panel = page.getByRole('region', { name: 'Shared session', exact: true });
    await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
    // This retained owner declares no input capability or writer grant. Local
    // editing is covered by the separately qualified draft-retention cases.
    await expect(panel.getByRole('textbox', { name: 'Message' })).not.toBeEditable();
    await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    await expect(panel.getByRole('textbox', { name: 'Message' })).not.toBeEditable();
    await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    expect(clientIds).toHaveLength(1);
    await bridge.reset();
    await expect.poll(() => clientIds.length, { timeout: 15000 }).toBe(2);
    expect(clientIds[1]).toBe(clientIds[0]);
    await expect(panel.getByRole('textbox', { name: 'Message' })).not.toBeEditable();
    await navigate(`/grid?ids=${data.id}`);
    await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
    await expect.poll(() => clientIds.length).toBe(3);
    await expect(panel.getByRole('textbox', { name: 'Message' })).not.toBeEditable();
    await expect(panel.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    await navigate(`/sessions/${data.id}?tab=terminal`);
    await expect(panel.getByRole('textbox', { name: 'Message' })).toBeVisible();
    // The application retains the session panel across Grid navigation. Going
    // back must reuse it, rather than create another observer connection.
    await expect.poll(() => clientIds.length).toBe(3);
    expect(new Set(clientIds).size).toBe(2);
    expect(launches).toEqual([]);
    expect(methods.filter(method => method.startsWith('ry/switch.') ||
      ['ry/operation.register', 'ry/control.claim', 'ry/writer.take', 'ry/terminal.input'].includes(method))).toEqual([]);
    await navigate('/sessions');
    await expect(panel).toHaveCount(0);
    await page.close();
    const proof = await bridge.stop();
    expect(proof.productionHub && proof.oneRetainedOwner && proof.ownedTUIRetained && proof.noInputClaimOrOperations).toBe(true);
    expect(proof.productionOwnerStopped).toBe(true);
    await testInfo.attach('retained-native-selector-proof', { body: JSON.stringify(proof), contentType: 'application/json' });
  } finally { await bridge.abort(); }
});

for (const provider of ['codex', 'claude'] as const) for (const enabled of [true, false]) test(`${provider}: shared decision review in both views with preview ${enabled}`, async ({ page }) => {
  const data = await fake(page, provider, enabled, true);
  const snapshot = data.snapshot as unknown as import('../src/lib/sessionRelayTypes').RelaySnapshot;
  snapshot.capabilities['decision.tool_approval'] = 'qualified';
  if (snapshot.selector) snapshot.selector.may_control = false; // changing view grants no owner/input
  const card = (id: string) => ({ decisionId: id, runId: 'fake-run', actionDigest: 'sha256:' + 'a'.repeat(64),
    description: 'Tool: Bash\nExact input:\n{"command":"artificial review"}', revision: 0, expiresAtMs: Date.now() + 60_000,
    status: 'pending', choices: [{ id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'decline', label: 'Deny', effect: 'deny' }] });
  snapshot.pendingDecisions = [card('first-question')];
  const answers: Record<string, unknown>[] = [];
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    const command = route.request().postDataJSON();
    if (command.method === 'ry/operation.get') return route.fulfill({ json: { result: snapshot.receipts.find(r => r.operationId === command.params.operationId) } });
    if (command.method !== 'ry/decision.reply') return route.fallback();
    answers.push(command.params);
    Object.assign(snapshot.pendingDecisions.find(d => d.decisionId === command.params.decisionId)!, {
      status: 'outcome_unknown', revision: 2, choiceId: command.params.choiceId,
      winner: { kind: 'person', principal: 'Example person', channel: 'chat' } });
    const receipt: RelayReceipt = { operationId: command.params.operationId, sessionId: data.id, runId: 'fake-run',
      method: 'decision.reply', payloadDigest: await operationDigest('decision.reply', command.params), actor: { kind: 'person', principal: 'Example person', channel: 'chat' }, state: 'uncertain', revision: 2 };
    snapshot.receipts.push(receipt);
    return route.fulfill({ json: { result: receipt } });
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const first = panel.locator('section[aria-label="Review decision"]').first();
  await expect(first).toBeVisible(); // initial preferred view is Terminal
  await expect(first.getByRole('button', { name: 'Allow once', exact: true }))[enabled ? 'toBeEnabled' : 'toBeDisabled']();
  await expect(first.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(first).toBeVisible();
  await first.getByRole('button', { name: 'Deny', exact: true }).click();
  await expect(first.getByText('Answer recorded; the native outcome is unknown. It will not be replayed.')).toBeVisible();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  expect(answers).toHaveLength(1);
  snapshot.pendingDecisions.push(card('second-question'));
  const second = panel.locator('section[aria-label="Review decision"]').nth(1);
  await expect(second).toBeVisible();
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled(); // unrelated unknown answer doesn't authorize or block this exact new question
  const previous = snapshot.receipts.find(receipt => receipt.method === 'decision.reply')!;
  const original = { payloadDigest: previous.payloadDigest, actor: previous.actor, runId: previous.runId };
  previous.payloadDigest = 'sha256:' + 'f'.repeat(64);
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  previous.payloadDigest = original.payloadDigest;
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled();
  previous.actor = { ...original.actor, principal: 'Different reviewer' };
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  previous.actor = original.actor; previous.runId = 'different-run';
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeDisabled();
  previous.runId = original.runId;
  await expect(second.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled();
  await second.getByRole('button', { name: 'Deny', exact: true }).click();
  await expect(second.getByText('Answer recorded; the native outcome is unknown. It will not be replayed.')).toBeVisible();
  expect(answers.map(answer => answer.decisionId)).toEqual(['first-question', 'second-question']);
  expect(answers.every(answer => answer.choiceId === 'decline' && answer.expectedRevision === 0 && answer.actionDigest === 'sha256:' + 'a'.repeat(64))).toBe(true);
  expect(data.methods.some(method => method.startsWith('ry/switch.') || ['ry/control.claim','ry/writer.take','ry/terminal.input'].includes(method))).toBe(false);
});

for (const exit of ['handover', 'abnormal', 'unknown'] as const) test(`R1 ${exit} recovery opens a new-session form without replay or re-enrollment`, async ({ page }) => {
  const data = await fake(page, 'claude');
  const execution = { view: 'chat', phase: 'owned', mine: false, available: false, inputEpoch: 1, waitingForFirstChat: true };
  Object.assign(data.snapshot, { execution });
  await page.route(`**/api/sessions/${data.id}/relay/ownership?*`, route => route.fulfill({ json: { execution } }));
  Object.assign(data.snapshot, exit === 'handover'
    ? { l1: { owner: 'terminal', phase: 'recovery_required', revision: 151, available: false, recoveryAvailable: true } }
    : { exitOutcome: exit, finalEventsDrained: true });
  data.snapshot.l1 = { owner: 'chat', phase: 'owned', revision: 150, available: false, ...data.snapshot.l1,
    terminalWaitingForChat: true, terminalWaitingReason: 'Retired first Chat wait must not hide old output.' };
  data.snapshot.receipts.push({ operationId: 'old-receipt', sessionId: data.id, runId: 'old-run',
    method: 'turn.submit', payloadDigest: 'old-digest', actor: { kind: 'person', principal: 'alex', channel: 'chat' }, state: 'uncertain', revision: 1 });
  Object.assign(data.snapshot, { pendingDecisions: [{ decisionId: 'old-review', runId: 'old-run', actionDigest: 'old-action',
    description: 'Retained original decision', revision: 1, expiresAtMs: Date.now() + 60_000, status: 'pending',
    choices: [{ id: 'decline', label: 'Deny old request', effect: 'deny' }] }] });
  await page.route(`**/api/sessions/${data.id}/relay/terminal?*`, route => route.fulfill({ json: {
    runId: 'fake-run', text: 'Retained original output', truncated: false } }));
  let launches = 0;
  page.on('request', request => { if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') launches++; });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
  await expect(recovery).toContainText('receipts, output and decisions stay readable. Nothing is replayed.');
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(panel.getByRole('log', { name: 'Native Terminal screen', exact: true })).toContainText('Retained original output');
  await page.waitForTimeout(2300);
  await expect(panel.getByRole('log', { name: 'Native Terminal screen', exact: true })).toContainText('Retained original output');
  await expect(panel.getByText('Retained original decision', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Deny old request', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
  await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('turn.submit: Delivery unknown. Check the conversation before sending again.');
  await recovery.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
  const form = page.getByRole('region', { name: 'New session', exact: true });
  await expect(form).toBeVisible();
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  expect(launches).toBe(0);
  await form.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await expect(recovery).toBeVisible();
  await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('turn.submit: Delivery unknown. Check the conversation before sending again.');
  expect(data.snapshot.runId).toBe('fake-run');
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.recover', 'ry/turn.submit', 'ry/terminal.input'].includes(method))).toEqual([]);
});

test('Claude Terminal waits for the first real Chat message without touching the running session', async ({ page }) => {
  const data = await fake(page, 'claude');
  const l1 = { owner: 'chat', phase: 'owned', revision: 211, available: true, terminalWaitingForChat: true };
  Object.assign(data.snapshot, { l1 }); data.snapshot.selector.owner = 'chat';
  await page.goto(`/sessions/${data.id}?tab=terminal`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeDisabled();
  await expect(panel.getByText('Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.', { exact: true })).toBeVisible();
  const message = panel.getByRole('textbox', { name: 'Message', exact: true });
  await expect(message).toBeEditable(); await message.fill('Unsent first Chat draft');
  await page.waitForTimeout(2300);
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.prepare', 'ry/switch.commit', 'ry/turn.submit', 'ry/terminal.input'].includes(method))).toEqual([]);
  l1.terminalWaitingForChat = false;
  await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeEnabled();
  await expect(message).toHaveValue('Unsent first Chat draft');
  expect(data.snapshot.runId).toBe('fake-run');
});

test('R1 crash recovery opens a fresh new-session review without changing original evidence', async ({ page }) => {
  const data = await fake(page, 'claude');
  Object.assign(data.snapshot, { l1: { owner: 'terminal', phase: 'recovery_required', revision: 151, available: false, recoveryAvailable: true } });
  const originalRun = data.snapshot.runId;
  const originalEvidence = JSON.stringify([data.snapshot.receipts, data.snapshot.pendingDecisions]);
  let launches = 0;
  await page.route('**/api/sessions', async route => {
    if (route.request().method() === 'POST') launches++;
    await route.fallback();
  });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
  await expect(recovery).toContainText("This session's receipts, output and decisions stay readable. Nothing is replayed.");
  await recovery.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
  const launch = page.getByRole('region', { name: 'New session', exact: true });
  await expect(launch).toBeVisible();
  await expect(launch.getByPlaceholder('What should the agent work on?')).toHaveValue('');
  expect(launches).toBe(0);
  expect(data.snapshot.runId).toBe(originalRun);
  expect(JSON.stringify([data.snapshot.receipts, data.snapshot.pendingDecisions])).toBe(originalEvidence);
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.recover', 'ry/turn.submit', 'ry/terminal.input', 'ry/session.resume'].includes(method))).toEqual([]);
  await launch.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await expect(panel).toBeVisible();
  await expect(recovery).toBeVisible();
});



test('R1 recovery leaves the old unsent Chat draft in the original session and never copies it', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.owner = 'chat';
  const l1 = { owner: 'chat', phase: 'owned', revision: 211, available: true, recoveryAvailable: false };
  Object.assign(data.snapshot, { l1 });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const input = panel.getByRole('textbox', { name: 'Message', exact: true });
  await input.fill('Retain this unsent recovery draft');
  l1.phase = 'recovery_required'; l1.available = false; l1.recoveryAvailable = true; l1.revision++;
  const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
  await expect(recovery).toBeVisible();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
  await recovery.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
  const launch = page.getByRole('region', { name: 'New session', exact: true });
  await expect(launch).toBeVisible();
  await expect(launch.getByPlaceholder('What should the agent work on?')).toHaveValue('');
  await launch.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('Retain this unsent recovery draft');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
  expect(data.snapshot.runId).toBe('fake-run');
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.recover', 'ry/turn.submit', 'ry/terminal.input'].includes(method))).toEqual([]);
});

test('Claude Terminal explains first-message wait without stopping or switching Chat', async ({ page }) => {
  const data = await fake(page, 'claude');
  const l1 = { owner: 'chat', phase: 'owned', revision: 7, available: true, terminalWaitingForChat: true };
  Object.assign(data.snapshot, { l1 });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const terminal = panel.getByRole('button', { name: 'Terminal', exact: true });
  await expect(terminal).toBeDisabled();
  await expect(terminal).toHaveAccessibleDescription('Terminal is waiting for your first real Chat message to finish. Your Chat session keeps running.');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toBeEditable();
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Unsent text is not native history');
  await expect(terminal).toBeDisabled();
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.prepare', 'ry/switch.commit', 'ry/turn.submit', 'ry/turn.interrupt', 'ry/terminal.input'].includes(method))).toEqual([]);
  l1.terminalWaitingForChat = false;
  await expect(terminal).toBeEnabled();
});

for (const fault of ['attachment', 'session-revoked', 'forbidden'] as const) test(`Claude ownership HTTP ${fault} distinguishes reader reset from lost access`, async ({ page }) => {
  const data = await terminalInputFixture(page, undefined, 'claude');
  await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await typing.press('a');
  await expect.poll(() => data.writes.length).toBe(1);
  const attaches = data.methods.filter(method => method === 'ry/session.attach').length;
  let refused = false;
  if (fault === 'session-revoked') await page.route(`**/api/sessions/${data.id}`, route => route.fulfill({ status: 403, json: { detail: 'Artificial access withdrawn' } }));
  await page.route(`**/api/sessions/${data.id}/relay/ownership?**`, async route => {
    if (refused && fault === 'attachment') return route.fallback();
    refused = true;
    await route.fulfill(fault === 'forbidden' ? { status: 403, json: { detail: 'Artificial reader revoked' } }
      : { status: 404, json: { error: { message: 'Artificial expired attachment', data: { code: 'NOT_FOUND_OR_DENIED' } } } });
  });
  if (fault === 'attachment') {
    const retained = data.panel.getByRole('textbox', { name: 'Previous Terminal draft', exact: true });
    await expect(retained).toHaveValue('a', { timeout: 10_000 }); await expect(retained).toHaveAttribute('readonly', '');
    await expect.poll(() => data.methods.filter(method => method === 'ry/session.attach').length, { timeout: 10_000 }).toBeGreaterThan(attaches);
    await retained.press('b'); await page.waitForTimeout(600);
    expect(data.writes).toHaveLength(1); expect(data.claims()).toBe(1);
  } else {
    await expect(data.panel).toHaveCount(0, { timeout: 10_000 });
    await expect(page.getByRole('textbox', { name: 'Previous Terminal draft', exact: true })).toHaveCount(0);
  }
});

test('unsupported file approval keys are explained in both views without sending input', async ({ page }) => {
  const data = await fake(page, 'codex');
  Object.assign(data.snapshot.capabilities, { 'decision.tool_approval': 'qualified' });
  Object.assign(data.snapshot, { pendingDecisions: [{ decisionId: 'unsupported-file-keys', runId: 'fake-run',
    actionDigest: 'sha256:' + 'a'.repeat(64), description: 'Review file changes to artificial.txt', revision: 0,
    expiresAtMs: Date.now() + 60000, status: 'pending',
    choices: [{ id: 'accept', label: 'Allow once', effect: 'allow_once' }, { id: 'decline', label: 'Deny', effect: 'deny' }] }] });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const notice = panel.getByRole('status').filter({ hasText: 'File changes and other key combinations are not supported there.' });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText('Use the review buttons below.');
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(notice).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Deny', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(notice).toBeVisible();
  expect(data.methods.filter(method => ['ry/terminal.input', 'ry/decision.reply', 'ry/operation.register'].includes(method))).toEqual([]);
});

test('native terminal prompt gives Chat a view-only link and never remote Allow', async ({ page }) => {
  const data = await fake(page, 'claude');
  await page.route(`**/api/sessions/${data.id}/relay/terminal?**`, route => route.fulfill({ json: { runId: 'fake-run', truncated: false, text: 'Artificial native overwrite prompt' } }));
  Object.assign(data.snapshot.capabilities, { 'decision.tool_approval': 'qualified' });
  Object.assign(data.snapshot, { l1: { owner: 'terminal', phase: 'owned', revision: 3, available: true },
    nativePrompt: { kind: 'overwrite', message: 'Claude is asking in the terminal; answer it there', remoteAllow: false,
      terminalUrl: `/sessions/${data.id}?tab=terminal` },
    pendingDecisions: [{ decisionId: 'stale-native-question', runId: 'fake-run', actionDigest: 'sha256:' + 'a'.repeat(64),
      description: 'An earlier separate review', revision: 0, expiresAtMs: Date.now() + 60000, status: 'pending',
      choices: [{ id: 'accept', label: 'Allow once', effect: 'allow_once' }] }] });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  const notice = panel.getByRole('status', { name: 'Native terminal prompt' });
  await expect(notice).toHaveText(/Claude is asking in the terminal; answer it there/);
  await expect(panel.getByRole('button', { name: 'Allow once', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(notice.getByRole('link', { name: 'Open Terminal' })).toHaveAttribute('href', `/sessions/${data.id}?tab=terminal`);
  await notice.getByRole('link', { name: 'Open Terminal' }).click();
  await expect(panel.getByRole('region', { name: 'Native Terminal', exact: true })).toBeVisible();
  await expect(panel.getByRole('log', { name: 'Native Terminal screen' })).toContainText('Artificial native overwrite prompt');
  expect(data.methods.filter(method => ['ry/terminal.input', 'ry/decision.reply', 'ry/operation.register', 'ry/switch.prepare', 'ry/switch.commit'].includes(method))).toEqual([]);
  Object.assign(data.snapshot, { nativePrompt: undefined });
  await expect(notice).toHaveCount(0);
});

test('native terminal Inbox notice has Open Terminal and ignores stale approval tokens', async ({ page }) => {
  const data = await fake(page, 'claude');
  await page.route('**/api/attention**', async route => {
    expect(route.request().method()).toBe('GET');
    await route.fulfill({ json: [{ id: 987654, kind: 'needs_input', severity: 'warn', source: 'claude_terminal', verified: false,
      title: 'Native modal needs your input', detail: 'Claude is asking in the terminal; answer it there',
      session_id: data.id, session_name: 'Native modal', project: null, created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(), acked_at: null, resolved_at: null, snoozed_until: null,
      permission_id: '1'.repeat(32), permission_can_approve: true, permission_input: 'stale tool input',
      permission_expires_at: new Date(Date.now() + 60000).toISOString() }] });
  });
  await page.goto('/inbox');
  await expect(page.getByText('Claude is asking in the terminal; answer it there', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Terminal', exact: true })).toHaveAttribute('href', `/sessions/${data.id}?tab=terminal`);
  await expect(page.getByRole('button', { name: /Allow/ })).toHaveCount(0);
  await expect(page.getByText('stale tool input', { exact: true })).toHaveCount(0);
});

test('R1 recovery opens a blank new-session review and preserves the old read-only session', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.owner = 'chat';
  data.snapshot.receipts.push({ operationId: 'old-approved', sessionId: data.id, runId: 'fake-run', method: 'decision.reply',
    payloadDigest: 'sha256:' + 'a'.repeat(64), actor: { kind: 'person', principal: 'alex', channel: 'web' }, state: 'accepted', revision: 2 });
  Object.assign(data.snapshot, { history: 'qualified', decisionOutcomes: [{ decisionId: 'old-decision', runId: 'fake-run',
    description: 'Old reviewed tool decision', status: 'answered', choices: [], winner: { principal: 'alex' } }] });
  await page.route(`**/api/sessions/${data.id}/relay`, async route => {
    if (route.request().postDataJSON().method !== 'ry/history.page') return route.fallback();
    await route.fulfill({ json: { result: { items: [{ itemId: 'old-output', role: 'assistant', text: 'Old native output stays readable' }] } } });
  });
  const launches: unknown[] = [];
  await page.route('**/api/sessions', async route => {
    if (route.request().method() !== 'POST') return route.fallback();
    launches.push(route.request().postDataJSON()); await route.abort();
  });
  const l1 = { owner: 'chat', phase: 'owned', revision: 211, available: true, recoveryAvailable: false, recoveryMode: 'new_session' };
  Object.assign(data.snapshot, { l1 });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await panel.getByRole('button', { name: 'Chat', exact: true }).click();
  await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('Keep this old unsent task');
  l1.phase = 'recovery_required'; l1.available = false; l1.recoveryAvailable = true;
  const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
  await expect(recovery).toBeVisible();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
  await expect(recovery).toContainText('receipts, output and decisions stay readable');
  await recovery.getByRole('button', { name: 'Recover into a new session' }).click();
  const review = page.getByRole('region', { name: 'New session', exact: true });
  await expect(review).toBeVisible();
  await expect(review.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  await review.getByRole('button', { name: /^(Cancel|Back to workspace)$/ }).click();
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('Keep this old unsent task');
  await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
  await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('decision.reply: accepted');
  await expect(panel.getByRole('log', { name: 'Conversation' })).toContainText('Old native output stays readable');
  await expect(panel.getByText('Old reviewed tool decision', { exact: true })).toBeVisible();
  expect(launches).toEqual([]);
  expect(data.methods.filter(method => ['ry/operation.register', 'ry/switch.recover', 'ry/turn.submit', 'ry/terminal.input'].includes(method))).toEqual([]);
});

test('Claude Terminal explains its first-message wait and leaves Chat running', async ({ page }) => {
  const data = await fake(page, 'claude'); data.snapshot.selector.owner = 'chat';
  const terminalRequests: string[] = [];
  await page.route(`**/api/sessions/${data.id}/terminal-reference`, async route => { terminalRequests.push(route.request().url()); await route.abort(); });
  Object.assign(data.snapshot, { terminalMediated: true, l1: { owner: 'chat', phase: 'owned', revision: 1, available: true,
    terminalWaitingReason: 'Terminal becomes available after your first real Chat message. Chat is still running; send a message here first.' } });
  await page.goto(`/sessions/${data.id}?tab=terminal`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeDisabled();
  await expect(panel.getByRole('status').filter({ hasText: 'first real Chat message' })).toBeVisible();
  const input = panel.getByRole('textbox', { name: 'Message', exact: true });
  await expect(input).toBeEditable(); await input.fill('A draft is not a real message');
  await expect(panel.getByRole('button', { name: 'Terminal', exact: true })).toBeDisabled();
  expect(terminalRequests).toEqual([]);
  expect(data.methods.filter(method => method.startsWith('ry/switch.') || ['ry/turn.submit', 'ry/operation.register'].includes(method))).toEqual([]);
});


for (const provider of ['codex', 'claude'] as const) test(`accepted_claim_snapshot: older receipt preserves exact connected input ${provider}`, async ({ page, _drainRoutes }) => {
  const data = await terminalInputFixture(page, undefined, provider);
  await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  const accepted = data.acceptedClaim()!;
  expect(accepted.state).toBe('accepted');
  data.snapshot.receipts.push({ ...accepted, state: 'submitting', revision: accepted.revision - 1 });
  await expect(data.panel.getByRole('list', { name: 'Operation receipts', includeHidden: true })).toContainText('control.claim: submitting');
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  await typing.pressSequentially('abc');
  await expect.poll(() => data.writes.map(frame => atob(frame.data as string)).join('')).toBe('abc');
  expect(data.claims()).toBe(1);
  data.snapshot.receipts.push({ ...accepted, operationId: 'another-operation', method: 'turn.submit', state: 'submitting' });
  await expect(typing).toBeDisabled();
  data.snapshot.receipts.splice(0);
  await expect(typing).toBeEnabled();
  expect(data.claims()).toBe(1);
});


for (const mismatch of ['operation', 'actor', 'digest', 'same-revision', 'newer-uncertainty'] as const) test(`accepted_claim_identity: pending ${mismatch} still pauses input`, async ({ page, _drainRoutes }) => {
  const data = await terminalInputFixture(page, undefined, 'claude');
  await data.connect.click();
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled();
  const accepted = data.acceptedClaim()!;
  const pending: RelayReceipt = { ...accepted, state: 'submitting', revision: accepted.revision - 1 };
  if (mismatch === 'operation') pending.operationId = 'another-claim';
  if (mismatch === 'actor') pending.actor = { ...accepted.actor, principal: 'Other person' };
  if (mismatch === 'digest') pending.payloadDigest = 'sha256:' + 'b'.repeat(64);
  if (mismatch === 'same-revision') pending.revision = accepted.revision;
  if (mismatch === 'newer-uncertainty') { pending.state = 'uncertain'; pending.revision = accepted.revision + 1; }
  data.snapshot.receipts.push(pending);
  await expect(typing).toBeDisabled();
  expect(data.writes).toHaveLength(0); expect(data.claims()).toBe(1);
});

test('accepted Terminal claim stays focused when an older pending claim snapshot arrives', async ({ page }) => {
  const data = await terminalInputFixture(page, undefined, 'claude');
  const claimed = page.waitForResponse(response => response.url().endsWith(`/api/sessions/${data.id}/relay`)
    && response.request().postDataJSON()?.method === 'ry/control.claim');
  await data.connect.click();
  const receipt = (await (await claimed).json()).result.receipt;
  const typing = data.panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  data.snapshot.receipts.push({ ...receipt, state: 'submitting', revision: receipt.revision - 1, payloadDigest: receipt.payloadDigest });
  await expect(data.panel.getByRole('list', { name: 'Operation receipts', includeHidden: true })).toContainText('control.claim:');
  await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
  await page.keyboard.type('c');
  await expect.poll(() => data.writes.length).toBe(1);
  expect(atob(data.writes[0].data as string)).toBe('c');
  expect(data.claims()).toBe(1);
  await expect(page.getByRole('region', { name: 'New session', exact: true })).toHaveCount(0);
});

test('R1 crash recovery ignores dormant same-session recovery availability and never registers it', async ({ page }) => {
  const data = await fake(page, 'claude');
  Object.assign(data.snapshot, { l1: { owner: 'terminal', phase: 'recovery_required', revision: 151, available: false, recoveryAvailable: false } });
  await page.goto(`/sessions/${data.id}`);
  const panel = page.getByRole('region', { name: 'Shared session', exact: true });
  const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
  await expect(recovery.getByRole('button', { name: 'Recover into a new session', exact: true })).toBeEnabled();
  expect(data.methods.some(method => /switch.recover|operation.register/.test(method))).toBe(false);
  await recovery.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
  const launch = page.getByRole('region', { name: 'New session', exact: true });
  await expect(launch).toBeVisible();
  await expect(launch.getByPlaceholder('What should the agent work on?')).toHaveValue('');
  await launch.getByRole('button', { name: 'Back to workspace', exact: true }).click();
  await expect(recovery).toBeVisible();
  expect(data.snapshot.runId).toBe('fake-run');
  expect(data.methods.some(method => /switch.recover|operation.register|turn.submit|terminal.input/.test(method))).toBe(false);
});

// Pure input state boundary: no provider/native process is involved.
test('security A3: browser refuses empty Backspace and every shared native command prefix', async () => {
  const { RelayInputGeneration } = await import('../src/lib/sessionRelayInput');
  for (const value of ['\x7f', '/', '!', '?', '#', '@', '&']) {
    const writes: string[] = [];
    const generation = new RelayInputGeneration(1, {
      claim: async () => ({ inputGrant: 'fixture', inputEpoch: 1 }),
      write: async (_grant, sequence, data) => { writes.push(data); return { watermark: { epoch: 1, admitted: sequence, processed: 0 }, partial: false }; },
      drain: async () => ({ epoch: 1, admitted: 0, processed: 0 }), idle: async () => true,
    }, () => {}, async () => {}, async () => {});
    await generation.connect();
    expect(() => generation.enqueue(value), JSON.stringify(value)).toThrow();
    expect(writes).toEqual([]);
    expect(generation.state.draft).toBe('');
  }
});
