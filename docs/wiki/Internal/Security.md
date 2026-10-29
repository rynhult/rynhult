# Security

## Access
- ACC listens only on a Unix socket that Caddy alone can reach (`/run/accd.sock`, mode 0660, group `caddy`), so no local process can call it directly or claim another client's address. Caddy serves it over HTTPS (with HSTS) and admits **private networks only** (LAN, VPN, Tailscale).
- Sign-in uses a user-verified discoverable passkey, or (when policy allows) a password (argon2id) **and** a TOTP code; replayed codes are refused. Five failures lock out an address (an IPv6 client's /64), and ten lock out an account, for ten minutes; attempts count before the password is checked, and the check runs outside any database write. A locked account still accepts a fully correct password and code (from an address that isn't locked), so failing on purpose can't lock its owner out; wrong attempts while locked answer 429.
- Sessions use a server-side cookie (HttpOnly, SameSite=Strict, 14 days, `__Host-` prefix so sibling sites can't plant or overwrite it). Every change also needs a CSRF header and the expected `Origin`; Caddy keeps that header out of its access log. The terminal websocket checks the cookie and origin before it opens, and closes on logout or when the login expires.
- Responses carry a strict Content-Security-Policy, so no inline scripts and no framing.

## Separation of duties

The proposed trusted launcher contract and owner-run qualification window are
in `docs/CONTAINER-LAUNCHER.md` (repository). It keeps broker no-new-privileges,
requires protected launch authorization, and stages AppArmor changes with rollback.
This is an approval note; no runtime or machine configuration changes are shipped.

**Proposed, not implemented:** `docs/CONTAINERS.md` designs a rootless container
per agent session, including isolation between sessions of the same person.
It recommends Podman/crun, private homes and Git metadata, immutable guard/hook
mounts, session-bound spools and reserved-port relays, with rootless Docker as
a fallback and a Linux VM on macOS. Higher-risk workloads should use qualified
gVisor support. Owner approval and runtime/provider qualification are still
required; the account and guard limitations below describe today's system.

| Identity | Can | Cannot |
|---|---|---|
| `acc` (accd) | Read and write ACC's database and secrets; talk to the broker; approved Publishing DNS through its bound credential and ownership ledger | Run shells; read agents' files; touch Caddy or backups directly |
| agent account, one per person (broker, sessions) | Run the CLIs and tmux in its allowed folders | Read ACC's database or secrets, reach root (no sudo, adm, lxd or docker), connect to or replace its broker's socket, read other accounts' sessions |
| projects' owner (services broker) | Bring the projects' Docker stacks up, only with compose files an owner approved (see *Project stacks*) | Anything else through ACC |
| root helpers | Publish (fixed templates), snapshot the host, back up | Take arbitrary commands: they accept structured request files only |

**Root helpers** each run in a systemd sandbox (read-only system, only their own output folders writable, a small capability set):
- The publishing helper limits what any request can do: names only in the configured zones, never ACC's own host or a zone apex (or `www`), ports 8000–8199, fixed templates, and an approver label stripped to safe characters. The owner's approval is checked in accd; the helper can't verify it, since anything `acc` can write it could also forge.
- The backup reads ACC's data as root but never writes, chowns or follows links in folders other users control: it copies secrets without following symlinks and mirrors other users' backups as those users.
- Root's own sessions keep their hook spool under `/srv/acc/run-root` (root-owned), not in the agent user's `/srv/acc/run`. The root session script parses its settings (never sources them), checks the conversation id strictly, keeps it in a root-only state file, needs a root-owned working directory (Claude loads project settings from it) and only links the repository catalog's skills.

### Root Python import isolation

Root-run Python programs in `deploy/` and `bin/` use `/usr/bin/python3 -I`
in their shebang or explicit invocation. This includes the accounts, backup,
host-state, publishing, packages, update and container helpers; DNS services;
root-session and account-setup inline scripts; installer utilities and probes;
container-image build utilities; and both device-CA tools. Unit names and install
paths are unchanged. Linux node enrollment and telemetry dependency installation
retain their root-owned virtualenv interpreters, also with `-I`, because they
need the installed application or dependency environment.

Python's isolated mode ignores `PYTHON*` settings (including `PYTHONPATH` and
`PYTHONHOME`), disables user site-packages, and excludes the script directory
and current directory from the import search path. It does not disable the
interpreter's system or virtualenv site-packages: those remain trusted,
root-managed code, not an extension point for agents.

Standalone helpers load `acc_env.py` by an absolute path, never by an ambient
module import. Root requires a regular root-owned reader with no group/other
write permission and protected root-owned ancestors. Root-owned sticky
directories such as `/tmp` cannot let another account replace root's entries.
An unsafe installed reader is never skipped for a source-tree fallback; that
fallback must pass the same checks. The wheel shim also checks its canonical
reader. Refusals exit 2, so `acc-guard` continues to block rather than fail open.
Unprivileged development retains the existing same-owner/same-group rule.
The Linux node installer stages its account reader and settings module in a
private temporary directory, removes them after reading, and never imports from
the operator's writable checkout. Main setup removes group/other write bits
from its root-owned application copy before using it.

The updater and image-preparation utility explicitly load their absolute
application package rather than trusting `PYTHONPATH`. Root checks the package,
its descendants and ancestors for ownership, writable modes and symlinks before
importing it. Generated image CLI wrappers check and load their fixed policy
module by absolute path, without adding its directory to `sys.path`.
Device-CA recovery similarly checks the fixed sibling signer and
its ancestors, then compiles its source without accepting a bytecode cache.
The CA tools remain uninstalled; isolated execution does not provision them or
install their cryptography dependency.

`tests/test_root_python_isolation.py` discovers Python programs under both
directories, checks root unit and Linux shell/image interpreter calls, and
exercises poisoned import locations and protected package loading.
`tests/test_rename_compat_security.py` covers unsafe readers and replaceable
reader directories. Run these with the helper, installer and security suites
before changing a privileged entry point.

### Tools on approval (M16)

Tool requests are untrusted proposals, not authorization. The server derives the
person/account/machine from the managed session and rechecks current permissions
at decision and execution. Members approve only their own current account.
Only owners request or approve system changes. Audit records request, approval,
rejection and result.

Registry coordinates are validated at each boundary. The root helper accepts
only apt/snap/flatpak with exact fields and fixed argv; it cannot verify the
dashboard's human approval independently. Account tools run through the target
broker, never root or the services role. Node results correlate to the same live
link and pending request; disconnects and restarts do not replay changes.

Approved registry code can still run with the installation account's privileges,
including root for system packages. The parsed-command guard remains a safety
net, not a sandbox: scripts, symlinks and arbitrary interpreters can evade text
checks. Exemptions affect only the tool-installation rule in new sessions and
grant no privilege. Existing executable use and workspace dependencies need no
tool approval.

ACC never holds a terminal into a root session: observe-only sessions open in Claude Remote Control instead.

### Project stacks (D4)
Agents can edit their project's compose files, and the services broker runs `docker compose up` under the services account's rootless Docker, which `install.sh` makes the human owner. A container's root is that uid, so a bind mount of the owner's home in `compose.yaml` would let an agent act as the owner. ACC therefore pins what it runs (`acc.server.compose`, column `project.compose_pin`):
- The services broker's `compose_digest` hashes every compose and override file and `.env` that compose reads on its own, plus the fully resolved `docker compose config` (includes, `extends`, `env_file`s, the environment). Only the compose files' text is kept, to show changes; `.env` and the resolved config are only hashed.
- Turning autostart on needs the digest the owner reviewed (`POST /api/projects/{id}/runtime` with `digest`), and records it with who and when (audit `project.autostart`). Autostart is off for new projects.
- At accd start, `compose_up` gets the approved digest and the broker refuses if the stack's digest differs just before it runs. A held stack gets an Inbox item (`compose:<id>`, owners only) and an **Approve compose files** button (on the project's card and its page) that shows the diff; approving brings it up.

**Residual risk.** This checks what compose reads, not what the containers do: an approved image or build that mounts nothing still runs code as the owner's uid inside rootless Docker, and a Dockerfile in the build context is not covered (it can't add host mounts, but it is what an approved `build:` builds). An owner who approves a change without reading it gives the agent what the change asks for. The stronger fix is a dedicated services account with nothing of value in its home (`ACC_SERVICES_ACCOUNT`); on a standard install it is the owner.

## Secrets
- Tokens live in `/srv/acc/data/secrets` (readable only by `acc`) and are never passed to agents or written to launch files.
- The audit log never records secret values. A check scans system logs for secret values.
- MCP environment values added in the catalog are vault keys (below); sessions that use that server can read them, by design. The UI says so.

