# Development

Contribute from an unprivileged fresh clone. Production deployment is separate: do not run `deploy/setup.sh` or `deploy/install.sh` to develop or test ACC. Changes land through pull requests.

## Layout
```
src/acc/server   accd: API routers (api/), supervisor, compiler, catalog, publishing, migrations
src/acc/broker   the broker
web/             the dashboard (React, Vite, Tailwind)
catalog/         repository catalog: skills, agents, MCP servers, profiles
deploy/          setup.sh (first install), install.sh (install/update), systemd units, root helpers, Caddy, AppArmor
docs/            plan, decisions, design system, this wiki (docs/wiki)
scripts/         checks, the demo instance, the UI review harness
tests/           pytest suite, fake CLIs, the end-to-end server
```

For dependency PRs and binary download pins, follow [Dependency updates](/Internal/Dependency-Updates).

## Running the checks

The supported full-suite platform is Linux, with `/proc`, git and tmux on PATH. Install Python 3.12+, uv, Node.js 22+ and npm using your platform's documented route. Chromium also needs its OS libraries; on a machine missing them, ask its administrator to provision Playwright's documented Linux dependencies. macOS and Windows node installers have separate checks; the full server suite is not qualified on those platforms. No vendor CLI, subscription, root access or running ACC service is needed for the default suite.

From the clone root:

```bash
uv sync --frozen --python 3.12
npm ci --prefix web
(cd web && npx playwright install chromium)
python3 scripts/check_pins.py
uv run --frozen ruff check src tests scripts/check_pins.py
PYTHONPATH=src uv run --frozen python -m pytest -q -n 8
(cd web && npm run build -- --configLoader runner)
(cd web && npx playwright test --project=desktop --project=mobile)
uv run --frozen python scripts/third_party_notices.py --check
```

The normal browser projects use the plain-HTTP disposable fixture. Secure-cookie
recovery tests run separately with its self-signed HTTPS fixture (Chromium and
WebKit); they are intentionally excluded from HTTP collection. CI runs both:

```bash
(cd web && ACC_E2E_PORT=19661 ACC_E2E_BASE=/tmp/acc-ci-csrf npx playwright test --config playwright.csrf.config.ts --workers=1)
```

For a bounded terminal-input and chunk-loading neighbour check, use
`npx playwright test --config playwright.misc.config.ts --repeat-each=3` from
`web/`, with your own `ACC_E2E_PORT` and `ACC_E2E_BASE`. The no-replay cases wait
for PTY echo before disconnecting; they do not assume queued WebSocket bytes
have already reached the process. The synthetic exhausted-import unit test asserts
its expected diagnostic rather than printing it as an unexplained gate failure.

These are required contributor checks. `-n 8` runs the Python suite in eight pytest-xdist worker processes, which takes about 9 to 15 minutes instead of about 55 on a shared 20-core machine. Use `-n auto` for one worker per core on a machine of your own, or leave `-n` out to run in one process. Tests stay parallel-safe by taking ephemeral ports (bind to port 0), per-test temporary folders, `HOME` and tmux socket names, and parametrize IDs that are the same in every worker; no test is serialised. To regenerate notices after changing locks, run the last command without `--check`, with the frozen Python environment and web dependencies installed. The checker fails if required metadata is missing or mismatched.

Machine-identity link tests distinguish the signed-link `ready` message from
background certificate issuance and retirement. Wait for the corresponding wire
message or committed registry state before disconnecting; closing the link cancels
its follow-up. Stream admission, TLS peer observation and sweep retries likewise
use observable conditions rather than settling sleeps. Identity workers reserve
SQLite write intent before reading rows they will update, so another commit cannot
invalidate their snapshot. The signer runs between transactions without holding
the database reservation. Concurrency regressions keep an owner transaction open
while issuance or retirement attempts its commit.

Optional: `scripts/security-check.sh` scans history and source with `gitleaks` and scans locked dependencies with `osv-scanner`. Supply independently verified tools on PATH, or set `ACC_TOOLS` to their directory. OSV receives package names and versions. A missing tool is an error, not a clean scan. Live provider qualification is opt-in and requires real CLIs and credentials; fake CLIs do not establish vendor compatibility.

The optional source-scanner regression requires a Gitleaks build with the `dir`
command. It accepts `GITLEAKS` as an explicit executable override, otherwise preferring
`ACC_TOOLS/gitleaks` over PATH, matching the security script. The isolated pytest
harness strips ACC/RYNHULT environment variables; use `GITLEAKS` pointing to a
verified binary copied inside the worktree to exercise this optional test there.
An older distro executable is an explicit test skip, not a clean scan. Findings
use a dedicated exit status and must include a JSON report; command errors cannot
masquerade as findings. Release qualification should provide a current verified tool.

Container-launcher fixtures model agent-owned files separately from the test
runner. Tests that switch to real Unix sockets and `SO_PEERCRED` must also use
real socket metadata, including when the runner is root. The launcher still
checks socket ownership, mode, inode and broker PID; test fixtures do not relax
those production checks. Different modeled-agent and runner UIDs are exercised
without requiring root.

The browser suite starts its own ACC with temporary data, a private tmux socket and fake CLIs. Its default base is a short temporary path; concurrent worktrees must set distinct `ACC_E2E_BASE` folders and `ACC_E2E_PORT` values (default 18741). Keep `ACC_E2E_BASE` short: its broker socket is `<base>/run-xxxxxxxx/broker.sock`, and Unix socket paths can be at most 107 bytes, so a base over 82 bytes is refused at start with a message that says so. It also starts a test preview on an available loopback port in 8000–8099. Use those ports only where your local environment permits. Set `ACC_E2E_PYTHON` to your interpreter if not using uv; the harness sets `PYTHONPATH=src` to test this checkout. `PLAYWRIGHT_BROWSERS_PATH` can select an existing browser cache. With read-only shared `node_modules`, retain `--configLoader runner` for the build.

Overlapping browser runs in the same checkout also need distinct Playwright
`--output` folders. Separate server bases and ports do not isolate worker trace
files; one runner can otherwise remove the other's active recordings.

Browser specs share server state within a run. The phone status-board checks read machine counts,
alerts and names from the API responses received by the page, while sessions and Inbox items use
test-owned fixtures. Keep these checks independent of earlier specs' data; retain the counts-first
ordering and first-machine-above-the-fold assertions in both themes.

The browser gate defaults to four workers. Setup is followed by single-worker
`desktop-stateful` and `mobile-stateful` projects; ordinary desktop and mobile
cases run in parallel only after both finish. These prerequisite phases contain
installation/authentication writers, real global rate-budget consumers and
whole-inventory proofs. File-local serial mode does not protect other specs.
Selecting either ordinary project also selects these prerequisites.
Whole-page containment proofs also require these phases: concurrent attention
producers can replace the real Inbox content while its dimensions are measured.

A failing assertion in a prerequisite project marks that project failed, so Playwright skips its dependent
projects. This is dependency gating, not a `maxFailures: 1` setting. A serial describe also skips the rest
of that file after a failure. Do not remove the dependency edges alone: installation-wide writers would
then overlap the ordinary cases again. Proposed follow-up: provision an isolated server/auth fixture for
each phase, depend only on a dedicated seed/setup project, and remove the
`desktop-stateful → mobile-stateful → desktop/mobile` assertion dependency chain. Keep dependent
scenarios serial inside their own fixture. That would retain failure reporting while letting unrelated
projects run, without sharing a partially mutated installation. This isolation change is not implemented.

For workspace promotion names, run `cd web && npm run test:workspace-names` (Node 22.6+ with type stripping).
The browser regression uses a topic ending in eight digits deterministically: otherwise the first eight
random hex characters contain only digits about 2.3% of the time, changing the default promoted name if
folder dates are stripped from the wrong end. Preserve the exact heading assertion and check the dialog's
name/slug; do not work around this with a longer timeout or a looser heading locator.

