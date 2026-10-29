// Screenshots for ACC's own (public) wiki, taken from the demo instance (scripts/demo_instance.py), never production.
//   node web/scripts/wiki-shots.mjs <demo-dir> [out-dir, default docs/wiki/.attachments] [--publication-only]
// Each shot shows the state its wiki page describes. Re-run after any UI change and commit the images.
import { chromium } from "@playwright/test";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const providersOnly = args.includes('--providers-only');
const telemetryOnly = args.includes('--telemetry-only');
const dnsOnly = args.includes('--dns-only');
const setupOnly = args.includes('--setup-only');
const projectOnly = args.includes('--project-only');
const publicationOnly = args.includes('--publication-only');
const [demo, outArg] = args.filter(arg => !['--settings-only', '--setup-only', '--project-only', '--dns-only', '--telemetry-only', '--providers-only', '--publication-only'].includes(arg));
const out = outArg ?? path.resolve(here, "../../docs/wiki/.attachments");
const h = JSON.parse(readFileSync(`${demo}/harness.json`, "utf8"));
if (publicationOnly && (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(h.origin).hostname)
    || h.artificial !== true || h.source !== 'scripts/demo_instance.py'))
  throw new Error('Publication captures require the disposable artificial-data demo');
mkdirSync(out, { recursive: true });
const api = async (p) => (await fetch(h.origin + p, { headers: { cookie: `acc_session=${h.cookie}` } })).json();

const browser = await chromium.launch();
async function context(viewport = { width: 1440, height: 900 }, theme = "dark", who = { cookie: h.cookie, csrf: h.csrf }) {
  const phone = viewport.width < 600;
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: phone ? 2 : 1, isMobile: phone, hasTouch: phone });
  const host = new URL(h.origin).hostname;
  // Headless Chromium always reports notifications as denied; show the first-run state a real browser has.
  await ctx.addInitScript(() => { try { Object.defineProperty(Notification, "permission", { get: () => "default" }); } catch {} });
  if (who) await ctx.addCookies([{ name: "acc_session", value: who.cookie, domain: host, path: "/" }, { name: "acc_csrf", value: who.csrf, domain: host, path: "/" }]);
  await ctx.addInitScript((t) => { try { localStorage.setItem("acc-theme", t); } catch {} }, theme);
  return ctx;
}
const page = await (await context()).newPage();
const go = async (p) => { await page.goto(h.origin + p); await page.waitForLoadState("networkidle").catch(() => {}); await page.waitForTimeout(700); };
const shot = async (name, opts = {}) => { await page.screenshot({ path: `${out}/${name}.png`, ...opts }); console.log(`  ${name}.png`); };
const dialog = () => page.getByRole("dialog");

// W4 can refresh only Settings, without driving unrelated pages or their demo actions.
async function settingsShots() {
  await go("/settings"); await shot("settings"); await shot("settings-sections");
  const phoneContext = await context({ width: 390, height: 844 });
  const phone = await phoneContext.newPage();
  await phone.goto(h.origin + "/settings");
  await phone.getByRole("heading", { name: "Passkeys", exact: true }).waitFor();
  await phone.waitForLoadState("networkidle");
  await phone.screenshot({ path: `${out}/settings-sections-phone.png` });
  console.log("  settings-sections-phone.png");
  await phoneContext.close();
  await go("/settings#all"); await shot("settings-all-sections");
  for (const [section, name] of [
    ["guardrails", "settings-guardrails"], ["budgets", "settings-budgets"],
    ["certificate", "settings-certificate"], ["updates", "updates-settings"],
    ["access-devices", "settings-your-devices"], ["container-limits", "settings-container-limits"],
    ["notifications", "settings"],
    ["dns", "settings-dns"], ["risk-labels", "risk-labels-settings"],
  ]) {
    await go(`/settings#${section}`);
    await page.locator(`#${section}`).waitFor({ state: "visible" });
    await shot(name);
  }
  await go("/settings#people");
  await page.getByRole("combobox", { name: "Agent account" }).selectOption("+new").catch(() => {});
  await page.getByRole("textbox", { name: "Their name" }).fill("jordan").catch(() => {});
  await shot("settings-people");
  await go("/settings#keys"); await shot("settings-keys");
  await page.getByRole("button", { name: "Paste .env" }).click();
  await dialog().getByLabel(".env contents").fill("# from the old server\nexport GITHUB_TOKEN=demo-not-a-real-token\nMAPS_API_KEY='demo-not-a-real-key'\nPATH=/usr/bin\n");
  await dialog().getByRole("button", { name: "Read it" }).click();
  await dialog().getByRole("list", { name: "Keys found", exact: true }).waitFor();
  await shot("settings-keys-paste");
  await dialog().getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "Add key" }).click();
  await shot("settings-keys-add");
  await dialog().getByRole("button", { name: "Close" }).click();
}
if (args.includes("--settings-only")) {
  await settingsShots();
  await browser.close();
  process.exit(0);
}

