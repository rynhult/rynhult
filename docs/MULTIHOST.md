# Multi-host: one dashboard, several machines (M11)

Status: in progress (overnight run 2026-10-22). This is the design both sides are built against.

## Goal
One ACC (the **primary**) shows and runs sessions on other machines (**nodes**): Linux servers, a Windows PC (through
WSL2) or a Mac. People still choose where each session runs; ACC never assigns work by itself. With no node enrolled,
ACC behaves exactly as before.

## Shape
A node runs the same per-account **brokers** as the primary does today, plus one small service, **acc-node**. acc-node
keeps one outbound, authenticated WebSocket to the primary and relays **socket connections** over it: when the primary
wants to talk to `<account>`'s broker on that node, it opens a *channel*; acc-node connects to
`/run/acc-broker/<account>.sock` (or the node's configured socket dir) and pipes bytes both ways until either side
closes. Everything that already goes over a broker socket (JSON-line calls, the framed PTY `attach` stream) works
unchanged, because the primary's `BrokerClient` only needs a different way to open its connection.

The node accepts no inbound connections from other machines; roaming git-backup jobs briefly use a token-scoped loopback forwarder. A node never sees dashboard tokens, other nodes, or the primary's data;
it can only relay to its own brokers, which keep their own checks (allowed roots, provider list, account names).

## Enrollment
1. An owner selects **Hosts → Add host** and names it (`^[a-z0-9][a-z0-9-]{0,39}$`). The primary stores a pending
   node with a one-time code (random, 10 minutes, single use; stored hashed).
2. The page shows the command for the node, with the primary's URL and how the node should trust its TLS. Not the
   leaf certificate (it renews: about every 60 days with Let's Encrypt, every few hours with Caddy's internal CA):
   - a public certificate: `--trust system` (system CAs, hostname checked);
   - a private CA (Caddy's internal CA on a LAN): `--ca-fingerprint <hex>`, the SHA-256 of the CA root's DER. acc-node
     downloads the root from `GET /api/nodes/ca` without verification, accepts it only if the hash matches, saves it as
     `ca.pem` in its state dir, and from then on verifies every connection (chain and hostname) against it alone.
     `--fingerprint` is a deprecated alias.
   `acc-node enroll --primary https://acc.example.lan --ca-fingerprint <hex> --code <code> --name <name>`
3. acc-node generates an Ed25519 key pair (private key `0600` in its state dir), then
   `POST /api/nodes/enroll {name, code, public_key (base64 raw 32 bytes), info: {os, arch, hostname, version}}`.
   The primary checks the code, stores the public key, and answers `{node_id}`. The code is now spent.
4. `acc-node run` connects and stays connected (reconnects with backoff, 1 s doubling to 60 s).

## Connection and authentication
`GET /api/nodes/connect` upgraded to a WebSocket (wss through Caddy). TLS is verified as chosen at enrollment (system CAs, or the saved private CA), hostname included.
1. Primary → node, text: `{"t": "challenge", "nonce": <base64 32 random bytes>, "primary": <primary id>}`
2. Node → primary, text: `{"t": "hello", "node_id": ..., "signature": base64(Ed25519 sign(nonce || primary id)),
   "info": {...}, "accounts": [<broker socket names>]}`
3. Primary verifies with the stored key (and that the node isn't revoked), then `{"t": "ready"}`. Otherwise it closes
   with code 4401. One connection per node: a new authenticated connection replaces the old one.

## Channels (after "ready")
Binary WebSocket messages, each one frame: `1 byte type | 4 byte big-endian channel id | payload`.
- `o` open (primary → node). Payload: JSON `{"account": "<name>"}`. The node connects to that account's broker socket.
- `k` opened (node → primary), normally empty payload; or `e` error (node → primary), payload UTF-8 reason, channel gone.
- `d` data, either way: raw bytes for the channel (max 1 MiB per frame).
- `c` close, either way: the sender closed its side; the receiver closes its side and forgets the channel.

Backup channels can negotiate request half-close without changing ordinary broker
channels: the `backup-v1` open request adds `"request_eof": true`; a supporting node
acknowledges with `k` payload `backup-eof-v1`. Only then may the primary send `h`
with an empty payload. The node flushes all preceding request data, calls the
receiver socket's write-half-close, and continues relaying the complete response.
Receiver EOF still sends the ordinary `c` close and releases the channel. Repeated
`h` is idempotent; subsequent request data closes the channel. An unnegotiated `h`
closes that channel without half-closing its socket. Older nodes send an empty `k`,
so the primary keeps the previous full-close behavior rather than waiting for a
receiver EOF it cannot trigger. Update both sides for half-close support.

Channel ids are chosen by the primary (odd, increasing). Account names must match `^[a-z_][a-z0-9_-]{0,31}$`; the node
refuses anything else and any socket outside its socket dir. Text messages after "ready": `{"t": "ping"}` /
`{"t": "pong"}` every 20 s; three missed pongs end the connection.

## On the primary
- `node` table: id, name, public key, status (pending/active/revoked), enrollment code hash + expiry, info, last seen,
  created by. Sessions and projects already have a `host` column (`local` by default); `host` names a node.
- `Brokers.get(account, host)`: `local` → Unix socket (today); a node → a `BrokerClient` whose connector opens a channel.
  Broker calls and `attach` take the session's host.
- Paths (Q4): a node keeps its files elsewhere than the primary (`/opt/acc` on Linux, `~/Library/Application
  Support/ACC` on a Mac). Each broker's `ping` reports its `run_dir`, `hook_path`, `statusline_path` and `home`;
  `Supervisor.layout(host, account)` asks for it (cached 5 minutes) before a session on that node is created, and the
  session's files (Claude's hook and statusline commands, Codex's skill paths) and arguments (`--settings`,
  `--plugin-dir`, `--mcp-config`, Copilot's folders) are compiled with the node's paths. `session.run_dir` is the
  node's folder. Launch and resume pass that `run_dir`, and the broker refuses one that isn't
  `<its ACC_RUN_DIR>/<id>`, so a mismatch (an older primary, a node reinstalled elsewhere) fails loudly instead of
  starting a CLI that reads settings from a path that doesn't exist. A node too old to report its layout is refused
  at launch with "update it". The spec is compiled once per session: after a node is reinstalled at another path,
  start a new session there.
- Hook events: sessions on a node write `events.jsonl` in the node's run dir. Ingest reads remote spools through a new
  broker method `read_spool(acc_id, offset, limit)` (the broker runs as the account and owns that file).
- UI: **Hosts** page (enrolled nodes, status, last seen, OS, agent software; Add host shows the command; Remove revokes
  and disconnects), a **Host** choice in New session (local workspaces on that host; projects registered on that host),
  host shown on session rows when there is more than one.

## On a node
- A node's hello `info` carries `wsl` ("2", "1" or "0": kernel release, `WSL_DISTRO_NAME`/`WSL_INTEROP`, the
  `WSLInterop` binfmt handler) and, inside WSL, `windows` ("Windows 11 (build 22631)", from `cmd.exe /c ver` over
  interop with a 3 s limit; left out when interop doesn't answer). `/api/nodes` turns them into `system` (`kind`
  macos/linux/wsl/unknown and a `label`); a Linux node that sends no `wsl` is `unknown`.
- `acc-node` (Python package `acc.node`): `enroll`, `run`, `status`. State dir `/var/lib/acc-node` (Linux), config in
  `/etc/acc-node.env` (primary URL, trust mode `ACC_NODE_TRUST=system|ca` and CA file, node id, socket dir). Runs as user `acc-node`, which brokers allow
  (`ACC_ALLOWED_UIDS`).
- Install: `deploy/node/install.sh` (Linux: brokers + acc-node systemd units, no accd/Caddy), `deploy/node/macos/`
  (launchd; brokers as the signed-in user; Homebrew tmux), `deploy/node/windows/install.ps1` (WSL2 Ubuntu with systemd,
  then the Linux install inside it; a logon task keeps WSL running). Each has a dry run (`--dry-run`, `-WhatIf`).
  A rerun of the Linux installer refreshes the agent accounts it already has (`/etc/acc/broker/*.env` with
  `ACC_BROKER_ROLE=agents`, each with its own `ACC_ALLOWED_ROOTS`); only a first install without `--agent` creates
  `<SUDO_USER>-agent` (or `acc-agent`). acc-update passes `--update`, which never creates an account.
- What the installers expect from `acc.node`: `python -m acc.node enroll|run|status`. Linux uses the defaults
  (`/etc/acc-node.env`, `/var/lib/acc-node`, `/run/acc-broker`); enroll runs as root through `/usr/local/bin/acc-node`
  (it writes `/etc/acc-node.env`), which makes the key and CA in a private root-owned staging folder; the `acc-node`
  user then stores them in its own state dir (`acc-node adopt`), so root never works on paths `acc-node` controls.
  The installer runs only a root-owned `uv` outside home folders, and makes `/opt/acc/app` root's with no group or
  other write bits before running anything from it. On macOS the paths come from the environment: `ACC_NODE_CONFIG`,
  `ACC_NODE_STATE_DIR`, `ACC_NODE_SOCKET_DIR` (all under `~/Library/Application Support/ACC`), and the broker runs as
  the signed-in user, so `ACC_ALLOWED_UIDS` is that user's own uid.

## Agent software and sign-ins (M14)
- **Sign-ins.** `/api/accounts…` routes take `machine` (default: this one): an enrolled, connected node and an account
  its hello reported. The login flow's broker calls (`login_start`, `login_submit`, `login_poll`, `login_cancel`) and
  `auth_health` go to that node's broker over its link, so the CLI's own login runs there. A full auth pass also asks
  every connected node's brokers; health is cached as the observation `auth.health.host.<machine>.<account>` (no inbox
  items for other machines yet).
- **Agent software.** Text frames after "ready":
  - primary → node `{"t": "packages", "id": "pkg-<12 hex>", "action": "install|update|remove|check", "package": <name>}`
    (owners only, audited as `package.<action>` with `{"machine"}`; auto-update stays a per-machine setting);
  - node → primary `{"t": "packages-state", "kind": "helper"|"mac", "helper": bool, "packages": {...}, "pending":
    [...], "refused": {"id", "detail", "at"}?, "daily_at", "updated_at"}` on every connect and whenever it changes
    (acc-node looks every 2 s).
  acc-node (`acc.node.packages`) accepts exactly those four keys, catalog names from `deploy/packages/acc-packages`
  (loaded from the installed copy), at most 10 requests a minute and 8 waiting, and reports refusals in the next
  state. On Linux/WSL it writes `/srv/acc/packages/requests/<id>.json` for the node's own acc-packages helper
  (installed by `deploy/node/install.sh` with its path unit and daily timer; `ACC_PACKAGES_GROUP=acc-node` so acc-node
  reads `state.json`; `acc-node.service` may write only that folder). On macOS there's no root helper: acc-node runs
  `npm install -g --ignore-scripts … <package>@latest` (or `uninstall -g`) with Homebrew's npm as the signed-in user,
  only for catalog entries marked `mac_ok` (Codex, Copilot), one at a time; state in `<state dir>/packages.json`.
  A Mac accepts `check` for every entry (it only runs the CLI's `--version`), so a CLI installed by hand shows up.
  The primary keeps a node's last state on its link (nothing while it's offline) and cleans it to catalog names and
  the dashboard's fields; text frames over 64 KiB are dropped.
  `/api/packages` adds, per entry, `group` (`agent`, or `backup` for restic, listed only on the main ACC and on
  machines that hold a backup target), `installable` (what that machine would accept now: its helper, or on a Mac
  only the `mac_ok` entries) and, when it isn't, `guide`: the provider's own install steps for that system
  (`acc.server.agent_setup`, with each command's source page cited there; Guide → Connect an agent repeats them and
  a test keeps both equal). The primary refuses with 409 what the machine would refuse, before sending anything.
- **Host.** `/api/nodes` carries each connected machine's CLIs (`software`) and cached sign-ins (`logins`).

## Host details (2026-10-23)

Owners see another machine's health on Host with the **Machine** picker (`GET /api/host?machine=<name>`, owners only;
members get 403 and keep the scoped view of this machine). Nothing new listens: the request and the answer travel as
text frames on the node's existing authenticated link.

