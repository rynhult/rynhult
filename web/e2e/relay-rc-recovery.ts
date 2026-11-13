import { test, expect, type Page } from './fixtures';
import { AUTH_FILE, csrfHeaders } from './helpers';
import { BASE } from './fixture-paths';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
test.use({ storageState: AUTH_FILE });

async function launchReviewed(page: Page, projectId: number) {
  const form = page.getByTestId('session-launch-frame');
  await expect(form).toBeVisible({ timeout: 15_000 });
  await expect(form.getByRole('textbox', { name: 'Task (optional)', exact: true })).toHaveValue('');
  // Wait for the workspace form, not an intermediate named dialog region.
  for (const id of ['launch-location-toggle', 'launch-permissions-toggle', 'launch-more-toggle']) {
    const toggle = form.getByTestId(id);
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    if (!await toggle.evaluate(node => (node.parentElement as HTMLDetailsElement).open)) await toggle.click();
  }
  const project = form.getByRole('combobox', { name: 'Project', exact: true });
  if (await project.count()) {
    await expect(project).toBeVisible();
    await project.selectOption(String(projectId));
    await expect(project).toHaveValue(String(projectId));
  }
  await form.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('claude');
  await form.getByRole('checkbox', { name: /^Own worktree/ }).uncheck();
  await form.getByRole('checkbox', { name: /Remote Control/ }).uncheck();
  const signIn = form.getByRole('combobox', { name: 'Claude sign-in', exact: true });
  await expect(signIn).toBeVisible({ timeout: 20_000 });
  await signIn.selectOption('api_key');
  await form.getByRole('checkbox', { name: /Use shared Chat and Terminal/ }).check();
  await expect(form.getByRole('button', { name: 'Launch', exact: true })).toBeEnabled();
  const response = page.waitForResponse(r => r.url().endsWith('/api/sessions') && r.request().method() === 'POST');
  await form.getByRole('button', { name: 'Launch', exact: true }).click();
  const result = await response;
  expect(result.ok(), await result.text()).toBe(true);
  const created = await result.json();
  expect(created.project_id).toBe(projectId);
  return created;
}