const sessions = await api("/api/sessions");
const projects = await api("/api/projects");
const byName = (n) => sessions.find((s) => s.name === n);
const demoApi = projects.find((p) => p.slug === "demo-api");
const research = byName("market-scan");
const folder = research.workdir.split("/").pop();

if (publicationOnly) {
  const { createHash } = await import('node:crypto');
  const records = [];
  const capture = async (target, name, subject) => {
    await target.evaluate(() => document.fonts.ready);
    await target.screenshot({ path: `${out}/${name}.png`, animations: 'disabled' });
    records.push({ path: `docs/wiki/.attachments/${name}.png`, scene: subject,
      sha256: createHash('sha256').update(readFileSync(`${out}/${name}.png`)).digest('hex') });
    console.log(`  ${name}.png`);
  };
  await go('/');
  await page.getByRole('heading', { name: 'Overview', exact: true }).waitFor();
  await capture(page, 'overview', 'Overview with fictional projects and fixed host metrics');
  for (const endpoint of ['projects', 'sessions'])
    await page.route(`**/api/${endpoint}`, route => route.fulfill({ json: [] }));
  await page.route('**/api/accounts', route => route.fulfill({
    json: { claude: { logged_in: false }, account: 'alex-agent', accounts: [] },
  }));
  await page.evaluate(() => localStorage.removeItem('acc.getStarted'));
  await page.reload(); await page.getByRole('list', { name: 'Get started steps' }).waitFor();
  await capture(page, 'get-started', 'Empty artificial install with all three setup actions');
  for (const endpoint of ['projects', 'sessions', 'accounts']) await page.unroute(`**/api/${endpoint}`);
  await go('/audit'); await capture(page, 'audit', 'Fictional audit history');
  await go('/host'); await capture(page, 'host', 'Fixed artificial host snapshot');
  await page.getByRole('heading', { name: 'Machines', exact: true }).scrollIntoViewIfNeeded();
  await capture(page, 'host-machines', 'Fictional enrolled machines');
  await capture(page, 'nodes-mac-update', 'Fictional Mac on an older node version');
  await page.getByText('Agent accounts', { exact: true }).scrollIntoViewIfNeeded();
  await capture(page, 'host-accounts', 'Fictional agent accounts and fixed resource use');
  await go('/host?machine=build-box');
  await page.getByText(/Reported by build-box/).waitFor();
  await capture(page, 'host-machine-picker', 'Fixed demo node details');
  await page.route('**/api/host?machine=build-box', async route => {
    const view = await (await route.fetch()).json();
    view.last_backup_via_public_address = '2026-01-15T10:00:00+00:00';
    await route.fulfill({ json: view });
  });
  await page.reload(); await page.getByText(/Last backup via public address/).waitFor();
  await capture(page, 'host-machine-public-backup', 'Fictional public backup receipt');
  await page.unroute('**/api/host?machine=build-box');
  await page.route('**/api/host?machine=build-box', async route => {
    const view = await (await route.fetch()).json();
    view.snapshot.system = 'mac';
    view.snapshot.disks = [
      { mount: '/System/Volumes/Data', device: '/dev/disk3s5', total: 100e9, used: 25e9, free: 75e9, percent: 25 },
      { mount: '/Volumes/External', device: '/dev/disk10s1', total: 100e9, used: 40e9, free: 60e9, percent: 40 },
    ];
    await route.fulfill({ json: view });
  });
  await page.reload(); await page.getByText('Macintosh HD (data)', { exact: true }).waitFor();
  const storage = page.getByRole('heading', { name: 'Storage', exact: true }).locator('xpath=ancestor::section[1]');
  await storage.scrollIntoViewIfNeeded();
  await capture(storage, 'host-macos-data', 'Fixed fictional APFS data and external volumes');
  await page.unroute('**/api/host?machine=build-box');

  await go('/providers');
  await capture(page, 'providers', 'Fictional provider accounts and usage');
  await capture(page, 'desktop-linux-providers', 'Shared provider UI in the browser demo');
  const provider = page.getByRole('article', { name: 'Claude Code account', exact: true });
  await provider.scrollIntoViewIfNeeded();
  await capture(provider, 'providers-software', 'Demo provider software controls');
  await go('/settings#budgets'); await page.locator('#budgets').scrollIntoViewIfNeeded();
  await capture(page, 'settings-budgets', 'Fictional plan limits');
  await go('/settings#issue-sources');
  await page.getByRole('button', { name: 'Add git server' }).click();
  const server = page.getByRole('dialog', { name: 'Allow git server' });
  await server.getByLabel('Server HTTPS base URL').fill('https://forge.example.lan');
  await page.route('**/api/issue-servers/resolve', route => route.fulfill({
    json: { base_url: 'https://forge.example.lan', addresses: ['192.0.2.8'] },
  }));
  await server.getByRole('button', { name: 'Resolve addresses' }).click();
  await server.getByText('192.0.2.8', { exact: true }).waitFor();
  await capture(server, 'settings-issue-server-confirm', 'Documentation-only address approval');
  await server.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.route('**/api/issue-servers', route => route.fulfill({ json: [{
    id: 'demo-server', base_url: 'https://forge.example.lan', addresses: ['192.0.2.8'], custom_ca: false,
  }] }));
  await page.reload(); await page.getByRole('button', { name: 'Re-confirm addresses' }).waitFor();
  const issues = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Issue sources', exact: true }) });
  await capture(issues, 'settings-issue-servers', 'Illustrative allowed server with a documentation address');
  await page.unroute('**/api/issue-servers/resolve');
  await page.unroute('**/api/issue-servers');

  await go(`/projects/${demoApi.id}`);
  await capture(page, 'project-page', 'Fictional Demo API project');
  for (const [section, name] of [['keys', 'project-keys'], ['backup', 'project-backup']]) {
    await go(`/projects/${demoApi.id}#project-${section}`);
    await page.locator(`#project-${section}`).waitFor();
    await capture(page, name, `Demo API ${section} settings`);
  }
  await go(`/sessions/${research.id}?tab=handoffs`);
  await capture(page, 'workspace-findings', 'Fictional market research findings');
  await page.getByRole('button', { name: 'Promote to project' }).click();
  await capture(page, 'promote-dialog', 'Fictional workspace promotion');
  await page.keyboard.press('Escape');
  const at = new Date().toISOString();
  await page.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ json: [{
    id: 9001, kind: 'publish_request', severity: 'critical', source: 'demo session', verified: true,
    session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
    acked_at: null, resolved_at: null, publish_id: 'pub-000000000001',
    title: 'Retarget live site shop.example.com', detail: 'Fictional preview: replace the old storefront',
    publish: { host: 'shop.example.com', port: 8004, access: 'public',
      replaces: { id: 'pub-000000000000', port: 8020, access: 'public', project: 'storefront', decided_by: 'alex' } },
  }] }));
  await go('/inbox');
  await page.getByRole('button', { name: 'Review and approve' }).click();
  await page.getByRole('dialog', { name: 'Replace the live site shop.example.com?' }).waitFor();
  await capture(page, 'publishing-retarget', 'Fictional retarget confirmation; not applied');
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.unroute(/\/api\/attention(?:\?.*)?$/);
  await go('/settings#tool-approvals');
  const policy = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Tool approvals', exact: true }) });
  await policy.scrollIntoViewIfNeeded();
  await capture(policy, 'tools-policy', 'Demo per-person and project tool approvals');
  await go('/guide/User-Guide/FAQ');
  await page.getByRole('heading', { name: /FAQ|Frequently asked/ }).first().waitFor();
  await capture(page, 'guide', 'Guide FAQ: text only, no recursive screenshot thumbnails');

  const checkout = await api(`/api/sessions/${byName('checkout-flow').id}`);
  const artifacts = `${checkout.worktree}/.acc/artifacts`;
  mkdirSync(artifacts, { recursive: true });
  writeFileSync(`${artifacts}/demo-tests.log`, 'FICTIONAL DEMO OUTPUT\n\ncheckout: passed\nempty cart: passed\n2 example tests passed\n');
  await go(`/sessions/${checkout.id}?tab=artifacts`);
  await page.getByText('demo-tests.log', { exact: true }).first().waitFor();
  await capture(page, 'session-artifacts', 'Readable fictional test log; no embedded thumbnail');
  const statusFixture = async target => {
    const base = { source: 'demo', severity: 'warn', verified: true, detail: 'Fictional example',
      session_id: null, session_name: null, project: null, created_at: at, updated_at: at,
      acked_at: null, resolved_at: null, snoozed_until: null, publish_id: null };
    await target.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ json:
      ['machine_offline', 'backup_failed', 'backup_stale'].map((persistent, i) => ({
        ...base, id: 6401 + i, kind: i ? 'backup' : 'machine_offline', persistent,
        title: ['demo-cloud offline', 'Demo backup failed', 'Demo backup stale'][i],
      })) }));
  };
  await statusFixture(page); await go('/');
  await page.getByRole('region', { name: 'Inbox status' }).getByText('Machine offline: 1', { exact: true }).waitFor();
  await capture(page, 'inbox-status-desktop', 'Artificial offline, failed and stale counts above Overview');
  await page.unroute(/\/api\/attention(?:\?.*)?$/);
  await page.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ status: 503, json: { detail: 'Demo outage' } }));
  await page.reload(); await page.getByText('Inbox unavailable', { exact: true }).waitFor();
  await capture(page, 'inbox-unavailable-desktop', 'Simulated Inbox-only outage with usable Overview');
  await page.unroute(/\/api\/attention(?:\?.*)?$/);
  for (const [width, height] of [[1440, 900], [1920, 1080], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await go('/board');
    await page.getByRole('heading', { name: 'Status board', exact: true }).waitFor();
    await capture(page, width === 390 ? 'status-board-phone' : `status-board-${width}`, 'Glance with fixed demo metrics');
    if (width === 390) {
      await page.getByRole('button', { name: 'Details', exact: true }).click();
      await capture(page, 'status-board-phone-details', 'Glance phone details from artificial metrics');
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  const light = await (await context(undefined, 'light')).newPage();
  await light.goto(h.origin + '/'); await light.waitForLoadState('networkidle');
  await capture(light, 'overview-light', 'Light-theme artificial overview');
  await light.context().close();
  const phone = await (await context({ width: 390, height: 844 })).newPage();
  await phone.goto(h.origin + '/providers'); await phone.waitForLoadState('networkidle');
  await capture(phone, 'providers-sections-phone', 'Fictional provider accounts at phone width');
  await statusFixture(phone);
  await phone.goto(h.origin + '/'); await phone.waitForLoadState('networkidle');
  await phone.getByText('Machine offline: 1', { exact: true }).waitFor();
  await capture(phone, 'inbox-status-phone', 'Artificial compact status at phone width');
  await phone.unroute(/\/api\/attention(?:\?.*)?$/);
  await phone.route(/\/api\/attention(?:\?.*)?$/, route => route.fulfill({ status: 503, json: { detail: 'Demo outage' } }));
  await phone.reload(); await phone.getByText('Inbox unavailable', { exact: true }).waitFor();
  await capture(phone, 'inbox-unavailable-phone', 'Simulated Inbox outage at phone width');
  await phone.context().close();
  const member = await (await context(undefined, 'dark', { cookie: h.member_cookie, csrf: h.member_csrf })).newPage();
  await member.goto(h.origin + '/'); await member.waitForLoadState('networkidle');
  await capture(member, 'member-overview', 'Fictional member Sam: own work only');
  await member.context().close();
  writeFileSync(`${out}/publication-captures.json`, JSON.stringify({
    source: 'scripts/demo_instance.py', artificial: true, host_metrics: 'tests/demo_host.py',
    records,
  }, null, 2) + '\n');
  await browser.close();
  process.exit(0);
}

if (providersOnly) {
  for (const width of [1440, 390]) for (const theme of ['dark', 'light']) {
    const ctx = await context({ width, height: width === 390 ? 844 : 900 }, theme);
    const providers = await ctx.newPage();
    await providers.goto(`${h.origin}/providers`);
    await providers.waitForLoadState('networkidle');
    await providers.getByRole('heading', { name: 'Providers', exact: true }).waitFor();
    await providers.evaluate(() => document.fonts.ready);
    const suffix = `${width === 390 ? '-phone' : ''}${theme === 'light' ? '-light' : ''}`;
    await providers.screenshot({ animations: 'disabled', path: `${out}/${width === 390 ? 'providers-sections-phone' : 'providers'}${theme === 'light' ? '-light' : ''}.png` });
    // Each provider's software controls are on its own tile now (UX follow-up to W11): show Claude Code's.
    const tile = providers.getByRole('article', { name: 'Claude Code account', exact: true });
    await tile.scrollIntoViewIfNeeded();
    await tile.screenshot({ animations: 'disabled', path: `${out}/providers-software${suffix}.png` });
    for (const [id, summary, name] of [
      ['models', /^Model catalog/, 'providers-models'],
    ]) {
      const section = providers.locator(`#provider-${id}`);
      const toggle = section.locator('summary').filter({ hasText: summary });
      if (await toggle.count()) await toggle.click();
      await section.scrollIntoViewIfNeeded();
      await section.screenshot({ animations: 'disabled', path: `${out}/${name}${suffix}.png` });
    }
    await ctx.close();
  }
  await browser.close();
  process.exit(0);
}

// Focused telemetry screenshots use the demo server; previewing does not send anything.
if (telemetryOnly) {
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(h.origin).hostname))
    throw new Error('Telemetry screenshots require the disposable loopback demo');
  // Keep the public screenshots artificial. Both choices remain off; no deletion or sending.
  const configured = await fetch(h.origin + '/api/telemetry', { method: 'PUT',
    headers: { cookie: `acc_session=${h.cookie}; acc_csrf=${h.csrf}`, 'x-acc-csrf': h.csrf,
      origin: h.origin, 'content-type': 'application/json' },
    body: JSON.stringify({ usage: false, reports: false, url: 'https://telemetry.example.com', delete_previous: false }) });
  if (!configured.ok) throw new Error('Could not prepare demo privacy choices');
  for (const width of [1440, 390]) for (const theme of ['dark', 'light']) {
    const ctx = await context({ width, height: width === 390 ? 844 : 900 }, theme);
    const telemetry = await ctx.newPage();
    await telemetry.goto(h.origin + '/settings#telemetry');
    const card = telemetry.locator('#telemetry');
    await card.getByRole('button', { name: 'Show exactly what would be sent' }).waitFor();
    await card.scrollIntoViewIfNeeded();
    const prefix = `telemetry-${width === 390 ? 'phone' : 'desktop'}-${theme}`;
    await telemetry.screenshot({ path: `${out}/${prefix}.png` });
    if (width === 1440 && theme === 'dark') await card.screenshot({ path: `${out}/usage-and-bug-reports-settings.png` });
    const operations = card.locator('summary').filter({ hasText: /^Collector and consent details$/ });
    if (await operations.count()) {
      await operations.click();
      await operations.evaluate(node => node.scrollIntoView({ block: 'start' }));
      await telemetry.screenshot({ path: `${out}/${prefix}-details.png` });
      await operations.click();
    }
    await card.getByRole('button', { name: 'Show exactly what would be sent' }).click();
    await card.getByRole('region', { name: 'Usage payload' }).waitFor();
    await card.getByRole('region', { name: 'Usage payload' }).scrollIntoViewIfNeeded();
    await telemetry.screenshot({ path: `${out}/${prefix}-preview.png` });
    // Read-only artificial report scene, never submitted to any collector.
    const state = await api('/api/telemetry');
    const report = { id: 'example-report', kind: 'reports', state: 'queued', created: 1792800000,
      result: '', attempts: 0, receipt: '', digest: 'a'.repeat(64), destination: 'https://telemetry.example.com',
      body: JSON.stringify({ schema: 'report/1', report: '11111111-1111-4111-8111-111111111111',
        install: 'a'.repeat(22), acc: 'dev', build: 'dev', os: 'linux', install_kind: 'git-checkout',
        trigger: 'supervisor_stage', component: 'supervisor', exception: 'TimeoutError',
        fingerprint: 'a'.repeat(16), repeats: '1', frames: [], message: 'Example refresh failed safely', log: [],
        provider: null, exit_code: null }) };
    await telemetry.route(/\/api\/telemetry(?:\?.*)?$/, route => route.fulfill({ json: { ...state,
      consent: { ...state.consent, reports: true }, total: 1, items: [report] } }));
    await telemetry.reload();
    const pending = card.getByRole('group', { name: 'Reports awaiting review' });
    await pending.locator('summary').click();
    const reportBody = pending.getByRole('region', { name: 'Payload example-report' }).locator('..');
    await reportBody.scrollIntoViewIfNeeded();
    await reportBody.screenshot({ path: `${out}/${prefix}-review.png` });
    if (theme === 'dark') await reportBody.screenshot({ path: `${out}/usage-and-bug-reports-review${width === 390 ? '-phone' : ''}.png` });
    await ctx.close();
  }
  await browser.close();
  process.exit(0);
}