- **Capability.** A node that can answer says `host-v1` in its hello's `info.capabilities`. The primary keeps only the
  capabilities it knows (`preview-v1`, `host-v1`). A node without it is never sent a request; the page says to update
  that machine.
- **Frames.** `{"t": "host", "id"}` (primary → node) asks for the full snapshot; the node answers
  `{"t": "host-state", "id", "snapshot"}`. On connect and every 60 seconds the node also sends
  `{"t": "host-light", "light"}`: CPU, memory, load, uptime and the fullest disk, for the status board.
- **Collector.** `acc.hostinfo` is the unprivileged part of the main's root collector (`deploy/hoststate/acc-hoststate`):
  the same field names for disks, GPU (nvidia-smi), systemd unit states and failed units, Tailscale, uptime and kernel,
  plus psutil's live numbers (the main's `host.live()` uses the same function). acc-node runs as its own unprivileged
  user, so SMART, containers and agent account limits are `null` and listed in `unavailable`, as is anything a
  platform lacks (systemd on a Mac or WSL without it, nvidia-smi). Null is shown as unavailable, never as 0.
  `acc-hoststate` stays a standalone root script (it is installed outside the virtualenv); a test checks that both
  keep the same field names.
- **Bounds.** The node collects a full snapshot at most every 10 seconds (requests in between get the last one) and one
  at a time; every command has a 5-second timeout; lists and strings are capped (24 disks, 8 GPUs, 48 units, 48 failed
  units) and a snapshot over 60 KiB drops its services and GPU parts. The primary cleans what arrives again
  (`server/node_host.py`: known fields only, numbers range-checked, the same caps), serves a snapshot younger than 15
  seconds from the link instead of asking again, shares one outstanding request between callers, and waits at most 8
  seconds before answering with what it has. Text frames over 64 KiB are dropped as before.