test('RC G7.R1 G2.R1: SDK Interrupt exit reviewed normal Launch distinct take refuses old scope', { tag: '@relay-native' }, async ({ page }) => {
  const fixture = JSON.parse(readFileSync(`${BASE}/info.json`, 'utf8'));
  test.skip(!fixture.relay_native_enabled, 'Requires installed pinned CLI offline fixture');
  test.setTimeout(240_000);
  const headers = { ...await csrfHeaders(page), 'x-e2e-isolation': fixture.isolation_token };
  const requests = page.request;
  let fresh: string | undefined;
  let finished = false;
  try {
    const prepared = await requests.post('/e2e-test/relay-claude/prepare', { headers });
    expect(prepared.ok(), await prepared.text()).toBe(true);
    const { projectId } = await prepared.json();
    await page.goto(`/projects/${projectId}`);
    const newSession = page.getByRole('banner', { name: 'Workspace header', exact: true }).getByRole('button', { name: /^New session(?: ·|$)/ });
    await expect(newSession).toBeVisible({ timeout: 10_000 });
    await newSession.click();
    const old = await launchReviewed(page, projectId);
    const state = async () => (await requests.get('/e2e-test/relay-claude/state', { headers })).json();
    const originalNative = await state();
    expect(originalNative.sessionId).toBe(old.id);
    const panel = page.getByRole('region', { name: 'Shared session', exact: true });
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: true, long: true } })).ok()).toBe(true);
    await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('RC original held SDK message');
    const submit = page.waitForResponse(r => r.url().endsWith(`/api/sessions/${old.id}/relay`) && r.request().postDataJSON()?.method === 'ry/turn.submit');
    await panel.getByRole('button', { name: 'Send', exact: true }).click();
    const oldTurn = (await (await submit).json()).result;
    expect(oldTurn.state).toBe('uncertain');
    await expect.poll(async () => (await state()).model_held).toBe(true);
    const interrupt = page.waitForResponse(r => r.url().endsWith(`/api/sessions/${old.id}/relay`) && r.request().postDataJSON()?.method === 'ry/turn.interrupt');
    await panel.getByRole('button', { name: 'Interrupt', exact: true }).click();
    const interrupted = await interrupt;
    const oldTicket = interrupted.request().postDataJSON().params;
    const oldConnection = interrupted.request().headers()['x-acc-relay-connection'];
    const receipt = (await interrupted.json()).result;
    expect(receipt.state).toBe('accepted');
    await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('Interrupt requested.');
    expect((await state()).sdk_interrupts.filter((v: { writeCompleted: boolean }) => v.writeCompleted)).toHaveLength(1);
    expect((await requests.post('/e2e-test/relay-claude/model', { headers, data: { held: false } })).ok()).toBe(true);
    const ended = await requests.post('/e2e-test/relay-claude/exit-for-recovery', { headers });
    expect(ended.ok(), await ended.text()).toBe(true);
    expect(await ended.json()).toEqual({ exited: true, active: 0 });
    const recovery = panel.getByRole('region', { name: 'Session recovery', exact: true });
    await expect(recovery).toBeVisible({ timeout: 20_000 });
    await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
    let launches = 0;
    page.on('request', r => { if (r.url().endsWith('/api/sessions') && r.method() === 'POST') launches++; });
    await recovery.getByRole('button', { name: 'Recover into a new session', exact: true }).click();
    await expect(page.getByTestId('session-launch-frame')).toBeVisible({ timeout: 15_000 });
    expect(launches).toBe(0);
    const created = await launchReviewed(page, projectId);
    fresh = created.id;
    expect(fresh).not.toBe(old.id); expect(created.native_id).not.toBe(old.native_id);
    expect(launches).toBe(1);
    const initial = await state();
    expect(initial.runId).not.toBe(originalNative.runId);
    expect(initial.sdk_queries).toBe(0);
    await panel.getByRole('button', { name: 'Chat', exact: true }).click();
    await panel.getByRole('textbox', { name: 'Message', exact: true }).fill('RC fresh explicitly reviewed message');
    const newSubmit = page.waitForResponse(r => r.url().endsWith(`/api/sessions/${fresh}/relay`) && r.request().postDataJSON()?.method === 'ry/turn.submit');
    await panel.getByRole('button', { name: 'Send', exact: true }).click();
    const newTurn = (await (await newSubmit).json()).result;
    expect(['accepted', 'uncertain']).toContain(newTurn.state);
    await expect.poll(async () => (await (await requests.get(`/api/sessions/${fresh}/relay/operations/${newTurn.operationId}`)).json()).state, { timeout: 25_000 }).toBe('accepted');
    await expect(panel.getByRole('status', { name: 'Execution readiness' })).toContainText('Ready to switch', { timeout: 25_000 });
    await panel.getByRole('button', { name: 'Terminal', exact: true }).click();
    const taking = page.waitForResponse(r => r.url().endsWith(`/api/sessions/${fresh}/relay`) && r.request().postDataJSON()?.method === 'ry/control.claim');
    await panel.getByRole('button', { name: 'Connect Terminal input', exact: true }).click();
    const taken = await taking;
    expect((await taken.json()).error).toBeUndefined();
    const freshConnection = taken.request().headers()['x-acc-relay-connection'];
    const typing = panel.getByRole('textbox', { name: 'Terminal typing', exact: true });
    await expect(typing).toBeEnabled(); await expect(typing).toBeFocused();
    const before = await state();
    expect(before.nativeId).toBe(created.native_id); expect(before.nativeId).not.toBe(old.native_id);
    expect(before.view).toBe('terminal'); expect(before.admitted).toBe(0);
    const rpc = async (sid: string, connection: string, method: string, params: object) => {
      const response = await requests.post(`/api/sessions/${sid}/relay`, { headers: { ...headers, 'X-ACC-Relay-Connection': connection },
        data: { jsonrpc: '2.0', id: randomUUID(), method: 'ry/' + method, params: { sessionId: sid, ...params } } });
      return response.json();
    };
    // An old ticket cannot be retargeted through the NEW browser attachment.
    const retargeted = await rpc(fresh!, freshConnection, 'turn.interrupt', { ...oldTicket, sessionId: fresh, expectedRunId: before.runId });
    expect(retargeted.error).toBeTruthy();
    const oldRefused = await rpc(old.id, oldConnection, 'turn.interrupt', oldTicket);
    expect(oldRefused.error || oldRefused.result?.operationId === receipt.operationId).toBeTruthy();
    for (const method of ['turn.submit', 'turn.interrupt', 'writer.take', 'control.claim']) {
      const refused = await rpc(old.id, oldConnection, 'operation.register', { expectedRunId: originalNative.runId,
        registrationKey: randomUUID(), method, payloadDigest: 'sha256:' + 'a'.repeat(64) });
      expect(refused.error).toBeTruthy();
    }
    const saved = await requests.get(`/api/sessions/${old.id}/relay/operations/${receipt.operationId}`);
    expect(saved.ok()).toBe(true); expect(await saved.json()).toEqual(receipt);
    const after = await state();
    expect(after.nativeId).toBe(before.nativeId); expect(after.runId).toBe(before.runId);
    expect(after.owner_process).toBe(before.owner_process); expect(after.admitted).toBe(0);
    await expect(typing).toBeFocused(); await page.keyboard.type('z');
    await expect.poll(async () => (await state()).admitted).toBe(1);
    await page.goto(`/sessions/${old.id}`);
    await page.getByRole('button', { name: 'Chat', exact: true }).click();
    await expect(panel.getByRole('list', { name: 'Operation receipts' })).toContainText('Interrupt requested.');
    await expect(panel.getByRole('textbox', { name: 'Message', exact: true })).not.toBeEditable();
    test.info().annotations.push({ type: 'rc_recovery', description: JSON.stringify({ oldSession: old.id, newSession: fresh,
      oldNative: old.native_id, newNative: created.native_id, actualSdkInterrupt: true, oldExitedBeforeLaunch: true,
      blankReviewedNormalLaunch: true, noAutomaticQuery: true, newTake: true, oldMutationsRefused: 4, oldTicketRetargetRefused: true,
      originalReceiptUnchanged: true, newInputAdmitted: 1, requestedNotConfirmed: true }) });
    finished = true;
  } finally {
    if (fresh) expect((await requests.post(`/api/sessions/${fresh}/stop`, { headers })).ok()).toBe(true);
    const end = await requests.post('/e2e-test/relay-claude/finish', { headers });
    expect(end.ok(), await end.text()).toBe(true);
    const result = await end.json();
    if (finished) expect(result.tui.empty_tree_records).toHaveLength(3);
    expect(result.tui.empty_tree_records.every((v: { empty: boolean }) => v.empty)).toBe(true);
    await page.close();
  }
});