### Key vault (docs/VAULT.md)
- **Storage.** Each value is encrypted with AES-256-GCM (`cryptography`), a random 96-bit nonce per value, and `acc-vault\0<key id>\0<variable>` as associated data, so a value copied to another row or variable fails to decrypt. The 32-byte data key is `/srv/acc/data/secrets/vault.key` (0600, user `acc`): `deploy/install.sh` makes it (`deploy/lib/vault-key`), and accd makes one (with a log line) only while the vault is empty; both write a temporary file and link it into place, so a crash never leaves an empty key. If it's missing while keys exist, accd refuses rather than replacing it, and so does the installer. `acc-backup` copies it with the other secrets, not with the database alone. So a day's backup holds the database and the key that opens it side by side: reading the backup drive is reading every secret, unless `ACC_BACKUP_PASSPHRASE_FILE` is set, which encrypts `secrets/` (`secrets.tar.enc`) with a passphrase kept off the backup drive (D24).
- **What it protects.** The database on its own (a copy, a backup without `secrets/`, an export) reveals no vault values. Other secrets in it, such as model endpoint keys and sign-in (TOTP) secrets, are stored in plain text for now. Root on the main machine can read everything, as with any self-hosted secret store; accd keeps decrypted values in memory to redact them.
- **Who.** A key's owner changes and deletes it; shared keys can be seen and given to projects by everyone, private ones by their owner only. ACC's views keep members' private keys from owners too (names and variables stay out of the audit log, which records a private key by id, and out of launch errors), and owners can delete them (audited with `as_owner`). Owners are fully trusted, though: they can attach read-write to members' terminals, where a session's environment can be printed, and removing a member can transfer their keys to the remover. Private means private from other members. Hidden keys answer 404, others' shared keys 403. No route returns a value, to anyone: a test walks every GET route as an owner and a member after adding canary keys, and checks the database dump and logs too.
- **Delivery.** At each launch and resume, the keys given to the project that the session's owner (the person who launched it) may use go into the run folder's `session.env` (0600, written by the broker as the agent account, sourced by `sh` before `exec`), never onto a command line, into the launch spec, the session row, events, logs or the audit log (`vault.use` lists names). Two keys with one variable refuse the launch. Each provider's instructions (where profile instructions go) get a paragraph naming variables and keys, never values, added per run and not stored in the spec.
- **Agent software on other machines.** The main ACC sends a node only `{"t": "packages", "id", "action", "package"}`. acc-node accepts exactly those keys, a `pkg-<12 hex>` id, install/update/remove/check and a name from the acc-packages catalog (read from the helper itself), at most 10 a minute and 8 waiting; it never runs anything the frame names. On Linux it only writes the helper's request file (its one writable path outside its state dir); on a Mac it runs Homebrew's npm with fixed arguments for catalog entries marked `mac_ok`. What a node reports back is cut to the dashboard's fields and catalog names.
- **Only while it runs (D21).** Stopping, exiting, recovery-needed and archiving remove `session.env` and the secret-filled MCP files (broker `clear_env` with `Provider.secret_files`); resume writes them again. The removal is queued with the state change (observation `env_cleanup`) and retried until the machine is reachable. `acc-backup` never copies run folders, and its mirrors skip `session.env`. One person's projects share an agent account, so project scoping keeps a key out of other projects' environments but is not a boundary between that person's running sessions.
- **Host details from other machines.** The main ACC sends a node only `{"t": "host", "id"}`, and only to a node that advertised `host-v1`. The node answers with a read-only snapshot from unprivileged readers (fixed commands, 5-second timeouts, at most one every 10 seconds); the main keeps only known, range-checked fields with capped lists and strings, and shows them to owners only (`/api/host?machine=`, `/api/board`). SMART, containers and agent account limits are never taken from a node.
- **Other machines.** Only keys with **other machines** on travel, inside the launch request over the node's signed link, into that machine's `session.env`. A machine can read the keys of the sessions it runs.
- **Redaction.** Before ACC stores or shows what an agent produced (hook payloads and the events built from them, "doing now", screen prompts, Changes diffs and summaries, history search, session exports, messages typed to a session), each value of 8+ characters of the session's keys becomes `«key: NAME»`, also percent-encoded, in base64 (inside HTTP Basic credentials too) and cut short to a 12+ character prefix. The mirror of a remote session's spool in `data/remote-spool/` is redacted as it is written. Values are redacted before payloads are truncated. The live terminal is not redacted: it shows what the agent's programs print as they print it. Values a key had before it was replaced, or before it was deleted, stay redacted for the sessions that had the key (`vault_retired`, still sealed, kept while a session lists the key).
- **Guardrail.** Reading `session.env` is part of the secrets rule; using a variable in a command isn't blocked.
- **MCP secrets.** Catalog MCP env values are `vault:<id>` references to shared keys owned by whoever added the server; at launch they resolve only for a person who may use the key (and on another machine only when allowed), otherwise the launch is refused with the reason. Values from before the vault moved in once at start.

### Git backup (docs/MULTIHOST.md, "Git backup")
- **Pushes only, one project per token.** `/api/git/<id>.git/` runs `git http-backend` for `receive-pack` alone; fetching, the dumb protocol and every other file answer 403/404. Each push needs a token that names that project, minted by accd for one push and revoked when the broker answers (10 minutes at most). Tokens are kept only in accd's memory, as SHA-256 hashes; a dashboard login or the API token never works there.
- **Nothing can be rewritten or deleted.** The bare repositories (`/srv/acc/git`, acc 0700) refuse non-fast-forward updates and deletions and check objects (`receive.fsckObjects`). An `update` hook that accd writes and keeps (rewritten before every push and in every repository when accd starts) applies the same rule to every ref, whatever the pusher sends: no deletions, no replacing an existing tag, branches only fast-forward, nothing outside `refs/heads/` and `refs/tags/`. A rewritten branch or moved tag is pushed again under `backup/<time>/` instead. Each repository records its project's slug and creation time, not only its id, so after restoring an older database a reused id never serves or takes pushes into another project's backup (that repository is moved to `/srv/acc/git/orphaned/`).
- **The token never reaches argv or a file.** The broker passes it to git as configuration in the environment (`GIT_CONFIG_COUNT/KEY/VALUE`, `http.extraHeader`), with hooks, credential helpers, global and system config switched off and only http(s) allowed. Logs record refs and timings, never tokens; Caddy's access log redacts `Authorization` headers (its default).
- **Bounded.** `git http-backend` runs as acc with a clean environment, four at a time, 30 minutes and 2 GiB per push (accd counts the bytes; Caddy's 4 MB limit skips `/api/git/`).
- **Limits.** The project's own `.git/config` still applies to its push (an agent could redirect it to another http(s) address, with a token that can only push to that project's backup for minutes). A compromised machine can add branches to its own projects' backups, never change or remove what's there.
- **Downloads.** A bundle goes to owners and the project's own member (everyone else gets 404) and is audited.

## Notifications
- Web Push messages are encrypted per device (RFC 8291) and signed with ACC's own key (VAPID, `data/secrets/vapid_private.pem`). Push services relay them unread.
- Subscriptions may only point at the browser vendors' push services, so a subscription can't make ACC send requests anywhere else.