- **Nothing is stored.** Snapshots live on the link object; a disconnected machine shows as offline with its last-seen
  time.

## Budgets
What one side holds for the other is bounded, and going past a bound ends only the offender's channel or link:
- Primary: a WebSocket message is at most 4 MiB (uvicorn `ws_max_size`, for every socket); a node's data payloads
  over 1 MiB, or control payloads over 4 KiB, close the link. Data before `k` is dropped. A channel holding more than
  16 MiB its reader hasn't taken is closed (and the node told with `c`); more than 64 MiB queued for a node that isn't
  reading closes its link; a drain waits at most 30 s. No frame or ping for 90 s closes the link. Replacing or
  revoking a link closes its WebSocket (4409, 4401) and cancels its sender.
- Handshake: at most 32 sockets wait for a hello at once, and at most 4 from one address (the client address as
  Caddy forwards it), so one host can't hold every slot and lock reconnecting nodes out (1013). A node has 5 s to
  answer the challenge. A hello that isn't an object with a hex `node_id` and a
  bounded `signature` is refused (4400) before any database or signature work. An enrollment code that is malformed
  or wrong never takes the database's write lock.
- Remote spools: all hosts' `read_spool` calls share a 10 s budget per pass, a failing host is skipped for 15 s
  doubling to 5 min, a reply must be strict base64 of at most 512 KiB, local ingest runs whatever happened, and each
  session's mirror stops at 64 MiB with one `remote_spool_full` event. The broker reads `events.jsonl` only as a
  regular file of its own, through no links, without blocking.
