# Components

Terminal attachment uses a separate Python interpreter (`broker/pty_exec.py`) in a new OS session to acquire the PTY as its controlling terminal before executing tmux. It needs no Linux `setsid` utility or threaded `preexec_fn`. Allocation, sizing, spawn and handshake share one cleanup lifetime. The output pump reads at most one 64 KiB chunk before awaiting socket drain, with no PTY reader registered while blocked. WebSocket writer claims are released even when connecting fails or is cancelled.

The terminal API reauthorizes both the current login role and the session's current ownership every 30 seconds. Owner demotion closes affected read-only and writable sockets immediately, preserving the person's own terminals and login.

The session terminal exposes xterm's accessibility tree by default, with a browser-local toggle. F6 exits from both the input and accessibility rows; a skip link bypasses the grid. Its text dialog reads an on-demand snapshot through `GET /api/sessions/{id}/terminal-output`, authorized by session ownership and routed to that session's broker account and machine. The read-only broker operation captures up to 10,000 tmux history rows plus the current screen, joins wrapped lines and returns at most 200,000 characters with a truncation flag. The response is not cached (`Cache-Control: no-store`), and no transcript is stored. A failed capture offers the parsed browser buffer with an explicit limitation. Details: `docs/decisions/0045-accessible-terminal-output.md` in the repository.

New session sends `project_id` to `/api/providers/registry` and `/api/models`. The API authorizes that project, then queries its host and execution account, rather than trusting arbitrary account selectors from the browser. Without a project it uses the current person's local account. The UI caches these results by project (or local account), separately from other screens' capability registry, and clears model/effort choices when switching scope.

Provider login dialogs are keyed by provider, account and machine. Each start operation owns its immutable scope and active lifetime. Cleanup cancels an existing flow or cancels a pending start when its response arrives. Polling is serial (the next timeout starts after the previous response), and late start/poll/submit responses cannot update a replacement dialog. Cancellation failures are reported to the browser console.

## accd (`src/acc/server`)
- `app.py` builds the shared context and wires the routers.
- `api/`: one router per area: `auth`, `projects`, `sessions`, `inbox`, `operations` (host, providers, models), `catalog`, `publishing`, `accounts`, `terminal` (websocket), `wiki`, and `frontend` (built UI and security headers). `context.py` holds shared state and the auth checks; `schemas.py` the request and response shapes.
- `supervisor.py` with mixins `polling.py`, `ingest.py`, `reconcile.py`: the background loop.
- `api/multiruns.py` composes ordinary session launches and redacted Changes into person-owned groups. Each accepted attempt keeps an ordinary session and worktree; refused attempts remain visible. Keep/archive never removes these worktrees or branches. In-process initial launches are excluded from recovery until their first broker launch completes; after a dashboard restart the usual recovery rules apply.
- Reconcile keeps a run open when tmux reports a dead pane without an exit status or signal: terminal closure can precede child exit. It records the result before scheduling a retry or replacing the pane, preserving recovery history across polling timing changes.
- One bounded pending-hook drain precedes both startup verification and exit closure. Team helpers can start after the iteration's ingest pass; slow intervening stages must not cause a false missing-start-hook alert when `SessionStart` is already available. Both decisions wait for remaining known backlog, preserving final hook reasons. The normal startup timeout still applies when the hook is absent.
- For a dead pane with no result, the broker runs a fixed `true` job on its own tmux socket and reads the panes again once. This wakes child collection on older tmux builds that can lose SIGCHLD during utempter cleanup. It changes no machine settings and never signals an arbitrary PID.
- `compiler.py`: turns launch inputs and the catalog into the exact CLI arguments and generated files. The New session preview uses the same function, so it matches the launch byte for byte.
- `catalog.py`, `catalog_store.py`, `catalog_import.py`: the repository catalog plus dashboard additions in a local overlay; GitHub import with review checks.
- `local_workspace.py`: workspace folders and the built-in group (`builtin = 'local'`, one per account on this machine). It is found by account and host, and refused unless its owner, account, host and folder all match; the slugs `local` and `local-*` are reserved at registration, import and promotion.
- `push.py`: Web Push (VAPID signing, aes128gcm encryption); the supervisor sends new inbox items to subscribed devices.
- `publish.py`, `cloudflare.py`: the publishing pipeline. `github.py`: read views and repository creation. `pulls.py` (routes in `api/pulls.py`): a session's pull request (create, update, ready, reviewers, merge checks) and the redacted, untrusted-framed messages for its agent; owners' calls use the dashboard token, members' go through the broker's `github_api` op as their own `gh` login (docs/PR-FINISHING.md).
- `tools.py`, `api/tools.py`, `acc.tooling`: registry-only tool approvals, scoped authorization, manager validation/execution and resolved-version inventory. System changes use `acc-packages`; account changes use a fresh unprivileged broker connection. The guard classifies parsed installs, not unfamiliar executable names.
- `vault.py`: the key vault: encryption with `data/secrets/vault.key`, `.env` parsing, which keys a launch gets (and the refusal of duplicate variables), the instructions paragraph, redaction, and the one-time move of the catalog's MCP secrets. The supervisor's `_start` (launch and resume) delivers keys through the broker's `session.env`.
- `db.py` and `migrations/`: SQLite by default or opt-in PostgreSQL, with Alembic migrations applied at start.
- `reconcile.py` reads session identities and broker routing in a short database scope, fetches pane lists after closing it, then reloads each session in its own transaction. A request or external writer committing during the broker list call cannot invalidate the later pane-state update. Deleted sessions and sessions moved out of ACC management are skipped. Broker parameters that can load placement/routing relationships are resolved before committing, so evaluating `cwd` or `host` cannot reopen a read snapshot immediately before a broker await.
- `history.py` fetches transcripts through the broker without holding a database transaction, then persists each session in a joined write-intent worker. `db.write_in_thread` keeps lock waits off the event loop and drains the worker on cancellation. Automation cancellation and model-cache persistence use the same helper; host-pressure polling uses the shared write-intent decorator in its existing worker.