Use `gitProject` prefixes rather than fixed project identities: it returns a
unique slug, folder and ID. Use the returned slug in exact name assertions.
It navigates after API creation/trust so the browser does not cache an older
project list before opening New session.
Keyboard action-menu checks Tab to **Filter projects**, type their returned
unique name, and wait for exactly that project's action menu before traversing
its controls. Do not Tab through the gate's entire accumulated project inventory.
The local Tab helper snapshots a traversal bound, checks the target's `:focus`
intersection without waiting for a detached locator, and asserts actual focus
before sending the next action key. Keep the normal deadline; do not add sleeps
or focus the target programmatically.
The final orchestrator keyboard journeys also wait for their target region's
tab-stop list to settle, and for dialog project selection and launch readiness
before tabbing. Their traversal budget comes from the live focusable controls
(within a modal when present), not a fixed number of Tabs. They poll
`document.activeElement` between keyboard actions and foreground the page without
assigning focus to a control. Prefer the shorter Tab/Shift+Tab route and use
**Skip to content** by keyboard when it does not replace a project-section hash;
walking every accumulated sidebar link can exhaust the journey's deadline even
with a correct traversal bound. Preserve the keyboard-only mode changes, grants,
answers and proposal decisions; a successful pointer action is not a substitute.
`sessionWithChanges` always creates its own session. Never borrow the first live
session or require the entire Inbox/project list to be empty.
Scope **Answer in terminal** to the owned session's link, not the first matching
Inbox action. The phone-pass regression deliberately prepends another waiting
session while retaining the real owned item and its terminal navigation.
Session-name link assertions must distinguish the row title from its separate
**Open** action; scope to the intended group and anchor the accessible name.
Provider install-state checks wait for the missing-provider account status
before clicking its **Install**: the packages query can expose a different
software-row Install button before the provider registry finishes loading.
For background jobs, wait for the owned generation's terminal API response
before asserting its rendered result, as in the walkthrough spec. This keeps
the normal test deadline without assuming reconciliation finishes in a
five-second UI assertion window.
`waitForSessionState` waits for the owned session's real API state within that
same test deadline. It uses `expect.poll` to await each asynchronous observation:
an async `waitForFunction` predicate returns a truthy Promise even when the
eventual state comparison is false. The fixture regression requires both the
requested state and status provenance before readiness returns.
Launch navigation waits for its POST response; multirun
usage waits for the owned statusline observation. Stop owned background jobs
in finally/afterEach so later cases do not inherit unnecessary reconciliation work.
For Codex startup, require the `codex_hook` status source as well as idle:
tmux can report idle before a late SessionStart overwrites a screen prompt.

For fake Claude events and statusline fixtures, likewise wait for idle from
`claude_hook`, not merely tmux idle, on every session receiving a command.
After requesting permission, wait for that session's `waiting_input` observation
before checking the rendered Team row. A terminal textarea can exist before its
socket connects; wait for **Read-write** before typing input that must reach the
server. A late terminal mount must not steal focus already placed on a tab or
another control. The tab-keyboard regression gates its JavaScript chunk until
the tab has focus, then verifies focus survives the real terminal mounting.
Schedule **Run now** first queues work: wait for the browser's schedule
response containing that exact run ID and its launched session before checking
**Open session**. These barriers stay inside the existing test deadline.
UI registration walkthroughs need unique folder and display names too, not just
API-created fixtures. Give group-board users, agent accounts and project slugs
unique suffixes so `--repeat-each` does not collide with an earlier repetition.
The isolated server advertises additional simulated accounts from empty
`fixture-accounts/<name>-agent` markers beneath its temporary state directory;
these use the same test broker and never create operating-system accounts.
Cancel/delete owned schedules and stop owned sessions even when an assertion fails.

`gotoAppPage` waits for the shell's **Content** main region and the disappearance
of **Loading page…** before starting normal UI assertions. A completed document
navigation is not proof that React's lazy shell or route has mounted.
The helper stays within the existing test deadline and does not wait for every
API query; retain the case's own semantic/data assertions. Do not use it in
intentional failed-shell tests, where the loading fallback must remain visible.

The exhausted-retry unit case in `01-units.spec.ts` deliberately logs
`App module failed to load after one retry`. That line alone is not evidence
of a browser chunk-delivery failure. Inspect the failing browser's trace and
network requests before changing application retries or preloading chunks;
keep the chunk-recovery proofs for unsaved edits and explicit reload intact.

The phone alert layout regression checks that at least 200 px of terminal height
is fully inside the viewport together with the operational alerts and Enter key,
with alerts below the terminal. It does not pin the terminal's top edge to an old
header height: accessible touch targets and section tabs also need room.
The Inbox privacy link must focus its Settings destination and reveal its heading.
Do not require the checkbox beneath its explanatory text to fit above the phone
fold too: asynchronously loaded warnings can move it while the heading stays visible.
Copy assertions on the Passkeys card wait for **Owner policy**, not just its
static introduction, and explicitly control whether company sign-in is offered.

For mocked Inbox recovery, keep automatic refreshes failing until Retry is
actually activated. `recoverAfterRetry` observes that click, holds successful
responses behind a promise, and releases them after the click completes.
It checks that Retry is disabled and the outage remains visible while pending.
Always release response gates in finally; gating an automatic fetch before the
click would disable Retry and deadlock the test instead.

Browser fake-CLI commands use the shared `control` helper, which publishes a
complete command by atomic rename. Writing directly to `fake.ctl` can let the
consumer read and remove an empty file between truncation and the command write.
The test server also refreshes its artificial host snapshot every minute, like
the real collector timer. Long runs must not acquire an unrelated stale-collector
Inbox item after five minutes; explicit stale-state tests still control their
own API responses or snapshot timestamps.

The harness enables its in-process test signer and simulated edge by default;
neither can contact production helpers. Explicit
`ACC_E2E_DEVICE_CA=0 ACC_E2E_REMOTE_EDGE=0` retains absent-helper qualification;
the contributor checks run those two remote-access cases that way in a separate step.
These defaults use the shared environment reader, including current
`RYNHULT_*` names taking precedence over their `ACC_*` aliases.
The fixture clears inherited `TMUX`/`TMUX_PANE` markers before starting its
private server, so running the suite from an agent's terminal cannot cause
tmux's nested-session refusal.
Pairing and terminal-text cases restore their real, test-owned rate bucket before
each serialized case through harness-only reset routes. The production 10/600s
pairing and 20/60s terminal budgets remain enforced within each case.

The absent-helper removal proof intentionally ends with removal pending. From
`web/`, qualify it in its own invocation, not alongside the enabled-helper lifecycle.
Also qualify the disabled-CA preparation case separately; both cases are
conditionally skipped by the default enabled-helper run:

```bash
ACC_E2E_DEVICE_CA=0 ACC_E2E_REMOTE_EDGE=0 npx playwright test e2e/63-device-pairing.spec.ts --project=desktop-stateful --grep="public status exposes review"
ACC_E2E_DEVICE_CA=0 ACC_E2E_REMOTE_EDGE=0 npx playwright test e2e/63-device-pairing.spec.ts --project=desktop-stateful --grep="remote access stays off"
```

To repeat changed cases, invoke each matching phase separately. Playwright runs
dependency projects once, without the selected files/repeat filters, even when
they are also named alongside a downstream project. Omit phases with no changed
files; pass the relevant changed spec paths to each command:

```bash
cd web
npx playwright test e2e/44-passkeys.spec.ts --project=desktop-stateful --workers=4 --repeat-each=3
npx playwright test e2e/35-terminal-output-mobile.spec.ts --project=mobile-stateful --workers=4 --repeat-each=3
npx playwright test e2e/19-host-backups.spec.ts --project=desktop --project=mobile --workers=4 --repeat-each=3
```

The shared axe helpers wait for document fonts, two rendering frames and finite animations
(including theme and hover transitions) before checking WCAG 2.2 A/AA. Infinite live-dot
and spinner animations are paused at their current frame during the scan and resumed even
if the scan fails. No accessibility rule is disabled. Failed scans attach the complete axe
violations, including node targets, foreground/background colours and contrast ratios, to
the Playwright report/trace. `e2e/axe-settling.spec.ts` reproduces a dark-to-light transition
through low-contrast intermediate colours and verifies that genuinely unreadable text
still fails the gate.

The container-launcher helper tests model the agent as uid 1004 independently of the pytest runner. Protected
files retain their actual temporary-file ownership; only the fixture's checkout/run directories model agent
ownership. Socket tests check real peer credential decoding before substituting the fake broker identity. Root
peers remain explicitly refused. No real systemd or container commands run in these tests.

`tests/test_launch_as_agent.py` checks run-folder creation/resume, Files browsing/editing,
and container-checkout artifact exclusions beneath a parent with mode `0311`.
These checks execute unprivileged even when pytest runs as root: root re-executes
the test as `nobody` using `runuser` or `setpriv`, with a temporary copy of this
checkout's Python source and a tree owned by that account. Missing privilege-drop
support is reported as a skip, never counted as a permission check passing as root.
Temporary permissions are restored before cleanup. These permission checks require Linux.

### PostgreSQL race regressions

Device pairing and group-board/launch security regressions opt into `backend_url`.
`env` selects that URL before the application and dependent fixtures start. Each
selected test runs on SQLite and on a fresh PostgreSQL schema in the existing
throwaway-container matrix. `api` asserts that its engine really uses the selected
URL and disposes its connection pool at teardown. No external database URL is accepted. Without usable
Podman/Docker, PostgreSQL cases skip with a reason; a skip is not PostgreSQL
qualification. The interpreter also needs the project's `postgres` extra.

