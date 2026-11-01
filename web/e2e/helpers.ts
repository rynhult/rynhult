import { environmentValue } from "../../src/acc/env.mjs";
import { createHmac, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import type AxeBuilder from "@axe-core/playwright";

export const BASE = environmentValue("ACC_E2E_BASE") ?? "/tmp/acc-e2e";  // as in playwright.config.ts
export const PASSWORD = "e2e correct horse battery staple";
export const AUTH_FILE = `${BASE}/auth.json`;

export const info = () => JSON.parse(readFileSync(`${BASE}/info.json`, "utf8")) as
  { state: string; setup_token_path: string; run_dir: string; workspace: string; origin: string };

function base32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of secret.replace(/=+$/, "")) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

/** RFC 6238 TOTP, same parameters as acc.server.auth (SHA1, 30 s, 6 digits). */
export function totp(secret: string, at = Date.now() / 1000): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30)));
  const digest = createHmac("sha1", base32(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

export const saveSecret = (secret: string) => writeFileSync(`${BASE}/totp.secret`, secret);
export const loadSecret = () => readFileSync(`${BASE}/totp.secret`, "utf8").trim();

/** Wait until a fresh 30 s step starts, so a login never replays a used code. */
export async function freshStep(page: Page) {
  const remaining = 30 - ((Date.now() / 1000) % 30);
  await page.waitForTimeout(remaining * 1000 + 250);
}

export async function shot(page: Page, project: string | import("@playwright/test").TestInfo, name: string) {
  const projectName = typeof project === "string" ? project : project.project.name;
  const names: Record<string, string> = {
    "keys": "settings-keys", "keys-paste": "settings-keys-paste",
    "compose-review": "projects-compose-review", "publish-retarget": "publishing-retarget",
    "machines-version": "nodes-old-machine-update", "add-machine-linux": "add-machine", "new-workspace": "new-workspace-dialog",
  };
  const wiki = `../docs/wiki/.attachments/${names[name] ?? name}.png`;
  const update = environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === "1" &&
    (projectName === "desktop" || projectName === "desktop-stateful") && existsSync(wiki);
  await page.screenshot({ path: update ? wiki : `e2e/screenshots/${projectName}-${name}.png`, fullPage: true });
}

export function control(acc_id: string, command: string) {
  const path = `${info().run_dir}/${acc_id}/fake.ctl`;
  writeFileSync(`${path}.tmp`, command);
  renameSync(`${path}.tmp`, path);
}

export function wikiShotPath(testInfo: import("@playwright/test").TestInfo, name: string,
  update = environmentValue("ACC_UPDATE_WIKI_SCREENSHOTS") === "1") {
  return update ? `../docs/wiki/.attachments/${name}` : testInfo.outputPath(name);
}

async function analyzeSettledAxe(page: Page, builder: AxeBuilder) {
  const { expect } = await import("@playwright/test");
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
  // Theme and hover transitions can interpolate two accessible endpoints through unreadable colours.
  await expect.poll(() => page.evaluate(() => document.getAnimations().filter((animation) =>
    (animation.pending || animation.playState === "running") &&
    Number.isFinite(animation.effect?.getComputedTiming().endTime),
  ).length), { message: "Waiting for finite page animations to settle" }).toBe(0);
  // Live dots and spinners never finish; freeze their current frame only for the scan, then restore them.
  const looping = await page.evaluateHandle(() => {
    const animations = document.getAnimations().filter((animation) =>
      animation.playState === "running" && animation.effect?.getComputedTiming().endTime === Infinity);
    animations.forEach((animation) => animation.pause());
    return animations;
  });
  try {
    return await builder.analyze();
  } finally {
    await looping.evaluate((animations) => animations.forEach((animation) => {
      if (animation.playState === "paused") animation.play();
    }));
    await looping.dispose();
  }
}

async function expectNoAxeViolations(result: Awaited<ReturnType<typeof analyzeSettledAxe>>) {
  const { expect } = await import("@playwright/test");
  if (result.violations.length) {
    await test.info().attach("axe-violations", {
      body: JSON.stringify(result.violations, null, 2), contentType: "application/json",
    });
  }
  expect(result.violations.map((v) =>
    `${v.id}: ${v.nodes.map((n) => `${n.target.join(" ")}\n${n.failureSummary}`).join("\n")}`,
  )).toEqual([]);
}

/** The axe gate (WCAG 2.2 A/AA): zero violations, with node and colour diagnostics on failure. */
export async function expectAxeClean(page: Page, scope?: string) {
  const { default: AxeBuilder } = await import("@axe-core/playwright");
  let builder = new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]);
  if (scope) builder = builder.include(scope);
  await expectNoAxeViolations(await analyzeSettledAxe(page, builder));
}