### API (selection)
| Route | Purpose |
|---|---|
| `GET /api/sessions`, `POST /api/sessions`, `POST /api/sessions/preview` | List, launch, preview |
| `GET · POST /api/multiruns`, `GET /api/multiruns/{id}` | List, start 2–5 independent runs with partial budget refusals, inspect a group |
| `GET /api/multiruns/{id}/compare?left=...&right=...`, `POST /api/multiruns/{id}/keep` | Compare two authorized member sessions; mark a winner and optionally confirm stopping/archiving the others |
| `POST /api/sessions/{id}/stop · resume · interrupt · archive · rename · promote` | Session actions |
| `WS /api/sessions/{id}/tty` | Terminal |
| `GET /api/workspaces` | Local workspaces |
| `GET /api/wiki/{project\|workspace}/{key}` (+ `/page`, `/attachment`) | Wikis; `guide/acc` is the Guide tab: this wiki, read by accd from its own checkout, for everyone signed in |
| `GET /api/projects`, `POST /api/projects`, `/import`, `/{id}/trust` | Projects |
| `GET /api/attention`, `POST /api/attention/{id}/ack` | Inbox |
| `GET /api/catalog`, `POST /api/catalog/preview · import · skills · mcps` | Catalog |
| `GET /api/publish`, `POST /api/publish`, `/{id}/approve · reject · unpublish` | Publishing |
| `POST /api/workspaces/{folder}/delete` | Delete a local workspace (typed folder name; archives its sessions) |
| `GET /api/push`, `POST /api/push/subscribe · unsubscribe · test` | Notification devices |
| `GET /api/host · /api/providers · /api/accounts · /api/audit` | Read views |
| `GET /api/host?machine=` | Another machine's details over its link (owners; `node_host.py`, docs/MULTIHOST.md "Host details") |
| `GET /api/board` | The status board's machines and projects (owners; `api/board.py`) |
| `GET /api/sessions/{id}/changes` | Current-branch diff, sharing the polled badge's baseline (broker `workspace_changes`) |
| `GET /api/search?q=` | History search, scoped like `/api/sessions`; optional `project_id`, `provider`, and inclusive UTC session-start date `since=YYYY-MM-DD` filters apply before ranking/limits. `indexed_sessions` counts only filtered, accessible conversations with indexed messages |
| `GET · PUT /api/guardrails` | Guardrail rules (everyone reads; owners write) |
| `GET · POST /api/schedules`, `PUT · DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/enable · disable · run · cancel` | Person-owned saved launches and timing; authenticated people only, creator or owner access; stable run intent and conditional dispatch claims in the supervisor loop |
| `GET /api/tools`, `POST /api/tools/requests`, `/requests/{id}/approve · reject`, `/{id}/remove` | Account/machine tool requests, decisions, installed versions and approved removal |
| `GET · PUT /api/tools/policy` | Owner-only person/project tool-approval settings |
| `GET · PUT /api/budgets`, `POST /api/budgets/extra` | Your own usage budgets |
| `GET · POST /api/vault/keys`, `POST /api/vault/keys/{id}` (+ `/replace · remove · give · take`) | Key vault: your keys and shared ones; values are write-only |
| `POST /api/vault/import/preview · import` | Paste a `.env`: variables and problems (never values), then add the chosen ones |
| `GET /api/projects/{id}/keys` | Keys given to a project, and the ones you could give it |
| `GET /api/vault/people`, `POST /api/vault/people/{username}/remove` | Owners: key counts per person, delete a leaver's keys |