// DNS-only captures use real demo authentication and never submit provider credentials.
if (dnsOnly) {
  for (const width of [1440, 390]) for (const theme of ['dark', 'light']) {
    const ctx = await context({ width, height: width === 390 ? 844 : 900 }, theme);
    const dns = await ctx.newPage();
    await dns.goto(h.origin + '/settings#dns');
    await dns.getByRole('button', { name: 'Add infrastructure credential' }).click();
    for (const provider of ['cloudflare', 'azure', 'route53', 'googleclouddns', 'digitalocean', 'hetzner']) {
      await dns.getByLabel('Credential provider').selectOption(provider);
      const prefix = `dns-${provider}-${width === 390 ? 'phone' : 'desktop'}-${theme}`;
      await dns.getByLabel('Credential name').scrollIntoViewIfNeeded();
      await dns.screenshot({ path: `${out}/${prefix}.png` });
      const secretField = { cloudflare: 'Cloudflare API token', azure: 'Client secret', route53: 'Secret access key',
        googleclouddns: 'Service account JSON', digitalocean: 'DigitalOcean API token', hetzner: 'Hetzner DNS API token' }[provider];
      await dns.getByLabel(secretField, { exact: true }).scrollIntoViewIfNeeded();
      await dns.screenshot({ path: `${out}/${prefix}-fields.png` });
      const summary = dns.getByRole('dialog').locator('details summary');
      if (await summary.count()) {
        await summary.click();
        await summary.evaluate(node => node.scrollIntoView({ block: 'start' }));
        await dns.screenshot({ path: `${out}/${prefix}-help.png` });
        await summary.click();
      }
    }
    await ctx.close();
  }
  await browser.close();
  process.exit(0);
}

