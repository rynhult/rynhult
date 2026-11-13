# Browser test state

**Own the state you change.** Import `test` and `expect` from `./fixtures` in
specs, not directly from `@playwright/test`. Types may still use Playwright.

The desktop-stateful and mobile-stateful projects share one disposable server.
Their automatic fixture resets presentation before **each test**, and restores
it in teardown even when the test fails, signs out, changes person or mocks APIs.
The control request uses a separate context and a random fixture capability;
there is no production reset endpoint. It never uses a live server.

The default is the release's feature defaults (with the fixture server's explicit
configuration pins preserved), no chosen setup level, default
header order/visibility, built-in theme and no personal layouts/file references.
All people are covered, including members created during a test. Chat is an
admin feature when present. P2 terminal hosting is an immutable build flag
(`VITE_P2_TERMINAL_HOST`), not an admin feature or runtime setting; it cannot leak
between tests in one build. The reset covers personal P2 layouts/file references
when those tables exist.

Every journey uses the production layout. The fixture does not seed a layout
preference, and the disposable server does not override the production frame.
Authenticated state supplies `person_id` for person-fenced local arrangements.
Additional people use the same frame automatically. The fixture owns and resets
automatic working-layout/file-reference writes from mounting that frame. Other
presentation changes still require explicit declaration.

Prefer restoring changes explicitly in `finally`. Tests intentionally exercising
temporary non-default presentation may instead declare the groups the fixture
owns for them:

```ts
import { test, expect } from './fixtures';
test.use({ presentationChanges: ['admin_features', 'layouts'] });
// The test still explicitly changes the declared setting through the real API.
```

Valid groups: `admin_features`, `setup_level`, `ui_header`, `theme`,
`layouts`. Declare only groups the test actually changes. This is not a blanket
leak exemption: the fixture restores declared groups, then checks for remaining
drift **before** the final unconditional cleanup. An undeclared changed setting
fails its responsible test with `Unowned shared presentation state leaked` and
its name (never its value). Revision-only changes and explicit default choices
are not leaks. Each subsequent stateful test starts from defaults. Before its first reset, drift
left between tests is reported with the previous and next test names, then
cleared. This catches `afterAll`/`beforeAll` changes at the next stateful boundary;
it cannot attribute a hook to an individual test with certainty. Do not share
presentation setup in these hooks: set up in each test or beforeEach and restore
in finally/afterEach. A final afterAll with no later stateful test has no next-test
boundary: its drift can persist undetected through the later desktop/mobile
projects. Only final disposable-server teardown removes that state.

This fixture does not erase accounts, projects, sessions, security policy,
credentials or other domain resources. Specs must clean up their own resources;
never rely on projects or presentation left by another test. The server's fake
CLIs, temp database and browser contexts remain disposable. Its vault-delivery
wrapper supplies an artificial API key to match fake Claude's default API auth
status; inherited worker credentials grant no launch authority. Subscription
guard tests explicitly change their own fake CLI status and must still refuse
automation.

Run the standard configuration with **one worker**. Shared-server projects depend
directly on setup; at one worker the shared-server families run in declaration order:
desktop-stateful, mobile-stateful, terminal-chromium, desktop, mobile. The
view-registry project owns its separate loopback Vite fixture. A stateful failure does not skip the later
projects. Do not add a failure dependency to enforce sequencing, and do not
raise worker concurrency when presentation projects share a run with other
families: global setup refuses `--workers>1` for those selections, including
projects pulled in as dependencies. Focused non-stateful families may use more
workers; a single stateful family keeps its project worker limit of one. Focused invocations can
select one feature file and setup normally; no extra configuration is needed.

When merging tests from another branch, migrate any new specs to `./fixtures`
and declare/restore their changed presentation. No product assertion should be
removed to accommodate isolation.

`npm run test:e2e-isolation` requires `npm run build -- --configLoader runner`
first: its collection check loads the payload spec, which reads built assets. CI
runs the isolation check immediately after the UI build.

Full gate and wiki screenshot commands explicitly select every normal project:

```bash
npx playwright test --project=view-registry --project=desktop-stateful --project=mobile-stateful --project=terminal-chromium --project=desktop --project=mobile --workers=1
```

CI allows 150 minutes for this step and 240 minutes for the job including Python,
build and separate browser qualifications; the prior three-project run took about
90 minutes. This is a timeout allowance, not a new runtime measurement.

Global setup and `npm run test:e2e-isolation` reject direct base-test imports in
specs **and shared suite modules**. `fixtures.ts` alone defines the extension. Helpers and shared suites use that
same test object; shared fixture paths live separately to avoid import cycles.
Types may still come from Playwright.

The batch-2 terminal-chromium project depends directly on setup and retains
workers: 1. It is explicitly selected by the gate and wiki commands, alongside
view-registry. The terminal cases are excluded from desktop and terminal-input
runs only in terminal-chromium. Custom project names can opt into presentation
isolation with `metadata: { presentationIsolation: true }` and must set
`workers: 1`. Keep setup as the direct dependency; depending on another test
family restores failure cascades.