Run the affected files from the checkout root:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_devices.py tests/test_group_board.py tests/test_group_launch.py tests/test_group_phase3_security.py
```

To qualify only PostgreSQL, add `-k postgresql -rs` (22 cases). Use an account
allowed to start the test container, such as root on a dedicated test host. The
fixture starts and removes its own container; do not point it at an existing ACC
database. Do not run these schema-resetting cases in parallel against the same
session-scoped container.

`tests/race_helpers.py` makes interleavings explicit:
- Pairing status transitions, policy revisions, claims and quota reservations
  rendezvous on two distinct connections. SQLite meets before writer admission;
  PostgreSQL meets immediately before the guarded write, after both transactions
  have read. Missing participants fail rather than silently timing out.
- Row-guard tests use a test-only PostgreSQL READ COMMITTED engine without the
  application's advisory writer lock. Otherwise that lock (or a supervisor
  REPEATABLE READ serialization error) would mask a missing compare-and-set or
  group fence. SQLite keeps its production writer serialization.
- Dispatch and checkout tests keep the normal application engine. They pause a
  real broker reply, commit revocation through a separate engine/worker, then let
  the caller consume the reply. They check stopped sessions, killed panes,
  removed environment files and refused checkout receipts. The pre-dispatch
  regression commits removal while the dispatcher's old transaction and cached
  membership are still live, then verifies no launch reaches the broker.
- Reservation workers call the real binding/quota guards and hold the transaction
  through session, work and launch-ledger insertion. HTTP-only launch races would
  instead be serialized by the route's process-local asyncio lock. Separate HTTP
  tests retain repeated-checkout/idempotency coverage.

When checking a regression, temporarily remove its production guard, run its
test, restore the guard, and rerun. Removing the pairing status predicate still
passes SQLite because writers serialize there; the two PostgreSQL pairing tests
are essential to prove that mutation is caught.

### Python tests under load

Keep `PYTHONPATH=src` when sharing an interpreter with another checkout: its editable install may otherwise import that other checkout. `tests/load_suite.py` runs independent pytest processes with this checkout's source path, saving per-run logs, JUnit XML and `summary.json` in a **new** output folder. It exits nonzero if any run fails.

```
PYTHONPATH=src .venv/bin/python tests/load_suite.py --repeat 3 --workers 3 --output .acc/artifacts/full-load
PYTHONPATH=src .venv/bin/python tests/load_suite.py --repeat 20 --workers 4 --output .acc/artifacts/auth-load tests/test_auth.py
PYTHONPATH=src .venv/bin/python tests/load_suite.py --repeat 20 --workers 3 --output .acc/artifacts/preview-schedule-load tests/test_schedules.py tests/test_preview.py tests/test_preview_stream.py tests/test_preview_listener.py tests/test_multihost.py::test_authenticated_link_negotiates_preview_and_cancels_on_disconnect
```

Each pytest process owns a short, randomly named temporary root and removes it at shutdown, rather than sharing pytest's numbered-directory retention. An explicit `--basetemp` is respected; callers supplying it must choose a different directory for each concurrent process.

Test helpers in `tests/helpers.py` keep the checks independent of scheduler luck:
- Use `wait_for` with the actual completion condition, or `reconcile_passes` when proving no extra action occurs after reconciliation. A sleep does not prove either.
- Use `preview_listener()` (or the `preview_socket` fixture) for every real preview app listener, including relay, machine-link and browser harness tests. It tries the development range 8000–8099 from a run-specific starting point, sets `SO_REUSEADDR` before binding so closed connections do not exhaust that range through TCP `TIME_WAIT`, and retains the bound socket through handoff to the server. Pass the socket to HTTP or WebSocket servers; do not find a port, close it and bind again. Concurrent listeners still have exclusive ports (`SO_REUSEPORT` is never enabled). The browser server passes its `ACC_E2E_PORT` as the preview allocation ID; Python fixture processes use their PID. Each run starts at its own candidate in 8000–8099 and falls back through exclusive binds if it is occupied; even equal allocation IDs cannot share a listener. Exhaustion fails startup. Consume `/preview-test-port` rather than guessing a fixture port. Playwright results are separated by ingress port too, so simultaneous runs in one checkout do not delete each other's traces. UI-only preview mocks must return both `url` and `frame_url` and intercept a separate-origin frame, rather than navigating a missing URL.
- Use `write_db(factory)` for direct database setup that reads and then mutates live application data. It takes the existing SQLite write-intent transaction up front, retaining the configured busy timeout and avoiding failed deferred-snapshot upgrades. Never await inside this synchronous scope. The existing connection busy timeout remains 15 seconds; a longer timeout cannot fix a stale read snapshot. Apply this to schedule and preview fixture mutations too. Tests that manually drive scheduler recovery, dispatch, push or issue-comment polling first use `pause_supervisor` so the background loop cannot consume the same intents or race their setup. Pause it before seeding a transcript index too: live indexing must not replace the messages a search-scope test owns.
- Generate authentication codes at `TOTP_AT`. The fixture replaces only the authentication module's wall clock; event-loop and throttle clocks remain real. Tests of rollover explicitly advance that local clock and retain the original code for replay checks.
- Publish fake CLI commands through `control`, which atomically replaces the control file. Treat a concurrently removed `session.env` as a pending polling result, not an exists-then-read operation.
- Cancel and await owned tasks, and close broker clients before awaiting listener shutdown. Cancelling `to_thread` does not stop its worker: `pause_supervisor` also joins tracked host work before a test drives stages itself. Keep node helper state and sockets inside the test's temporary directory; never depend on a host's installed helper state.

The planned-downtime regressions commit a reconnect after the scheduler reads an
offline node. Reconnect detection and downtime suppression must use that same node
snapshot: a second read can see the cleared one-shot flag and queue an ordinary
capture without catch-up metadata. The authenticated-link test first completes an
offline worker tick, then reconnects with the worker idle or held at the end of that
tick. Events, not sleeps, establish both interleavings. Job polling runs off the
event loop so a real capture's exclusive maintenance lease cannot reject the
test's read with a retryable maintenance error.

Database contention regressions in `test_history.py`, `test_autoresume.py` and
`test_host.py` hold a real second writer and release it only after indexing,
cancellation or host polling checks out its worker connection (before admission). They verify the event loop
can release the writer, persistence succeeds, and concurrent edits survive.
`test_db.py` also checks that unmarked read-then-write sessions remain writable
while a worker waits, including across engines for the same database file, and
that commit, rollback, close and failed maintenance entry release admission.
A plain write-intent thread regression covers API middleware context without
the worker decorator, while the GET contention checks preserve ordinary reads.
The reconcile regression commits a competing edit during broker listing and checks
that the reloaded session retains that edit and persists pane output without errors.
Placement-backed cases also commit during workspace and automation broker calls: resolve lazy routing before the pre-broker commit.
A real exclusive capture lease also verifies that a queued worker cannot delay
the event loop's immediate retryable maintenance response.
It also checks independent pooled connections, migration settings,
joined worker cancellation (including repeated shutdown requests) and surfaced
lock timeouts without replay. A worker failure while cancellation drains it must
be logged without replacing `CancelledError`, so the supervisor still shuts down.

The private-checkout regression in `test_containers.py` independently forces a
same-size edit with an unchanged cached timestamp and a racy Git index timestamp.
The broker's metadata copy must preserve index timestamps so Git rechecks the
contents instead of reporting a clean checkout. This is separate from SQLite
contention; neither test depends on sleeping across a filesystem clock tick.

Browser checks scope visible feedback to the main content when live announcements
repeat it. State-refresh tests seed their own initial session data so an isolated
Playwright shard does not depend on an earlier spec.

## The demo instance
`scripts/demo_instance.py <empty-dir>` runs a complete ACC with **artificial data only**: user `alex`, host `acc-host`, projects under `example-org`, sites under `example.com`, seeded usage and host readings, and fake CLIs. Use it to try changes, and to take this wiki's screenshots:
```
uv run python scripts/demo_instance.py /tmp/acc-demo
node web/scripts/wiki-shots.mjs /tmp/acc-demo      # writes docs/wiki/.attachments/*.png
```
Retake screenshots whenever the UI they show changes, then run `acc-wiki-check`.

## Adding a database migration

Before choosing a migration number, run this from the repository root:

```bash
python scripts/next_migration_number.py
PYTHONPATH=src uv run --frozen python -m pytest -q tests/test_migration_numbering.py tests/test_migration_portability.py
```

The helper prints the next zero-padded number above every migration number in
`main` and all other **local branch tips**, plus this worktree's uncommitted files.
It checks both filenames and declared revision IDs, reads source without running
it, and does not check out branches, fetch, write refs or reserve the number.
Other worktrees' uncommitted changes and remote-only branches are not visible.
Recheck immediately before committing: two parallel authors can still receive
the same suggestion before either commits.

Use that number in both `<number>_<description>.py` and its `revision` string.
Set `down_revision` to the intended current head. The graph test requires exactly
one Alembic head, unique filename/revision numbers, matching filename/revision
IDs and a gapless sequence starting at `0001`. A failure names conflicting files
or heads and suggests the next free number across local branches and the worktree.
For an intentional skipped number, add a nonempty justification to
`DOCUMENTED_GAPS` in `tests/test_migration_numbering.py` (with a decision reference
when appropriate); unexplained gaps fail. Numbers reserved on unmerged branches
may create such gaps: document which branch owns each reservation. Remove stale
gap entries when those migrations land and fill the gaps.
Resolve collisions in undeployed work before integration; never renumber a
migration already applied to an installation.

### Upgrade rehearsal

`tests/upgrade_rehearsal.py` builds a synthetic, populated database at 0066 (people, projects, sessions with history,
devices, a group, backups, DNS credentials, telemetry consent; nothing from a real installation). It upgrades it with
`db.migrate` and compares every 0066 column value by value, then downgrades back to 0066.
`tests/test_upgrade_rehearsal.py` runs this on SQLite, and on PostgreSQL when local binaries exist
(`ACC_TEST_POSTGRES_BIN`, default `/usr/lib/postgresql/16/bin`): a throwaway cluster in the test's temporary folder,
Unix socket only. A migration that changes or loses existing data fails it.

Before a release, also run the whole rehearsal:

```bash
.venv/bin/python scripts/upgrade_rehearsal.py --work <new folder> --port <your e2e port> [--postgres /usr/lib/postgresql/16/bin]
```

It starts `tests/e2e_server.py` on the seeded data (`ACC_E2E_SEED_DATA`, plus `ACC_E2E_DATABASE_URL` for PostgreSQL),
so the app's own startup does the upgrade. It then signs in as the synthetic owner and checks what the API returns.
It stops only the server it started, and writes `result.json` in the work folder. Unix sockets must fit in 107 bytes,
so the script puts the cluster's socket and the e2e state in short folders under `/tmp` and removes them afterwards.

## Conventions
- **UI:** follow the [Design system](/Internal/Design-System). Pages compose shared primitives and never define their own colours or sizes.
- **API:** one router per area under `src/acc/server/api/`. Changes are audited, and mutating requests take the database write lock up front.
- **Docs:** update this wiki in the same pull request as the change it describes (the `acc-workflow` skill has the rules).

## Performance check

`scripts/perf_check.py --work <new short folder> --port <your e2e port>` seeds a large synthetic installation
(`tests/perf_seed.py`) into the e2e server through `ACC_E2E_SEED_DATA` and times the main API endpoints. The
installation has 5 people, 40 projects, 2,000 sessions with about 300,000 events, 20,000 audit entries, 200 Inbox items
and 10 machines. With `--keep`, `web/scripts/perf-pages.mjs` opens the main pages against it and records each page's
API calls; `--phone` adds a throttled phone profile.

Lists polled by every page must stay a fixed number of queries. Resolve per-row lookups once per request (for
example `offline_machines()` for `machine_offline`) and load relationships with `selectinload`. Large lists go out
through `json_list()`, which encodes directly and sends an `ETag` with `Cache-Control: private, no-cache`, so an
unchanged poll is an empty 304. `tests/test_performance.py` checks the query counts, the unchanged bytes and the 304.

Background loops must cost nothing per idle session:
- **Ingest** first reads one cheap mark per session: the spool offset, plus the size and time of the event spool,
  the statusline and `remote.json`, all with `lstat`. It opens a session's transaction only when that mark changed
  since a pass that read everything; every `FULL_PASS_SECONDS` it reads all sessions anyway.
- **Reconcile** loads placements and projects with the sessions. It opens no transaction for a session whose machine
  and account weren't listed.
- `tests/test_ingest_idle.py` covers the skip, a multi-pass backlog, the full pass, offset resets and launching
  sessions.
- To measure idle CPU like production, start the e2e server with `ACC_E2E_PRODUCTION_INTERVALS=1`, and pass
  `scripts/perf_check.py --running 0`: seeded "running" sessions can never start and keep the supervisor busy.

## Preparing public source

Keep the working repository private. Build a clean, history-free tree from committed
HEAD using `scripts/public-snapshot.sh` with an external private denylist and a new output
folder. The complete release procedure and exclusions are in
`docs/PUBLISHING-THE-SOURCE.md` in the source checkout. Publication requires review of
that exact tree; the script never creates a repository or pushes. See `SECURITY.md`
for private vulnerability reporting and `CONTRIBUTING.md` for contribution guidance.

Source bundles (installation, node downloads and release archives) and the history-free exporter share the private-content exclusions in `src/acc/bundle.py`. Entire private folders, including future descendants and case variants, are excluded. Both paths also omit generated artifacts, secrets, and environment files (including `.env.local`). A checkout with broken Git metadata refuses bundle creation instead of including untracked files. New source folders still need the external denylist and a review of their contents. The external denylist and final publication review remain required.

Developer documentation, including the Internal wiki and technical decision
records, is published after scrubbing. Host-specific `docs/runbooks/` and
`docs/drills/`, session logs, handoffs, reviews, security reports and `.acc/`
artifacts stay private. Keep portable operations contracts in this wiki, with
fictional hosts and documentation addresses rather than live deployment details.

Test process cleanup records each owned tmux socket before use. A separate cleanup process collects its descendants even if the test parent is interrupted; Playwright also stops the test server from global teardown. Cleanup never targets other runs. Linux `/proc` supplies process identities; cleanup pins pidfds before signalling so PID reuse cannot redirect a signal. Each watchdog also checks its per-owner process marker before touching a server, so a reused socket belongs only to its new owner. Global teardown never treats a stale manifest’s socket name as authority; the server’s independent watchdog owns tmux cleanup.

Container-test relays start fake agents in separate process groups, outside tmux.
Their launcher teardown drains accepted launch requests before collecting relays,
stops each agent group and joins the relay threads. A closed terminal during this
stop is expected; other relay failures still surface. Per-test leak checks report
the responsible test, and a final check in each pytest worker also covers tests
without the `env` fixture and late launches. The xdist controller checks the whole
run's base at session end too, including workers that crashed before teardown.
These checks restrict process discovery to that run's private temporary directory
and uid (use a fresh directory for every explicit `--basetemp`), pin identities before
signalling and wait for leaked agents to exit before failing. They never clean up
leftovers belonging to earlier or concurrent runs.

Leak-check self-tests launch nested pytest from their own temporary directory and
explicitly load both `host_isolation` and the test-fixture `conftest` plugin through
the checkout's `tests` import path. Running from the checkout root would instead
resolve its different `conftest.py`, which loads only the host guard, silently
omitting the leak checks. These self-tests keep the guard enabled and use the
runner's existing uid, including in root gates; no privilege change is needed.

The orphan-owner interruption test installs its own Python SIGINT handler before
atomically publishing its readiness file, then waits for a signal. Background
runners can inherit an ignored SIGINT across `exec`; starting a new session does
not reset it. The regression includes that inherited disposition explicitly, so
the test proves watchdog cleanup rather than depending on how pytest was launched.
Its parent-exit and cleanup deadlines are unchanged.

Ordinary browser captures go to per-test Playwright output and leave maintained wiki images untouched. To deliberately refresh the feature screenshots from the artificial-data test server, run `cd web && npm run build && npm run screenshots:wiki`: the test server serves the last built UI (`src/acc/server/static`), so skipping the build captures an older UI. Specs also rewrite images other generators (the demo tour, other specs) own, and their fixture data differs from run to run; keep only images whose UI actually changed. Review every changed image visually, then run `acc-wiki-check` before committing. The demo capture command above remains available for the main guide tour.

Browser authentication runs once in the dedicated `setup` project. Stateful
authentication writers preserve a valid replacement `AUTH_FILE` when they revoke
its login. Mobile creates its own project and session; a separate context still
must not be mistaken for a separate server-side login.

The group-board scenarios invite separate members on the test broker's `board-1440-agent` and
`board-390-agent` accounts. The setup owner approves and provisions their placements, but each member
launches and manages their own session. Do not launch group work as the setup owner: other specs
legitimately add owners on its shared default account, which group-session isolation correctly refuses.

Pull requests run the Contributor checks workflow: frozen dependencies, Ruff, Python tests, strict UI build, current notices and both browser projects on Linux. Repository owners must require this check in branch rules when publishing; the workflow itself cannot configure those rules.

Release tags must pass the notice check after frozen Python and npm installs, before creating any source artifact. Notice generation reads versions from the locks and rejects absent/mismatched installed metadata or missing web license text; it never emits a partial file. The same check is required in pull-request CI and the Python suite.

Secret-scanner fixtures use narrowly annotated synthetic canaries (`gitleaks:allow` on the fixture line) or explicit filename inventories. Built-in scanner rules stay enabled. Only generated Playwright results, captures and reports are excluded by `.gitleaks.toml`; they can contain test authentication data, must not be published, and should be removed after review. With gitleaks on PATH or in `ACC_TOOLS`, `tests/test_source_scan.py` verifies that an unannotated token-shaped value in ordinary source still fails the scan. History scanning remains strict; old private-history findings are another reason to publish a new history-free snapshot.

Ingestion defers a newly launching session until its run record exists. A deterministic regression holds the broker launch reply after the fake CLI writes SessionStart, runs ingestion in that window, and verifies startup once the reply is released. This prevents a fast CLI from losing startup verification and later replacing permission advice with a false launch-timeout warning.

For query-populated browser selects, select the intended option by value rather
than index. Before asynchronous account options arrive, the same index may name
**New account…**; selecting a value waits for that actual account.

Preview test listeners enable `SO_REUSEADDR` before binding. This lets later
checks reuse a test port after an active TCP connection closes; without it,
`TIME_WAIT` can exhaust the small preview test range during a full run. It does
not allow sharing a port with another listening server.

Both `scripts/demo_instance.py` and `tests/e2e_server.py` keep host control paths
inside their temporary state: update/account/package requests, publishing, root
spool, broker settings, agent home and git backups. Their brokers use this
checkout's catalog and hooks. The screenshot demo starts with update checks
unconfigured, so trying Settings never reaches the installed ACC's update helper.
`tests/test_demo_isolation.py` captures both harness configurations before any
broker/server starts and checks those boundaries. Importing browser-harness helpers must not change feature
switches: the preview default belongs only to the standalone harness entry point, before application imports.
The Python fixture clears both `ACC_` and `RYNHULT_` spellings of the orchestrator preview, working-list/planning
and delivery switches before each test. Module-local feature fixtures opt in afterwards; off-by-default tests
must not depend on the previous test in an xdist worker.

## DNS provider phase 2 qualification

`DNSAdapter` is the narrow wire interface. Publishing uses `DNSProvider` through `OwnedDNS`, which adds a durable
ownership ledger. There is no cloud SDK credential chain: HTTPS endpoints are fixed, credentials explicit,
response bodies bounded, reads paginated, each adapter operation bounded at 20 seconds, and Publishing DNS work at
60 seconds. The Route 53 adapter implements the small required SigV4/XML surface with standard-library primitives;
that signing and the pinned Go module require live qualification. No system package is required.

Fixtures in `tests/fixtures/dns` are synthetic schema-based transcripts, not live recordings. They cover each
provider's create/update/delete and drift refusal. Missing/forged ownership, uncertain writes, vault isolation,
explicit legacy-source rollback and root credential generation behavior have regression tests too.

The preferred owner entry point is `PYTHONPATH=src python -m acc.dns_qualify --help` on a disposable Linux host.
See [DNS qualification](/User-Guide/DNS-Qualification) for exact IAM checklists, the command and cleanup procedure.
Secrets are hidden prompt input only; there is no vault export, credential-file flag, environment fallback or
secret argv. Reports have fixed labels and never promote providers. State must be a new private directory.

`acc.dns_live_test` remains the lower-level legacy harness for compatibility; its private JSON-file interface is
not the qualification kit's credential path. The kit reuses its probes and the server adapters/OwnedDNS ledger.
Certificate renewal uses the same temporary storage on a second Caddy launch, an explicit forced renewal window,
and a different serial/fingerprint. Both cycles must preserve the sentinel. Caddy JSON error events stop the probe;
process output is private, bounded and discarded. Only the owned subprocess is stopped. The verified executable
is a private copy of independently hashed bytes. Azure is held before remote writes until its shared-TXT defect is fixed.

Related local verification (synthetic API fixtures and fake executable only):

```bash
PYTHONPATH=src python -m pytest -q tests/test_dns_qualify.py tests/test_dns_providers_p2.py tests/test_dns_wave2.py tests/test_dns_hardening.py tests/test_security_review_dns_p2.py tests/test_security_review_dns_w2.py
```

Release qualification additionally requires: credential revocation/expiry tests; denied writes outside the one
zone (and wrong types/names on Route 53); parallel TXT values preserved by the exact pinned Caddy module; actual lifetime renewal scheduling;
controlled root apply/failure/rollback/restart and systemd sandbox behavior; dependency/advisory/provenance review;
and security review before merge. Record provider, trial-zone identifier, credential schema, IAM/role policy,
architecture, exact binary/module hashes, results and cleanup evidence in the release review. Never record secrets.

Only after review may maintainers mark the provider available in the fixed schema registry, update qualification
metadata, independently commit binary hashes and mark the release qualified. There is no portal override or
automatic promotion based on an all-PASS harness run. The owner still confirms Cloudflare legacy removal.

## Keyboard primitives

Compose `Tabs`, `Segmented`, `Dialog` and `Popover` from `components/ui.tsx` rather
than adding another document-level key handler. Their shared behavior lives in
`components/keyboard.ts`; `lib/keyboard.ts` contains the pure navigation policy.
No additional runtime library is required.

| Primitive | Responsibility | Main consumers |
| --- | --- | --- |
| `navigationIndex(key, current, count, options)` | Horizontal/vertical/both arrow navigation, wrapping or clamping, optional Home/End, empty lists | Roving focus and the Jump to combobox |
| `useRovingFocus(root, options)` | Find visible, enabled items at key time; arrows/Home/End move actual focus; optional prefix search; call `onNavigate` for automatic selection | Tabs, segmented radio filters and action menus |
| `useDismissableLayer(options)` | One topmost Escape owner, optional outside dismissal, modal Tab/Shift+Tab containment and focus restoration | Dialogs, popovers, Jump to and Open in editor |

Keep roving `tabIndex` and selected/checked state controlled by the widget.
`Tabs` and `Segmented` activate their existing button action on navigation;
menus move focus without running an action. The hook ignores modified,
already-handled and composing keys, native input/select/textarea editing, hidden
items, disabled fieldsets and inert ancestors. Horizontal tabs leave Up/Down
available for browser scrolling. Menu prefix search is case-insensitive,
cycles repeated letters and expires after one second; a remounted menu starts
with a fresh prefix. Buttons retain native Space/Enter activation. Menu links
retain their `href` and native Enter activation; Space explicitly activates the
link to satisfy the menu pattern rather than scrolling the page.
Typeahead owns unmatched letters too. The global C shortcut honors
`defaultPrevented` and composition, so typing C in a menu never opens New session.
The explicit Ctrl/Command+K shortcut remains available from the terminal.

Use `<Popover menu>` only for a list of actions. Give each action
`role="menuitem"` and `tabIndex={-1}`; links remain real links with `href`.
The trigger advertises `aria-haspopup="menu"`. Enter/Space open at the first
item, Down opens at the first, Up at the last; arrows and Home/End move through
the items, letters find an item, Tab leaves/closes and Escape returns to the
trigger. Mixed-content Details panels stay ordinary `<Popover>` groups, not
menus. Do not put form fields inside an action menu.
Popover explicitly focuses its trigger before opening, so it remains the return
target even when a browser or programmatic invocation did not focus the button.

For a bespoke overlay, pass a stable root ref to `useDismissableLayer` and
attach its `onFocusCapture` when children can autofocus. This preserves the
original opener even though React autofocus runs before effects. Inline
dismissal callbacks remain current without re-registering or moving focus on
polling rerenders; the layer's modal/outside/Tab modes stay fixed while open.
Only the top layer answers Escape. A non-modal child does not disable its
parent's Tab containment, including a roving item beyond its last normal Tab
stop. Empty modal scopes focus their own `tabIndex={-1}`
boundary. Never use positive `tabIndex`.

Call the returned `restoreFocus()` before an action replaces a layer with a
new dialog: that dialog should remember a stable opener, not a disappearing
menu item or combobox. Cleanup does not steal another dialog's autofocus or an
outside-click target. The Jump to surface mounts fresh on each opening, so its
old query is gone before the combobox receives focus. Its listbox keeps DOM
focus in the input and uses `aria-activedescendant`; Up/Down clamp, while
Home/End remain native text-editing keys. Its listbox has `tabIndex={-1}` so
Chromium's automatic scroll-container focusability cannot add an extra Tab stop
when a large result set overflows.

These contracts follow the WAI-ARIA Authoring Practices patterns for
[tabs](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/),
[radio groups](https://www.w3.org/WAI/ARIA/apg/patterns/radio/),
[menu buttons](https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/) and
[modal dialogs](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/).
Keep domain-specific handlers local: global C/Ctrl+K shortcuts, numbered diff
line navigation/comment editing, and xterm's CLI keys/F6 escape are not generic
menu or focus-scope behavior. Native selects, details/summary and toggle buttons
retain their browser keyboard behavior.

From `web/`, run `npx playwright test 01-keyboard 78-keyboard-primitives
--project=desktop`. The existing Playwright runner executes pure-policy tests
and isolated mounted-hook tests in a real browser (including React Strict Mode),
then keyboard-only desktop/phone application flows with axe. The test harness
bundles with the already-installed Vite/React tools, writes no production
assets and adds no production route. CI's ordinary desktop/mobile browser gate
includes these specs. Keep the existing tab, dialog, phone-drawer, login-focus
and terminal-accessibility specs as integration coverage too.

## Frontend payload budget

The production build emits Vite's manifest and runs `web/scripts/check-bundle-size.mjs` automatically.
`web/scripts/payload-budgets.json` records measured cold-load baselines for **sign-in**, **Board** and
a managed session's default **Terminal** tab. Each includes the bootstrap, route and static imports,
counting shared JS/CSS files once. Automatically mounted lazy features are explicit roots: the session
includes its shell and terminal, not just its route chunk. Unopened tools and fonts are excluded.

The build fails if **any** page's raw, gzip or Brotli JS/CSS total exceeds its baseline by more than **5%**.
Compression is per response (gzip level 6, Brotli quality 5); these are reproducible transfer-size
estimates, not measured server compression or latency. Missing page roots, imports, files or baseline
metrics fail closed. Browser regressions compare actual cold-load asset requests with those closures.

From `web/`, run `npm run test:payload` for the fixture tests (also in CI), then
`npm run build -- --configLoader runner`. `npm run report:payload` prints JSON with all three totals,
the largest emitted chunks and their module attribution. Module `renderedLength` is pre-minification
code length, not network bytes. No visualizer dependency is required.
For an intentional change, review the report and update the affected baselines and lazy roots together;
the build never increases its own budget automatically.

The initial optimization reduced gzip JS from 181,252 to 128,429 bytes for sign-in, 184,448 to 140,332
for Board, and 343,082 to 294,180 for a session including xterm. Keep the authenticated shell out of
sign-in/Board and Files, Changes, Preview, Pull request and Team out of unopened session tabs.
CodeMirror and its language grammars load when a file opens; Markdown preview loads for Markdown files.
The launch form stays mounted with the shell: opening it does not swap a smaller loading dialog for
a larger form, and Close still retains the draft.

Routes load on demand with a **Loading page…** status. Wrap every lazy import in
`lib/lazyImport.ts`'s `lazyImport(() => import("…"))`, inside the React `lazy`
callback; keep the import literal so Vite still splits and preloads it. The same
wrapper covers CodeMirror language loads. It retries recognized module/stylesheet
download failures once after 500 ms, including explicitly retrying a failed Vite
stylesheet preload. Module evaluation/programming errors still reject instead of
being labelled as an update; React's lazy component loader passes them to the
ordinary error boundary.

After the retry fails, the shared wrapper publishes reload state and leaves that
lazy part suspended rather than throwing away its nearest error boundary's tree.
`ChunkReloadBanner` is outside every Suspense/error boundary, so it also works when
Layout or the sign-in page cannot load. It uses the shared polite announcer, a
normal-flow banner and the shared confirmation dialog; there is no automatic
reload, draft storage or cache-busting copy of React. Already mounted editor/form
state remains available where its own Suspense boundary allows it; a full reload
still discards unsaved input. See [download recovery](/User-Guide/Troubleshooting#a-page-is-loading-or-couldnt-be-shown).

`80-chunk-recovery.spec.ts` aborts real production chunk requests or returns 404s,
checks a single retry, preserved editable drafts, keyboard reload/cancellation,
the shell-failure fallback and stylesheet recovery, with desktop/phone axe checks.
`44-quality-routes.spec.ts` also runs in both desktop and phone projects. Its
failed-download flow checks the recovery banner, cancellation without navigation
and explicitly confirmed reload, not the generic programming-error boundary.
The installer retains one previous generation's assets; see
[web deployment](/Internal/Operations#web-assets-across-updates). An older root
updater cannot retroactively preserve assets on its first upgrade to this code.

Ordinary browser captures belong in Playwright's per-test output directories, including screenshots
used as review artifacts. To keep a selected capture on ACC's Artifacts tab, deliberately copy it
from the test results into `.acc/artifacts/` after the run; tests do not overwrite tracked review images.

## Wave 2 provider boundaries

Google uses explicit service-account JSON with an RS256 OAuth assertion to the fixed Google token endpoint.
Publishing describes a single managed zone and uses its project-qualified identity in the ledger; changes atomically
delete the exact old RRset before adding the new one. The pinned certificate module lists managed zones and reads
credentials from a file. The harness creates an owner-only temporary JSON file and quotes TXT sentinel values in
Google/Hetzner wire form. Production files live under a fixed root:caddy 2750 directory, with content-addressed root:caddy 0640 JSON files.
Only files referenced by retained environment generations survive cleanup; failed candidates are removed.
Credential-file provisioning, rotation/rollback and purge require live qualification before setting flags. Do not grant ambient ADC as a workaround.

DigitalOcean uses fixed-domain record IDs, reconstructing pagination requests rather than following remote links.
Hetzner uses primary Console zones, paginated RRsets and bounded action polling. Neither offers conditional writes
here; a remote edit between the ledger check and mutation remains possible. Hetzner address-family changes require
unpublishing first. All wave 2 flags remain false, and infrastructure values never enter ordinary vault delivery.
Fixtures are synthetic documented-API transcripts. No account recordings or credentials belong in these fixtures.

## Windows phase 0 owner-run probe

`scripts/windows-phase0/README.md` documents the reviewed Windows-only
qualification procedure. It builds an in-box C# service/ConPTY/identity probe,
not a native Rynhult node. Installation and manifest-driven removal require an
owner's elevated Windows console; evidence collection requires the recorded
owner's unelevated console, including a reboot with no user signed in.
Never run it through a Rynhult agent, WSL session or live hook.

No run of these administrator scripts or passing gate is claimed by their Linux
source checks. Gates 1, 2 and 5 are the probe's scope; provider hooks, sandbox
denial and guard feasibility remain untested. Earlier read-only Windows
observations do not qualify them. See [current Windows support](/User-Guide/Install-Windows#can-rynhult-run-without-wsl)
and [the native-node proposal](/Internal/Decisions-and-Roadmap#native-windows-proposal-2026-10-23).

## Linux desktop spike

The approved comparative phase 0 spike lives in `desktop/`: a Tauri host, a
separate Python entry point, a bundled-Python build (`desktop/build-runtime.sh`)
and a self-contained .deb (`desktop/package.sh`). It does not change installed
server behavior. Its README documents build/test commands, the private loopback
capability and config profile, and the native checks. `DESKTOP_NATIVE_CHECK=1`
runs the real window on a private GTK Broadway display and AT-SPI bus. Run
`desktop/tests` explicitly as well as the normal Python suite; the normal suite's
discovery excludes that directory.

The Linux phase 1 source slice adds `desktop/setup.py` (dry-run by default),
`desktop/broker.py` (root-owned socket activation, separate locked account) and
`desktop/execution.py` (local owner bootstrap, audited project native consent,
CLI install and launch gates). Only `desktop/main.py` imports this composition;
the installed Python server and its defaults do not change. No users/services
were provisioned on the development host. Apply the setup only on a disposable
Linux system or an administrator-reviewed installed package. See
`docs/DESKTOP-APPS.md` for paths, privileges, uninstall and release limitations.

Build the UI separately so server assets never enable desktop authority:

```sh
cd web
VITE_DESKTOP=1 node node_modules/vite/bin/vite.js build --configLoader runner --outDir ../desktop/web-dist
```

The Tauri host and package use `desktop/web-dist`; normal web builds leave
`VITE_DESKTOP` unset. The desktop uses the existing project, provider, session,
Files/history, terminal WebSocket and broker code. Native project consent is
stored as an Observation binding project id, path and account, with an audit
entry. It is checked before creation and again before broker launch/resume.
The systemd socket is owned by the owner's UID with mode 0600 (never a shared
primary group); the broker additionally accepts only the exact owner UID. The
agent account is expired, so only its unit (NoNewPrivileges, ProtectHome) runs it. The broker
requires socket activation and clears inherited environment before starting
its tmux server. Account separation is not per-session isolation.

Run `tests/test_desktop_sessions.py` for fixture-owned broker/provider tests and
`desktop/tests/test_setup.py` for provisioning dry-run/negative checks.
`DESKTOP_BROWSER_CHECK=1` runs `tests/test_desktop_browser.py` against temporary
HTTP/WebSocket listeners, a real broker, fake providers and fake npm. The fake
broker uses the test runner UID; production rejects same-user execution.
The browser supplement checks the re-login confirmation and omits section links
for unavailable desktop cards. Its captures go to `.acc/artifacts` by default;
only `ACC_UPDATE_WIKI_SCREENSHOTS=1` refreshes maintained Guide images.
Real CLI auth probes skip unless the suite is deliberately run inside a disposable
account selected with `DESKTOP_REAL_CLI_ACCOUNT`, with its CLIs installed and
logged in. They never borrow the worker's provider credentials. Native window
checks remain separate from Chromium browser checks.

The native fake-provider checks now exercise both Claude and Codex in the actual
Tauri/WebKitGTK window, including terminal keyboard input echoed through the
PTY and the shared attach audit. `desktop/tests/session_companion.py` is an
explicit test fixture supplied through the existing interpreter override, never
a packaged same-user mode. A test-only GTK initialization shim sets Broadway
screen resolution to 96 DPI; its unset default otherwise yields a negative
WebKit device scale. Neither fixture nor shim is packaged.

Standalone first navigation satisfies owner authentication through the native
per-launch capability; it never asks for a sudo-readable setup token. New Linux
account discovery is platform-gated. Reconcile the profile lease with the
coordinator's Windows LockFileEx/EOF/runtime/NSIS patch when it lands; the Windows
results were reported separately and were not rerun on this Linux host.


### macOS standalone account/launchd port

The desktop-only mac_setup module generates JSON account, ACL and launchd plans
without privileges on Linux. Apply is restricted to macOS Installer/root and its
fixed protected Runtime; it creates an independent hidden account, private home,
per-account spool, projects, root receipt and LaunchDaemon. No general root RPC
or deprecated elevation API is present. mac_broker activates and validates the
launchd listener and reuses shared peer checks, broker handlers and tmux. Human
login stays disabled; only job commands explicitly use /bin/sh.

The package builder stages in the private .acc build tree. It wraps Python agent
helpers with bundled Python because Apple's Python is too old and the fixed
resource path contains spaces. Default uninstall preserves a GUID-bound identity
receipt; an explicit purge removes the recorded account/data, optionally the
unused shared Runtime. App/profile and Installer receipt removal remain separate.
The coordinator smoke checks the package's owner/action recipe before Installer;
this is accidental-mismatch protection, not signature verification.

Portable Mac-plan tests live in desktop/tests/test_mac_setup.py,
test_mac_broker.py and test_mac_package.py. The broker/PTY probe has its own
fixture-only test in tests/test_desktop_mac_probe.py. The real Mac entry point
is desktop/tests/mac-phase1-smoke.sh; native account creation, launchd, ACLs,
Keychain/TCC, physical terminal/recovery and signing still require the MacBook.

Phase 2 macOS standalone, slice 1, adds:
- the portable-main bundle (`desktop/build_mac_main.py`);
- a Keychain credential interface with a fake (`desktop/mac_keychain.py`);
- the start-at-login LaunchAgent (`desktop/mac_launchd.py`);
- tmux recovery on macOS paths (`desktop/mac_tmux.py`).

All of them are behind `desktop/mac_gate.py`, the same staff/admin membership check. Tests:
`desktop/tests/test_mac_standalone.py`. Mac commands: `desktop/tests/mac-standalone-plan.md`.

### macOS preparation

The same spike now has an Apple-silicon runtime lock and native assembly branch,
`.app`/`.dmg` config (provisional macOS 13.0 floor), minimal hardened-runtime
entitlements and nested-Python signing. `desktop/package.sh --unsigned` skips
Developer ID/notarization and does not consume inherited signing credentials.
`--signed` requires the owner's complete signing/notarization environment.
See `desktop/README.md` for variable names, lock regeneration and the entitlement
rationale; never commit certificates, keys or passwords.

Linux can cross-check the shared Rust supervisor/transport with
`cargo check --locked --target aarch64-apple-darwin --no-default-features --lib`
from `desktop/src-tauri`; the default feature still builds the full host.
The full Mac host needs Xcode/SDK. Linux tests exercise script dispatch, locked
artifact hashes and the process monitor, not Mac OS behavior.

On a logged-in Apple-silicon Mac, `desktop/tests/mac-smoke.sh` builds an unsigned
app and records readiness, observed-tree idle and native-quit/SIGTERM/SIGKILL
shutdown checks. Its reports are not whole-app memory qualification: WebKit XPC
services may be outside the process tree, and RSS differs from Linux PSS.
The README has the manual VoiceOver, keyboard and sleep/lock checklist.
**No Mac results exist from this Linux preparation.** Intel wheels, signing,
Keychain/TCC and the earlier product release gates remain unqualified.

## HA connection-loss test safety

HA application wiring tests can substitute SQLite at the PostgreSQL boundary;
production HA startup requires PostgreSQL. When injecting loss of a retained
`Authority.leader` connection while workers run, take `Authority.lock` around
`close()` or `invalidate()`, just as authority checks and release do. The runtime's
async transition lock only serializes monitor/owner tasks on the event loop; it
does not stop worker threads from checking authority. Never hold the threading
lock across an await.

SQLAlchemy invalidation physically closes the DBAPI connection. SQLite's
`check_same_thread=False` permits sequential use on different threads; it does not
make closing a handle concurrently with a query safe. The connection-monitor test
asserts lock ownership in the invalidation event before physical closure so a
future unsafe fault injection fails an assertion instead of risking a native crash.

### WSL warning concurrency and conditional HA inventory

A WSL risk choice uses the `wsl_isolation.revision` returned by `GET /api/nodes`.
Send it as required `expected_revision` to the warning preference PUT route. A
successful transaction advances an enrollment-bound generation even if the
boolean did not change. Missing revisions return 422; old revisions return 409
without a preference or Audit write. Keep the revision captured when opening a
confirmation and refresh after conflicts. This prevents stale owner choices; it
does not attest node-reported Windows permissions.

`tests/ownership_inventory.json` includes HA-only registrations with
`configuration: "ha"`. The ordinary route gate verifies the default app; the HA
fixture verifies its three conditional routes and member refusal. Adding a new
configuration requires an explicit inventory and a registration/authorization
probe rather than exempting its routes from the catalogue.

Node handshake/update-state JSON may contain escaped unpaired Unicode surrogates.
Normalize those before persistence and SQL parameter use; normalize legacy report
projections as well. Preserve valid Unicode. JSON parsing alone does not guarantee
that text can be emitted in a UTF-8 API response. The authenticated node regression
in `tests/test_wsl_isolation.py` covers both ingestion paths and saved reports.

For machine background work, see [Node task ownership](/Internal/Node-Task-Ownership).
Register connection-bound jobs with the captured link, or use `NodeHub.start` for
a newly scheduled machine job; do not create a detached asyncio task.


### Skills and code-graph research

The source checkout's `docs/research/skills-and-codegraph-2026-10-25.md` reviews
Superpowers, Matt Pocock's skills, CodeGraph and Serena at pinned commits. It
records instruction-size and repository-size measurements, network/update
behavior and proposed catalog/guard constraints. Recommendations are research;
no catalog item, profile or MCP server is enabled by that report. In particular,
local code analysis does not imply zero telemetry or an authorization boundary.

### Release candidates

Source, macOS and Windows release attachment workflows classify every tag containing a hyphen as a GitHub
prerelease and explicitly prevent it becoming latest. Stable tags keep the existing release behavior.
Publication approval gates still apply; this does not authorize tagging or publishing an RC. Installed updaters
continue to follow stable releases. `tests/test_release_prerelease.py` exercises the workflow shell with a fake
GitHub CLI, without creating releases.

Release day for a candidate follows a private, owner-held checklist (excluded from source bundles and public
snapshots) that marks each step as the owner's or the coordinator's.

RC builds reach testers privately, so `nodes.release_links()` returns no installer links for a hyphenated
version: Add machine and Mac updates keep the from-source commands, and the Add machine response carries
`prerelease: true` for the UI's note. Telemetry reports an installed RC's version (`-alpha.N`, `-beta.N` or `-rc.N`
only; `telemetry_schema.RELEASE_VERSION`), so the collector must run the same schema before RC testers send data.

### Startup and end-of-run hook ingestion

Reconcile uses one pending-hook drain before startup verification and exit closure. It reads one bounded chunk
for an unverified or ended run and defers both decisions while more backlog is known, rather than declaring a
missing startup hook, draining an arbitrary backlog in one transaction or assigning a late SessionEnd to a later
run. Remote exits refresh the mirror before the database snapshot, within one
shared ten-second spool budget per reconcile pass; unavailable remote hooks defer closure. Existing mirror and
record-size limits still apply, and a permanently partial final line cannot hold a dead run forever.

Hook events and offsets commit together. Agent proposals from those hooks enter the deferred queue only after
the outer transaction commits; outer and savepoint rollback discard them. Re-reading a rolled-back hook does
not replay an uncommitted proposal. The next ordinary ingest pass processes committed proposals through the
usual owner approval checks. No separate audit writer or nested database session is opened inside ingestion.

### Framework dependency upgrades

Run the API suites with the candidate lock's packages, not just an older shared interpreter environment.
`tests/test_dependency_validation_contracts.py` pins required JSON object bodies, explicit backup confirmation,
secret-free DNS validation errors and signed collector rejection without storage. These are HTTP-boundary tests;
they do not replace the authentication, permission and lifecycle suites.

The standalone telemetry collector has its own hash-locked requirements and may pin a different Starlette version
from the main app's `uv.lock`. Exercise collector security/ops tests in that exact environment too. Keep dependency
installs private to the checkout; never change a shared environment merely to run a dependency review.

### Synthetic restore rehearsal

From the repository root with test dependencies and git installed:

```sh
PYTHONPATH=src python -m pytest -q -n 4 tests/test_rc_restore_rehearsal.py
```

The shared fixture obtains the pinned official restic in pytest scratch and verifies its
committed checksum; it does not install a system package. An unavailable download skips
the fixture, so a skipped run is not a successful qualification. Replacement preparation
must run unprivileged; that case skips when pytest itself runs as root.

Each case starts an isolated ASGI main, pauses its supervisor, seeds two projects/two
stopped sessions/four browser and native device records/owner settings/a vaulted canary,
and creates a bare git backup plus excluded workspace/provider-login canaries. Owner API
requests export the kit and queue a real capture; the worker is explicitly awaited. The
kit and independent generation record live outside the source fixture, never in artifacts.
The test closes the main, deletes its complete fixture root and relocates the ciphertext
repository. Only the saved kit and exported ciphertext are available to the offline CLI.

Both ordinary restore and `--prepare-replacement` must recover counts, settings, vault
values, catalog bytes and the git commit into a fresh tree, reject a wrong repository
password without publishing a destination, and start the real app in quarantine. Startup
is guarded against migrations, engine creation and supervisor construction. The test never
removes the hold or activates authority. Browser sessions/invites are deleted; captured
revocations survive. A deliberate post-capture revocation demonstrates the independent
record requirement. Preparation revokes all devices and removes the old API token.

No live service, S3, root helper, real provider login, machine configuration or shared
repository is used. Local mutable ciphertext does not qualify protection, real-host fencing,
controller activation/release, external PKI custody, encrypted scratch media or PostgreSQL.

### Tests must not access the installed host

Root gates run beside a real installation. Use the complete `isolated_settings(root, **overrides)` factory for
standalone server worlds, `isolated_broker(root, **overrides)` for broker helpers, or the shared `env` fixture. Every writable server/helper path must point into the test's
temporary tree, including `containers_dir` even when containers are disabled: audit ingestion still runs. Keep source,
catalog and Guide reads in this checkout. Pure assertions about production defaults may still construct `Settings()`;
they must not probe those paths.

On Linux, the repository conftest loads `tests/host_isolation.py` for both ordinary and desktop tests. It refuses installation
paths, real homes/provider configuration, privileged Unix sockets and host-control commands before Python accesses
them. A caught refusal still fails test teardown. Temporary homes/configuration and a safe tool PATH apply to tests
without `env` too; inherited ACC/RYNHULT deployment variables are removed before collection. Python bytecode writes
are disabled to avoid changing a shared interpreter environment. Only this checkout is writable under installation
roots; the running interpreter and Playwright browser tools are read-only exceptions. Metadata checks on their exact
ancestors support pytest discovery, without allowing directory listings or sibling-file access.

License integration checks need this worktree's own `npm ci --prefix web` and Python dependencies matching `uv.lock`.
Do not symlink `web/node_modules` to the installed application: that can both mask lockfile drift and read host files
in root gates. The license test rejects a shared directory link before traversal; the host guard has no dependency
exception for the installed app. Catalog and metadata-refusal unit tests use only their specific inputs.

Read-only OS/runtime probes outside installation roots are allowed: for example `/etc/os-release` or the systemd
runtime binary `/usr/lib/systemd/systemd`. These are platform/dependency facts, not Rynhult state. Tests must still
stub platform facts when their expected result depends on them. Installed system/user unit directories (including
listing or metadata probes), installation paths, homes and credentials remain forbidden even for reads; redirect
those probes into the fixture. Systemd runtime writes and real service-control commands remain forbidden. Do not
allow an entire installation root just to test whether a dependency exists.

Creating, renaming or unlinking an inert symlink in a temporary fixture does not access its target and is allowed.
The guard checks the entry's lexical location and resolves its parent; a symlink in a parent cannot redirect the
operation into protected state. Reading through the same link remains refused.

The live-root gate policy is Linux-only; native Mac/Windows desktop runners retain their existing setup.

A tripwire failure is a fixture defect: pin the missing path or install a fake helper. Do not add a production-path
exception or rely on PermissionError, nonexistent files, root-only skips or disabled feature flags. Unit tests of a
fixed helper transport must redirect its socket to a temporary listener. Permission regressions may explicitly drop to the existing `nobody` account; they never create accounts.
Disposable Podman tests must provide private stores/configuration/runtime directories; shared Docker daemons are refused.
Real tmux calls require a uniquely owned
`acc-test-*` socket (or a temporary `-S` path); real systemctl calls require an offline temporary `--root`.

This Python tripwire is not an OS sandbox: native child processes and arbitrary shell script internals need explicit
temporary paths and fake tools too. Keep installer/root-helper fixtures in dry-run mode or against their disposable
roots; qualify real users/services only in disposable systems. Never run live agent tools from a test session.

## Release artifact checks

Qualify releases from disposable clean worktrees, with project-local dependencies and caches. Build twice with
separate output directories and different checkout timestamps, and compare the Python wheel/sdist, Linux source
archive and generated web-file hashes. The source tarball embeds its installer; Python packages alone do not
include the built dashboard. Python sdists intentionally exclude the private checkout's documents.

Run `tests/test_release_dryrun.py` with `RELEASE_TEST_DIST` naming the Python artifact directory and
`RELEASE_TEST_LINUX` naming the RC Linux tarball to include the artifact-backed privacy and fake-root installer
checks. Use `PYTHONPATH=src` and the project's Python test environment. The `ACC_INSTALL_ROOT` seam stubs privileged
setup and does not replace disposable-VM qualification.

The updater sorts RC versions but admits only stable automatic-update candidates. Release checksums and `BUILD`
are unsigned today; signing and public-source privacy approval are separate owner decisions. The repository's
`docs/PUBLISHING-THE-SOURCE.md` documents the checksum verification and proposed manual signing ceremony.

## Assignment and attempt identity

The v2 assignment ledger lives in `server/orchestrator/assignments.py`.
`Team.protocol_version` freezes legacy versus assignment-based starts at grant time;
0075 backfills issued v2 tool configurations and selection evidence without treating
every old team as a new v2 grant. The global switch cannot downgrade a v2 grant. An assignment
pins its classification, requirements and plan/brief versions. Each helper launch
reserves a distinct attempt before broker I/O. The reserved attempt ID also names
the session, checkout and branch. The session and attempt binding commit together;
a lost reply is adopted from that binding or refused, never relaunched. A committed
session owns its checkout even when its caller sees an exception.

Reports retain the legacy session/task primary key for upgrades, with a unique
`(attempt_id, version)` key for assignment reports. Delivery rows carry the receiving
attempt. A transactional team issuance counter orders attempts independently of civil
clock changes at a main handover. Persistent task reports and live report facets
resolve only the current attempt; a previous task never becomes current after a
clock rollback. `give` requires enabled delivery, an idle screen and no active
writer. Default reports wait for the new brief receipt, or explicitly name its
issued attempt. `read` accepts an attempt and optional report version, and defaults to its
latest report; continuation packs contain summaries and paths rather than all
findings bodies. Read-only findings remain private DB evidence, not a file-write
capability. Existing session authorization protects the person-facing ledger.

Planning/brief integrations call `assignments.invalidate` in their change transaction:
it returns affected assignment IDs, marks dependent reported/verified work as needing
revalidation, and queues versioned notices for running attempts. With delivery
disabled, affected helpers must stop before the accepted scope changes. Requirement IDs
are `R-n`; absent or unknown requirement data refuses a start. Accepted requirements,
constraints and material decision blocks must be resolved before starting work.

## Image qualification

`server/image_capabilities.py` decorates model metadata from owner-triggered
qualification records in the existing settings store (no new migration). The key
binds account, machine, provider and exact model ID; the broker installation ID,
adapter/CLI versions and a
30-day lifetime are checked separately. Raw node-reported `image_input` is always
replaced by recorded evidence. Selection readiness and pre-launch context use the
execution runtime's fingerprint (adapter source is fixed at broker import, so replacing
files underneath a running broker cannot relabel its loaded adapter); custom endpoints and unresolved default models
remain unknown. The launch overlay updates lane A's fixed image sentence without
persisting runtime capability facts into a launch specification.

`broker/image_probe.py` runs a bounded restricted native turn, without project
files, inherited customizations, MCPs or Rynhult hooks. The account's sign-in is lent
using the explanation helper's credential isolation, with bootstrap disabled.
The generated PNG contains no textual answer metadata. Stored evidence contains
only bounded generated-test answers, digests, outcomes and enum error categories. Timeout/output limits kill the process group started by the
probe and remove its temporary working tree and credential links/copies.

The broker seals the session/canonical cwd/device/inode binding at launch with an
in-memory HMAC key, never an agent-readable signing key. Edited/copied bindings
and replaced workspaces refuse; the receiver never changes Git exclusions.
`brief_references.receive` uses that binding, not a caller-supplied path, writes
chunked private uploads with descriptor-relative no-follow operations, and refuses
unless Git ignores the destination. This is transport only: lane E's assigned-
reference authorization and lane C's qualified assignment remain required. Old
sessions without a current binding must be launched again before receiving uploads.
A broker restart rotates the key; replace team helpers through a new attempt.