/** A uniquely named git project with one commit, registered and trusted through the API. */
export async function gitProject(page: Page, prefix: string) {
  const { execFileSync } = await import("node:child_process");
  const { mkdirSync } = await import("node:fs");
  const slug = `${prefix.slice(0, 30)}-${randomUUID().replaceAll("-", "")}`;
  const folder = `${info().workspace}/${slug}`;
  mkdirSync(folder, { recursive: true });
  const git = (...args: string[]) => execFileSync("git", ["-C", folder, "-c", "user.name=Alex Example", "-c", "user.email=alex@example.com", ...args]);
  execFileSync("git", ["init", "-q", "-b", "main", folder]);
  writeFileSync(`${folder}/app.py`, "print('hello')\n");
  git("add", ".");
  git("commit", "-qm", "Initial version");
  const headers = await csrfHeaders(page);
  const project = await page.request.post("/api/projects", { headers, data: { slug, name: slug, workspace_path: folder } });
  if (!project.ok()) throw new Error(`project creation failed (${project.status()}): ${await project.text()}`);
  const id = (await project.json()).id as number;
  const trusted = await page.request.post(`/api/projects/${id}/trust`, { headers });
  if (!trusted.ok()) throw new Error(`project trust failed (${trusted.status()}): ${await trusted.text()}`);
  await page.goto("/sessions");
  return { id, folder, headers, slug };
}

/** Wait for the creation transaction and its exact destination, not a generic
 * five-second URL assertion while a large Projects page/lazy route settles. */
export async function createProjectFromDialog(page: Page, dialog: import('@playwright/test').Locator) {
  const created = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/projects/new');
  await dialog.getByRole('button', { name: 'Create project', exact: true }).click();
  const response = await created;
  expect(response.status(), await response.text()).toBe(201);
  const project = await response.json();
  await page.waitForURL(url => url.pathname === `/projects/${project.id}`);
  return project;
}

export async function csrfHeaders(page: Page) {
  const name = info().origin.startsWith("https:") ? "__Host-acc_csrf" : "acc_csrf";
  const csrf = (await page.context().cookies()).find((c) => c.name === name)!.value;
  return { "X-ACC-CSRF": csrf, Origin: info().origin };
}

export async function gotoAppPage(page: Page, url: string) {
  await page.goto(url);
  await expect(page.getByRole("main")).toBeVisible({ timeout: test.info().timeout });
  await expect(page.getByRole("status").filter({ hasText: /^Loading page\u2026$/ }))
    .toHaveCount(0, { timeout: test.info().timeout });
}

export async function waitForSessionState(page: Page, id: string, state: "idle" | "active" | "waiting_input", source?: string) {
  await expect.poll(() => page.evaluate(async ({ id, state, source }) => {
    const response = await fetch(`/api/sessions/${id}`);
    if (!response.ok) throw new Error(`session readiness failed (${response.status})`);
    const session = await response.json();
    return session.observed_state === state && (!source || session.status_source === source);
  }, { id, state, source }), {
    message: `Waiting for session ${id} to be ${state}${source ? ` (${source})` : ""}`,
    timeout: test.info().timeout,
    intervals: [250],
  }).toBe(true);
}

/** A stop request is acceptance, not confirmation from every member's machine. */
export async function waitForTeamStopped(page: Page, id: string) {
  await expect.poll(async () => {
    const response = await page.request.get(`/api/sessions/${id}/team`);
    expect(response.ok()).toBe(true);
    const team = await response.json();
    return team.stopped && team.members.every((member: { stop_state?: string }) => member.stop_state !== 'stop requested');
  }, { message: `Waiting for team ${id} stop confirmation`, timeout: test.info().timeout }).toBe(true);
}

/** Drive the disposable provider's real tool spool and wait within the test's existing budget. */
export async function teamRequest(session: string, action: string, args: object = {}) {
  const id = randomUUID().replaceAll("-", "");
  appendFileSync(`${info().run_dir}/${session}/events.jsonl`, JSON.stringify({ acc_session: session,
    hook: { hook_event_name: "AccTeam", request: { id, action, ...args } } }) + "\n");
  const file = `${info().run_dir}/${session}/team/${id}.json`;
  await expect.poll(() => existsSync(file), {
    message: `Waiting for ${action} reply from lead ${session}`,
    timeout: test.info().timeout,
    intervals: [100, 250, 500],
  }).toBe(true);
  return JSON.parse(readFileSync(file, "utf8"));
}