// Project-only refresh leaves parallel Settings/session screenshot work untouched.
if (projectOnly) {
  for (const width of [1440, 390]) for (const theme of ['dark', 'light']) {
    const ctx = await context({ width, height: width === 390 ? 844 : 900 }, theme);
    const project = await ctx.newPage();
    const prefix = `project-${width === 390 ? 'phone' : 'desktop'}-${theme}`;
    await project.goto(`${h.origin}/projects/${demoApi.id}`);
    await project.waitForLoadState('networkidle');
    await project.getByRole('heading', { name: demoApi.name, exact: true, level: 1 }).waitFor();
    await project.screenshot({ path: `${out}/${prefix}.png` });
    // Each section is a tab of its own (UX follow-up to W7): its link opens it.
    for (const section of ['repository', 'knowledge', 'keys', 'backup', 'ports', 'stack']) {
      await project.goto(`${h.origin}/projects/${demoApi.id}#project-${section}`);
      await project.waitForLoadState('networkidle');
      const target = project.locator(`#project-${section}`);
      if (await target.count()) {
        await target.scrollIntoViewIfNeeded();
        await target.screenshot({ path: `${out}/${prefix}-${section}.png` });
      }
    }
    await ctx.close();
  }
  await browser.close();
  process.exit(0);
}

