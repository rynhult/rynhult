import { expect, test, type Page } from "./fixtures";
import { AUTH_FILE, expectAxeClean, shot, wikiShotPath } from "./helpers";

test.use({ storageState: AUTH_FILE });

// UX review IN-1 to IN-4 and the warning part of PB-1: the Inbox shows what Publishing shows, Approve asks when there
// is a warning, no green badge, actions say what they do, and sources are words.
const now = () => new Date().toISOString();
const base = { verified: true, detail: null, session_id: null, session_name: null, project: null, acked_at: null, resolved_at: null, publish_id: null };
const pending = { host: "preview.example.com", port: 8010, access: "internal", action: "publish", requested_by: "session ui-polish",
  reason: "Let the team review the new checkout flow", project: "demo-web", replaces: null, port_owner: null };

async function routeInbox(page: Page, acked: number[] = []) {
  const at = now();
  await page.route("**/api/attention*", (route) => route.fulfill({ json: [
    { ...base, id: 1, kind: "needs_input", severity: "warn", title: "api-refactor needs your input", source: "claude_hook",
      session_id: "s-api", detail: "Claude needs your permission", created_at: at, updated_at: at },
    { ...base, id: 2, kind: "needs_input", severity: "warn", title: "docs-pass needs your input", source: "screen", verified: false,
      session_id: "s-docs", created_at: at, updated_at: at },
    { ...base, id: 3, kind: "publish_request", severity: "warn", title: "ui-polish asks to publish preview.example.com", source: "session ui-polish",
      publish_id: "pub-000000000010", publish: pending, created_at: at, updated_at: at },
    { ...base, id: 4, kind: "guardrail_blocked", severity: "info", title: "checkout-flow: guardrail blocked a force push", source: "guardrails",
      session_id: "s-co", created_at: at, updated_at: at },
  ].filter((i) => !acked.includes(i.id)) }));
}