- Node: at most 64 channels, live or still draining (30 s); 16 MiB queued for one broker and 64 MiB for all of them together (each frame counted with its overhead, empty ones skipped); everything is aborted on disconnect.
  `acc-node.service` has `MemoryMax`, `TasksMax` and `LimitNOFILE`.

## Approved tools (M16)

Design and manager boundaries: [TOOLS.md](TOOLS.md). Account tool changes use
`tool_change` on a fresh broker connection for the session's account and host,
through the existing binary channels. The broker validates registry coordinates,
refuses root/services execution, and supplies fixed manager argv and destinations.
This operation is not automatically retried after a broken connection.

System tools use bounded text frames:

```json
{"t":"tool","id":"tool-012345abcdef","action":"tool","manager":"apt","name":"ripgrep","version":"14.1.0-1","operation":"install"}
{"t":"tool-result","id":"tool-012345abcdef","ok":true,"version":"14.1.0-1","detail":"ripgrep installed"}
```

The main permits only approved requests. acc-node accepts exactly the tool fields
in `acc.tooling.helper_request`, system managers only, at most 8 outstanding and
10 requests a minute (shared with catalog requests). It writes this machine's
`acc-packages` request file atomically and waits for its result; it never executes
the package command itself. Frames over 2048 characters are ignored. On a Mac,
system managers fail explicitly; account tools still use its broker.