// A focused mode avoids refreshing unrelated wiki pages when only Setup changes.
if (setupOnly) {
  for (const width of [1440, 390]) for (const theme of ['dark', 'light']) {
    const ctx = await context({ width, height: width === 390 ? 844 : 900 }, theme);
    const setup = await ctx.newPage();
    await setup.goto(`${h.origin}/sessions/${byName('api-refactor').id}?tab=spec`);
    await setup.waitForLoadState('networkidle');
    await setup.getByRole('heading', { name: 'How this session was set up' }).waitFor();
    const prefix = `session-setup-${width === 390 ? 'phone' : 'desktop'}-${theme}`;
    await setup.screenshot({ path: `${out}/${prefix}.png` });
    const files = setup.locator('details').filter({ has: setup.locator('summary').filter({ hasText: /^Generated files/ }) });
    if (await files.count()) {
      await files.locator('summary').click();
      await files.scrollIntoViewIfNeeded();
      await setup.screenshot({ path: `${out}/${prefix}-files.png` });
    }
    await ctx.close();
  }
  await browser.close();
  process.exit(0);
}

await go("/"); await shot("overview");
await go("/sessions"); await shot("sessions-list");
await go(`/sessions/${byName("api-refactor").id}`); await shot("session-page");
await go(`/sessions/${byName("api-refactor").id}?tab=activity`); await shot("session-activity");
await go(`/sessions/${byName("api-refactor").id}?tab=changes`); await shot("session-changes");
await go(`/sessions/${byName("checkout-flow").id}?tab=changes`); await shot("session-worktree");
{  // a handoff note the agent wrote in its own worktree (not yet merged into the project's checkout)
  const checkout = await api(`/api/sessions/${byName("checkout-flow").id}`);
  mkdirSync(`${checkout.worktree}/docs/handoffs`, { recursive: true });
  writeFileSync(`${checkout.worktree}/docs/handoffs/2026-10-22-${checkout.id}-checkout.md`,
    "# Checkout flow\n\nOne-page checkout works; the cart survives a refresh.\n\n## Tests\n\n- e2e checkout: pass\n");
  await go(`/sessions/${checkout.id}?tab=handoffs`); await shot("session-handoffs");
}
{  // what an agent leaves for its reviewer in .acc/artifacts: a screenshot of the page it built and its test log
  const checkout = await api(`/api/sessions/${byName("checkout-flow").id}`);
  const folder = `${checkout.worktree}/.acc/artifacts`;
  mkdirSync(folder, { recursive: true });
  const built = await (await context({ width: 960, height: 600 }, "light", null)).newPage();
  await built.setContent(`<body style="font:15px system-ui;margin:0;background:#f6f7f9;color:#1d2330">
    <main style="max-width:520px;margin:48px auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 1px 3px #0002">
    <h1 style="font-size:22px;margin:0 0 18px">Checkout</h1>
    <p style="display:flex;justify-content:space-between"><span>2 × Tournament pass</span><b>$48.00</b></p>
    <p style="display:flex;justify-content:space-between"><span>Shipping</span><b>Free</b></p><hr>
    <p style="display:flex;justify-content:space-between;font-size:18px"><span>Total</span><b>$48.00</b></p>
    <button style="width:100%;padding:12px;border:0;border-radius:8px;background:#4f5bd5;color:#fff;font-size:15px">Pay now</button>
    </main></body>`);
  await built.screenshot({ path: `${folder}/checkout.png` });
  await built.context().close();
  writeFileSync(`${folder}/e2e.log`, "npx playwright test checkout\n\n  ✓ cart survives a refresh (1.2s)\n  ✓ one-page checkout pays (2.4s)\n"
    + "  ✓ an empty cart can't check out (0.8s)\n\n  3 passed (5.1s)\n");
  await go(`/sessions/${checkout.id}?tab=artifacts`); await shot("session-artifacts");
}
{  // a tile is narrower than the session page; the demo's fake CLI can't reflow like a real one, so print a narrow screen
  const run = `${h.run_dir}/${byName("api-refactor").id}`;
  copyFileSync(`${run}/fake.screen.phone`, `${run}/fake.screen`);
  await go(`/grid?ids=${["api-refactor", "ui-polish", "checkout-flow", "market-scan"].map((n) => byName(n).id).join(",")}`);
  writeFileSync(`${run}/fake.ctl`, "screen"); await page.waitForTimeout(1500); await shot("grid");
  copyFileSync(`${run}/fake.screen.desktop`, `${run}/fake.screen`); writeFileSync(`${run}/fake.ctl`, "screen");
}
await go("/search?q=refunds%20idempot"); await page.waitForTimeout(800); await shot("search");

