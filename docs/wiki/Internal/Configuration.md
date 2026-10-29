# Configuration

## accd (environment variables)
Set them in `/etc/default/accd` (`root:root`, mode `0600`), which systemd loads before switching to the `acc` account. Setup creates it privately; the installer also tightens existing files before reading them. `deploy/accd.env.example` is a commented starting point. Every variable has a neutral default, so nothing about one installation lives in the code.

| Variable | Default | Effect |
|---|---|---|
| `ACC_GROUPS_REMOTE` / `RYNHULT_GROUPS_REMOTE` | off | Enable the reviewed own-machine group account/placement path. Requires owner account approval and group project approval; remote delivery includes only the launcher's own remote-enabled keys. |
| `ACC_HA_AUTO_EVALUATE` | off | Only `1` enables owner-configured dry-run HA evaluation; no live telemetry adapter or automatic promotion. Authenticated owner status reads audit state changes. |
| `ACC_DATABASE_URL` | empty | Optional PostgreSQL URL; empty retains SQLite. Requires the `postgres` Python extra; one main by default, manual HA is explicit |
| `ACC_DATA_DIR` | `/srv/acc/data` | Database and secrets |
| `ACC_RUN_DIR` | `/srv/acc/run` | Per-session launch files and hook spools |
| `ACC_ROOT_RUN_DIR` | `/srv/acc/run-root` | Hook spools of root's own (external) sessions, root-owned |
| `ACC_BROKER_SOCKET` | `/srv/acc/sock/broker.sock` | Where to reach the broker |
| `ACC_CATALOG_LOCAL_DIR` | `/srv/acc/catalog-local` | Catalog items added in the dashboard |
| `ACC_HOST_LABEL` | the machine's hostname | How the UI names this host |
| `ACC_AGENT_HOME` | `/home/agent` | Home of the agent account new sessions and projects use; its name is that account |
| `ACC_BROKER_SOCKET_DIR` | | One broker per agent account: `<dir>/<account>.sock` (`/run/acc-broker`). Unset: every account uses `ACC_BROKER_SOCKET` |
| `ACC_SERVICES_ACCOUNT` | the agent account | The user whose (rootless) Docker stacks ACC brings up; its broker runs nothing else |
| `ACC_PREVIEW_DOMAIN` | wildcard zone, otherwise ACC's first origin hostname | Private wildcard DNS suffix for isolated preview origins; must equal the wildcard zone in Cloudflare mode |
| `ACC_WILDCARD_ZONE` | | Zone whose wildcard certificate covers published names |
| `ACC_PUBLISH_ZONES` | the wildcard zone | Comma-separated zones names may be published in. Each zone's apex and `www`, and ACC's own hosts, are always refused. The publishing helper reads the same file |
| `ACC_LISTEN` | `127.0.0.1:8740` | `host:port`, `unix:/path`, or `systemd` (socket activation; what `accd.service` uses with `accd.socket`) |
| `ACC_ALLOWED_ORIGINS` | `http://127.0.0.1:8740` | Comma-separated exact origins the browser uses, e.g. `https://acc.example.com`; WebAuthn uses the verified origin hostname as its RP ID, with separate enrollment per hostname |
| `ACC_SSH_HOST` | the host of the first `https` origin | The SSH host **Open in editor** links to (VS Code Remote-SSH); empty with no `https` origin hides the menu |
| `ACC_ORIGIN_PUBLIC_IP` | | One-time bootstrap for the public Publishing address inventory; owners manage subsequent targets in Settings → Publishing |
| `ACC_ORIGIN_INTERNAL_IP` | | One-time bootstrap for the internal Publishing address inventory (e.g. `10.0.0.20`) |
| `ACC_COOKIE_SECURE` | `1` | Set `0` only for local HTTP testing |
| `ACC_RETRY_BACKOFF` | `0,30,120` | Seconds before resume attempts 1–3 |
| `ACC_INGEST_INTERVAL`, `ACC_RECONCILE_INTERVAL` | `1`, `5` | Supervisor loop timing (seconds) |
| `ACC_NODE_CA_FILE` | set by `deploy/install.sh` when Caddy signs ACC's certificate: `/srv/acc/data/node-ca.pem` | The private root CA ACC's certificate chains to: Add machine pins it (`--ca-fingerprint`), `GET /api/nodes/ca` serves it, **Settings → Certificate** shows it and offers `acc-root.crt` and `acc-root.mobileconfig`. Unset: ACC's certificate is expected to be public |
| `ACC_GIT_DIR` | `/srv/acc/git` | Git backups: one bare repository per project (acc 0700) |
| `ACC_BACKUP_URL` | the first `https://` origin in `ACC_ALLOWED_ORIGINS` | The address brokers (this machine's and nodes') push git backups to; with neither, git backups are off |
| `ACC_BACKUP_INTERVAL` | `900` | Seconds between looks for new commits per project (a stopped session triggers one at once) |
| `ACC_BACKUP_MAX_PUSH` | `2147483648` | Bytes one backup push may send |
| `ACC_SOURCE_URL` | ACC's own repository | Where the **Source code** link in Settings points (a fork that changes ACC can point it at its own source). On GitHub it also gives Add machine its links to this version's Windows and Mac installers (`releases/download/v<version>/`). **Source of this version** downloads `data/bundle/acc-<version>.tar.gz` |