test("inbox: a publish warning shows as a note, and Approve asks first", async ({ page }, testInfo) => {
  await routeInbox(page);
  let posted: unknown = "nothing";
  await page.route("**/api/publish/pub-000000000010/approve", async (route) => { posted = route.request().postDataJSON() ?? {}; await route.fulfill({ json: {} }); });
  await page.goto("/inbox");
  await page.getByRole("button", { name: /ui-polish asks to publish/ }).click();
  const row = page.getByRole("region", { name: "Inbox item details" });
  await expect(row.getByText("Port 8010 isn't reserved for any project.")).toBeVisible();
  await expect(row.getByText("Requested by session ui-polish · project demo-web · port 8010 · private networks only")).toBeVisible();
  await row.getByRole("button", { name: "Review and approve" }).click();
  const dialog = page.getByRole("dialog", { name: "Publish preview.example.com?" });
  await expect(dialog.getByText(/will serve port 8010 on private networks only/)).toBeVisible();
  await expect(dialog.getByText("Port 8010 isn't reserved for any project.")).toBeVisible();
  await shot(page, testInfo.project.name, "inbox-approve-confirm");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(posted).toBe("nothing");  // Cancel leaves the request pending
  await row.getByRole("button", { name: "Review and approve" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Approve and publish" }).click();
  await expect.poll(() => posted).toEqual({});
});

test("Overview points to Inbox actions, which show the same warning as Publishing", async ({ page }) => {
  await routeInbox(page);
  await page.route(/\/api\/publish(?:\?.*)?$/, (route) => route.fulfill({ json: [{ ...pending, id: "pub-000000000010", status: "pending_approval",
    project_id: 2, session_id: null, zone_name: "example.com", detail: null, decided_by: null, created_at: now(), decided_at: null, completed_at: null }] }));
  await page.goto("/");
  await page.getByRole("main").getByRole("link", { name: /^Inbox/ }).click();
  await page.getByRole("button", { name: /ui-polish asks to publish/ }).click();
  await expect(page.getByRole("main").getByText("Port 8010 isn't reserved for any project.").filter({ visible: true })).toBeVisible();
  await page.goto("/publishing");
  await expect(page.getByRole("main").getByText("Port 8010 isn't reserved for any project.")).toBeVisible();
  await expect(page.getByRole("main").getByRole("button", { name: "Review and approve" })).toBeVisible();
});

test("inbox rows: no green badge, sources in words, actions that say what they do", async ({ page }, testInfo) => {
  const acked: number[] = [];
  const registry = await (await page.request.get('/api/providers/registry')).json();
  const claudeLabel = registry.providers.find((p: { id: string; label: string }) => p.id === 'claude').label;
  await routeInbox(page, acked);
  await page.route("**/api/attention/*/ack", async (route) => { acked.push(Number(route.request().url().split("/").at(-2))); await route.fulfill({ json: { ok: true } }); });
  await page.goto("/inbox");
  const main = page.getByRole("main");
  const detail = page.getByRole('region', { name: 'Inbox item details' });
  await expect(main.getByRole('button', { name: /api-refactor needs your input/ })).toBeVisible();
  await expect(main.locator('.text-ok')).toHaveCount(0);
  await expect(main.getByText('verified', { exact: true })).toHaveCount(0);
  for (const [title, word, href] of [
    ['api-refactor needs your input', claudeLabel, '/sessions/s-api'],
    ['docs-pass needs your input', 'Prompt on screen', '/sessions/s-docs'],
    ['ui-polish asks to publish', 'Agent request', null],
    ['checkout-flow: guardrail', 'Guardrail', '/sessions/s-co'],
  ] as const) {
    await main.getByRole('button', { name: new RegExp(title) }).click();
    await expect(detail.getByText(word, { exact: true }).first()).toBeVisible();
    if (href) await expect(detail.getByRole('link', { name: 'Open related work' })).toHaveAttribute('href', href);
    if (title.startsWith('docs')) {
      await expect(detail.getByText('guessed', { exact: true })).toBeVisible();
      await expect(detail.getByRole('link', { name: 'Answer in terminal' })).toHaveAttribute('href', href!);
    }
  }
  for (const raw of ['claude_hook', 'guardrails', 'session ui-polish ·']) await expect(main.getByText(raw, { exact: true })).toHaveCount(0);
  await expect(detail.getByRole('link', { name: 'Open session', exact: true })).toHaveAttribute('href', '/sessions/s-co');
  // A prompt's dismiss sits behind ⋯ and says it doesn't answer; a note's is a plain button.
  await expect(main.getByRole("button", { name: "Dismiss notice" })).toHaveCount(1);
  await expectAxeClean(page, "main");
  await shot(page, testInfo.project.name, "inbox-rows");
  await main.getByRole('button', { name: /api-refactor needs your input/ }).click();
  await expect(detail.getByText(claudeLabel, { exact: true }).last()).toHaveAttribute('title', 'Source: claude_hook');
  await expect(detail.getByRole('link', { name: 'Answer in terminal' })).toHaveAttribute('href', '/sessions/s-api');
  await main.getByRole("button", { name: "More actions" }).first().click();
  await expect(main.getByText("The session keeps waiting for its answer")).toBeVisible();
  await main.getByRole("menuitem", { name: /^Dismiss notice/ }).first().click();
  await expect.poll(() => acked).toEqual([1]);
});

test("inbox: a restored session still running elsewhere is stopped only after confirming, by a verified owner", async ({ page }, testInfo) => {
  const at = now();
  await page.route("**/api/attention*", (route) => route.fulfill({ json: [
    { ...base, id: 7, kind: "replacement_held", severity: "warn", source: "backup", session_id: "s-held",
      title: "A restored session is still running on build-box", created_at: at, updated_at: at,
      detail: "It was still running when Rynhult was restored from backup, so Rynhult holds it: it gets no input and its terminal is read-only. Check it there, then stop it on that machine, or use Resume if it was approved during recovery." },
  ] }));
  const posts: string[] = [];
  let verified = false;
  await page.route("**/api/sessions/s-held/stop-held", async (route) => {
    posts.push(route.request().method());
    await (verified ? route.fulfill({ json: { id: "s-held" } })
      : route.fulfill({ status: 403, json: { detail: "Verify your sign-in again in Settings → Passkeys, then try again within five minutes." } }));
  });
  await page.goto("/inbox");
  const row = page.getByRole("region", { name: "Inbox item details" });
  await expect(row.getByTitle("Source: backup", { exact: true })).toBeVisible();
  await expect(row.getByRole("link", { name: "Open session" })).toHaveAttribute("href", "/sessions/s-held");
  const stop = row.getByRole("button", { name: "Stop on that machine" });
  await stop.click();
  const dialog = page.getByRole("dialog", { name: "Stop it on that machine?" });
  await expect(dialog.getByText(/sends one stop for this session/)).toBeVisible();
  await expect(dialog.getByText(/stays archived/)).toBeVisible();
  await dialog.screenshot({ path: wikiShotPath(testInfo, "inbox-replacement-held-stop.png") });
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(posts).toEqual([]);  // nothing stops without confirming
  await stop.click();
  await page.getByRole("dialog").getByRole("button", { name: "Stop on that machine" }).click();
  await expect(row.getByText("Verify your sign-in again in Settings → Passkeys")).toBeVisible();
  verified = true;
  await stop.click();
  await page.getByRole("dialog").getByRole("button", { name: "Stop on that machine" }).click();
  await expect.poll(() => posts).toEqual(["POST", "POST"]);
  await expectAxeClean(page, "main");
});

test("inbox: members never get the stop action for a held session", async ({ page }) => {
  const at = now();
  await page.route("**/api/auth/state", (route) => route.fulfill({ json: { authenticated: true, setup_required: false,
    username: "robin", role: "member", account: "robin-agent" } }));
  await page.route("**/api/attention*", (route) => route.fulfill({ json: [
    { ...base, id: 8, kind: "replacement_held", severity: "warn", source: "backup", session_id: "s-held",
      title: "A restored session is still running on build-box", created_at: at, updated_at: at },
  ] }));
  await page.goto("/inbox");
  await expect(page.getByRole("region", { name: "Inbox item details" }).getByRole("heading", { name: "A restored session is still running on build-box" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop on that machine" })).toHaveCount(0);
});

test("Claude permission decisions are explicit and one-shot", async ({ page }) => {
  const at = now();
  const token = "a".repeat(32);
  const decisions: unknown[] = [];
  await page.route("**/api/attention*", route => route.fulfill({ json: [{ ...base,
    id: 101, kind: "needs_input", severity: "warn", title: "Review permission", source: "claude_hook",
    session_id: "s-review", detail: "Bash: git status", permission_id: token, permission_can_approve: true, permission_input: '{"command":"git status"}', created_at: at, updated_at: at,
  }] }));
  await page.route("**/api/attention/101/permission", route => {
    decisions.push(route.request().postDataJSON());
    return route.fulfill({ json: { answered: true } });
  });
  await page.goto("/inbox");
  const main = page.getByRole("main");
  await expect(main.getByText("Bash: git status", { exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "Deny", exact: true })).toBeVisible();
  expect(decisions).toEqual([]);
  await main.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(main.getByText("Approved", { exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "Approve", exact: true })).toBeDisabled();
  expect(decisions).toEqual([{ token, decision: "allow" }]);
});

for (const expired of [false, true]) {
  test(`permission input unavailable, expired=${expired}`, async ({ page }) => {
    await page.route("**/api/attention*", route => route.fulfill({ json: [{ ...base,
      id: 101, kind: "needs_input", severity: "warn", title: "Review permission", source: "claude_hook",
      session_id: "s-review", permission_id: "a".repeat(32), permission_can_approve: false,
      permission_reason: "Complete tool input exceeds the 8 kB display limit. Answer in terminal to approve.",
      permission_expires_at: expired ? "2020-01-01T00:00:00Z" : new Date(Date.now() + 50000).toISOString(), created_at: now(), updated_at: now(),
    }] }));
    await page.goto("/inbox");
    await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
    if (expired) await expect(page.getByText("This request expired. Answer in the terminal.")).toBeVisible();
    else await expect(page.getByRole("button", { name: "Deny", exact: true })).toBeVisible();
  });
}

for (const width of [1440, 390]) test(`bulk notices use shown selection and Undo · ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const at = now();
  const all = [
    { ...base, id: 201, kind: 'state', severity: 'info', title: 'First notice', source: 'test', bulk_dismissible: true, created_at: at, updated_at: at },
    { ...base, id: 202, kind: 'state', severity: 'info', title: 'Second notice', source: 'test', bulk_dismissible: true, created_at: at, updated_at: at },
    { ...base, id: 203, kind: 'needs_input', severity: 'warn', title: 'Answer required', source: 'screen', bulk_dismissible: true, created_at: at, updated_at: at },
    { ...base, id: 204, kind: 'publish_request', severity: 'warn', title: 'Approval required', source: 'test', bulk_dismissible: false, created_at: at, updated_at: at },
  ];
  let hidden: number[] = [];
  const batches: unknown[] = [];
  await page.route('**/api/attention**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/dismiss/undo')) { hidden = []; return route.fulfill({ json: { restored: 2 } }); }
    if (path.endsWith('/dismiss')) {
      const body = route.request().postDataJSON(); batches.push(body);
      hidden = body.items.map((item: { id: number }) => item.id);
      return route.fulfill({ json: { batch: 99, dismissed: hidden.length, skipped: 0 } });
    }
    return route.fulfill({ json: all.filter(item => !hidden.includes(item.id)) });
  });
  await page.goto('/inbox');
  await expect(page.getByRole('checkbox')).toHaveCount(2);
  await page.getByRole('checkbox', { name: 'Select First notice' }).check();
  await page.getByRole('checkbox', { name: 'Select Second notice' }).check();
  await page.getByRole('button', { name: 'Dismiss selected (2)' }).dblclick();
  await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeVisible();
  expect(batches).toEqual([{ items: [{ id: 201, updated_at: at }, { id: 202, updated_at: at }] }]);
  await expect(page.getByRole('button', { name: /Answer required/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Approval required/ })).toBeVisible();
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(page.getByRole('checkbox')).toHaveCount(2);
  await page.getByRole('button', { name: 'Dismiss all shown', exact: true }).click();
  await expect.poll(() => batches.length).toBe(2);
  await expectAxeClean(page, 'main');
});