await go("/");
await page.getByRole("button", { name: "New session" }).click();
await dialog().getByRole("combobox", { name: "Project" }).selectOption({ label: "Demo API" });
await dialog().getByRole("textbox", { name: /^Task/ }).fill("Add refunds to the payments API, with tests.");
await dialog().getByRole("combobox", { name: "Profile" }).selectOption("fullstack");
await page.waitForTimeout(600);
await shot("new-session-dialog");
await dialog().getByRole("button", { name: /^Advanced/ }).click();  // model, permissions, skills and the preview
await dialog().getByRole("button", { name: /Skills and tools/ }).click();
await page.waitForTimeout(300);
await shot("new-session-skills");
await dialog().getByRole("button", { name: /Skills and tools/ }).click();
await dialog().getByRole("combobox", { name: "Agent", exact: true }).selectOption("opencode");
await dialog().getByLabel("Model").selectOption("__custom");
await dialog().getByPlaceholder("provider/model").fill("opencode/big-pickle");
await page.waitForTimeout(600);
await shot("new-session-opencode");
await dialog().getByRole("button", { name: "Close" }).click();

await go("/workspaces");
await page.getByRole("button", { name: "New workspace" }).first().click();
await dialog().getByRole("button", { name: "Choose session" }).click();  // Research / Council opens New session as a workspace
await dialog().getByLabel("Topic").fill("Tournament scheduling apps");
await page.waitForTimeout(600);
await shot("new-workspace-dialog");
await dialog().getByRole("button", { name: "Close" }).click();
await go("/workspaces"); await shot("workspaces");
await go(`/sessions/${research.id}?tab=handoffs`); await shot("workspace-findings");
await page.getByRole("button", { name: "Promote to project" }).click();
await page.waitForTimeout(300);
await shot("promote-dialog");

