#!/usr/bin/env node
/** Serial AC-056 baseline. Private fixture only; no Playwright config or product changes. */
import { chromium, request } from '@playwright/test';
import { spawn, execFileSync } from 'node:child_process';
import { createHash, createHmac } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, writeFileSync, openSync, closeSync, statSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cpus, loadavg, freemem, totalmem, platform, release } from 'node:os';
import { parseArgs } from 'node:util';
import { Traffic, median, polling, sockets, resumeTrials, expectedCancellation } from './metrics.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../../..');
const PYTHON = process.env.ACC_E2E_PYTHON ?? '/srv/acc/app/.venv/bin/python';
const { values: args } = parseArgs({ options: {
  base: { type: 'string', default: process.env.ACC_E2E_BASE ?? '/tmp/pb64' }, port: { type: 'string', default: process.env.ACC_E2E_PORT ?? '18964' },
  out: { type: 'string', default: join(HERE, 'results.json') }, smoke: { type: 'boolean', default: false },
  scales: { type: 'string', default: '10,100,500' },
  executable: { type: 'string', default: '/srv/acc/tools/ms-playwright/chromium-1247/chrome-linux64/chrome' },
  resume: { type: 'string' },
  'readings-probe': { type: 'boolean', default: false },
  'runtime-probe': { type: 'boolean', default: false },
  help: { type: 'boolean', default: false },
} });
if (args.help) {
  console.log('node web/e2e/perf/baseline.mjs [--base /tmp/pb64] [--port 18964] [--out FILE] [--scales 10,100,500] [--smoke] [--readings-probe] [--executable installed-chromium]\nDefault: five serial trials per shell/size, 10 s cold-route windows, 60 s visible/hidden polling, 300 s session dwell. Smoke uses one trial, 2 s cold windows, 1 s polling and a 5 s dwell; never a baseline. --readings-probe adds five alternating owner API comparisons after the browser trials.');
  process.exit(0);
}
if (args['runtime-probe'] && (args.smoke || args.resume || args['readings-probe'])) throw new Error('Runtime-only probes use a fresh base, five full route windows, and no resume/readings flags.');
const base = resolve(args.base), output = resolve(args.out), port = Number(args.port);
if (!base.startsWith('/tmp/') || base.length > 28 || existsSync(base)) throw new Error('Use a new, short, private /tmp base (max 28 characters).');
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Choose an unprivileged fixture port.');
if (!existsSync(args.executable)) throw new Error('Use an installed Chromium executable; this harness installs nothing.');
const scales = args.scales.split(',').map(Number);
if (!scales.length || scales.some(n => ![10, 100, 500].includes(n))) throw new Error('Supported project sizes: 10, 100, 500.');
const runs = args.smoke ? 1 : 5;
const coldMs = args.smoke ? 2000 : 10000, pollMs = args.smoke ? 1000 : 60000, dwellMs = args.smoke ? 5000 : 300000;
const wait = ms => new Promise(resolve => setTimeout(resolve, Math.max(0, ms)));
const log = message => console.log(`${new Date().toISOString()} ${message}`);
const checked = async (response, operation) => { if (!response.ok()) throw new Error(`${operation}: HTTP ${response.status()} ${await response.text()}`); return response.json(); };
const optionalFile = path => { try { return readFileSync(path, 'utf8').trim(); } catch { return null; } };
const sha = path => createHash('sha256').update(readFileSync(path)).digest('hex');
const machine = () => ({ at: new Date().toISOString(), load_1_5_15: loadavg(), free_memory_bytes: freemem() });
const result = { schema: 1, readings_probe_enabled: args['readings-probe'], harness_sha256: Object.fromEntries(['baseline.mjs', 'metrics.mjs', 'seed.py'].map(name => [name, sha(join(HERE, name))])),
  build_manifest_sha256: sha(join(ROOT, 'src/acc/server/static/.vite/manifest.json')), playwright_version: JSON.parse(readFileSync(fileURLToPath(import.meta.resolve('@playwright/test/package.json')), 'utf8')).version, smoke: args.smoke, revision: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim(),
  started_at: new Date().toISOString(), runs_per_condition: runs, viewport: { width: 1440, height: 900 },
  cold_window_ms: coldMs, polling_window_ms: pollMs, session_dwell_ms: dwellMs,
  browser_executable: args.executable, machine: { cpu: cpus()[0]?.model, logical_cpus: cpus().length, total_memory_bytes: totalmem(), os: platform(), kernel: release(), cpu_cgroup_max: optionalFile('/sys/fs/cgroup/cpu.max'), memory_cgroup_max: optionalFile('/sys/fs/cgroup/memory.max') },
  method: 'One serial browser/worker. Cold HTTP cache, new browser/profile per trial; real hidden tab through CDP noDefaults. One live fake CLI plus stopped history. Session dwell includes one hidden polling window. No forced GC. TTI is data-ready + header-button-to-palette paint proxy, not Lighthouse TTI.', trials: [], progress: null };