## Broker (`src/acc/broker`)
`server.py` (JSON lines over a peer-checked Unix socket), `ops.py` (operations, including git: worktrees, `workspace_changes`, screen prompts), `attach.py` (framed PTY stream for the terminal), `logins.py` (CLI re-login flows; a pseudo-terminal for CLIs that read the code only from one), `models.py` (model catalog). Each CLI's conversation file is read for history search by `providers/transcripts.py`.

`workspace_changes` recomputes the current HEAD's merge-base with origin/main or local main first, then origin's recorded default, the configured upstream, origin/master or master. It never fetches. `comparison_ref` identifies that base in summaries and detail; without a shared reference it explicitly reports working changes against HEAD (the empty tree before the first commit). Unexpected Git errors remain errors. Plain folders and untracked nested scratch folders return `git: false`; tracked subfolders use relative, subfolder-scoped diffs. The stored launch base/argument remains for older node brokers. See `docs/decisions/0066-current-branch-change-baselines.md` in the source.

Codex question detection checks only the last eight non-empty screen rows and requires live footer controls: a positive question count with **shift+← to answer** at the bottom (optionally above the main prompt), or **Type your answer** with the submit/skip/main-prompt key hints at the bottom. Only the prompt detail reaches the supervisor. Reconciliation refreshes a screen-sourced non-command prompt's activity and Inbox detail when it changes (for example, opening a queued question), and clears them when the controls disappear. Hook-sourced waiting states remain owned by hooks.

The provider's `screen_idle` signal reports a Codex turn-ending error only when a line beginning with `■` is directly above the main input prompt in that live footer, with no **esc to interrupt** indicator. The broker passes the error text, not the screen, to reconciliation, which redacts it and records **Idle** with the error as activity. Other providers return no idle signal by default.