export async function resetPairingThrottle(page: Page) {
  const response = await page.request.post("/e2e-test/reset-pairing-throttle", { headers: await csrfHeaders(page) });
  if (!response.ok()) throw new Error(`pairing fixture reset failed (${response.status()}): ${await response.text()}`);
}

export async function resetTerminalThrottle(page: Page) {
  const response = await page.request.post("/e2e-test/reset-terminal-throttle", { headers: await csrfHeaders(page) });
  if (!response.ok()) throw new Error(`terminal fixture reset failed (${response.status()}): ${await response.text()}`);
}

/** Fresh verification (five minutes) with password and a new code: owner invites, resets of others, passkey changes. */
export async function verifyAgain(page: Page) {
  test.slow();  // waiting for a fresh code step can take 30 s
  await freshStep(page);
  const verified = await page.request.post("/api/auth/passkeys/reauth", { headers: await csrfHeaders(page),
    data: { password: PASSWORD, code: totp(loadSecret()) } });
  if (!verified.ok()) throw new Error(`verification failed: ${await verified.text()}`);
}

/** Review Security through the real owner verification flow; never use a retired writer. */
export async function reviewSecurityChanges(page: Page) {
  await page.locator('#security').getByRole('button', { name: 'Review changes', exact: true }).click();
  const review = page.getByRole('dialog', { name: 'Review Security changes', exact: true });
  const verify = page.getByRole('dialog', { name: 'Verify your sign-in', exact: true });
  await expect(review.or(verify)).toBeVisible();
  if (await verify.isVisible()) {
    test.slow(); // A real authenticator step may need the next 30-second counter.
    await freshStep(page);
    await verify.getByLabel('Verification password').fill(PASSWORD);
    await verify.getByLabel('Verification authenticator code').fill(totp(loadSecret()));
    await verify.getByRole('button', { name: 'Verify with password and authenticator', exact: true }).click();
  }
  await expect(review).toBeVisible();
  return review;
}

/** Sessions shaped like the demo's (scripts/demo_instance.py), oldest activity on the one that needs you, as in the
    UX review's SL-1 screenshot. Route /api/sessions to these to test ordering without launching anything. */
export function demoSessions() {
  const at = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
  const base = { project_id: 1, provider: "claude", tmux_name: "acc-x", native_id: null, desired_state: "running", status_source: "claude_hook",
    activity: null, activity_at: null, worktree: null, worktree_branch: null, changes: null, remote_url: null, recovery_attempts: 0,
    next_attempt_at: null, last_error: null, model: null, permission_mode: "acceptEdits", management: "acc", run_as: "alex-agent",
    external_unit: null, workdir: null, standalone: false, cwd: "/srv/acc-demo/demo-api" };
  const row = (id: string, name: string, project: string, state: string, minutes: number) =>
    ({ ...base, id, name, project, observed_state: state, status_at: at(minutes), last_output_at: at(minutes), created_at: at(minutes + 60) });
  return [
    row("aaaaaaaaaaa1", "notes-cleanup", "sam-notes", "idle", 1),
    row("aaaaaaaaaaa2", "go-no-go", "local", "idle", 2),
    row("aaaaaaaaaaa3", "market-scan", "local", "idle", 3),
    row("aaaaaaaaaaa4", "checkout-flow", "demo-web", "active", 4),
    row("aaaaaaaaaaa5", "ui-polish", "demo-web", "active", 5),
    row("aaaaaaaaaaa6", "api-refactor", "demo-api", "waiting_input", 30),
    { ...row("aaaaaaaaaaa7", "old-spike", "demo-api", "stopped", 600), desired_state: "stopped" },
  ];
}

export async function routeDemoSessions(page: Page) {
  await page.route("**/api/sessions", (route) => route.request().method() === "GET"
    ? route.fulfill({ contentType: "application/json", body: JSON.stringify(demoSessions()) }) : route.continue());
}

/** The pages and states the axe gate covers (UX plan WP5), in the current colour scheme. Grid and session pages hold
    live terminals; xterm's own markup is excluded there, as in the gate in 10-app (terminal accessibility is WP6). */
export const AXE_PAGES = ["/", "/inbox", "/sessions", "/grid", "/projects", "/workspaces", "/search", "/schedules", "/publishing",
  "/host", "/host?machine=lab-box", "/board", "/providers", "/catalog", "/audit", "/settings#all", "/guide"];
export const SESSION_TABS = ["", "?tab=changes", "?tab=files", "?tab=spec", "?tab=activity", "?tab=handoffs", "?tab=preview"];

export async function expectAxeCleanPage(page: Page, url: string, ready?: () => Promise<unknown>) {
  const { default: AxeBuilder } = await import("@axe-core/playwright");
  await page.goto(url);
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await ready?.();
  const builder = new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).exclude(".xterm");
  await expectNoAxeViolations(await analyzeSettledAxe(page, builder));
}