if (args['runtime-probe']) {
  result.mode = 'runtime-probe'; result.session_dwell_ms = null; result.polling_window_ms = null;
  result.runtime_probes = [];
  result.method = 'Five fresh new-shell Projects visits per size. Runtime request starts through the later of the 10-second window and action readiness/closing the palette. No memory or polling baseline; 15-second backend drain after closing each browser.';
}
mkdirSync(base, { mode: 0o700 }); mkdirSync(dirname(output), { recursive: true });
const save = () => { writeFileSync(`${output}.tmp`, JSON.stringify(result, null, 2) + '\n'); renameSync(`${output}.tmp`, output); };
const progress = message => { result.progress = { message, ...machine() }; save(); log(message); };
if (args.resume) {
  if (resolve(args.resume) === output) throw new Error('Archive the prior attempt first; resume input and output must differ');
  const source = JSON.parse(readFileSync(resolve(args.resume), 'utf8'));
  result.trials = resumeTrials(source, result.build_manifest_sha256);
  result.prior_attempts = [...(source.prior_attempts ?? []), { revision: source.revision, harness_sha256: source.harness_sha256, started_at: source.started_at, failure: source.failure ?? null, retained_trials: source.trials.length }];
  result.measurements_started_at = source.measurements_started_at ?? source.started_at;
  progress(`Resuming ${result.trials.length} verified completed observations from ${args.resume}`);
}
const timers = new Set();
const stopProcess = async child => {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  for (let i = 0; i < 100 && child.exitCode === null && child.signalCode === null; i++) await wait(100);
  if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await new Promise(resolve => child.once('exit', resolve)); }
};
let server, browserProcess, browser, api, currentBase;
let stopping = false;
const clean = async () => {
  if (stopping) return; stopping = true;
  for (const timer of timers) clearInterval(timer);
  try { await browser?.close(); } catch {}
  await stopProcess(browserProcess);
  try { await api?.dispose(); } catch {}
  await stopProcess(server);
  if (currentBase && existsSync(join(currentBase, 'info.json'))) {
    try { execFileSync(PYTHON, [join(ROOT, 'tests/owned_tmux.py'), currentBase], { cwd: ROOT, stdio: 'pipe' }); } catch (error) { result.cleanup_error = error.message; }
  }
};
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { result.interrupted = signal; save(); await clean(); process.exit(130); });