## Node (`src/acc/node`)
`acc-node` runs on a machine the primary drives (multi-host, design in `docs/MULTIHOST.md`). It opens no port: it keeps one outbound WebSocket to the primary's `/api/nodes/connect` and relays *channels* to the node's own broker sockets.
- `cli.py`: `acc-node enroll | run | status`. `enroll` makes an Ed25519 key, posts the public half with the one-time code to `/api/nodes/enroll` and writes the config file; the key is replaced only after the primary accepts. Hidden: `enroll --installed-state-dir` and `adopt node.key|ca.pem` (stdin), which the Linux wrapper uses so root enrolls into a staging folder and the `acc-node` user stores its own files. Files are written with `write_atomic` (random temporary, renamed over the target, never following a link).
- `identity.py`: the key, the challenge signature (`nonce || primary id`) and TLS trust. Every connection is fully verified (chain, hostname, `CERT_REQUIRED`), either against the system CAs (`--trust system`, a public certificate) or against the primary's private CA only (`--ca-fingerprint`, e.g. Caddy's internal CA). The leaf is never pinned, since it renews (Let's Encrypt about every 60 days, Caddy's internal CA every few hours).
- `acc.wsl` supplies shared WSL detection, effective interop observation and metadata-only drive access checks. `client.py` asks each agent broker's `ping(wsl_isolation=true)` for access under that account, with bounded parallel calls; absent/old brokers report unknown. The authenticated hello and periodic update-state info carry the sanitized observations. Host warns on either reported exposure, not pending configuration. Owner-only `PUT /api/nodes/{name}/wsl-isolation` stores dismissal under `node.wsl_isolation.<node-id>` in Setting and audits `node.wsl_isolation`; it does not change node configuration and cannot be set by node reports.
- `deploy/node/install.sh` offers isolation only as an explicit WSL opt-in (interactive default No, or `--wsl-isolate`; `--wsl-disable-automount` is a separate choice). It runs the stdlib-only `acc.wsl` editor in Python isolated mode, preserving comments/unrelated keys and making a protected backup before atomic replacement of a safe `/etc/wsl.conf`. Unparsable or linked files are refused; `--update` never changes this configuration.
- `enroll.py`: with `--ca-fingerprint`, the CA root comes from `GET /api/nodes/ca` (fetched without verification, accepted only if the SHA-256 of its DER matches) and is saved as `ca.pem`; the enrollment POST is already verified against it. `--fingerprint` is a deprecated alias.
- `client.py`: the connection (challenge → hello → ready; 4401 means rejected or revoked), `ping`/`pong` every 20 s (three unanswered pings drop it), reconnects with backoff 1 s doubling to 60 s (back to 1 s after a successful handshake), and the relay. Frames are `type | channel id (4 bytes, big-endian) | payload`: `o` opens a Unix connection to `<socket dir>/<account>.sock` (names must match `^[a-z_][a-z0-9_-]{0,31}$`, and a symlink out of the socket dir is refused), answered by `k` or `e`; `d` carries at most 1 MiB; `c` closes either way. Each channel is its own connection; data for one broker that stops reading is capped at 16 MiB, then that channel is dropped. At most 64 channels, live or draining, exist at once (more opens get `e`); a channel the primary closed has 30 s to drain; a disconnect aborts them all. The primary's side (`src/acc/server/nodes.py`) has its own budgets, listed in `docs/MULTIHOST.md` under Budgets.

## Web UI (`web/`)
React, TypeScript, Vite and Tailwind. `components/ui.tsx` holds the design system's primitives; pages in `pages/`. Built into `src/acc/server/static` and served by accd.

## Scripts and helpers
- `bin/acc-hook`, `bin/acc-statusline`: what sessions call; append to the spool.
- `bin/acc-guard`: every CLI's pre-tool hook. Records the step (the "doing now" line) and enforces the session's `guardrails.json`, answering in each CLI's deny format (table in `docs/PROVIDERS.md`).
- `bin/acc-request`: agents ask to publish or change tools; tool coordinates are validated before spooling. `bin/acc-wiki-check`: checks public wikis.
- `deploy/`: systemd units, the root helpers, Caddy and AppArmor files, `install.sh`.
- `deploy/containers/acc-containers`: the trusted container launcher (root, standard library only). Its protocol,
  checks and lifecycle are in `docs/CONTAINERS.md` ("Trusted launcher"). accd's side is `server/containers.py`
  (limits, grants, audit ingestion) and `api/containers.py`; the broker's is `broker/containers.py`.

## ACC updater

`acc.server.updating` checks stable releases daily and schedules opt-in installations. `/api/updates` is owner-only.
`acc-update.path` starts the root helper for action/version request files. It backs up with `acc-backup`, verifies
release SHA256SUMS, safe archive members and BUILD, swaps source, runs the new `deploy/setup.sh --yes` (which keeps the existing main settings and calls `deploy/install.sh`), and checks version/build
health. Failure restores the previous source and pre-install database before reinstalling the previous version.
Steps and results are in `updates/state.json`; full backups and previous source trees remain for recovery.
Linux nodes use the same helper in node mode, driven through `update` / `update-state` signed-link messages.
The shared source bundle validator lives in `acc.bundle`. See the repository's `docs/UPDATES.md`.