The main correlates results only to a pending ID on the same live link. Unknown
IDs are ignored. Disconnects fail pending work; reconnects do not replay it.
Timeout or restart means an uncertain change, recorded as failed with an Inbox
item; a person should inspect the machine before retrying. A compromised node's
result is still the node's word, as for its existing software inventory.

## Known limits
- **Remote hook events are the node's word.** Whatever a node returns from `read_spool` is ingested like local hook
  events: native ids, verified runs, states, attention items, guardrail records and publish requests (which still need
  approval). A compromised node can make these up for its own sessions. It cannot touch another node's sessions: the
  mirror is chosen by the primary's session id, and only sessions routed to that node are asked.
- **A Mac node runs sessions as the signed-in user**, the same user as acc-node. A session can read the node's private
  key, config and CA and change its installed code, and a primary can run code as that user through ordinary broker
  calls. Treat a Mac node as giving the primary and every session that account; use a separate macOS user for ACC
  if that's too much.
- (Fixed 2026-10-22: `--install-prereqs` and setup.sh no longer pipe uv's install script into a root shell; they
  install a pinned uv release checked by SHA-256, `deploy/lib/install-uv.sh`.)

## Git backup (M15, 2026-10-22)
Every project's git history gets a copy on the main ACC, so there is a backup besides GitHub (and the only one for
projects without a remote).

**Storage.** One bare repository per project at `/srv/acc/git/<project id>.git` (`ACC_GIT_DIR`; acc 0700, created by
`deploy/install.sh`; each repository on its first backup). Settings, re-applied before every push:
`receive.denyNonFastForwards`, `receive.denyDeletes`, `receive.fsckObjects`, `http.receivepack=true`,
`http.uploadpack=false`, `http.getanyfile=false`, `core.logAllRefUpdates=always`. git's settings protect branches
only (a forced push could still move or delete a tag), so each repository also has an `update` hook that accd writes
(`gitbackup.HOOK_SCRIPT`, 0700) and puts back whenever it differs: before every push, and in every repository when
accd starts. For each ref it refuses a deletion, replacing an existing tag, a branch update that isn't a fast-forward
and anything outside `refs/heads/` and `refs/tags/`. It judges refs one by one, so a refused ref doesn't refuse the
rest of the push. Each repository records whose it is (`acc.project` = `<slug>@<creation time>`): project ids come
back after an older `acc.db` is restored next to newer backups, so a repository recording another project is moved
aside to `/srv/acc/git/orphaned/<id>-<slug>-<time>.git` before the next push and a fresh one made; the Backup card,
the bundle and pushes only see a repository that records the project. A repository from before identities is claimed
by its project if it is younger than the project, otherwise moved aside too. Nothing already backed up can be rewritten or deleted through a push, whatever the pusher sends,
and the reflog records every update.

**Transport.** accd serves git's smart HTTP at `/api/git/<id>.git/` by running `git http-backend` (as acc: a subprocess
with a clean environment, no global or system git config, at most 4 at once, 30 minutes and 2 GiB per push:
`ACC_BACKUP_MAX_PUSH`; the response is capped at 64 MiB). Only `GET info/refs?service=git-receive-pack` and
`POST git-receive-pack` are served; fetching (upload-pack), the dumb protocol and every other path answer 403/404.
accd's 4 MiB body limit and Caddy's `request_body` limit skip `/api/git/*` (accd counts those bytes itself).

**Tokens.** Each push needs `Authorization: Bearer <token>` naming that one project. accd mints the token right before
it asks a broker to push, and revokes it when the broker answers; it expires after 10 minutes in any case. Tokens live
only in accd's memory (the verifier indexes SHA-256 hashes; an active roaming job caches its issued token for retries; a restart forgets them) and in the pushing git's environment. A project
with backups turned off refuses pushes even with a valid token.