## Workstation sleep

Owners set `node.idle_sleep_minutes` through `POST /api/nodes/{name}/policy`: `null` (the default) disables it; 1–10080 selects minutes. Host enables it at 30 minutes. Omitted fields preserve this setting. Only active, connected Workstation nodes qualify; Always-on nodes ignore a saved duration. See [Let a workstation sleep](/User-Guide/Nodes#let-a-workstation-sleep).

The supervisor evaluates after ingestion and reconciliation. Before an ordinary stop it rechecks the role, fresh screen, process output and hook spool under the stop lock. Unknown, incomplete or failed observations cannot authorize a pause. No OS power configuration changes or sleep commands are involved.

## Broker
One per agent account, `acc-broker@<account>.service`, started from `acc-broker@<account>.socket`; settings in `/etc/acc/broker/<account>.env`, written by `acc-agent-add`.

| Variable | Default | Effect |
|---|---|---|
| `ACC_BROKER_ROLE` | `agents` | `agents` runs that account's CLI sessions; `services` only brings Docker stacks up (`compose_up`, `compose_ps`) |
| `ACC_RUN_DIR` | `/srv/acc/run` | Where its session folders go (`/srv/acc/run/<account>`) |
| `ACC_ALLOWED_UIDS` | uid of `acc` | Who may use the socket |
| `ACC_ALLOWED_ROOTS` | `/srv/acc/workspaces` | Folders the broker may work in (`:`-separated). Add ACC's own checkout to read its wiki as a read-only project |
| `ACC_PROVIDERS` | `claude=claude,codex=codex` | CLI executables |
| `ACC_TMUX_SOCKET` | `acc` | tmux socket name |
| `ACC_CATALOG_DIR`, `ACC_CATALOG_LOCAL_DIR` | | Where catalog skills live (for Codex links) |

## Root helpers

Personal **Settings → Show advisory risk labels** defaults on. The
`risk_labels.<username>` Setting stores `enabled`; authenticated
`GET/PUT /api/preferences/risk-labels` reads or changes only the caller's choice.
It hides advice in Inbox and Needs you, never changes guardrails or sessions.
There is no model-provider configuration for this feature.

Tool approval defaults on. Owner settings `tools.person.<username>` and
`tools.project.<id>` hold `{"required": false}` exemptions; either exemption
disables only the tool-change guard for newly launched sessions. The effective
value is frozen into `guardrails.json`; resuming an existing session does not
change its launch policy. Exempt direct installs are not inventoried.

The package helper and request CLI load the deployed sibling
`acc-tool-policy.py` (from `acc.tooling`); update both alongside the broker and
node. Tool manager executables must already exist. System operations use only
apt, snap and flatpak; account operations have fixed account-local destinations
and refuse root. See [Host and Providers](/User-Guide/Host-and-Providers#tools-per-machine-and-account).

| Setting | Where | Effect |
|---|---|---|
| `ACC_SERVICES_ACCOUNT` | `/etc/default/accd` (the host snapshot loads it too) | Whose Docker containers and user services the Host page shows |
| `ACC_HOSTSTATE_MOUNTS` | `/etc/default/accd` | Space-separated mounts for the Host page's storage; default: every disk-backed filesystem |
| `ACC_BACKUP_DIR` | `/etc/default/acc-backup` | Where daily backups go (default `/var/backups/acc`; best on another drive than `/srv/acc`) |
| `ACC_BACKUP_MIRRORS` | `/etc/default/acc-backup` | `user:/source:/dest;…`: folders copied as their user (e.g. a project's own database dumps). Rerun `deploy/install.sh` after changing it: it renders the unit drop-in that makes each destination writable |
| `ACC_BACKUP_PASSPHRASE_FILE` | `/etc/default/acc-backup` | A root-only (0600) file whose first line encrypts each day's `secrets/` into `secrets.tar.enc` (`openssl enc -aes-256-cbc -pbkdf2`); unset: plain `secrets/` next to `acc.db`. See [Operations](/Internal/Operations#back-up-and-restore) |

Setup (`deploy/setup.sh`) writes `/etc/default/accd`; the installer reads the owner, agent account, ACC's hostname and the zone from it.

## Secrets (`/srv/acc/data/secrets`, readable only by `acc`)
`vapid_private.pem` (push notifications; created on first start), `github_token` (read views and repository creation), `cloudflare_token` (DNS), `api_token` (local CLI access), `setup_token` (first run only).

## acc-node (on a node)
`acc-node enroll` writes `/etc/acc-node.env` (`--config` elsewhere); `run` and `status` read it. The path options take their defaults from the environment when given: `ACC_NODE_CONFIG` (`--config`), `ACC_NODE_STATE_DIR` (`--state-dir`) and `ACC_NODE_SOCKET_DIR` (`--socket-dir` of `enroll`, which writes it into the config file); the macOS launchd job uses these. An option on the command line wins, and an empty variable counts as unset. The state dir (`--state-dir`, default `/var/lib/acc-node`) holds the private key `node.key` (`0600`), the private CA `ca.pem` (trust `ca` only) and `status.json`, the last connection state that `acc-node status` shows.

| Variable | Effect |
|---|---|
| `ACC_NODE_PRIMARY` | The primary's URL, e.g. `https://acc.example.com` |
| `ACC_NODE_TRUST` | `system` (system CAs) or `ca` (only the primary's private CA); both check the hostname |
| `ACC_NODE_CA_FILE` | Trust `ca`: the CA root saved at enrollment (`<state dir>/ca.pem`) |
| `ACC_NODE_CA_FINGERPRINT` | Trust `ca`: SHA-256 of that root's DER, as given to `--ca-fingerprint` (for reference) |
| `ACC_NODE_ID` | The id the primary gave this node at enrollment |
| `ACC_NODE_NAME` | The node's name, as entered under Host → Machines |
| `ACC_NODE_SOCKET_DIR` | Where this node's broker sockets are (default `/run/acc-broker`) |

Optional WSL isolation is an explicit installer choice stored in `/etc/wsl.conf`,
not an environment setting. `--update` leaves it unchanged. Host reports effective
interop and agent-account drive access, not merely the saved configuration.
See [the WSL isolation procedure](/User-Guide/Install-Windows#isolation-for-dedicated-wsl-nodes).

Project and session database rows require an explicit `account` / `run_as`; there is no
built-in operator username. API creation paths select the configured default account.
Upgrades preserve existing assignments and remove the historical SQL defaults.
The bundled repository-protection ruleset is `deploy/github/ruleset-protect-main.json`.

## ACC updates

Owners set the GitHub repository, automatic installation (off initially), daily server-local time and per-machine
following in Settings. Choices live in `data/updates-config.json`; check results and audit reconciliation live in
the `setting` table under `updates`. Empty repository disables checks. The default derives from `ACC_SOURCE_URL`
only for a GitHub URL. An optional private repository token is read from `data/secrets/update_token` (0600,
readable by accd and root), never returned or logged.

`ACC_UPDATE_ROOT` defaults to `/srv/acc/updates`; `ACC_UPDATE_SOURCE` defaults to the installed app source.
The root helper uses `ACC_DATA` for its data root (default `/srv/acc/data`) and `ACC_UPDATE_GROUP` for state-file
readers (default `acc`). Linux node service overrides set mode `node`, source `/opt/acc/app`, and group `acc-node`.
Node primary and CA trust come from `/etc/acc-node.env`; nodes never read a GitHub repository setting.

For isolated helper tests, root-controlled environment overrides also accept `ACC_UPDATE_HEALTH_SOCKET`,
`ACC_UPDATE_BACKUP_COMMAND`, `ACC_NODE_CONFIG` and `ACC_NODE_STATE_DIR`. None can be supplied in a dashboard
request or in the acc-owned updates configuration file.

`ACC_UPDATE_REPOSITORY` in root-owned `/etc/default/accd` authorizes the source root may install. Its default
is the GitHub repository derived from `ACC_SOURCE_URL`; an explicit empty value disables installation. A different
repository chosen in Settings can be checked, but cannot be installed until the root-owned setting matches it.

In node mode the root helper rechecks a private CA against the fingerprint in the root-owned enrollment file,
and trusts that one certificate only. Appending another root to the acc-node-owned CA file cannot widen trust.

## Preview network setup

Previews use `preview-<project id>-<port>.<preview domain>`. Run the normal root installer during deployment to render the updated Caddy templates; accd never writes Caddy configuration itself. DNS and certificate trust must be in place before the Preview tab can load an app.

- **Cloudflare wildcard mode:** with ACC at `acc.example.com` and `ACC_WILDCARD_ZONE=example.com`, the default preview domain is `example.com`. Configure private wildcard DNS for `*.example.com` to ACC's LAN/VPN address. Caddy routes preview names inside its existing wildcard site and reuses the Cloudflare DNS-01 certificate. Do not create individual project DNS records. A configured `ACC_PREVIEW_DOMAIN` must equal `ACC_WILDCARD_ZONE`; the installer refuses a different value.
- **Internal CA mode:** with ACC at `acc.example.lan`, the default is `*.acc.example.lan`. Configure that wildcard in private DNS and trust Caddy's root on client devices. The installer adds a `tls internal` wildcard site; no public certificate or DNS record names a project. A configured private preview suffix may override the default. If ACC is opened by IP address, set `ACC_PREVIEW_DOMAIN` to a private DNS suffix instead.

Both modes apply the same private client allow list as ACC before proxying to the accd Unix socket. The existing root installer checks the private Caddy admin socket before reload. Publishing cannot use the reserved `preview-` namespace. Prefer a suffix on the same site as ACC; unrelated sites can encounter third-party cookie blocking in iframes.

Preview credentials last five minutes and are bound to the current browser sign-in, project, port and hostname. The gateway rechecks permissions on every request and every second during a WebSocket. Remote projects currently display an unavailable message: the machine link needs a general HTTP/WebSocket stream before they can be proxied safely.

### Backup server

The encrypted backup server uses the existing `ACC_DATA_DIR`, `ACC_CATALOG_LOCAL_DIR` and `ACC_GIT_DIR` settings.
Its private service state is `data/backup-service` (0700), with signing key and independent repository-password
files (0600), target configuration, immutable staged generations and verified-result records. These
passwords are outside the DB/vault so offline recovery does not depend on opening the vault first. The recovery
kit is a confirmed owner download, not a stored session artifact.

Local target paths are provisioned by a local administrator with `python -m acc.backup_server add-local` as the
ACC service account; local target API actions accept IDs only. S3 setup accepts endpoint/bucket/region, never a filesystem destination. The qualified restic binary is `/usr/local/bin/restic`, installed
by `acc-packages` at `/opt/acc-restic/versions/0.19.1/restic` with committed checksums. Test-only/offline CLI use can
supply `--restic` to point at a separately verified temporary binary. Never use a backup-supplied executable.

Default policy: hourly schedule, two-hour stale threshold, 10 MiB/s transfer default, 48 hourly/30 daily/12 monthly
retention, 90-day owner drill reminder. Private payload staging is bounded to 20 GiB cumulatively (the in-flight capture
plus at most one retained failed upload), additionally limited by available free space with a 64 MiB reserve and scratch headroom. Uploads never prune automatically.
`data/RESTORE-HOLD` causes quarantine before ordinary startup; `.update-in-progress` blocks capture across updates.
The whole-capture `.capture-update.lock` is separate from the shorter DB mutation barrier.
See [Backups](/User-Guide/Backups) for kit custody and exclusions.


### Per-session containers
### Container phase 0 prototype

Off by default; see `docs/CONTAINERS.md` ("Trusted launcher") for the design and the owner's qualification run.

| Setting | Where | Default | Effect |
|---|---|---|---|
| `ACC_CONTAINERS` | `/etc/default/accd` | off | `1` lets owners switch projects to containers. The launcher's audit lines are copied even when off. Off: the project switch is refused, and a project still switched on refuses new sessions (never a native fallback) |
| `/etc/acc/containers/host-qualified.json` | root, written by `sudo /srv/acc/app/.venv/bin/python -I -m acc.container_host_record record report.json` | absent | Containers are a Preview: switching a project on is refused (409, "containers are a Preview that needs a qualified host: …") until this root-owned record names a passing qualification report for this machine (`/etc/machine-id`). A record that isn't root-owned, sits in a folder others can write, names a failing report or another machine counts as absent. Switching off never needs it. A new session in a project already switched on is refused the same way (409, audited `container.launch_refused`); running sessions and resumes are left alone. `python -I -m acc.container_host_record check` shows the verdict |
| `ACC_CONTAINERS_ATTACH_QUALIFIED` | `/etc/default/accd` | off | Until the container terminal attach path is qualified on a real machine, container sessions are refused with 409 "container sessions are not available yet: the terminal attach path is not qualified". Resumes, restarts and retries of existing container sessions fail with the same message. Set `1` only after that qualification run |
| `ACC_CONTAINERS_DIR` | `/etc/default/accd` | `/srv/acc/containers` | Where accd writes grants (`grants/`, `acc:acc` 0750) and reads the launcher's audit lines (`audit/`) |
| `ACC_CONTAINERS_POLICY_ID` | `/etc/default/accd` | `default` | Which pinned image (policy id) container sessions use |
| `ACC_ORGANIZATIONS` | `/etc/default/accd` | off | `1` shows owners **Settings → Company sign-in** and mounts the OIDC routes. Company sign-in itself stays off until an owner turns it on; the redirect URI is the first https `ACC_ALLOWED_ORIGINS` entry plus `/api/auth/oidc/callback` |
| `ACC_CONTAINERS_ATTACH_ROOT` | `/etc/acc/broker/<account>.env` and the launcher's environment | `/run/acc-ct-attach` | Where each container unit's terminal relay listens (`<digest>/attach.sock`, a 0700 folder of the account's that the launcher makes and removes). The root itself is root's 0755 folder (the launcher sets a narrower mode back to 0755 and refuses a link or another owner). Both sides must agree |
| `ACC_CONTAINERS_SOCKET` | `/etc/acc/broker/<account>.env` | `/run/acc-containers.sock` | The launcher's socket, for the broker |
| `/etc/acc/containers/policy.json` | root, 0644 | absent | ACC's pinned images: `{"version": 1, "images": {"default": {"image": "sha256:…", "network": "none", "providers": {"claude": "/usr/local/bin/claude"}}}}`. Absent: every container launch is refused. Example: `/etc/acc/containers/policy.example.json` |
| `container_limits.<account>` | Setting (Settings → Container limits) | members 2 CPUs, 4096 MiB, 512 processes, 20 GiB, 4 sessions; owners none | Per-session `cpus`, `memory_mib`, `pids`, `disk_gib`, and `sessions` at once; `null` is no limit. Written for new accounts by `create_account`; otherwise the role's defaults apply |

The phase 0 qualification runner stays separate: `scripts/qualify_containers.py --enable-prototype` (add
`--user-manager` to run it through the account's lingering user manager, outside the broker's NoNewPrivileges).
It uses only the current non-root account and temporary runtime storage and always reports no-go; the repository's
`scripts/container_qualification/README.md` lists what it can't certify.

The launcher accepts only `network: none`. `/etc/acc/containers/hooks.d` is an empty, root-owned OCI hook directory
on a fresh installation. Helper processes share `acc-launcher.slice` (256 MiB, 4352 tasks: every admitted connection at its unit's limit, one CPU), separately from
session budgets; a launch request's service gets 256 tasks, since it runs Podman as the account for the image check
(refused as `launcher_task_limit` if that hits the limit). Audit buffers retain up to 16 MiB/day of lifecycle records and 64 KiB/day in each of 64 fixed uid refusal buckets of refusals for 30
days; saturation is reported to the journal. Disk scans refuse unsafe or excessively large directory trees.
It uses only the current non-root account and temporary runtime storage, prints
a JSON report and exits with no-go. Without the flag it performs no runtime work.
Optional `--image` (immutable sha256 image ID) and `--image-archive` arguments
import a reviewed archive into temporary storage for a fixed offline payload;
no images are downloaded. Never run as root or point it at another account's
runtime. The repository's `scripts/container_qualification/README.md` documents
prerequisites and the gates this script cannot certify. Phase 0 remains blocked
on rootless mapping in the restricted broker context, AppArmor, nested sandboxes,
real mandatory provider hooks and credential adapters. It grants no isolation
status or automatic native fallback.

## DNS phase 1

Owner-only credentials and bindings are stored in the existing encrypted vault database/key pair; there are no
provider secret settings in project/session configuration. The production Publishing provider is `cloudflare`. `azure` and `route53` have fixed credential forms and candidate adapters, with activation pending both live consumers.

The optional inactive root deployment uses `/etc/acc-dns/policy.json` (allowed zones), `release.json` (independently
reviewed binary manifest), `/etc/caddy/acc-dns` (0700 credential generations), `/run/acc-dns/helper.sock` (root:acc
0660, kernel peer UID check) and a root-only shared configuration lock. New helper requests have no path, endpoint,
module, service or executable overrides. The checked-in release remains pending CI artifacts and live qualification.

Installer and Publishing Caddy validation prefer the managed certificate EnvironmentFile after cutover, including
when the legacy file has been removed. No credential is put in argv. The internal-CA installation path needs no
provider. An operator activates the root socket and hourly retention timer deliberately; the helper installer does
not start Caddy or the helper. See [Caddy hardening](/User-Guide/Caddy-Hardening) for deployment and recovery gates.

The managed multicloud wildcard template is `deploy/dns/wildcard-managed.caddy.in`. An operator must deliberately
render it for the root-approved zone before first Azure/Route 53 apply. The root policy defaults to Cloudflare;
expanding its fixed `providers` list is a local reviewed operator action, never a portal request. The managed
EnvironmentFile contains a strictly regenerated provider block, not caller configuration. Both helper installers
install the same root-owned standalone credential parser; installer and Publishing validation consume it too.

## Enrolled Linux backup targets

Enrolled Linux backup targets are prepared in **Host → Backups → Set up**. Target records bind the enrolled
node ID and restic repository ID; repository keys live in `backup-service/keys`, independent ciphertext
transport tokens in `backup-service/transport` (private 0600 files). No target maintenance credential is stored.
The owner installs root-owned `/etc/acc/backup-target.json`, `/etc/acc/backup/writers` and
`acc-backup-receiver.service` on the target using the exact rendered commands. The receiver uses target
loopback port 18762 and `/var/lib/acc-backup`, owned by the dedicated non-login `acc-backup` account;
`acc-node` gets no filesystem access. The unit drops all capabilities, allows only loopback addresses
(`IPAddressDeny=any`, `IPAddressAllow=localhost`) and uses the usual systemd sandboxing. Default repository
ceiling is saved as `max_size_bytes` when preparing a target: 1–10240 GiB, defaulting to half the reported free space on the receiver filesystem (whole GiB, rounded down), or 20 GiB when unavailable. Records without the field retain 100 GiB. This is not a filesystem quota. On the main, each job's adapter is a Unix socket in a private
`acc-backup-*` folder under the system temporary directory, removed when the job ends.
Official rest-server 0.14.0 amd64/arm64 archive checksums are committed in `acc.backup_targets`.

## Database backend

`ACC_DATABASE_URL` defaults to empty, which uses SQLite at `ACC_DATA_DIR/acc.db`. A PostgreSQL URL such as `postgresql+psycopg://db.example.lan/acc?sslmode=verify-full` selects PostgreSQL and requires the `postgres` Python extra. Application, migration and transfer connections default to `sslmode=verify-full` when omitted; an explicit URL mode overrides it. Configure trusted database CA material and retain hostname verification. Protect this environment value as a secret. One main is the default; [manual HA](/Internal/Manual-Failover) requires explicit setup and fencing. The source runbook `docs/POSTGRESQL.md` covers root-only offline transfer, checksums, search differences and independent PostgreSQL backups. ACC server-backup capture refuses PostgreSQL; it cannot protect that database.

### Manual HA

`ACC_HA_NODE_ID` defaults to empty (single-main behavior). Set a distinct stable
lowercase main name on each PostgreSQL main to enable manual active/passive mode;
every process starts standby. SQLite refuses this setting. An initialized cluster
database requires it on all subsequent mains. `ACC_NODE_ALTERNATE_PRIMARIES` is an
optional whitespace-separated list of owner-approved HTTPS control endpoints on
execution nodes; it defaults to empty and uses the existing verified TLS trust.
It changes node reconnect attempts, not download/git/update endpoints. Read the
[owner runbook](/Internal/Manual-Failover) before enabling either setting.

`ACC_HA_AUTO_EVALUATE=1` opts into dry-run automatic evaluation; it defaults to
off and is not a promotion switch. Current production status has no witness,
heartbeat or replication telemetry, so enabled evaluation reports **Telemetry
unavailable** and **Would promote now: No**. Manual owner promotion and independent
fencing remain mandatory. See [automatic evaluation](/Internal/Manual-Failover#automatic-evaluation-phase-3-adapters).


## S3 backup targets

Owner setup records endpoint, bucket, region, unique prefix, repository identity and infrastructure credential ID
in `backup-service/targets.json`. Provider `s3`, purpose `backup-writer` uses fixed access-key/secret-key/optional
session-token fields sealed in the infrastructure table; these are not general Keys or DNS credentials. Temporary
`s3-job-*` credential files are private and removed on job exit and startup reconciliation. There is no ambient
AWS identity, custom proxy or insecure TLS configuration. Timed credentials require owner replacement.
Retention inspections cap each XML response at 2 MiB and the complete paginated scan at 100,000 repository objects.
Compressed responses are refused before body reads, and paginated inspection has a 1,000-page/two-hour limit.
Captures record the minimum COMPLIANCE deadline; status requires at least 30 days still remaining and capture/check
ages below two hours, with an exported kit bound to the repository. Maintenance credentials are never configured in accd or its vault.

The opt-in independent-host `acc-s3-maintain` CLI accepts only explicit private target and purpose-tagged identity
files (at most 1 MiB each) and a durable private state directory. Writer-key history is bounded by that file
size rather than an arbitrary rotation count. Audit events identify the maintenance key ID, never its secret.
No main setting or timer enables it. Its defaults and limits are:

| Control | Default / bound |
|---|---|
| Mutation | Dry-run; `--apply` explicitly resumes the last reviewed plan |
| Extension rule | Below 35 UTC days remaining, extend to 60 days from inventory completion; never shorten |
| `--requests-per-second` | 2; range 0.1–10 |
| `--max-updates` | 1,000 attempts per apply; range 1–100,000 |
| `--max-extra-gib-days` | 1,000; positive finite plan-wide added-storage ceiling |
| `--storage-usd-per-gib-month` | Unset/0 means unknown dollar estimate |
| `--requests-usd-per-1000` | Unset/0 means unknown dollar estimate |
| Inventory | All surviving valid restic versions except locks; 100,000 inspected rows, 1,000 pages |
| Reviewed plan | Expires at 24 hours; bound to target and maintenance key ID |
| Private audit journal | 128 MiB maximum; owner archives independently |
| Main runway warning | Seven days until last version-bound minimum deadline falls below 30 days |

The CLI shares the bounded HTTPS/SigV4 client, without ambient AWS identity or insecure TLS options. A separate
IAM principal, all writer key IDs and owner-reviewed effective policies are required; key-ID refusal alone cannot
prove separation. See [IAM policy, cost interpretation and owner commands](/User-Guide/Backups#independent-retention-maintenance).


## Remote access preparation

The owner setting `remote_access` defaults to `pairing_enabled: false`, an empty
public hostname/private recovery origin and `certificate_days: 90`. Owners prepare
it in Settings → Remote access after recent verification on LAN. Certificate
lifetime can be shortened to 1–90 days. Saving preparation never enables public
access. Separate durable `remote_access_runtime` intent defaults off and records
the last verified helper result, revision, count and pending state. Remote
previews remain off. Apply requires the separately provisioned root policy and
qualification flag; HA enablement is refused.

Pairing links last five minutes. Approved import bundles and separate passwords
stay in memory for at most five more minutes and are removed on one download,
revocation or person removal. The root signer client uses only the fixed
`/run/acc-device-ca.sock`; rendering reads only the public `device-ca.pem` in
ACC's data directory. The apply client uses only `/run/acc-remote-access.sock`,
checks the root peer and has a 30-second deadline. A worker reconciles active
registry leaves and checks the loaded policy every 15 seconds. OFF needs no
recent verification, hostname or CA and is retried while pending. No helper,
service or CA is installed by these settings. See [Device pairing](/Internal/Device-Pairing).

## Rynhult environment compatibility

Every `ACC_*` setting also accepts the same suffix under `RYNHULT_*`. The new
name wins when both are present, even when it is explicitly empty; the setting's
usual validation still applies. For example, `RYNHULT_ALLOWED_ORIGINS=https://main.example.lan`
is an alternative to `ACC_ALLOWED_ORIGINS`. Unrelated vendor variables keep their names.

`acc.env.Environment` is the shared Python reader for process environments and
parsed configuration files. Standalone helpers load the same dependency-free
module, installed beside them as `acc_env.py`. Shell installers/hooks use
`deploy/lib/acc-env.sh`; JavaScript readers use `src/acc/env.mjs`. The Windows
installer uses its shared `Get-AccEnvironmentValue` function. Subprocess/session
exports include both spellings with the resolved value. Both spellings of
`RUN_DIR`, `SESSION_ID` and `GUARD_FILES` are protected against project overrides,
including when the main delivers to an older broker. The vault reserves both
product prefixes and revalidates previously stored keys before delivery; the
compiler and broker also refuse overrides of these protected launch values.

Keep the existing configuration file as the single source of truth. Enrollment
continues writing legacy keys so retained older node readers can load it. No
second writable config file or database is created. Existing installations keep
all paths, service accounts, authentication cookies and the CSRF header.

### Public git backup consent

`node.public_git_backup` defaults to false for every machine, including existing machines upgraded by migration 0068. An owner enables it under **Host → Machines → Machine settings → Allow git backups over the public address**. Verified remote access and machine identities are also required; the private backup URL remains preferred. This is separate from the project’s git-backup switch. Opt-out closes public git streams and revokes pending/public capabilities without stopping private transfers.

## Native mobile server APIs

`ACC_MOBILE` defaults off. `1`, `true` or `yes` exposes the experimental native
lifecycle APIs and includes native leaves in edge policy. Native activation also
requires a separately owner-provisioned random assertion key in root edge policy
and the private main-owned `secrets/mobile-edge-key` file. No app, relay, public
bootstrap or service installation is included. Owner enrollment and renewal stay
on the configured private LAN origin. See [Mobile devices](/Internal/Mobile-Devices).

## Admin feature availability

The existing `setting` table's `admin_features` row stores `choices` and an integer `revision`; no migration is needed.
`GET /api/admin-features` and `PUT /api/admin-features/{key}` are owner-only. Changes use the revision to reject
stale tabs, serialize with database writer admission, audit `admin_features.update`, publish the committed snapshot
and then drain optional work. Enabling trust-widening features calls the same recent-verification check as other
sensitive settings. No request-controlled environment variables are introduced.

Explicit server configuration wins: `RYNHULT_FEATURE_<ID>` / `ACC_FEATURE_<ID>` (uppercase feature id), then the
legacy feature flag where one exists, then `Settings.feature_overrides` / explicitly constructed legacy Settings
flags. A present RYNHULT spelling wins over ACC, including empty values. These values lock the panel. Otherwise
DB choice wins over the original default. Supported IDs are returned by the owner API. Qualification flags and
per-project choices remain independent checks. Telemetry sharing consent and remote-access apply remain separate.

One main process owns the effective snapshot, shared by API and background workers without opening nested
transactions. HA promotion reloads it before starting workers. Derived Settings retain dataclasses.replace
compatibility. Orchestrator routers remain registered for hot enable; the shared request boundary, dynamic tool
registry and per-slice checks refuse access while Off, including explicit deployment slice opt-outs.