For ordinary older Linux/WSL nodes that cannot accept the update message,
**Host → Machines** supplies copyable, enrollment-preserving root recovery commands
from this main's verified source bundle. Mac rows offer an installer only if one
exists for the build; pre-v1 builds have none.
See [older-machine recovery](/User-Guide/Nodes#update-an-older-machine), including
the separate independent-update rule for backup receivers.

## Session automation

`server/automation.py` validates per-session settings and makes deterministic continuation decisions. Migration 0033 adds the session's JSON automation state: goal bounds, original start and counters, waiting reset identity, consumed reset, input cancellation and dispatch claim/result. Reconcile captures provider limit/permission signals through the broker's `automation_screen` operation, checks indexed assistant markers, then commits a stable claim before sending literal text and Enter. Claimed or uncertain sends are not replayed after restart. A missing delivery acknowledgement needs human inspection; this is at-most-once dispatch, not guaranteed delivery.

The API uses session ownership (or ACC owner), with `session.autoresume` / `session.goal.*` audit entries. Terminal input cancels before forwarding; submitted prompt hooks also cancel unless they match ACC's expected prompt. Goals run independently of Orchestrator teams and scheduled sessions. Budget pauses veto automated input. Plan readings belong to the default agent account; other accounts use CLI reset signals and wait when an optional usage cap cannot be measured.

## Group authority API (phase 1)

All group routes live below `/api/groups`. Inaccessible resources return 404;
instance-owner-only actions return 403 to ordinary members. Authority writes carry the
current positive `revision`, compare it atomically and advance it on commit.
A 409 means refresh and review again. Shared group views never proxy a live checkout.

| Route relative to `/api/groups` | Contract |
|---|---|
| `GET /`, `GET /{group}` | Current memberships only, or all groups for a trusted instance owner |
| `POST /` | Instance owner: `name`, existing `maintainer_id`; explicit opt-in |
| `GET /{group}/people` | Maintainer/owner: existing ACC IDs, usernames, group roles/provenance only |
| `POST /{group}/members/{user}` | Maintainer/owner, or local self-removal: `role` (`maintainer`, `member`, `viewer`, `remove`); only instance owners appoint maintainers; departure revokes the person's group key consents atomically |
| `POST /{group}/archive` | Instance owner; preserves history, revokes all group key consents atomically and refuses future changes |
| `POST /{group}/unpause` | Maintainer/owner; audited override authority, execution still gated |
| `GET/POST /{group}/projects` | Members read safe metadata; maintainers/owners create with `name`, `slug`, nonempty maintainer `owners`, optional `workspace` |
| `POST /{group}/projects/{project}/owners` | Maintainer/owner: replace nonempty named maintainer `owners` atomically |
| `POST /{group}/projects/{project}/archive` | Maintainer/owner; metadata archival |
| `POST /{group}/projects/{project}/approvals` | Current named maintainer: `branch`, exact 40-character `old_sha`, `new_sha`; records receipt, never executes a merge |
| `GET/POST /{group}/projects/{project}/placements` | Own reservations only (all for instance owner); instance-owner-only approval takes `user_id`, derives same-host account/path, leaves trust pending |
| `POST /{group}/key-grants` | Key owner only: `key_id`, optional `project_id` and `host`; consent only, no delivery |
| `POST /{group}/key-grants/{grant}/revoke` | Key owner only; invalidates current consent; departure/archive already deletes affected grants |
| `GET /{group}/audit` | Maintainer/owner: allowlisted action/actor/time/receipt ID, no detail JSON |

Archive and membership changes invalidate earlier merge receipts through revision
changes. Phase 2 Git exchange rechecks receipt, membership and named ownership
under the ref-update fence; an HTTP receipt alone cannot authorize a merge.

## Group exchange (phase 2)

The exchange adds authenticated upload-pack/receive-pack through private per-request
staging and bounded pack helpers. Account brokers provision private placements and
standalone session worktrees, with expanded-content checks before checkout.
Published Files/Wiki reads Git blobs rather than live files. Group and private
recovery use separate authorization/stores and existing host capture/restore.
See [Group Exchange](/Internal/Group-Exchange) for routes, tokens, limits, identity
fences and crash recovery. Group sessions launch in private same-host checkouts (see [Groups](/User-Guide/Groups#launch-a-group-session)).