**Who pushes.** The supervisor's `backups` stage, every 60 s, looks for projects that are due: every 15 minutes
(`ACC_BACKUP_INTERVAL`), and right away after a session in the project stops (stopped, exited or archived). For each it
asks the project's broker (this machine's or a node's, through the node link) for `git_refs(path)`: a SHA-256 of its
branches and tags. Only when that changed since the last good backup (or the last attempt failed, or the bare
repository is missing) does it mint a token and call `git_backup(path, url, token, ca_pem)` on a separate broker
connection with a 30-minute timeout, so the shared connection (reconcile, Changes) never waits behind a push. The
broker runs, as the project's account,
`git push --porcelain --no-verify <url> refs/heads/*:refs/heads/* refs/tags/*:refs/tags/*` with the token in
`GIT_CONFIG_COUNT/KEY/VALUE` (`http.extraHeader`), never in argv, a file or a remote. The same environment sets
`core.hooksPath=/dev/null`, an empty `credential.helper`, `http.followRedirects=false`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0` and `GIT_ALLOW_PROTOCOL=http:https`. With a private CA, the primary
sends its root (public) and the broker points `http.sslCAInfo` at a temporary copy. Projects on this machine are
pushed the same way, through the same address.

**Rewritten history.** A branch or tag the backup refuses (amended and force-pushed upstream, a moved tag) is pushed
again as `refs/heads/backup/<UTC yyyymmdd-hhmmss>/<branch>` (tags: `refs/tags/backup/<time>/<tag>`), so both
histories are kept. The result lists it under `kept`; accd records a `git_backup_kept` event and shows it on the
project's Backup card. Branches deleted in the project stay in the backup.

**People.** On by default for every project (migration 0022, `project.git_backup`); the project page's **Backup** card
turns it off (audited: `project.backup`), shows the last backup, what it holds (branches, tags, size), errors and kept
rewrites, and offers **Back up now** and **Download bundle** (`git bundle create --all` from the bare repository,
audited: `project.backup_download`; owners and the project's own member, 404 for anyone else). Host shows all backups'
size. Push results are logged (refs updated and kept, seconds), never tokens. `deploy/backup/acc-backup` copies
`/srv/acc/git` into each day's backup, hard-linking files unchanged since the previous day and copying refs before
objects.

**Address.** `ACC_BACKUP_URL`, or else the first `https://` origin in `ACC_ALLOWED_ORIGINS`. With neither, backups
are off (the card says so): a plain-http address would carry tokens in the clear.

### Roaming machines: public-address fallback

Owner decision 2026-10-25: public git-backup fallback is **off by default per
machine**, including existing enrollments. Owners enable it in Machine settings;
`Node.public_git_backup` is stored against the immutable node ID (migration 0068),
participates in the policy revision and is audited as `node.public_git_backup`.
Disabling it revokes pending/public grants and closes this machine's public git
streams. Private pushes and the signed control link remain available. Already
written refs remain in the backup repository.

With machine identities, verified public remote access and per-machine owner
consent enabled, a node
advertising `git-backup-v1` gets a scoped job through the signed link. It first
requests a token at the usual private backup origin. Only connection establishment
failure permits retrying that request at the public origin, using system server
trust and its own current machine certificate. TLS validation errors, HTTP
refusals, read failures and redirects do not trigger fallback. The selected
origin is fixed for the token and push; pushes never redirect or switch midway.
The signed job also carries the SHA-256 of the push token the main minted with
the grant; the node refuses to push unless the exchange returned that token, so a
server that merely holds a certificate accepted by system trust receives no git data.

Four more rules apply:

- **Origin pin.** The node takes `git-backup` jobs only over a control link
  established privately. A job through the public name (current or previously
  pinned) is refused. It pins both origins learned from private-link jobs.
- **Per-machine queue.** Jobs for one machine wait for each other instead of
  failing.
- **Bare origins only.** A backup URL with a path or plain `http://` keeps the
  broker push.
- **Audit.** The first public token exchange of a grant is audited as
  `git_backup.public_token`.

The main issues a ten-minute project/node grant over the signed link; only that
grant can exchange for this project's token. Public requests additionally require
the machine's active leaf and the project must still live on it. Browser/owner/
agent tokens cannot request a node push token. Completion, cancellation or grant
expiry revokes its push token. Token responses are no-store and capabilities are
not persisted or logged. Grants cache the token only for active-job retries.

