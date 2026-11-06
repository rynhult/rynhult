# AC-056 shell performance harness

This is measurement code only. It runs one serial Playwright browser against a
private `tests/e2e_server.py` fixture, using three disposable datasets. It creates
no Playwright config and does not address a live installation.

```sh
ln -s /srv/acc/app/web/node_modules web/node_modules
(cd web && ./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build --configLoader runner)
node --test web/e2e/perf/metrics-check.mjs
PLAYWRIGHT_BROWSERS_PATH=/srv/acc/tools/ms-playwright ACC_E2E_PYTHON=/srv/acc/app/.venv/bin/python ACC_E2E_PORT=18964 ACC_E2E_BASE=/tmp/pb-new node web/e2e/perf/baseline.mjs --out web/e2e/perf/results.json
rm web/node_modules
```

Use your own unprivileged port and a **new**, short `/tmp` base. The script refuses
an existing base. The shown Chromium executable is already installed here; use
`--executable PATH` to select another installed full Chromium. It installs
nothing. `--help` lists the options. `--smoke` uses short windows and one trial;
its results are explicitly marked **not a baseline**. `--scales` can restrict a
smoke/diagnostic run. The default measured matrix is 10/5, 100/50 and 500/200
projects/sessions, both shells, five runs each.

The full serial run needs roughly 2¾ hours, dominated by thirty actual five-minute
session dwells. Results are saved atomically after each route, memory sample and
trial. A failure stops the run, retains progress and cleans up the owned browser,
server and tmux fixture. SIGINT/SIGTERM do the same. Private browser profiles and
fixture data stay under the owned `/tmp` base; they contain synthetic sign-in
state and must not be committed. The JSON report contains no cookies, headers,
credentials or conversation contents on successful runs.

## Measurement definitions

- The `new_shell` Preview is enabled for both conditions. The caller's personal
  preference selects the old or new shell, keeping backend feature availability
  identical. A new Chromium process/profile is used for each trial. Shell order
  alternates between pairs; Projects/Overview order alternates too, with Session
  last. All runs use 1440×900, dark theme, no network/CPU throttling.
- Each dataset has one actual running fake CLI and the remaining sessions are
  stopped history. Every project is local, owned by the synthetic owner, has no
  remote repository, and lives under the private fixture root. This measures
  list volume, not hundreds of concurrent terminals or remote-machine latency.
- Each route uses a cold HTTP cache and bypasses service workers. The first
  **10 seconds from navigation** count HTTP requests and received bytes. CDP's
  `loadingFinished.encodedDataLength` replaces chunk byte counts; unfinished
  SSE transfers contribute received chunks. JS/CSS transfer counts include
  response overhead; decoded body bytes are recorded separately. This is the
  direct fixture server, without Caddy compression. Cache/service-worker bypass
  remains enabled for the complete trial; this is a cache-disabled stress baseline,
  including the steady polling and memory windows. WebSocket connections and
  received/sent payload bytes are separate: payload sizes are not wire sizes.
- **Interactive proxy**: the named page and representative data/terminal are
  ready, the header's palette button responds, and the palette reaches its next
  paint. It is a concrete action-ready proxy, not Lighthouse's formal TTI. The
  terminal owns Ctrl+K, so the probe uses the header button on every route.
- Session polling: settle for 20 seconds, count a 60-second visible window,
  activate an actual second tab, verify native `document.visibilityState` is
  `hidden`, settle 5 seconds, count 60 seconds hidden, then restore visibility.
  API GETs are counted by path; SSE openings are separate. Counts are normalized
  by actual elapsed time. There is no visibility-property override or page freeze.
- Full Chromium is started with its own private profile and loopback-only CDP
  port, then connected through `connectOverCDP({ noDefaults: true })`. The default
  headless-shell/focus emulation reports pages visible even after a tab switch;
  the harness verifies actual visibility before accepting either window.
- Memory is sampled **300 seconds after Session is action-ready**, including the
  one hidden window. It records current-target JS heap/DOM counts and the sum of
  owned browser-process PSS/RSS from `/proc`. PSS avoids double-counting shared
  pages; RSS sum is retained only as a secondary diagnostic. No GC is forced.
  The prior Projects/Overview navigations are part of this journey, so ordinary
  browser caches/retained documents are included. Permission failures remain null.
- Interaction latency runs after memory sampling. It measures a real click
  captured in the browser through expected DOM state and two animation frames.
  New-shell rail browsing keeps the URL unchanged; old shell has no rail and is
  reported N/A. The comparable sidebar probe opens Sessions in both shells.
  This is an input-to-render proxy, not a W3C INP score.
- Real host load averages/free memory are captured around each trial and in
  progress heartbeats. UI host readings are synthetic fixture data and are not
  used as evidence of machine load. Browser/build/harness versions and hashes
  are recorded for reproducibility.

`metrics-check.mjs` checks cutoffs, redirect accounting, incomplete streams,
WebSocket encoding, median calculation and missing values. No budgets are
enforced. The results document proposes P1/P2 budgets for review.

## Resume after a stopped attempt

Archive the prior JSON, then use `--resume ARCHIVED.json` with a new private base
and output path. The harness retains only completed, uniquely identified trials
from the same built assets and the same full measurement windows; smoke,
short-memory, duplicate or invalid-visibility observations are rejected. The
failed/partial trial is rerun. Each observation keeps its harness revision and
prior attempt metadata is retained. There is no automatic retry or silent
removal of a failed attempt. Cold-window failures record their URL/type/reason
before stopping, so cancellations can be distinguished from transport failures.

The native Node checks use `metrics-check.mjs` so the main Playwright config
does not discover them as browser specs.

Cold-window traffic retains explicit `net::ERR_ABORTED` cancellations of the two
paired brand-theme SVG images. Those narrowly identified image cancellations are
nonfatal, with their request/byte counts and cancellation details preserved.
Other cancellations and transport failures still stop the run. Resume validates
that every recorded nonfatal failure matches this rule.

## Performance fixes round 1

Use `--scales 500` for five repeats per shell at 500 projects / 200 sessions.
The before build must be a separate, unchanged checkout of the assigned main
revision, rather than reusing observations from another product revision. Keep
the before and after output JSON files, hashes and actual host load in the handoff.

`--readings-probe` additionally compares `/api/board` and `/api/readings/machine`
after the browser trials: five authorized GETs each, with pair order alternating.
It records response duration and **JSON body bytes**, excluding HTTP headers;
these are separate from the browser's cold-window traffic. This isolates the
new endpoint without modifying another lane's Header components. Until that lane
switches to `useMachineReadings` from `web/src/lib/queries.ts`, browser Header
traffic still uses the full board endpoint. Do not attribute browser gains to
that pending integration.

With `--resume` and `--readings-probe`, completed browser observations remain
retained and the endpoint comparisons run on a fresh identically sized fixture.
This also permits recovering an interrupted endpoint probe without repeating
the five-minute browser dwells.

When host load delays route readiness past the fixed first 10 seconds, use
`--scales 500 --runtime-probe` on an unchanged reference checkout. This separately
records five fresh new-shell Projects visits and the unique runtime project reads
through the later of the ten-second window and action readiness/closing the
palette. Its `mode: runtime-probe` output has no Session dwell/polling measurements,
uses a 120-second per-action observation limit and a 15-second backend drain
after each closed browser, and cannot be resumed as a full baseline. Normal runs
also record this expanded `runtime_observation` alongside the unchanged ten-second
traffic counters. Compare fan-out counts, not readiness times, across these modes.