await go("/projects"); await shot("projects");
await go(`/projects/${demoApi.id}`); await shot("project-page");
await go(`/projects/${demoApi.id}/wiki`); await shot("project-wiki");
await go(`/projects/${demoApi.id}/wiki/User-Guide/Getting-Started`); await shot("project-wiki-page");

await go("/inbox"); await shot("inbox");
await page.getByRole("button", { name: "Review and approve" }).click(); await page.waitForTimeout(300);  // the port no project reserved
await shot("inbox-approve-confirm");
await dialog().getByRole("button", { name: "Cancel" }).click();
await go("/publishing"); await shot("publishing");
await go("/catalog"); await shot("catalog-profiles");
await page.getByRole("tab", { name: /Skills/ }).click(); await page.waitForTimeout(300); await shot("catalog-skills");
await page.getByRole("button", { name: "Add skill or agent" }).click(); await page.waitForTimeout(300); await shot("catalog-add-skill");
await dialog().getByRole("button", { name: "Close" }).click();
await page.getByRole("tab", { name: /Profiles/ }).click();
await page.getByRole("button", { name: "Edit Frontend" }).click(); await page.waitForTimeout(300); await shot("catalog-edit-profile");
await dialog().getByRole("button", { name: "Close" }).click();
await go(`/projects/${demoApi.id}`);
await page.locator("#project-ports > summary").click();
await page.getByText("Reserved for this project").scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
await shot("project-ports");
await go("/host"); await shot("host");
// Machines sits below the health cards: bring it to the top so the shot is about machines
{ const box = await page.getByRole("heading", { name: "Machines", exact: true }).boundingBox();
  await page.mouse.move(840, 450); await page.mouse.wheel(0, box.y - 100); await page.waitForTimeout(400); }