function totp(secret) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits = '';
  for (const char of secret.replace(/=+$/, '')) bits += alphabet.indexOf(char).toString(2).padStart(5, '0');
  const key = Buffer.from(bits.match(/.{8}/g).map(byte => parseInt(byte, 2)));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = createHmac('sha1', key).update(counter).digest(), offset = digest.at(-1) & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, '0');
}
async function startFixture(projects) {
  currentBase = join(base, `s${projects}`); mkdirSync(currentBase, { mode: 0o700 });
  writeFileSync(join(currentBase, 'perf-owned.json'), JSON.stringify({ pid: process.pid, projects }));
  const fd = openSync(join(currentBase, 'server.log'), 'w');
  server = spawn(PYTHON, [join(ROOT, 'tests/e2e_server.py'), currentBase, String(port)], { cwd: ROOT,
    env: { ...process.env, PYTHONPATH: join(ROOT, 'src'), ACC_E2E_PRODUCTION_INTERVALS: '1', ACC_E2E_DEVICE_CA: '0', ACC_E2E_REMOTE_EDGE: '0' }, stdio: ['ignore', fd, fd] }); closeSync(fd);
  writeFileSync(join(currentBase, 'server.pid'), String(server.pid));
  const origin = `http://localhost:${port}`;
  api = await request.newContext({ baseURL: origin, extraHTTPHeaders: { Origin: origin } });
  for (let i = 0; i < 300; i++) {
    if (server.exitCode !== null) throw new Error(`Private fixture exited: see ${currentBase}/server.log`);
    try { if ((await api.get('/api/health')).ok() && existsSync(join(currentBase, 'info.json'))) break; } catch {}
    if (i === 299) throw new Error('Fixture startup timeout'); await wait(200);
  }
  const info = JSON.parse(readFileSync(join(currentBase, 'info.json'), 'utf8'));
  if (!resolve(info.state).startsWith(`${currentBase}/`)) throw new Error('Fixture state is outside the owned base.');
  const setup_token = readFileSync(info.setup_token_path, 'utf8').trim();
  const begin = await checked(await api.post('/api/auth/setup/begin', { data: { setup_token } }), 'begin fixture owner');
  const password = 'artificial baseline owner password';
  await checked(await api.post('/api/auth/setup/complete', { data: { setup_token, username: 'alex', password, code: totp(begin.totp_secret) } }), 'create fixture owner');
  const state = await api.storageState(), csrf = state.cookies.find(c => /acc_csrf$/.test(c.name))?.value;
  if (!csrf) throw new Error('Fixture CSRF cookie missing');
  const headers = { 'x-acc-csrf': csrf };
  const feature = async () => {
    const registry = await checked(await api.get('/api/admin-features'), 'feature registry');
    return api.put('/api/admin-features/new_shell', { headers, data: { enabled: true, revision: registry.revision } });
  };
  let enabled = await feature();
  if (enabled.status() === 403) {
    await wait(30500 - Date.now() % 30000);
    await checked(await api.post('/api/auth/passkeys/reauth', { headers, data: { password, code: totp(begin.totp_secret) } }), 'verify fixture owner');
    enabled = await feature();
  }
  await checked(enabled, 'enable new-shell Preview');
  const project = await checked(await api.post('/api/projects', { headers, data: { slug: 'cedar-0001', name: 'Cedar 0001', workspace_path: info.workspace } }), 'create fixture project');
  await checked(await api.post(`/api/projects/${project.id}/trust`, { headers }), 'trust fixture folder');
  const live = await checked(await api.post('/api/sessions', { headers, data: { project_id: project.id, provider: 'claude', name: 'Cedar running session' } }), 'start fake CLI');
  const sessions = { 10: 5, 100: 50, 500: 200 }[projects];
  const counts = JSON.parse(execFileSync(PYTHON, [join(HERE, 'seed.py'), currentBase, String(projects), String(sessions), live.id], { cwd: ROOT, encoding: 'utf8' }));
  const actualProjects = await checked(await api.get('/api/projects'), 'verify project count');
  const actualSessions = await checked(await api.get('/api/sessions'), 'verify session count');
  if (actualProjects.length !== projects || actualSessions.length !== sessions) throw new Error('Fixture counts changed after seeding');
  return { origin, headers, storage: await api.storageState(), sessionId: live.id, counts };
}
async function launchBrowser(fixture) {
  const profile = mkdtempSync(join(base, 'chrome-'));
  const fd = openSync(join(profile, 'chrome.log'), 'w');
  browserProcess = spawn(args.executable, ['--headless=new', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions', '--disable-default-apps',
    '--disable-breakpad', '--disable-client-side-phishing-detection', '--metrics-recording-only', '--password-store=basic',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--window-size=1440,900', 'about:blank'], { stdio: ['ignore', fd, fd] }); closeSync(fd);
  writeFileSync(join(profile, 'browser.pid'), String(browserProcess.pid));
  for (let i = 0; i < 200 && !existsSync(join(profile, 'DevToolsActivePort')); i++) {
    if (browserProcess.exitCode !== null) throw new Error('Installed Chromium exited during launch'); await wait(100);
  }
  const [debugPort] = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n');
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`, { noDefaults: true });
  result.browser_version = browser.version();
  const context = browser.contexts()[0]; await context.addCookies(fixture.storage.cookies);
  const page = context.pages()[0]; page.setDefaultTimeout(45000); await page.setViewportSize(result.viewport); await page.emulateMedia({ colorScheme: 'dark' });
  const cdp = await context.newCDPSession(page); await cdp.send('Network.enable'); await cdp.send('Network.setCacheDisabled', { cacheDisabled: true }); await cdp.send('Network.setBypassServiceWorker', { bypass: true });
  await cdp.send('Performance.enable');
  const traffic = new Traffic(); traffic.socket_events = [];
  cdp.on('Network.webSocketCreated', e => traffic.socket_events.push({ kind: 'open', time: performance.now() }));
  for (const [name, kind] of [['Network.webSocketFrameReceived', 'received'], ['Network.webSocketFrameSent', 'sent']]) cdp.on(name, e => traffic.socket_events.push({ kind, time: performance.now(), opcode: e.response.opcode, payload: e.response.payloadData }));
  cdp.on('Network.requestWillBeSent', e => traffic.request(e)); cdp.on('Network.responseReceived', e => traffic.response(e));
  cdp.on('Network.dataReceived', e => traffic.data(e)); cdp.on('Network.loadingFinished', e => traffic.finish(e)); cdp.on('Network.loadingFailed', e => traffic.failed(e));
  return { context, page, cdp, traffic };
}
async function coldRoute(page, traffic, fixture, shell, kind) {
  const path = kind === 'Projects' ? '/projects' : kind === 'Overview' ? '/' : `/sessions/${fixture.sessionId}`;
  const start = performance.now();
  await page.goto(fixture.origin + path, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: kind === 'Session' ? /^Cedar running session/ : kind, level: 1, exact: kind !== 'Session' }).waitFor();
  if (kind === 'Projects') await page.getByRole('main').getByText('Cedar 0001', { exact: true }).first().waitFor();
  if (kind === 'Overview') await page.getByRole('main').getByText('Cedar running session', { exact: true }).first().waitFor();
  if (kind === 'Session') { await page.locator('.xterm').waitFor(); await page.getByText('Read-write', { exact: true }).waitFor(); }
  if (shell === 'new') await page.getByRole('navigation', { name: 'Rail', exact: true }).waitFor();
  else await page.getByRole('navigation', { name: 'Main', exact: true }).waitFor();
  await page.getByRole('button', { name: shell === 'new' ? 'Go to…' : 'Jump to…', exact: true }).click();
  await page.getByRole('dialog', { name: 'Jump to…', exact: true }).waitFor();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const ready = performance.now(); await page.keyboard.press('Escape');
  await wait(coldMs - (performance.now() - start));
  const end = start + coldMs;
  const network = traffic.window(start, end);
  const observedEnd = performance.now();
  const runtimePaths = Object.entries(traffic.window(start, observedEnd).by_path)
    .filter(([path]) => /^\/api\/projects\/\d+\/runtime$/.test(path));
  network.runtime_observation = { duration_ms: observedEnd - start, unique_projects: runtimePaths.length,
    requests: runtimePaths.reduce((total, [, value]) => total + value.requests, 0) };
  const failures = traffic.records.filter(r => r.time >= start && r.time < end && r.failure && r.failure.time < end).map(r => ({ url: r.url, type: r.type, ...r.failure }));
  network.expected_cancellations = failures.filter(expectedCancellation);
  const fatal = failures.filter(r => !expectedCancellation(r));
  if (fatal.length) {
    result.failed_window = { kind, ...network, failures }; save();
    throw new Error(`${kind} had failed requests during its cold window: ${JSON.stringify(fatal)}`);
  }
  return { path, interactive_proxy_ms: ready - start, ...network, websocket: sockets(traffic.socket_events, start, end), start, ready, end };
}
async function measureAction(page, locator, condition) {
  const measured = page.evaluate(condition => new Promise((resolve, reject) => {
    let started, scheduled = false;
    const cleanup = () => { observer.disconnect(); document.removeEventListener('click', begin, true); clearTimeout(timer); };
    const valid = () => condition.kind === 'rail'
      ? [...document.querySelectorAll('#shell-rail button')].some(b => b.textContent.trim() === condition.label && b.getAttribute('aria-pressed') === 'true')
      : location.pathname === '/sessions' && document.querySelector('main h1')?.textContent.trim() === 'Sessions';
    const check = () => { if (started === undefined || scheduled || !valid()) return; scheduled = true; requestAnimationFrame(() => requestAnimationFrame(() => { cleanup(); resolve(performance.now() - started); })); };
    const observer = new MutationObserver(check); observer.observe(document, { subtree: true, attributes: true, childList: true, characterData: true });
    const begin = () => { started = performance.now(); queueMicrotask(check); };
    document.addEventListener('click', begin, { capture: true, once: true });
    const timer = setTimeout(() => { cleanup(); reject(new Error('Interaction did not reach its expected rendered state')); }, 45000);
  }), condition).then(value => ({ value }), error => ({ error }));
  await locator.click();
  const outcome = await measured; if (outcome.error) throw outcome.error; return outcome.value;
}
async function memory(page, cdp) {
  const metrics = Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m => [m.name, m.value]));
  const root = await browser.newBrowserCDPSession();
  const { processInfo } = await root.send('SystemInfo.getProcessInfo');
  let rss = 0, pss = 0; const missing = [];
  for (const proc of processInfo) {
    try {
      const path = `/proc/${proc.id}/smaps_rollup`;
      if (statSync(path).uid !== process.getuid()) throw new Error('not owned');
      const text = readFileSync(path, 'utf8');
      rss += Number(text.match(/^Rss:\s+(\d+)/m)[1]) * 1024; pss += Number(text.match(/^Pss:\s+(\d+)/m)[1]) * 1024;
    } catch { missing.push(proc.type); }
  }
  return { js_heap_used_bytes: metrics.JSHeapUsedSize, js_heap_total_bytes: metrics.JSHeapTotalSize, dom_nodes: metrics.Nodes,
    documents: metrics.Documents, browser_pss_bytes: missing.length ? null : pss, browser_rss_sum_bytes: missing.length ? null : rss,
    process_count: processInfo.length, missing_process_types: missing, visibility: await page.evaluate(() => document.visibilityState) };
}
async function trial(fixture, shell, run, projects) {
  await checked(await api.put('/api/preferences/ui-shell', { headers: fixture.headers, data: { enabled: shell === 'new' } }), 'set personal shell');
  const before = machine(); const { context, page, cdp, traffic } = await launchBrowser(fixture);
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  const pages = {};
  result.current_trial = { projects, sessions: fixture.counts.sessions, shell, run: run + 1, pages, load_before: before };
  for (const kind of run % 2 ? ['Overview', 'Projects', 'Session'] : ['Projects', 'Overview', 'Session']) {
    pages[kind] = await coldRoute(page, traffic, fixture, shell, kind);
    progress(`${projects}/${fixture.counts.sessions} ${shell} run ${run + 1}/${runs}: ${kind} ready ${Math.round(pages[kind].interactive_proxy_ms)} ms`);
  }
  const sessionReady = pages.Session.ready;
  await wait(args.smoke ? 200 : 20000);
  const visibleStart = performance.now();
  if (await page.evaluate(() => document.visibilityState) !== 'visible') throw new Error('Visible polling page is not visible');
  await wait(pollMs);
  const visibleEnd = performance.now(); const visible = polling(traffic.records, visibleStart, visibleEnd);
  const cover = await context.newPage(); await cover.goto('about:blank'); await cover.bringToFront();
  const hiddenState = await page.evaluate(() => document.visibilityState);
  if (hiddenState !== 'hidden') throw new Error(`Native hidden-tab transition failed: ${hiddenState}`);
  await wait(args.smoke ? 200 : 5000);
  const hiddenStart = performance.now(); await wait(pollMs); const hiddenEnd = performance.now();
  const hidden = polling(traffic.records, hiddenStart, hiddenEnd);
  progress(`${projects}/${fixture.counts.sessions} ${shell} run ${run + 1}/${runs}: visible/hidden polling complete`);
  hidden.observed_visibility = await page.evaluate(() => document.visibilityState);
  if (hidden.observed_visibility !== 'hidden') throw new Error('Hidden polling page became visible');
  await page.bringToFront(); await cover.close();
  if (await page.evaluate(() => document.visibilityState) !== 'visible') throw new Error('Visible state did not restore');
  const heartbeat = setInterval(() => progress(`${projects}/${fixture.counts.sessions} ${shell} run ${run + 1}/${runs}: session dwell ${Math.round((performance.now() - sessionReady) / 1000)}/${dwellMs / 1000}s; polling ${visible.requests}/${hidden.requests} visible/hidden`), 30000);
  timers.add(heartbeat);
  await wait(dwellMs - (performance.now() - sessionReady)); clearInterval(heartbeat); timers.delete(heartbeat);
  const memory_after_ms = performance.now() - sessionReady;
  const memory_5m = await memory(page, cdp);
  Object.assign(result.current_trial, { memory_after_ms, memory_5m, polling: { visible, hidden } }); save();
  const interactions = { rail_projects_ms: null, rail_sessions_ms: null, sidebar_sessions_ms: null };
  if (shell === 'new') {
    const rail = page.getByRole('navigation', { name: 'Rail', exact: true });
    interactions.rail_sessions_ms = await measureAction(page, rail.getByRole('button', { name: 'Sessions', exact: true }), { kind: 'rail', label: 'Sessions' });
    interactions.rail_projects_ms = await measureAction(page, rail.getByRole('button', { name: 'Projects', exact: true }), { kind: 'rail', label: 'Projects' });
    await rail.getByRole('button', { name: 'Sessions', exact: true }).click();
    interactions.sidebar_sessions_ms = await measureAction(page, page.getByRole('complementary', { name: 'Context sidebar' }).getByRole('link', { name: 'All sessions', exact: true }), { kind: 'navigation' });
  } else {
    interactions.sidebar_sessions_ms = await measureAction(page, page.getByRole('navigation', { name: 'Main', exact: true }).locator('a[href="/sessions"]'), { kind: 'navigation' });
  }
  const row = { harness_revision: result.revision, projects, sessions: fixture.counts.sessions, live_sessions: 1, shell, run: run + 1, pages,
    polling: { visible: { ...visible, observed_visibility: 'visible', duration_ms: visibleEnd - visibleStart }, hidden: { ...hidden, duration_ms: hiddenEnd - hiddenStart } },
    memory_after_ms, memory_5m, interactions, load_before: before, load_after: machine(), page_errors: errors };
  if (errors.length) throw new Error(`Page errors: ${errors.join('; ')}`);
  result.trials.push(row); delete result.current_trial; save();
  await browser.close(); browser = null; await stopProcess(browserProcess); browserProcess = null;
  progress(`${projects}/${fixture.counts.sessions} ${shell} run ${run + 1}/${runs}: saved; heap ${(memory_5m.js_heap_used_bytes / 1048576).toFixed(1)} MiB`);
}
try {
  for (const projects of scales) {
    if (result.trials.filter(r => r.projects === projects).length === runs * 2 && !args['readings-probe']) continue;
    progress(`Preparing ${projects} projects`);
    const fixture = await startFixture(projects);
    if (args['runtime-probe']) for (let run = 1; run <= runs; run++) {
      await checked(await api.put('/api/preferences/ui-shell', { headers: fixture.headers, data: { enabled: true } }), 'set probe shell');
      const load_before = machine();
      const { page, traffic } = await launchBrowser(fixture);
      page.setDefaultTimeout(120000);
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const projectsPage = await coldRoute(page, traffic, fixture, 'new', 'Projects');
      if (errors.length) throw new Error(`Runtime probe page errors: ${errors.join('; ')}`);
      result.runtime_probes.push({ projects, sessions: fixture.counts.sessions, shell: 'new', run,
        page: projectsPage, load_before, load_after: machine(), page_errors: errors }); save();
      await browser.close(); browser = null; await stopProcess(browserProcess); browserProcess = null;
      progress(`Runtime probe ${run}/${runs}: ${projectsPage.runtime_observation.unique_projects} unique projects`);
      await wait(15000);
    }
    else for (let run = 0; run < runs; run++) for (const shell of run % 2 ? ['new', 'old'] : ['old', 'new']) {
      if (result.trials.some(r => r.projects === projects && r.shell === shell && r.run === run + 1)) continue;
      await trial(fixture, shell, run, projects);
    }
    if (args['readings-probe']) {
      const load_before = machine();
      const readings = [];
      for (let run = 1; run <= 5; run++) {
        const paths = ['/api/board', '/api/readings/machine'];
        if (run % 2 === 0) paths.reverse();
        for (const path of paths) {
          const start = performance.now();
          const response = await api.get(path);
          const body = await response.body();
          if (!response.ok()) throw new Error(`Readings probe ${path}: HTTP ${response.status()}`);
          readings.push({ run, path, response_ms: performance.now() - start, json_body_bytes: body.length });
        }
      }
      (result.readings_probes ??= []).push({ projects, load_before, load_after: machine(), samples: readings }); save();
    }
    await api.dispose(); api = null; await stopProcess(server); server = null;
    execFileSync(PYTHON, [join(ROOT, 'tests/owned_tmux.py'), currentBase], { cwd: ROOT, stdio: 'pipe' });
  }
  if (args['runtime-probe'] ? result.runtime_probes.length !== scales.length * runs : result.trials.length !== scales.length * 2 * runs) throw new Error('Incomplete measurement matrix');
  result.completed_at = new Date().toISOString(); result.progress = null;
  result.summaries = args['runtime-probe'] ? [] : scales.flatMap(projects => ['old', 'new'].map(shell => {
    const rows = result.trials.filter(t => t.projects === projects && t.shell === shell);
    return { projects, shell, samples: rows.length, pages: Object.fromEntries(['Projects', 'Overview', 'Session'].map(kind => [kind,
      Object.fromEntries(['interactive_proxy_ms', 'requests', 'wire_bytes', 'js_wire_bytes', 'css_wire_bytes', 'js_decoded_bytes', 'css_decoded_bytes'].map(key => [key, median(rows.map(r => r.pages[kind][key]))]))])),
      polling_visible_per_minute: median(rows.map(r => r.polling.visible.requests_per_minute)), polling_hidden_per_minute: median(rows.map(r => r.polling.hidden.requests_per_minute)),
      memory: Object.fromEntries(['js_heap_used_bytes', 'browser_pss_bytes', 'dom_nodes'].map(key => [key, median(rows.map(r => r.memory_5m[key]))])),
      interactions: Object.fromEntries(['rail_projects_ms', 'rail_sessions_ms', 'sidebar_sessions_ms'].map(key => [key, median(rows.map(r => r.interactions[key]))])) };
  }));
  save(); log(`Complete: ${result.trials.length} trials; ${output}`);
} catch (error) {
  try { result.failure_page = await browser?.contexts()[0]?.pages()[0]?.locator('body').innerText({ timeout: 3000 }); } catch {}
  result.failure = { at: new Date().toISOString(), message: error.message, stack: error.stack }; save(); console.error(error); process.exitCode = 1;
} finally { await clean(); save(); }