## People
- Owners see and manage everything; members only what they own (projects, sessions, workspaces, inbox items, publish requests). Every route checks ownership; someone else's id answers 404, so ids don't reveal what exists. A test walks every route with a resource in its path and fails if one isn't covered.
- Ownership compares usernames, so a username is one person for good. Removing someone retires their name (`retired_user`; no one can sign up as it, in any case) and disposes of what they owned: given to the owner who removed them, or archived (projects and sessions archived, private keys deleted, shared keys and ports given to the remover). Their push devices and open invite links go too. What the API token creates belongs to `@api`, which no username can be; the names ACC writes itself (`api`, `supervisor`, `acc`, `system`, `root`) are reserved in any case, and a new name may not differ from an existing one only in case.
- Each account's **Local workspaces** project is ACC's own (`builtin`), found by account and host rather than by its slug, and used only when its owner, account, machine and folder are the expected ones, so a project registered first under `local` can't receive someone else's workspace sessions. Migration 0025 renamed any such project out of the way (audited as `project.renamed`).
- Members don't learn other people's things from errors: an unknown and an offline machine both answer 404 (owners get 409 for offline), a machine's accounts aren't listed, and another person's pending publish request isn't named. Known, accepted: slugs are global, so a taken slug answers 409 to anyone. (A demoted owner's already-open terminals are a separate fix.)
- Owner-only: people, the audit log, host-wide readings (plan usage, observations; on `/api/host` the systemd units, all containers, other agent accounts and git backup totals, which members' view leaves out), catalog changes, publishing approvals, Docker stacks, and anything that uses the dashboard's GitHub token. A member's project may name any folder, so its container status (asked as the services account) is read only once an owner has turned on Autostart for it, and broker errors reach members only as a generic note.
- Each person's sessions run as their own agent account; members can only see and log in their own. A member is never given the owners' default account, the services account or an account someone else has (checked when the invite is made and again when it's used), and an owner sharing one can't be demoted (`users.account_refusal`). Owners may share the default account. New accounts come from the `acc-accounts` root helper (a path unit, like publishing): it accepts only a name ending in `-agent`, refuses existing users that aren't ACC agent accounts, and runs `acc-agent-add` with a fixed root and no admin groups.
- Inbox items and notifications are personal: owners get their own and host-wide ones (approvals, the host, ACC), not members'.
- Admins with sudo on the host are trusted; ACC separates people's agents and views, it isn't a boundary between admins.

## Untrusted input
- Session folders belong to the agent user, so accd reads them defensively: no symlinks, FIFOs or devices, at most 1 MiB of events per pass, every line shape-checked, and each session ingested in its own transaction. A bad line becomes a `spool_parse_error` event; it never stops other sessions.
- Records in a session's `events.jsonl` aren't tied to the process that wrote them. Any process in the agent account can append to its own or a sibling session's spool, so it could fake guard blocks, Inbox items or publish requests for another of the same person's sessions. The guard refuses tool calls that write to the session's run folder, but a program the agent runs can still do it. A per-session token wouldn't help, because sibling sessions run as the same account and can read each other's environment. This stays within one person's sessions; an owner still approves every publish request.
- Wiki and knowledge reads open each folder from a descriptor of the workspace and never follow a symlink at any level (even one that stays inside the workspace), refuse FIFOs and other non-regular files without blocking, and read at most the size limit plus one byte from the descriptor they checked, so swapping a file or folder between check and read gains nothing.
- Conversation files read for history search are opened the same way (regular files only, no link at the leaf, non-blocking). Parsing stops once 5,000 messages are kept, reads at most 64 MiB per file and skips lines (or Cursor blobs) over 4 MiB; Cursor stores are read row by row.
- Model discovery reads at most 2 MiB of an endpoint's model list (compressed bodies refused) within 20 s, checks its shape, looks at no more than 500 rows and bounds labels; anything else is recorded as the endpoint's error and its previous models stay.
- Launch values that end up on a CLI's command line (model, effort, modes, profile, opening prompt) are pattern-checked; a prompt can't start with `-`.
- **Pull requests (docs/PR-FINISHING.md).** CI logs, check output and review comments are written by others, so ACC never acts on them by itself: a person previews the message, and the agent is told it is untrusted input to evaluate. The server builds the text: characters the preview can't show (Unicode format characters such as zero-width, bidi and tag characters, variation selectors, private-use code points) removed before redaction, so nothing reaches the agent that the person couldn't read and no secret is split past redaction; control and ANSI codes stripped, each log cut to its last 25 lines of at most 220 characters, everything within the 4,000-character terminal limit, and the session's vault keys plus common secret shapes (GitHub, OpenAI-style, AWS, Slack and GitLab tokens, private keys, `Bearer …`, `password=`/`token=` values, URL credentials) redacted. A `<untrusted-…>` marker inside the content is defused so it can't close the frame. The preview is stored server-side for 10 minutes, bound to the person and session, and sent once, so what is typed is exactly what was shown. Handoff text used to prefill a pull request description is redacted the same way before it is shown.
- Request bodies over 4 MB are refused by Caddy and accd. Pages that fail to render show an error in place of the page, never a blank app.

## Agents
- Permission mode, sandbox and approval policy come from the profile. Modes that let an agent act without asking need an explicit per-launch confirmation, which is audited.
- ACC adds only its own hooks, and catalog imports flag any hooks they find. Hooks from elsewhere can still load, and an agent can try to switch ACC's off; what ACC does about that differs by CLI:
  - **Claude Code:** ACC's hooks come from the session's `--settings` file, which also sets `disableAllHooks: false` (command-line settings outrank user, project and local ones). The repository's own `.claude/settings*.json` hooks still load, because ACC accepts the folder's trust prompt. The guard refuses edits that touch hooks in any `.claude/settings*.json`. (The precedence is from Claude Code's documentation and has not been tested against a live session.)
  - **Codex:** bootstrap keeps other people's entries in `~/.codex/hooks.json` and counts ACC's as present only when its exact group is there (a copy with a matcher doesn't count). New entries need a one-time trust in the TUI.
  - **Copilot CLI, Cursor and Antigravity:** their hook files are in the agent's own home folder. The guard refuses tool calls that change them, but a program the agent runs can still change them.
  - **OpenCode:** the guard is ACC's plugin in the session's own config folder (`OPENCODE_CONFIG_DIR=<run>/opencode`, in the run folder), not a hook file. A repository's `opencode.json` can switch plugins off (`"plugins": ["-acc"]`, or an `integration.use` deny policy); both were tried live against 2.1.3. ACC counters both: `OPENCODE_CONFIG_CONTENT`, in the CLI's environment, lists the plugin again by its absolute path and outranks project files, and the session's global config allows the plugin by policy, which outranks project policies. It must be a path: OpenCode treats a plugin name that matches no loaded plugin as an npm package, and fetches and runs it. The guard also refuses writes to `.opencode/plugins/` (and the `plugin/`, `tools/` and `tool/` folders beside it) and changes to plugins or policies in a project's `opencode.json(c)`, because a plugin or custom tool is code that runs inside OpenCode's server with no tool call in between. If the plugin doesn't load, shell commands fail closed: the session's `shell` is ACC's wrapper (`<run>/opencode/bin/bash`), which runs a command only when the plugin marked it (`ACC_OPENCODE_GUARDED`, set in OpenCode's `shell create.before` hook). There is also no `acc.ready` event, so the session shows **hasn't started**. File tools would run unchecked in that case, which is why the version is pinned. Each session runs its own private server (`--standalone`), never the account's shared background service. Conversations and saved provider accounts are in one SQLite database for the account (`~/.local/share/opencode/opencode.db`), which the guard treats as a secret file.
  - Nothing yet warns when a tool runs without its pre-tool hook (for example, a PostToolUse with no PreToolUse before it).
- **Merging pull requests** from ACC is a person's action: the session's Pull request tab, behind sign-in, CSRF and an explicit confirmation, refused while the pull request is a draft, a required check hasn't passed, GitHub reports conflicts or a blocked/behind state, the method isn't allowed by the repository and its rules, or the head moved since the person looked; GitHub's own branch protection decides last and ACC never asks for a bypass. Owners act with the dashboard token; members with their own agent account's `gh` login through the broker's `github_api` op (GET/POST/PATCH/PUT on `repos/<owner>/<repo>/…` and `graphql` only, output capped), so a member's project never reaches the owners' token. Agents' own merges are refused by the `merge_pr` guardrail (on by default): `gh pr merge` in any spelling or alias, and `gh api` merge calls (REST PUT …/pulls/N/merge, GraphQL mergePullRequest / enablePullRequestAutoMerge). A program calling GitHub's API itself isn't seen, so a required approving review in branch protection remains the control that holds.
- Agents can't publish sites or create repositories. They **ask**, and a person approves. A session's publish request is bound to its own project, for every role: only that project's reserved ports, only its own sites to unpublish (D18). A request for a name that is already live records the site it would replace (`publish_request.context`), is raised as critical, and approving it needs `confirm_replace` (the UI shows both targets first); an owner publishing a live name from the dashboard must pass `replace`.
- **Guardrails** (`bin/acc-guard`, each CLI's pre-tool hook) refuse force pushes, pushes to main/master, recursive deletes outside the workspace and access to secrets (`.env`, `session.env`, private keys, the CLIs' own token files), plus owner-defined patterns, in every permission mode. They are a safety net against mistakes and casual prompt injection, not a sandbox: an agent in its own account can still do whatever that account may (write a script and run it, use another interpreter, edit a CLI's hook file with a program). The controls that hold are GitHub branch protection, Linux account separation and backups. Coverage differs by CLI: Copilot CLI hasn't been tested against a real account, Cursor's hooks may not fire on Linux, Codex requires hook trust (ACC records it only for its exact definitions on reviewed versions), and OpenCode's comes from ACC's plugin (it refuses a call the guard can't check; see above). A check that runs past its 4 s deadline is refused, and so is one the checker fails on (`guard_error`, recorded in the session's events; until security review #2 such a call was allowed, and a crafted team command could trigger it). The rules are frozen into each session's launch files (`guardrails.json`, written read-only), and the broker puts their SHA-256 hashes, with those of the CLI's hook settings, into the CLI's own environment (`ACC_GUARD_FILES`), which a tool call can't change. Inside a session, a policy that is missing, unreadable or changed since launch blocks every tool call until the session is restarted, and the guard refuses writes, deletes and moves aimed at the session's run folder or a CLI's hook settings. The agent's account owns its run folder, so read-only alone would be only a speed bump.

- **Guardrails** (`bin/acc-guard`, each CLI's pre-tool hook) refuse force pushes, pushes to main/master, merging pull requests, recursive deletes outside the workspace and access to secrets (`.env`, `session.env`, private keys, the CLIs' own token files), plus owner-defined patterns, in every permission mode. They are a safety net against mistakes and casual prompt injection, not a sandbox: an agent in its own account can still do whatever that account may (write a script and run it, use another interpreter, edit a CLI's hook file with a program). The controls that hold are GitHub branch protection, Linux account separation and backups. Coverage differs by CLI: Copilot CLI hasn't been tested against a real account, Cursor's hooks may not fire on Linux, and Codex requires hook trust (ACC records it only for its exact definitions on reviewed versions). A check that runs past its 4 s deadline is refused. The rules are frozen into each session's launch files (`guardrails.json`, written read-only), and the broker puts their SHA-256 hashes, with those of the CLI's hook settings, into the CLI's own environment (`ACC_GUARD_FILES`), which a tool call can't change. Inside a session, a policy that is missing, unreadable or changed since launch blocks every tool call until the session is restarted, and the guard refuses writes, deletes and moves aimed at the session's run folder or a CLI's hook settings. The agent's account owns its run folder, so read-only alone would be only a speed bump.
- Screen-prompt detection (Cursor, Antigravity) runs in the broker; only the matched command text reaches accd, never the screen.

- **Guardrails** (`bin/acc-guard`, each CLI's pre-tool hook) refuse force pushes, pushes to main/master, recursive deletes outside the workspace and access to secrets (`.env`, `session.env`, private keys, the CLIs' own token files), plus owner-defined patterns, in every permission mode. They are a safety net against mistakes and casual prompt injection, not a sandbox: an agent in its own account can still do whatever that account may (write a script and run it, use another interpreter, edit a CLI's hook file with a program). The controls that hold are GitHub branch protection, Linux account separation and backups. Coverage differs by CLI: Copilot CLI hasn't been tested against a real account, Cursor's hooks may not fire on Linux, Codex requires hook trust (ACC records it only for its exact definitions on reviewed versions), and OpenCode's comes from ACC's plugin (it refuses a call the guard can't check; see above). A check that runs past its 4 s deadline is refused. The rules are frozen into each session's launch files (`guardrails.json`, written read-only), and the broker puts their SHA-256 hashes, with those of the CLI's hook settings, into the CLI's own environment (`ACC_GUARD_FILES`), which a tool call can't change. Inside a session, a policy that is missing, unreadable or changed since launch blocks every tool call until the session is restarted, and the guard refuses writes, deletes and moves aimed at the session's run folder or a CLI's hook settings. The agent's account owns its run folder, so read-only alone would be only a speed bump.
- Screen-prompt detection (Cursor, Antigravity; OpenCode as a fallback) runs in the broker; only the matched command text reaches accd, never the screen.
- History search indexes only prompts and replies (no tool output) and is scoped like sessions: members find only their own conversations. Ownership and project/agent/start-date filters apply before ranking and limits, so other people's matches can't crowd a member's out. The index-status count uses that same filtered ownership scope and actual indexed messages, never another person's conversations or an `indexed_at` timestamp alone.

## Supply chain
- Source release signing is owner-run and offline: only `SHA256SUMS` receives a detached OpenPGP signature. The public key ships in `keys/rynhult-release-key.asc` and the site build publishes it at `https://rynhult.dev/keys/rynhult-release-key.asc`. Compare the full primary and signing-subkey fingerprints before importing, verify `SHA256SUMS.asc`, then check every listed hash as described in [Install](/User-Guide/Install#verify-a-source-release). CI holds no private release key; rc.1 signature attachment is a manual publication step. The in-app updater does **not** verify signatures yet, and neither a checksum nor a valid signature attests freshness or software safety.
- The offline signing helper verifies in a short, private public-key directory under `/tmp`, avoiding GnuPG socket-path limits when the release folder is deeply nested. It never relocates the owner's private keyring. Signature staging remains beside the manifest for no-overwrite, same-filesystem publication; both temporary directories are removed on exit. GnuPG failure diagnostics are limited to 4096 bytes and no secret key is exported.
- Catalog items are pinned to a commit, vendored as text only, and reviewed before they are added. ACC flags scripts, installs, network calls, telemetry and instruction-override wording; red flags (hooks, `allowed-tools`, `mcpServers`, `permissionMode`, `!`-shell on load) need an explicit confirmation. Frontmatter is parsed as YAML (`catalog_import.parse_frontmatter`), so quoted keys and flow mappings count; anchors, aliases, merge keys, duplicate keys, non-mappings and malformed delimiters are refused and flagged. At launch every skill and agent Claude, Copilot, Cursor, Antigravity and OpenCode get has its frontmatter rebuilt from an allowlist (`ALLOWED_FRONTMATTER`: name, description, license, version, metadata, argument-hint, user-invocable, disable-model-invocation, model, tools, disallowedTools, color); frontmatter that can't be read safely becomes an empty block (Antigravity's subagents keep only name and description: it refuses Claude's `tools`/`model`; OpenCode's keep only description and `mode: subagent`). Codex reads catalog skills unmodified through links in `~/.codex/skills/acc-*` (it ignores those keys and doesn't run `!`-shell); they are not in `~/.agents/skills`, which Copilot also reads and can't hide per session. Cursor does read `~/.codex/skills` (as third-party skills) and has no per-session switch, so a Cursor session sees every Codex-capable catalog skill.
- MCP servers run from one install made from a committed lockfile (`deploy/mcp`, root-owned `/srv/acc/tools/mcp`, `npm ci --ignore-scripts`), never fetched with `npx` at session start.
- MCP environment values never appear in the API, the stored launch spec or any command line: the catalog holds `vault:<id>` references and the spec placeholders, Claude and Copilot get the values in the run folder's `mcp.json`, Cursor and Antigravity in their session plugin's MCP file in the run folder, OpenCode in `opencode-mcp.json` in the run folder (removed when the session stops), Codex through a 0600 environment file and `env_vars`.
- The GitHub workflow uses actions pinned to commits (Dependabot updates them) and only runs on owner-authored issues when the owner writes `@claude`. Pull requests and review comments cannot invoke it; checkout uses the default branch without persisted credentials. The job allows Read, Grep and Glob tools. Owner issues must still avoid quoting untrusted instructions.
- Python and web dependencies have lockfiles. Dependabot currently updates GitHub Actions only. `scripts/security-check.sh` is a manual check for gitleaks, osv-scanner and dependency audits; no workflow runs it. Agent CLIs can update from upstream (including npm `@latest`); the Cursor and Caddy Cloudflare downloads lack independent checksum verification. OpenCode's platform binary is checked against the npm registry's SHA-512 `integrity` (the same registry serves both, so this catches a corrupted or swapped download, not a compromised registry) and pinned to one release line. Because that checksum bounds nothing, the root helper caps the download (512 MiB), reads the tarball as a stream capped at 1 GiB unpacked and 10,000 headers, refuses tar metadata headers over 64 KiB, and never runs the binary. GitHub MCP and Chrome for Testing provisioned under `/srv/acc/tools` currently require operator installation; this repository does not reproduce or verify those binaries.
- The Chrome used by browser tools, and Codex's own sandbox, run with their sandboxes on: AppArmor profiles (`deploy/apparmor/`) allow the user namespaces each needs, for those binaries only, and Ubuntu's restriction stays on for everything else. One of them, `acc-system-bwrap`, covers the distribution's `/usr/bin/bwrap`, which Codex prefers when that package is installed. It's a general-purpose tool, so any local account can create user namespaces through it, as on Ubuntu before 24.04, Fedora and Arch. See [Operations: Codex sandbox](Operations.md#codex-sandbox-and-bubblewrap).

## Installing machines
- The pasted Add machine line is data, never a command: `deploy/acc-install` accepts only `rynhult-node enroll` or legacy `acc-node enroll` with an `https://host[:port]` primary, one trust mode, and code, name and fingerprint of their exact shapes; any shell special character, extra word or repeated option is refused. It then runs the node enrollment command with the parsed values.
- With a private CA, the fingerprint in the command is the trust anchor, as in `acc-node enroll`: the one-liner and the bootstrap fetch the CA unverified, keep only its first certificate (an appended second one is never trusted) and stop unless its SHA-256 matches. Everything after that is verified against it, hostname included.
- The bootstrap installs only a gzip tar whose members are regular files and folders under `acc-<version>/` (no links, devices, absolute paths or `..`), unpacked root-owned without group or other write.
- `/api/nodes/bundle` and `/api/nodes/install.sh` are public (they are ACC's own source) and share a per-address limit. The bundle never holds `.git`, virtualenvs, `node_modules`, data, secrets or `.env` files. `/api/nodes/ca` is also public certificate material: enrollment trusts it only after checking the supplied fingerprint. The separate device-certificate downloads need a sign-in; certificate details are for owners.
- The installers are unsigned for now (`docs/INSTALLER.md`).

Provider hook commands quote executable paths, including macOS paths beneath `Library/Application Support/ACC` and paths containing apostrophes. This applies to the guard and Claude status line as well as lifecycle hooks.

WSL agent accounts can retain Windows-user authority through interop and readable
Windows drives. The node installer's isolation choice is explicit and leaves
`--update` unchanged; it needs a Windows-side WSL shutdown/restart. Reported state
comes from effective interop and per-agent mount-permission checks, never Windows
file contents. Owner warning dismissal is audited but changes no access.
This is not per-session isolation or a boundary against trusted root/Windows
administrators. See [WSL limits and undo](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes).


## Installer privilege boundaries
Main setup and updates use a system PATH and the Linux node installer's shared tool check: uv must resolve outside homes, be root-owned, and have protected ancestors. If absent, the pinned SHA-256-verified uv is installed; the owner's home is never a fallback. uv and Caddy selection execute the same resolved path that was validated, so changing a candidate symlink cannot change the selected executable. npm dependency installation and the web build run as the non-login `acc-build` account in disposable staging with a clean environment, with lifecycle installation scripts disabled. Root installs regular build artifacts without following links; npm MCP packages may retain internal relative executable links.

Installation refuses symlinks and unexpected owners throughout `/srv/acc/data`. Data belongs to `acc`, except the root-owned bundle tree. Root installs bundles through directory descriptors opened with `O_NOFOLLOW`; vault key creation runs as `acc` with umask 077 and noclobber. ACL sharing runs the walk as each folder's owner, so a swapped link cannot grant access to root-owned files. The Chrome AppArmor executable directory is created as root 0755.

Both Caddy modes restrict the admin API to `/run/caddy/admin.sock` and install its runtime directory drop-in. The adapted JSON is checked before reload. The private client allow list includes loopback and `172.16.0.0/12`: local processes and Docker containers intentionally count as private clients. They still need an ACC sign-in; private addressing is not a user identity.

NodeSource and Docker apt keys are checked against pinned primary fingerprints before their sources are enabled. The Cloudflare Caddy build requires an independently approved `ACC_CADDY_SHA256` before downloading a missing binary. GitHub MCP v2.0.2 is installed for amd64/arm64 from a release archive checked against committed SHA-256 sums.

## Container launcher

`acc-containers` is the only root code on the container path (decision 0038). It answers one fixed six-field
request on `/run/acc-containers.sock` and acts only when both hold: the caller is the account's broker (peer uid,
and the pid is `acc-broker@<account>.service`'s main process, never one of its sessions), and accd wrote a grant for
that session and generation in `grants/`, which only `acc` can write. Everything that matters (image, mounts,
command, limits) comes from the grant and the root-owned policy, never from the broker. It refuses unpinned images,
any mount besides the session's private checkout and run folder, links anywhere on those paths, folders the account
doesn't own, overlaps with protected folders, worktrees (shared git administration), administrative or root
accounts, and overlapping subordinate id ranges. Podman, crun and the agent run as the agent account in a delegated
system unit with the account's limits. Refusals, launches, stops, cleanups and unknown units are audited into ACC's
audit log. A refused launch is an error, never a native fallback.

The broker marks new private checkouts outside the container mount. Host Git reads use a bounded no-follow copy
of metadata in broker scratch, with hooks and fsmonitor off and a config allowlist. Rewritten administration,
links and shared object directories are refused. Host Git writes are refused: use the session terminal. Legacy
unmarked container checkouts must be recreated before qualification.

Git that the broker runs for a session outside a guarded tool call, in an ordinary checkout or worktree, runs with
the same settings switched off. This covers team merges and imports, creating and removing session worktrees, a
team checkout on another machine, and the commits made when a local workspace is set up or promoted. Promotion's
push keeps the agent's normal config, because the GitHub sign-in comes from it; it only turns hooks and fsmonitor
off. A worktree's `.git/config` is shared with every other session on the project,
and the agent's global config is the agent's own, so either could name a program for git to run, outside the
guard. Those commands therefore get:
- no system or global config, with only the agent's name and email carried over;
- no global attributes file;
- hooks, fsmonitor, signing, signature checks, submodule recursion, automatic maintenance and lazy fetches off;
- `diff.external` empty;
- every filter, diff and merge driver the repository defines neutralised. Filters pass content through, diff
  commands and textconv do nothing, and a custom merge driver fails, so its files conflict and the merge is aborted.

The driver list is read again before each command, because a merge can change a tracked file that the
repository's config includes. With filters off, a merged Git LFS file is written as its pointer text. The team
view and the merge result say "LFS files arrive as pointers; run git lfs checkout" when a committed
`.gitattributes` uses `filter=lfs`. The marker tests are in `tests/test_team_git_hardening.py`.

Metadata copies preserve file timestamps, especially the Git index's modification
time. Git uses that time to recheck potentially unchanged-looking files edited
within its timestamp resolution. Replacing it with the copy time could hide a
same-size edit from Changes and incorrectly allow checkout removal.

Mount sources are pinned by root descriptors and bound by PID 1 into private staging before Podman opens them.
Run-folder writes use held descriptors. Launch, stop and reconcile are serialized per session. Leftovers are
quarantined; a name or label never authorizes removal. Image volumes, healthchecks, file logging and automatic
writable rootfs tmpfs are disabled; network is always `none` and OCI hooks use a root-owned directory. Runtime/storage config and subscription mounts
are suppressed. Staging paths are unique per session and generation; rootless pause namespace reuse must fail
closed if it cannot see them, and remains an owner qualification gate for multiple-session operation. The helper's
own processes share a bounded resource slice. Disk traversal and audit buffers have explicit budgets; refusal
buffers are separate from lifecycle records, and saturation is visible in the journal. Audit ingestion continues
while the launch flag is off; the installed public socket remains exposed to local connections.

Launcher limits cover managed units. Native processes sharing the uid can still use their own runtimes or user
manager. The AppArmor `flags=(unconfined)` user-namespace profiles do not confine that code or container payloads.

Not yet: credentials inside containers, immutable hooks in a pinned image, nested sandboxes, private/LAN egress
denial. Until those pass qualification a container is a resource and filesystem boundary under test, not a
security claim. See `docs/CONTAINERS.md`.

## Live preview boundary

Preview apps run on isolated per-project-and-port origins. An outer ASGI gateway handles the entire preview hostname, including paths resembling ACC APIs; no preview host reaches dashboard routes. ACC's host-only `__Host-` cookies never belong to those origins, and a preview credential cannot authenticate an ACC API. ACC rejects foreign Origin and cross-origin non-navigation fetch metadata and sends `frame-ancestors 'none'`. The iframe and gateway CSP sandbox allow scripts, forms and app-origin storage, without parent scripting or top navigation; service workers are disabled. Preview responses also permit their own origin as a frame ancestor, so a gateway-owned wrapper can inspect its same-origin app child; other projects' preview origins are not permitted ancestors.

A browser login can mint a five-minute signed token for its authorized project and reserved listening port. A fragment bootstrap exchanges it for a host-only Secure HttpOnly `__Host-acc_preview` cookie. Tokens never appear in URL queries. Every request rechecks the login, current user role/project ownership, local machine, archive state and port reservation. WebSockets repeat the checks each second. Logout and access revocation stop new network access, though already-rendered content cannot be recalled.

The upstream is always literal loopback and a project reservation in 8000–8199. The gateway never follows redirects and strips credentials, forwarding headers, app cookies and upstream security headers. Previews intentionally do not support app-managed cookies. Caddy applies ACC's private-network allow list before the gateway. Wildcard DNS and certificates avoid per-project public records or Certificate Transparency entries. See [Configuration](/Internal/Configuration#preview-network-setup).

## Files and editor

Files uses the same owner/member scope as sessions and forwards only database-selected roots, accounts and hosts. The broker rejects absolute paths, traversal, all symlinks and non-regular nodes. The broker opens the configured allowed root component by component with `O_DIRECTORY|O_NOFOLLOW`, walks to the registered folder through descriptors, and holds that descriptor for the complete operation, including rename destinations and git subprocesses. This prevents a swapped workspace ancestor from redirecting reads, writes or commits. Protection checks include the registered root and its ancestors, so choosing a root inside `.git` or a protected credential folder cannot bypass them. Secret policy mirrors the guard (tested for parity), with additional protection for the entire broker run folder and git internals. Protected entries are listed but never opened, downloaded, mutated or moved via a parent folder. Directory renames use an exclusive atomic rename; deletion requires empty folders. Text reads/writes are capped at 1 MiB, downloads at 4 MiB and directory listings/rename validation at 2,000 entries.

accd applies vault redaction, including retained historical values, to listings, reads and conflicts. Redacted text cannot be saved through the editor. Text downloads are redacted; binary downloads containing known values are refused. Responses do not allow executable HTML/SVG previews or shared caching. Markdown preview does not load embedded images.

Mutations record `files.write`, `files.delete` and successful `files.commit` audits with paths, never file contents or commit messages. A commit uses the caller's stored identity, a private index and the current branch, with a compare-and-swap branch update. It never invokes push, repository hooks or clean filters. The directory advisory lock serializes cooperating editors; external agents can still write between the final comparison and atomic replacement.

### Source archive parsing limits

`acc.bundle` hashes the compressed file incrementally and keeps that inode open for validation and extraction. Both passes stream through the same validator: at most 100,000 raw headers (including extension headers), 2 GiB cumulative effective file sizes, 64 KiB per PAX/GNU extension body, 8 MiB cumulative extension bodies and applied PAX fields and 16 nested extensions. Declared metadata sizes are checked before the tar parser reads their bodies. Sparse formats are refused before sparse-map parsing. Decompression uses bounded buffers and a physical byte budget covering file data, metadata, headers, padding and trailing bytes. The entire validation pass must succeed before extraction starts; extraction repeats every check. These bounds apply to main/node updates and latest-release catalog previews.

## Private Development workspaces

Development environments have individual project records and the usual ownership scope; creation uses the caller’s own agent account and broker. Session worktrees inherit a frozen `local_only` guard policy and instructions independently of instance rule toggles. Conversion is owner-only, checks the exact account and path, refuses live sessions and git remotes, and prevents older permissive sessions from resuming. Explicit promotion may add a remote; old session policies remain local-only. Internal backups use the privileged broker protocol, outside agent guard hooks, without adding a remote. See [Guardrails](/User-Guide/Guardrails) for the guard’s safety-net limits.

## Proposed public device gate

`docs/REMOTE-ACCESS.md` is the owner-approved design; current private-network enforcement
above remains the implemented edge behavior. It specifies a separate public Caddy mTLS hostname, a dedicated
device CA and active-leaf policy, LAN pairing, explicit exposure confirmation, and revocation of both
new connections and live streams. Nodes keep their signed-link authentication and server-trust pinning.
The owner rejected Cloudflare termination: use direct access or qualified raw TCP
passthrough preserving browser-to-Caddy TLS. Nothing in
this document enables public access or claims iOS Home Screen certificate compatibility.


### Security review 3 hardening

Issue-source HTTP requests resolve every destination and refuse non-public DNS answers unless an owner has explicitly allowed the exact server and address set in Settings. Connections use the
validated IP with the original Host and TLS server name, without environment proxies or redirects. A public hostname
cannot rebind the connection to a private address. Owner-allowed GitLab/Gitea/Forgejo servers can use ordinary
LAN/ULA addresses, with exact DNS-set comparison on every outbound request. Changed addresses need owner
re-confirmation. Metadata/link-local, loopback, unspecified, multicast and mapped/transition addresses stay refused.
Only owners add/re-confirm/remove entries; members select their ids. Uploaded PEM CA trust stays in a fresh
host/port/path-scoped SSL context. Audit records never include PEM; vault-only key access is rechecked before cache
lookup, and removed entries fail there too. See source decision `0034-owner-pinned-issue-servers.md`.
The adapters request identity encoding, refuse compressed responses before decoding, and cap JSON bodies at 2 MiB with a 20-second total network deadline.
Provider rate limits, token-scoped caching and vault grants still apply.

Preview gateways and nodes relay raw HTTP response bytes, preserving the response Content-Encoding for the browser.
They do not decompress upstream bodies before stream limits. Remote frame credit, reservation leases, cancellation,
origin isolation, token purpose separation and cookie stripping still apply. CI feedback redacts complete log text
before shortening individual lines, preventing truncation from defeating exact vault-value redaction.

The merge guard normalizes API paths and recognizes absolute GraphQL URLs and JSON-escaped mutations. An unresolved
write destination is refused; spell out the endpoint so it can be checked. Aliases under command-local gh
configuration overrides are also refused; use the explicit command instead. This remains an advisory command guard;
repository branch protection is the enforcement boundary for arbitrary programs running in the agent account.

### Infrastructure DNS credentials (phase 1)

`infrastructure_credential` is separate from `vault_key`; project and group grants, MCP references, issue-source
resolvers, context, exports and remote launch delivery cannot resolve its IDs. Browser-owner APIs additionally
require a current password for mutations; bearer tokens cannot manage this namespace. Encryption binds the ID,
provider, immutable purpose, fixed field and revision into AES-GCM associated data. Replaced encrypted values expire
after 24 hours, independent of session history. Missing data keys never trigger creation over infrastructure ciphertext.

The private root helper checks peer UID, fixed schemas, locally allowed zones, root-owned non-writable ancestors,
no-follow descriptor-relative regular files, owner/mode/link count and bounded input/output. DNS and Publishing share
a root-only configuration lock. It validates/adapts with the candidate credential, checks the exact private admin
address and reloads JSON, with a 20-second command budget. A pending crash journal requires recovery; failed reload
restores the previous credential. Successful reload is explicitly not issuance evidence. Error categories and audit
metadata contain no request bodies, expanded config or provider output. Provider validation discards diagnostics
rather than forwarding them. An infrastructure redactor exists for current/retired values but no output path uses it
yet, because no session, transcript or agent output receives these values. Refused reauthentication, throttling and
unavailable or refused helper outcomes are audited with fixed result categories. Apply and import call the helper
outside a database transaction, so a slow helper never holds accd's write lock.

Caddy loads the certificate credential as expanded configuration (`{$CF_API_TOKEN}`), so `persist_config off` keeps
it out of Caddy's autosave file. Caddy and the root helper run with reset capability lists, a `@system-service`
system-call filter and no core dumps; the helper keeps only CAP_SETUID, CAP_SETGID and CAP_KILL, and the purge
service none. Restore validation decrypts infrastructure ciphertext too, so a backup paired with the wrong key is
refused.

Release installation checks independently committed hashes before installing/executing candidate binaries and keeps
a previous binary without a gap in the active path. The committed manifest is pending CI/qualification, so this path
cannot activate a release yet. Root and accd remain trusted; Caddy necessarily reads certificate credentials. This is
not protection from host root/accd compromise. The phase 2 adapters and ownership ledger are implemented; new-provider activation remains gated on live qualification.
### Codex hook trust

Bootstrap records trust only for ACC’s exact minimal command groups on Codex 0.163.0 and 0.164.0, using the configured executable’s version. It preserves unrelated hooks, config comments and explicit disablement. Unknown versions, malformed files and unsupported state layouts retain manual review. Writing trust keeps config.toml's mode and, for a symlinked config, writes where the link points (a dangling link is left alone). A remaining **Hooks need review** dialog (the heading, its count line and both trust choices, not text that merely mentions it) becomes **Needs you**, with an Inbox explanation and no automatic keypress. Anything that can write the agent account's config.toml can record trust itself, so this is convenience, not a boundary. See [Codex hook review](/User-Guide/Guardrails#codex-hook-review).
Review five hardens Publishing detection with the public-only pinned transport, a four-second total deadline,
and a 128-byte uncompressed response limit. It never inherits an issue server's custom CA. Node collection
timestamps are validated independently of receipt times; Host tolerates unrenderable cached dates, and Host
and board text wraps within cards. These reports remain claims by an enrolled node, not trusted measurements.

### Protected session images (CT-05)

The owner provisions a dedicated `rynhult-build` system account with the reviewed
`deploy/containers/setup-build-account` helper, inspecting `--dry-run` first.
It has a locked password, nologin shell, private 0700 home, exclusive subordinate
UID/GID range and private primary group, with no agent-group membership, user
manager, lingering, timers or crontab. Conflicting existing properties refuse;
interrupted provisioning needs owner review. Never run sessions under this account.
`build-image` retains the quiet-account census and uses a private runtime directory
plus cgroupfs for every Podman call, so it does not need a login or systemd user
manager. The owner copies the reviewed archive into a new root-only directory and
verifies the independently recorded checksum/image ID before protected import.
Only fake-tool coverage exists for this provisioning/build path; the owner must
qualify real rootless Podman on the target host before relying on it.


The launcher storage profile lists `/var/lib/acc/session-images` as a read-only
additional image store. Only an owner acting as root imports the reviewed OCI
archive, after verifying the archive checksum, manifest, config and layer hashes.
The root-owned policy pins the expected image ID. Before launch, resolved overlay
image layers must stay under the protected store with root-owned parents and no
group/other write; an account-local image with the same ID fails.
Inspection and execution are separate operations, so this check alone cannot
prevent a same-ID image introduced between them. The owner now publishes an
independent flattened tree at `session-images/rootfs/<config-digest>` with a
protected `.acc-image-id` completion marker. The launcher validates and holds
that directory, binds its descriptor read-only into per-launch systemd staging,
and uses plain `--rootfs` without a writable overlay or image lookup. Missing,
symlinked, writable or mismatched trees are refused. Image ENV is supplied
explicitly; image configuration is not inherited in rootfs mode.
The transient unit supports systemd 255 by starting its protected guard briefly
as root in `/`. The guard creates a new parent user namespace owned by root,
with full identity UID/GID mappings, using a short-lived root mapper. Only the
unit directory and three cgroup delegation controls are assigned to the agent;
resource limits and ancestors remain root-owned. The mapper is reaped before
the guard clears supplementary groups and drops every saved/real/effective UID
and GID. Before Podman runs, it verifies the distinct namespace, full maps,
kernel-reported owner, all IDs and absence of effective/permitted/inheritable/ambient
capabilities. Missing isolation fails
startup even if a sandbox setting was ignored. Graphroot, runroot, temporary
state and `XDG_RUNTIME_DIR` live in fresh namespace-private tmpfs, not the host
account's state. The outside same-UID account cannot use proc-root/fd aliases or
enter this root-owned namespace. Failed setup refuses launch without a weaker retry.
The guard accepts only canonical decimal arguments, and every failure, including an
unexpected error or a signal after the privilege drop, is a structured refusal; the
`container.launch_failed` audit entry carries it as `reason`, read from the unit's
journal. The launcher sets `USER` and `LOGNAME` to the account, since systemd gives a
`User=0` unit `USER=root` and Podman selects subordinate ranges by `$USER`.
Same-UID signals still reach the session after the drop (kill or stop, not inspect
or modify); before the drop the bootstrap and mapper are root. No `no_new_privs` is
set at this level, because Podman's `newuidmap`/`newgidmap` helpers are set-ID; the
container itself runs with `no-new-privileges` and no capabilities.

The owner runbook is `deploy/containers/ROOT-IMAGE-STORE.md` in the repository. It
removes ACLs and agent ownership before publication. Qualification adds a mandatory
`image_store` row from a fresh root-captured report: the selected agent must fail
to open image files for writing both on the host and under `podman unshare`. The
probe checks the running process-root inode against the held tree and requires
read-only rootfs mode; runtime copies into account storage fail qualification.
The proof must attest `execution_source: held-rootfs`; old image-lookup proofs
and payload reports cannot pass this row. Owner provisioning refuses existing
unsafe/symlinked ancestors and uses fresh import/store/runtime directories rather
than changing permissions on an agent-writable path.
The proof must also show denied proc-root/fd/user-namespace/mount-namespace access
to a root-owned test service, both natively and through `podman unshare`.
Installation and root-store runtime qualification still require an owner run.

## Company sign-in (OIDC preview)

Organizations phase 1, first slice; unqualified against a real tenant. Only with `ACC_ORGANIZATIONS=1` and an owner's
explicit switch. `src/acc/server/oidc.py` is the relying party; `src/acc/server/api/organizations.py` the routes.

- **Flow:** authorization code + PKCE (S256), `response_mode=query`, scope `openid profile email`; never implicit. State, nonce and
  the verifier are 256-bit random values. The flow row (`oidc_flow`) is keyed by the state's SHA-256, bound to an
  httpOnly `SameSite=Lax` cookie (also `__Host-` over HTTPS), expires in 10 minutes and is deleted (rowcount checked)
  before the code is exchanged. A callback whose binding doesn't match is refused without consuming the flow.
- **Tokens:** RS256 only, from the JWKS named by pinned https discovery whose `issuer` must equal the configured one.
  `alg` values other than RS256 (none, HS256 with the public key, ES/PS) and `crit` are refused; `jku`/`jwk`/`x5u`
  headers are ignored. RSA keys under 2048 bits are skipped. Exact `iss`; `aud` contains the client and `azp` equals
  it when present or when there are several audiences; `exp`, `iat` and `nbf` within 120 s skew; `iat` no older than
  10 minutes; constant-time nonce; `at_hash` checked when present. Keys refresh after an hour, or after 60 s for an
  unknown `kid` (rotation), never more often; an unknown key after refresh fails closed.
- **Admission:** a 7-day, one-use sign-in link (`sso_invite`) carries the invited directory account; it is redeemed
  only when the verified token names it (`email` with `email_verified: true`, `upn`, or `preferred_username` only
  from an Entra ID issuer, where it is the administrator-set UPN). Names compare with ASCII-only case folding, so
  Unicode look-alikes (e.g. KELVIN SIGN) never match an ASCII invite. A mismatch neither consumes the link nor creates anyone.
  Its owner-only audit records `returned_account` from a matcher-eligible preferred user name, UPN or verified email
  (or null if none), plus `invited_account`. These names are not put in redirects or public failure messages.
  `admitted_by_directory()` is the hook for
  future directory group / OU sync (SCIM phase) and admits nobody today.
- **Invitation withdrawal:** owner-only `DELETE /api/organizations/sso-invites/{id}` uses the normal cookie-session
  CSRF/Origin checks. It marks an unused invitation's `withdrawn_at` and audits `org.invite_withdrawn` with its ID and
  username. Repeating withdrawal is idempotent (audited as `org.invite_withdraw_repeated`); used invitations remain
  history (409). A used or unknown invitation is audited as `org.invite_withdraw_refused`, with `reason` `used` or
  `missing`. Withdrawn invitations leave
  the active list. Pending flows retain their invite reference until consumed or normally expired, allowing the
  callback to report `invite_withdrawn`, not a misleading expired-flow message.
  Starts recheck revocation after discovery; callbacks check before and after provider exchange/verification,
  under the write lock. Other invitations and existing company sign-ins are unaffected. A sign-in that started
  through the withdrawn link is refused even if that person already has a linked identity; their ordinary sign-in
  still works. When the provider also refused that sign-in, the audit keeps the provider's error next to
  `invite_withdrawn` (`provider_reason` and the diagnostics below). Migration 0066 follows 0054; rollback expires
  revoked invitations before dropping the marker, so links do not reactivate.
- **MFA:** while passkeys are Required for members, company MFA counts only if the token proves it (`amr` contains
  `mfa`, `hwk` or `fido`, or `acr` is one of the owner's tenant values in `oidc_provider.mfa_acr`; none by default,
  and `0`/`1` are never accepted; decision 0061). Adding an acr value, or turning `passkey_enrollment` back on,
  needs the owner's `passkeys.recently_verified` (never the API token), refused as `organizations.oidc_change_refused`
  (security review SRF-1). Otherwise a member with a passkey gets a `step_up`
  session that `auth.lookup_session` hides from every route except sign-in state, sign-out and the passkey
  verify ceremony, until `passkey_verified`; a member without a passkey is refused (`mfa_required`), with nothing bound.
- **Passkey enrollment for company-only members** (`user.company_only`: created by pre-provisioning or an invite, no
  known local credentials): an MFA-proven company sign-in whose signed `auth_time` is between now minus 600 seconds
  and now sets `auth_session.passkey_enroll_until` to five minutes ahead, unless the owner cleared
  `oidc_provider.passkey_enrollment`. Authorization requests include `max_age=600` only while enrollment is enabled.
  Missing, malformed, non-finite or future authentication times open no window; token `iat` is not sufficient.
  This does not itself refuse ordinary company sign-in. Only passkey registration accepts the window in place
  of `passkeys.recently_verified`; registering clears it with a conditional update, so a window adds one passkey even
  under concurrent ceremonies. Turning enrollment off clears every open window. Other fresh-verification routes
  ignore the window. The audit records `auth.passkey_registered` with `via: company_mfa`.
  Required policy and role changes exempt company-only members from the recovery-code prerequisite, never from
  the one-passkey minimum. Owners and local-credential members retain their recovery prerequisite. Reset links
  preserve `company_only`. Migration 0054 refuses downgrade before any DDL while company-only accounts have
  passkeys, preventing their keys from becoming standalone credentials on older code; remove those keys or accounts
  before retrying.
- **Identity:** unique `(organization, issuer, subject)` in `external_identity`, referencing `user.person_id`
  (uuid5 of the saved installation namespace `organizations.namespace` and the user row id, as the phase 0 report
  proposes). No email or name matching. Unknown subjects are refused unless pre-provisioned or invited; linking an
  existing member needs their own local sign-in from the last 15 minutes. Owners are never bound and an owner
  role refuses SSO even if promoted later: `auth.lookup_session` also ignores an open company sign-in once its person
  is an owner, so a promotion never inherits owner authority from the provider.
- **Sessions:** a successful callback ends any session the browser presented (fixation) and issues fresh tokens;
  `auth_session.method = 'oidc'`, owner-set lifetime of 1-24 hours (12 by default; lowering it caps open ones). Turning SSO off, changing issuer/client, or unbinding a person
  deletes their OIDC sessions and closes their terminals. Provider (RP-initiated) logout is not implemented.
- **Unbinding a company-only member** (`user.company_only`: made by pre-provisioning or an invite, no password anyone
  knows, passkeys that only confirm a company sign-in) would leave an account nobody can enter, so
  `DELETE /api/organizations/identities/{id}` requires `?resources=transfer|archive` (422 without) and runs People >
  Remove's own path (`ctx.remove_person`), deleting the identity and auditing `organizations.identity_unbound`
  (`person_removed: true`) in the same transaction, which re-checks `company_only` and the role and refuses (409) if
  either changed. A linked member keeps today's unbind-only behaviour. Completing a password reset clears
  `company_only`. An owner is never removed this way, even a company-only member promoted later: owners never sign in
  through the provider, so only the binding is deleted (`removes_person` is false; People > Remove stays the way to
  remove an owner).
- **Secrets:** the client secret is sealed with the vault data key (AES-GCM, associated data bound to issuer and
  client ID), write-only over the API, never delivered to sessions. Responses are capped at 1 MiB, 10 s timeout,
  redirects not followed. Errors carry fixed reason codes and locally generated detail. Token refusals may also
  audit an `oauth_error` ASCII identifier (at most 64 letters, digits, underscores or hyphens, starting with a letter)
  and an `aadsts_code` extracted from the description (`AADSTS` plus 1-10 digits, not part of a longer identifier).
  `error_code` prefers the safe AADSTS code, otherwise the OAuth error. `error_text` retains at most 300 characters
  of the description. It strips Unicode controls, format characters and line/paragraph separators, and turns other
  spaces into plain spaces. Before truncation it redacts every run of 8 or more characters, in any letter case, that
  is part of a request credential (authorization code, PKCE verifier, client secret) or a returned token, also in
  URL-encoded form. A value shorter than 8 characters is never treated as a credential: the callback's `code` is
  whatever the caller sent. Text that still contains such a run is dropped. Invalid/oversized responses supply no provider diagnostics; reflected
  credential codes are omitted. Provider prose stays out of exception messages and public pages. Only owners can
  read the audit, including its visible failure diagnostics. Codes, tokens and secrets aren't audited as they were
  sent. A provider that echoes fewer than 8 consecutive characters of one could still leave that fragment in
  `error_text`.
  accd runs uvicorn with `app.uvicorn_log_config()`, whose handlers drop the query of `/api/auth/oidc/*` and the
  token of `/invite/<token>` from access and WebSocket lines; Caddy already deletes request URIs from its log.
- **Provider addresses:** every request (discovery, keys, token) resolves its host and refuses loopback,
  link-local, metadata, private and other non-global addresses (`issue_sources.allowed_address`), then connects to
  the checked address with SNI and Host of the name (`oidc.PinnedTransport`; no second lookup). Owners may allow a
  LAN host (10/8, 172.16/12, 192.168/16, fc00::/7 only) by confirming its exact DNS answer
  (`organizations.oidc_private_hosts`, audited `organizations.oidc_private_host_allowed`/`_removed`); a different
  answer later refuses (`address_changed`) until re-confirmed.
- **Starting sign-in:** the shared middleware refuses cross-site requests to `/api/`, except top-level GET
  navigations to `/api/auth/oidc/start` and `/callback` with organizations on (the IdP's redirect back, a link
  opened from mail). A start without a sign-in link must be same-origin or typed (`Sec-Fetch-Site`), and any
  `Origin` must be Rynhult's. Open attempts are capped: one per browser (a new start replaces it), 30 per address,
  3 per sign-in link and 3 per local sign-in being linked. Failed callbacks count against the address only with
  this browser's own flow cookie, so other sites can't lock an office out.
- **Break-glass:** password + TOTP and passkeys share no code path with OIDC and keep working when the provider is
  down or misconfigured; tested in `tests/test_oidc.py`.

## Local WebAuthn

`webauthn==3.0.1` verifies registration and assertions with required user verification
and none attestation. Exact configured Origin and its hostname bind each ceremony;
Caddy forwarding headers cannot select an RP. Private CA server trust remains a
browser prerequisite. Separate hostnames require separate enrollment. Passkeys do
not grant remote device pairing or provider authentication.

Migration 0041 adds public credentials, per-user opaque handles and recovery-code
digests, session reauthentication state, one-use expiring challenges and per-role
policy. Challenges bind the browser cookie, client address, purpose, origin and
(for authenticated ceremonies) user/session. Fresh verification lasts five minutes.
Store no private authenticator keys or raw credential responses in audit events.
Zero counters work. The signature is checked before the counter (py_webauthn compares
counters first, so ACC passes it 0 and compares itself); a validly signed nonzero
regression is refused, audited with `counter_regressed` and raised as a critical Inbox
item for the person and owners, so an unsigned guess can't raise false alarms.
Address/account throttles are shared with login; like login, a locked account still
accepts a fully correct answer (password reauthentication included). Challenges are
persistent, consumed on failure and bounded: anonymous sign-in ceremonies share 1,000
slots where the oldest give way, and each login session has five of its own, so a flood
from many addresses never refuses sign-in or verification outright.

Required policy needs two credentials per owner, one per member at the selected
address, and offline recovery material for everyone affected. Password plus a
one-use offline code revokes the person's credentials/sessions/reset links and
explicitly restores Allow for owners when recovering an owner, preserving sole-owner recovery. A member under Require needs an owner to explicitly Allow member fallback first; member recovery cannot change policy.
Owner-mediated reset is blocked while Require is active; allowing fallback is an
explicit owner action. Inviting an owner and resetting another person's sign-in need the acting owner's
recent verification under any policy (refusals audited as `user.invite_refused` and
`user.reset_refused`; your own reset is exempt for sole-owner recovery). The API token
has no human session and is refused for these. Changing a role whose passkeys are
required needs the acting owner's recent verification (a passkey when owners require one), so a copied owner
cookie can't create a lasting required-role account. Registration refuses credential
IDs over 1,023 bytes. Audit includes register, rename, remove, sign-in, failure,
password or passkey verification, recovery-code replacement, recovery and policy
change (with before and after). Recovery and relaxed policy also raise Inbox items for owners.
See
[People and sign-in](/User-Guide/People-and-Sign-in#owner-policy-and-recovery) for the
operator-facing recovery path, and `docs/decisions/0013-passkeys.md` for the focused
library source review and the boundaries for future SSO and device pairing.

### Manual HA control boundary

Owner-only `/api/ha/promote` and `/api/ha/demote` retain normal bearer or cookie,
Origin and CSRF authorization. Each request carries the epoch the owner observed;
accepted transitions are audited. The public health projection contains role,
epoch, database connectivity and a generic uncertainty flag, not main identities
or connection credentials.
Full holder/node details require owner authorization.

Health also projects dry-run evaluation status, without witness identities or
credentials. `ACC_HA_AUTO_EVALUATE` is off by default; enabling it supplies no
telemetry or automatic promotion. Only authenticated owner status reads audit
evaluation-state changes. See [the evaluation boundary](/Internal/Manual-Failover#automatic-evaluation-phase-3-adapters).

A standby rejects workload HTTP and WebSockets, including preview gateway traffic,
and starts no background workers. Authority is checked on application SQL writes,
broker RPC dispatch and stream input/output, with a dedicated database connection
outside application snapshots. Closing or invalidating that connection cannot
silently reconnect it into authority. A separate activity barrier drains admitted
work before an epoch changes. HA mains refuse all local broker dispatch.

The owner must independently fence the previous host and privileged helpers before
crash-recovery attestation. An epoch cannot retract a command already handed to an
external system. Matching builds, file/secret readiness, stable TLS routing and
PostgreSQL HA/backup remain operator responsibilities; automatic failover is not
implemented. Security review and a real disposable PostgreSQL contention/partition
drill remain deployment gates. See [the runbook](Manual-Failover.md).

A canceled/timed-out mutating broker RPC or canceled publish/tool execution latches
`ha_authority.uncertain`. The holder keeps its epoch and keeps serving: a
lost reply from one node (or an agent stopping its own broker) must not stop the
whole control plane. The owner cannot turn an unknown external outcome into a
clean demotion. Recovery requires independently
fencing the old authority, resolving affected external work and explicit crash
promotion; the accepted transition clears the durable flag. Read-only RPC
cancellation does not set it. Audit records operation names, never command
parameters or credentials.

Failover enforces these boundaries:

- A Cloudflare DNS change, a root-helper request (Caddy publishing, packages,
  accounts, updates) and a node tool command re-check authority immediately before
  the effect, not only at the last guarded SQL write.
- Work carries the epoch it was admitted under (a context variable that tasks and
  `to_thread` work inherit). Work that outlives a demotion stays fenced even after
  the same process is promoted again.
- A standby refuses workload traffic locally, before the cluster-wide activity
  lock, so unauthenticated requests cannot hold the barrier that promotion and
  demotion need. The public health projection reads the database at most once per
  second.
- A database error in one request (constraint, length, lock timeout) re-checks
  authority instead of fencing the main.
- Startup never migrates a shared schema while a main holds authority; a schema
  change holds the leadership lock. A database initialized for HA refuses a main
  without `ACC_HA_NODE_ID` before migrating anything.
- HA PostgreSQL connections use client keepalives, `tcp_user_timeout` and the
  matching server settings, so a partitioned main's session locks clear in about
  half a minute instead of hours.
- A promotion whose workers cannot start hands authority back by clean demotion.
### DNS phase 2 boundary

Publishing decrypts only the bound Publishing-purpose credential in accd, per job or uncached zone listing, and
keeps no credential cache.
A selected vault failure never falls back to a legacy token. An owner can deliberately restore the legacy source
with password confirmation; the legacy file remains until the owner confirms removal. Under a binding the legacy
token publishes only in the bound zone. Removing, disabling or replacing the imported legacy Publishing credential
sets the durable `dns.legacy_publishing` setting, after which the legacy source can't be selected and a selected one
refuses jobs. Zone listings (reachable by members) are cached for five minutes per selection identity and coalesced,
so they can't spend the Publishing credential's provider rate limit. Members see only allowed
zone names, never remote account IDs or credential metadata. New endpoints retain browser-owner, CSRF and password
checks. Provider response bodies and exception strings are converted to fixed categories before Publishing errors.

The ownership ledger and remote snapshot jointly authorize changes. Explicit legacy adoption also requires an
applied historical publication ID, matching remote ID, pinned address and DNS-only state. Uncertain writes retain
intent and block retries. Certificate schemas and fixed rendering are shared with root deployment as a standalone
root-owned parser; generic environment text is never sourced. The managed template expands a fixed provider block;
Caddy autosave remains disabled. Azure and Route 53 remain unavailable in production pending live qualification.
Route 53 binds the entered hosted-zone ID to the zone name through that zone's SOA before listing or changing records.
The root helper accepts a certificate provider only if the installed release qualifies it
(`provider_qualification.<id>.certificates`) and root policy allows it; neither alone is enough. Retiring the legacy
Publishing token is permanent by owner decision.
The live-test harness executes a private copy of the exact candidate bytes it hashed.
Azure's requested zone-scoped DNS Zone Contributor role permits all DNS record types within its one public zone.

## Remote access device gate qualification

Machine-leaf registration and presentation updates reserve their registry write
transactions before reading. This prevents a concurrent database writer from
invalidating the SQLite snapshot during issuance or retirement. The privileged
signer call runs outside that reservation; a slow signer does not hold the
registry writer. Node-removal checks and identity/audit updates remain atomic.

Public access defaults off. [Device pairing](/Internal/Device-Pairing) provides a
registry, one-use imports with matching-code confirmation, and an owner exact
review/apply/off flow through a closed, uninstalled root helper. The helper
requires a separately provisioned qualification flag and reviewed private-only
base. No live edge changes or production exposure were made by this slice.
Phase 1's isolated tests qualify
new-connection rejection on the installed Caddy 2.6.2 using mandatory CA/expiry
verification plus the TLS leaf verifier. Empty membership retains the verifier;
omitting it with a deprecated empty leaf list admitted a valid-chain unlisted
identity in the negative control. Strict SNI/Host matching prevents HTTP/1.1 and
HTTP/2 cross-host routing; TLS tickets are disabled and previously issued tickets
are rejected after revoke/reload. The stub counter proves rejected probes add no
upstream requests. The exact JSON fields and owner physical-device checklist
are in `docs/REMOTE-ACCESS.md` in the repository.

Run the isolated qualification from the checkout root:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_remote_access_qualification.py
```

Tests own their loopback Caddy, Unix admin socket, counting upstream and temporary
CAs; they never use the live edge or real DNS. This does not qualify existing
streams, ACC session revocation, public IPv4/IPv6 routing or browser identity
stores. Overall release remains NO-GO pending physical-device qualification and
later product gates. Owners must qualify iOS Safari and Home Screen app behavior
separately, and Android identity import and browser selection on actual devices.

### DNS wave 2 boundary

Google Cloud DNS, DigitalOcean and Hetzner candidates use the same owner-only purpose-bound infrastructure vault
and ownership ledger. Google OAuth assertions use explicit service-account JSON and a fixed token endpoint; no ADC,
metadata, external-account URLs or credential chain. Because the same JSON reaches Caddy's Go library (whose decoder
matches keys case-insensitively), the shared parser accepts only the exact keys of a downloaded key file, no
duplicates even by case, ASCII strings and fixed Google endpoints (review SRD3-01). Managed-zone identities include the project. Google describes
that zone before records and uses atomic exact-delete/create changes. Like Route 53 it cannot distinguish an
externally recreated identical RRset. DigitalOcean and Hetzner use fixed HTTPS APIs and bounded local pagination;
remote next links never receive credentials. They have a residual read/write race without conditional mutations.

The Google certificate module needs a credentials file. Root delivers only content-addressed names under the fixed
`/etc/caddy/acc-dns-google` root:caddy 2750 directory. Files are root:caddy 0640, checked through descriptors for
owner, group, mode, link count and bounded size; no caller selects a path. Caddy has read access but cannot replace
files. Current, previous and candidate root-only environments determine retention; failed candidates and retired
files are removed on rollback/purge. The setgid directory supplies Caddy's group without adding CAP_CHOWN to the
helper. Installer and systemd allowlists include only this extra fixed directory. Live apply/rollback/restart,
credential-file custody and module compatibility remain unqualified; security review must precede merge.

### Preview element descriptions

The gateway-owned wrapper and app share only their project/port origin. No app response is rewritten and no preview
script is injected into the dashboard. A fixed JSON-string protocol checks exact origins and frame sources in both
directions; ACC correlates a single reply with a fresh pick id armed by a person. Payloads are limited to 4,096
characters, eight structural selector segments (512 characters), 1,000 excerpt characters, and finite bounded
viewport coordinates. HTML, attributes, URLs, form values, editable contents and images are omitted. The preview
can forge all description fields, so the protocol authenticates neither the DOM nor its meaning.

Only the dashboard's browser/CSRF-authorized API prepares a draft, using existing vault/common-secret redaction,
control/bidi stripping and untrusted-message framing. Characters the dialog can't show are dropped first: Unicode
format characters (zero-width, bidi, tag characters), variation selectors, Hangul fillers, and private-use,
surrogate or unassigned code points. They could otherwise carry instructions the agent reads but the person never
sees, split a secret past redaction, or disguise the closing marker. The shared confirmation dialog renders that
exact plain text. Because the preview page decides when the dialog opens, **Send to agent** stays disabled for
800 ms with focus on **Cancel**, so a double-click or a repeated Enter meant for the page can't send.
Drafts are bounded to 64 in memory, one per login and four per person, and expire after two minutes. When either limit is reached a new selection is refused (429); nobody's open confirmation is evicted. Confirmation binds the login,
session, project, machine and reserved port, and checks permission and running managed-session state again. The
draft is consumed before the existing broker send path is awaited, preventing replay/concurrent duplicate delivery;
uncertain sends are not retried. Picker audits hold metadata; existing session message audit/history records the
confirmed redacted message. Revocation cannot recall already displayed data or messages already submitted.

### Public source privacy exclusions

The shared `acc.bundle.private_content` policy excludes security review reports,
owner drafts (including draft decisions), handoffs, qualification and operations
records from snapshots and source bundles. Operator-specific decisions 0050 and
0063 are excluded explicitly; reviewed technical ADRs remain public. Review each
new document rather than treating an accepted decision as automatically public.
Use the history-free snapshot procedure in `docs/PUBLISHING-THE-SOURCE.md`; never
publish the private Git history. A provisional denylist and a passing secret
scanner do not replace the authoritative private inventory or image review.

### Gated container integration adapters

Container credentials and HTTPS egress require the installation/project container gates, the account broker's
`ACC_CONTAINERS=1`, and explicit opt-in in a root-owned pinned-image policy. The broker reads only the selected
agent-account credential file. Root authenticates its bootstrap socket by UID and broker MainPID and checks the
session/generation/provider/policy binding. A sealed memfd supplies the narrow credential read-only; no home,
refresh-token store or runtime socket enters the container. Existing explicit session environment delivery is
unchanged. Claude access-only renewal, Codex API-key auth and selected-account GitHub.com Copilot remain
owner qualification gates; unsupported rotating sign-ins refuse.

Networking remains `none`. An image-local loopback bridge carries HTTPS CONNECT to a session-only broker gateway.
All DNS answers must be public and outside the root-approved control-plane/public-host exclusions; dialing uses
the checked numeric address. Private/LAN, link-local/metadata, IPv4 transition ranges, the host's own addresses and
its directly connected networks (also when publicly addressed, as IPv6 LANs usually are), direct networking, UDP and
non-443 access remain denied. Connection/header/time/byte limits bound the gateway. Stop closes streams even if
the launcher cannot be reached; stale stops cannot close newer generations. Broker restart loses egress and
requires session recreation. Hooks remain in immutable image policy and use bound session output files.

This code has fake-transport and filesystem tests, not root/runtime/provider qualification. Neither installer nor
policy examples enable it. The source document `docs/CONTAINERS.md` records the exact supported formats, trust
boundary and remaining owner gates. No new API route, sudoers entry, ACL or launcher socket permission is added.