await shot("host-machines");
await page.getByRole("button", { name: "Add machine" }).click();
await dialog().getByLabel("Name").fill("gaming-pc");
await dialog().getByRole("button", { name: "Create enrollment code" }).click();
await page.waitForTimeout(800); await shot("add-machine");  // the Linux tab: the private-CA one-liner
await dialog().getByRole("tab", { name: "Windows" }).click(); await page.waitForTimeout(300); await shot("add-machine-windows");
await dialog().getByRole("tab", { name: "Mac" }).click(); await page.waitForTimeout(300); await shot("add-machine-mac");
await dialog().getByRole("button", { name: "Done" }).click();
await page.getByText("Agent accounts").first().scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
await shot("host-accounts");
await go("/providers"); await shot("providers");
await go("/audit"); await shot("audit");
await settingsShots();
await go(`/projects/${demoApi.id}`);
await page.locator("#project-keys > summary").click();
await page.getByText("Project keys (yours and shared ones)").scrollIntoViewIfNeeded(); await page.waitForTimeout(300);
await shot("project-keys");
await go("/guide"); await shot("guide");

await go("/");
await page.keyboard.press("Control+k");
await page.getByRole("dialog").getByRole("combobox").or(page.getByRole("dialog").getByRole("textbox")).first().fill("demo");
await page.waitForTimeout(300);
await shot("command-palette");

const light = await (await context(undefined, "light")).newPage();
await light.goto(h.origin + "/"); await light.waitForLoadState("networkidle").catch(() => {}); await light.waitForTimeout(700);
await light.screenshot({ path: `${out}/overview-light.png` }); console.log("  overview-light.png");
const phone = await (await context({ width: 390, height: 844 })).newPage();
await phone.goto(h.origin + "/"); await phone.waitForLoadState("networkidle").catch(() => {}); await phone.waitForTimeout(700);
await phone.screenshot({ path: `${out}/phone-overview.png` }); console.log("  phone-overview.png");
await phone.goto(`${h.origin}/sessions/${byName("api-refactor").id}`); await phone.waitForLoadState("networkidle").catch(() => {}); await phone.waitForTimeout(900);
// The demo's fake CLI can't reflow for a narrow window like a real one; have it print a phone-width screen
const run = `${h.run_dir}/${byName("api-refactor").id}`;
copyFileSync(`${run}/fake.screen.phone`, `${run}/fake.screen`); writeFileSync(`${run}/fake.ctl`, "screen");
await phone.waitForTimeout(1200);
await phone.screenshot({ path: `${out}/phone-terminal.png` }); console.log("  phone-terminal.png");
copyFileSync(`${run}/fake.screen.desktop`, `${run}/fake.screen`); writeFileSync(`${run}/fake.ctl`, "screen");  // ready for the next run

// A member's view: only their own projects and sessions, no Audit
const member = await (await context(undefined, "dark", { cookie: h.member_cookie, csrf: h.member_csrf })).newPage();
await member.goto(h.origin + "/"); await member.waitForLoadState("networkidle").catch(() => {}); await member.waitForTimeout(700);
await member.screenshot({ path: `${out}/member-overview.png` }); console.log("  member-overview.png");

// What someone opening an invite link sees
const invite = await fetch(h.origin + "/api/users/invites", { method: "POST",
  headers: { cookie: `acc_session=${h.cookie}; acc_csrf=${h.csrf}`, "x-acc-csrf": h.csrf, origin: h.origin, "content-type": "application/json" },
  body: JSON.stringify({ role: "member", account: (await api("/api/users")).default_account }) }).then((r) => r.json());
if (invite.link) {
  const guest = await (await context(undefined, "dark", null)).newPage();
  await guest.goto(h.origin + invite.link); await guest.waitForLoadState("networkidle").catch(() => {}); await guest.waitForTimeout(700);
  await guest.screenshot({ path: `${out}/invite.png` }); console.log("  invite.png");
}

await browser.close();
console.log(`wiki screenshots in ${out}`);