The node opens a temporary authenticated IPv4 loopback endpoint for that project's
GET receive-pack advertisement and POST receive-pack only. It streams to the
selected TLS origin with a 2 GiB request and 64 MiB response cap. HTTPX ignores
proxy environment variables; the broker receives no identity/key path. Git's
bearer header is URL-scoped and server verification/redirect/proxy settings are
explicit, so a repository URL rewrite cannot carry it to a different destination.
The forwarder ends when the job/link ends, and one job runs per node.

The main compares the successful node result with the authenticated token path,
saves `via: private|public`, and records successful public use as
`git_backup.public` plus `backup.public.<immutable node id>`. Owners see **Host →
Machine → Last backup via public address**, including for an offline machine.
Local and older nodes retain the private transport. All other node routes keep
their existing addressing; scheduled backups still need an active signed control
link. No public control-link fallback or autonomous offline scheduler is added.
See decision 0058 and the Guide's Remote Access page.

### What changed from the plan, and why
- PLAN.md said machines would push "signed with their machine key". Instead the primary decides when to push and
  mints a one-push token per project, and the broker pushes with it. A machine key would let any session on a node
  that can read acc-node's key push anywhere that machine is allowed, for as long as the key lives; a token names one
  project and lives for one push. The node link already authenticates the machine, so the token only has to say "this
  push, this project".
- A push that the backup refuses is not just reported: the refused branch is kept under `backup/<time>/` (the date
  plus time of day, so two rewrites on one day don't collide).
- Local workspaces (the built-in `local` project) aren't backed up: they are one project whose folder isn't a
  repository. Promote a workspace to a project to back it up.
- `git_refs` is its own broker op (rather than a flag on `git_backup`) so "nothing new" costs no token and no push.

### Known limits
- **A repository's own config still applies to the push, but the backup authorization header is scoped to the intended URL.** The broker runs git as the project's account, inside the
  project's checkout; its `.git/config` (which agents can edit) could, for example, set `url.<x>.insteadOf` and send
  the push to another http(s) address; that destination does not receive the backup token. The scoped node
  forwarder additionally fixes the upstream origin and protocol paths for a roaming job. Hooks, credential helpers, other
  protocols and global config are switched off.
- **What a node sends is the node's word**, as for its hook events: a compromised node can push junk branches into its
  own projects' backups (never rewrite or delete what is there, never touch another project's).
- Pushes go through Caddy like any request, so the address must resolve on every machine (and on this one).
- Old nodes without `git_refs`/`git_backup` show the broker's "unknown method" on the card until they are updated.

## Not now
Native Windows terminals (ConPTY) remain unavailable; the [native Windows proposal](WINDOWS-NATIVE.md)
is for owner approval, with node-only qualification before a native main. Also out of scope: moving a session between
hosts, nodes talking to each other, a node serving its own dashboard at the same time.


## Live preview streams

Updated nodes advertise `preview-v1` in authenticated hello info. Uppercase binary frame types share the existing connection but use a separate bounded stream-id namespace and lower sender priority. The complete frame, credit, reservation-lease, cancellation and ownership contract is in [PREVIEW.md](PREVIEW.md#projects-on-other-machines-transport-design). Terminals and existing git-backup broker calls retain their channels. The node opens only literal loopback TCP connections to freshly declared project reservations; it exposes no listener. Connection replacement/revocation discards all preview state without replay. The main retains browser authorization, credential stripping and sandboxing.


## Group members on their own machine (first slice)

The opt-in main setting `ACC_GROUPS_REMOTE=1` enables owner-approved person/node-ID/
broker-account bindings plus maintainer/owner project placement approval. Enrollment
and account-name equality alone confer no group authority. Group sessions freeze
their approved placement and use the existing authenticated broker channel for
private checkout, execution and explicit commit-pack exchange. Only the launcher's
own remote-enabled vault keys can be delivered; provider logins stay on the node.

Disconnected editing may continue, but fresh group launches and remote placement
exchange need a connected, authorized link. Membership, placement or account
revocation persists stop/key-cleanup intent; reconnect checks current authority
before adoption or restart and retries cleanup. Revoked node credentials cannot
reconnect; cleanup stays pending for operator action. No offline execution lease,
automatic commit publication or cross-person credential delivery is introduced.
Full contract and qualification limits: [Groups phase 4](GROUPS.md#phase-4-first-slice-own-enrolled-machine-2026-10-25-design).