/** A git project with an uncommitted change and a session in its checkout, so Changes and Files have rows. */
export async function sessionWithChanges(page: Page, prefix: string) {
  const { id, folder, headers } = await gitProject(page, prefix);
  writeFileSync(`${folder}/app.py`, "print('hello')\nprint('changed')\n");
  writeFileSync(`${folder}/README.md`, "# Demo\n\nA new file.\n");
  const created = await page.request.post("/api/sessions", { headers,
    data: { project_id: id, provider: "claude", name: `${prefix.slice(0, 7)}-${randomUUID().replaceAll("-", "")}` } });
  if (!created.ok()) throw new Error(`session creation failed (${created.status()}): ${await created.text()}`);
  const session = await created.json();
  return session.id as string;
}

/** A Files row button: "Open <path>" or "Toggle <path>", then its git status in words when it has one (UX review NAV-7). */
export const fileRow = (path: string, verb: "Open" | "Toggle" = "Open") =>
  new RegExp(`^${verb} ${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(, (new|modified|ignored))?$`);

/** New session keeps model, permissions, run mode, worktree and the command preview under Advanced (UX WP7). */
export async function openAdvanced(scope: import("@playwright/test").Locator) {
  const button = scope.getByRole("button", { name: /^Advanced/ });
  if (await button.getAttribute("aria-expanded") !== "true") await button.click();
}

/** A QR code a phone camera reads: measured from the pixels the browser painted (so a transparent QR shows the page's
 * theme behind it), it has a light background and really dark modules whatever the theme, a quiet zone of at least
 * four modules on every side, and finder patterns in three corners (break-it 10). */
export async function expectScannableQr(qr: import("@playwright/test").Locator, where: string) {
  const png = (await qr.screenshot({ animations: "disabled" })).toString("base64");
  const result = await qr.page().evaluate(async (base64: string) => {
    const image = new Image();
    image.src = `data:image/png;base64,${base64}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d")!;
    context.drawImage(image, 0, 0);
    // Leave out a 2px frame: an element screenshot at a fractional position takes in the page's edge around the box.
    const { data, width, height } = context.getImageData(2, 2, canvas.width - 4, canvas.height - 4);
    const luminance = (x: number, y: number) => { const i = (y * width + x) * 4; return 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]; };
    const dark = (x: number, y: number) => luminance(x, y) < 80;
    let top = height, left = width, right = -1, bottom = -1, darkest = 255;
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      if (dark(x, y)) { top = Math.min(top, y); left = Math.min(left, x); right = Math.max(right, x); bottom = Math.max(bottom, y); darkest = Math.min(darkest, luminance(x, y)); }
    }
    let run = 0;
    while (left + run < width && dark(left + run, top)) run++;  // the top-left finder pattern's top edge: 7 modules
    const module = run / 7;
    const middle = (from: number) => from + Math.floor(module / 2);
    return {
      background: luminance(1, 1), darkest, module,
      margins: [left, top, width - 1 - right, height - 1 - bottom].map(margin => margin + 2),  // the frame left out
      finders: [dark(middle(left), middle(top)), dark(right - Math.floor(module / 2), middle(top)), dark(middle(left), bottom - Math.floor(module / 2))],
    };
  }, png);
  expect(result.background, `${where}: a light background, whatever the theme`).toBeGreaterThan(200);
  expect(result.darkest, `${where}: dark modules`).toBeLessThan(40);
  expect(result.module, `${where}: a measurable module`).toBeGreaterThanOrEqual(1);
  for (const margin of result.margins) expect(margin, `${where}: a quiet zone of four modules`).toBeGreaterThanOrEqual(4 * result.module - 1);
  expect(result.finders, `${where}: finder patterns in three corners`).toEqual([true, true, true]);
}

/** Existing feature journeys open their controls through the real disclosure buttons. */
export async function expandTeamPanels(page: Page) {
  const section = new URL(page.url()).hash === '#settings' ? 'Settings' : 'Overview';
  const content = page.getByRole('tabpanel', { name: 'Team', exact: true })
    .getByRole('tabpanel', { name: section, exact: true });
  await expect(content).toBeVisible();
  for (const button of await content.locator('[data-team-panel] > section > button').all()) {
    if (await button.getAttribute('aria-expanded') === 'false') await button.click();
  }
}

/** Process owned fixture bytes through the normal readers and team request lock. */
export async function settleSession(page: import('@playwright/test').Page, id: string) {
  const response = await page.request.post(`/e2e-test/sessions/${id}/settle`);
  expect(response.ok(), await response.text()).toBeTruthy();
}