Wiki screenshot updates require `ACC_UPDATE_WIKI_SCREENSHOTS=1`. Desktop and
desktop-stateful projects may write wiki attachments through the shared helpers.
`wikiShotPath` also allows mobile/mobile-stateful to update these exact
mobile-only attachments: `settings-dns-mobile.png`, `settings-dns-google-mobile.png`
and `ha-standby-phone.png`. Other mobile captures use per-test output, protecting
shared desktop images from overwrite. Extend the explicit `mobileWikiImages`
allow-list only for attachments owned exclusively by mobile specs. Desktop
specs also capture explicitly selected phone viewports for the wiki.

Tests that reauthenticate or sign out while running in parallel must request the
`isolatedOwner` fixture before creating their projects or navigating. It seeds a
unique artificial owner and an unverified sign-in through the disposable server's
capability-protected fixture route. Pass its `secret` to `verifyAgain` for real
reauthentication, or use its `username` and `secret` for a real login. This keeps
TOTP replay counters and sign-out local to the test. It does not isolate global
Security registry revisions: project policy setup must re-review an explicitly
stale revision and fail on other refusals. Owners and their data remain in the
disposable database for inspection.

For a second person in a terminal/control test, request `otherOwner`. It owns a
separate browser context with a unique disposable owner, using the same
capability-protected identity route. Owners share the default agent account;
sharing that account does not make them the same person for control or partial
input confirmation. Do not borrow `finn-agent` or another named member account
from the shared installation just to obtain a second signed-in person. Tests of
invites or account assignment must still exercise those real flows explicitly.

Request `_drainRoutes` in tests with finite fetch/fulfill interceptors that can
still be running after the last assertion. It waits for page and context route
handlers before the default page/context teardown, preserving handler errors.
It does not suppress failures. Release test-owned gates in `finally` first.
Tests that deliberately hold loading requests indefinitely must own their
release/abort policy instead of requesting this fixture.

Use `createTeamHelper(lead)` to create a managed child with its immutable parent
and compiled proof. Then use `arrangeTeam('member' | 'history', lead, helper)` for synthetic team membership
and the large assignment-history fixture. These capability-protected disposable
membership/history routes run short synchronous database mutations through normal writer admission.
Membership refuses a helper created under another parent; fixtures never reparent persisted sessions.
Do not seed the shared browser database from a separate SQLite process: its
commit can invalidate another test's in-flight tool result and leave a correctly
refused interrupted request. Wait for the owned Team/Selections responses before
asserting a large history's rendered cards.

When measuring concurrent intercepted requests, remove canceled requests on
`requestfailed` as well as when their route handlers finish. Pagination can abort
an old row while its artificial response delay is still running; counting that
handler as an active browser request creates a false concurrency failure. Keep
awaiting all handlers before disposing their routes.

## Phone containment with accumulated data

`50-ux-final-containment-mobile.spec.ts` includes a stateful-data variant in
addition to the empty-fixture scan. It creates long project/session/team names,
long paths and branches, paused schedules, an orchestrator plan with assignments,
a multi-run comparison, a retained launcher draft, and pending/refused tool requests.
The refusal notice uses a long session name, matching notices accumulated by
earlier stateful specs. Inbox titles and metadata wrap within their column. It checks all `AXE_PAGES`,
Groups, the Install guide, and representative project/team details at 320 and
390px in both themes. Multi-run summaries and page headings wrap long names and paths instead of
forcing the main area wider. Settings recovery actions wrap their labels to
fit narrow screens. Working column controls wrap below the tab strip when a
narrow pane cannot fit them alongside it. Root and main widths must still fit exactly; tables,
code and tab strips may scroll only within their own containers. A JSON
attachment lists overflowing routes and elements on failure. Both scans include
the route, viewport, widest element selector/width and overflowing elements in
the assertion message, so gate logs identify the broken content without traces.

Run just this file with the normal config and a unique fixture base/port:

```bash
ACC_E2E_PORT=19846 ACC_E2E_BASE=/tmp/containment-example npx playwright test e2e/50-ux-final-containment-mobile.spec.ts --project=mobile-stateful --workers=1
```

The fixture owns its resources and stops sessions, deletes schedules/projects,
archives groups, rejects its pending tool request and dismisses its refusal notice
in teardown. It uses the disposable fake-provider harness;
no accumulated state from earlier specs or live installation is needed.

Git backup CGI children belong to their HTTP request's process group. Request
cleanup and server shutdown reap that group, including receive-pack helpers.
Global teardown independently checks the run's private `GIT_PROJECT_ROOT` paths
for surviving Git CGI/helpers, reports leaks as failures, and kills only those
processes using pinned identities. It runs even if tmux teardown fails. Always
use a unique `ACC_E2E_BASE` when running alongside another checkout.
