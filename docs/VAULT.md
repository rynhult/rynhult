# Key vault (M13)

Approved by the owner, 2026-10-22: API keys are stored once, named, and given to projects. They must not leak. Any
signed-in person can add keys; the key's owner chooses **private** (only theirs) or **shared** (owners and members may
use it too). Keys can be given to existing and archived projects. Sessions on other machines get them only when the
owner allows it.

## What a key is
- **Name** (shown everywhere, e.g. "OpenAI (work)"), **variable** (the environment variable sessions see, e.g.
  `OPENAI_API_KEY`: `[A-Z_][A-Z0-9_]{0,63}`, not one of ACC's or the shell's own such as `PATH`, `HOME` or
  `ACC_*`), **value** (write-only), **owner**, **sharing** (private / shared), **other machines** (allowed or not,
  off by default), optional **note**, created/updated/last-used times.
- A key is given to projects, any number of them, including archived ones (their sessions can be resumed). Giving a
  key to a project is a separate step from adding it, and it's audited.

## Who can do what
| | Its owner | Others, if shared | Others, if private |
|---|---|---|---|
| See that it exists (name, variable, note) | yes | yes | no |
| Give it to projects they can launch in | yes | yes | no |
| Replace its value, rename it, change sharing, delete it | yes | no | no |
| Read its value | **nobody, ever**: there is no endpoint that returns a value, not even to its owner |

ACC's views don't show owners members' private keys: their names and variables stay out of the key lists, the audit
log (which records a private key by id only) and launch errors. An owner can still delete any key, for example when
someone leaves; it's audited. That is not a boundary against owners, who are fully trusted: an owner can attach
read-write to a member's terminal (where the session's environment, keys included, can be printed), and removing a
member can transfer their keys to the owner. Treat a private key as hidden from other members, not from owners.

## How a session gets keys
- **Issue-source exception:** project keys selected for GitLab or Linear are marked server-only on that project's
  grant (migration 0030). They are excluded from environment delivery, key context and MCP resolution. Disconnecting
  the source keeps that purpose; removing and explicitly giving the key again resets it. Existing running sessions
  retain previously delivered environments until stopped/resumed. Use dedicated source credentials. Every source
  request checks the person's vault access and project grant before decrypting or consulting the short cache.
- At launch and at every resume, the keys given to the session's project that the person *launching* may use
  (theirs, or shared ones) go into the session's `session.env` (0600, written by the broker as the agent account,
  as MCP secrets and endpoint keys already are). They never go on a command line, into the stored launch spec, the
  database's session rows, logs, the audit log or events.
- If two keys use the same variable, the launch is refused with both names, rather than ACC choosing one.
- **Context:** the session's instructions get one short paragraph listing the variables and names it has (e.g.
  "`OPENAI_API_KEY`: OpenAI (work)"), never the values, so agents know what they can use.
- **Other machines:** a key is sent only if its "other machines" switch is on. It travels over the machine's signed
  link inside the launch request, into the session's 0600 `session.env` on that machine. A machine can read the keys
  of the sessions it runs; that's inherent, and it's why the switch exists.
- **Only while it runs (D21):** when a session stops, exits, needs recovery or is archived, the broker removes its
  `session.env` and the run folder's files that carry secret values (the providers' MCP configs: `mcp.json`, Cursor's
  and Antigravity's plugin MCP files; `Provider.secret_files`). A resume writes them again. The removal is queued in
  the observation `env_cleanup` in the same transaction as the state change, so a machine that is offline (or an
  accd restart) gets it as soon as the supervisor can reach it. Backups never hold them: `acc-backup` doesn't copy
  `/srv/acc/run`, and its mirrors skip `session.env`.
- **Not a boundary within one account:** all of one person's projects run as the same agent account, so a session can
  read the files of that person's other *running* sessions (the guardrail blocks the direct read; a script the agent
  writes could still do it). Giving a key to one project keeps it out of other projects' environments, not out of
  reach of the same person's other agents.
- **MCP server secrets** in the catalog become references to vault keys (`acc-secret:{mcp}/{key}` →
  `vault:{key id}`), with a one-time migration of existing values into the vault, owned by the person who set them.

## Storage
- Values are encrypted with AES-256-GCM (`cryptography`), a random nonce per value, and the key id plus the variable
  as associated data, so a value can't be moved to another row. The data key lives in
  `/srv/acc/data/secrets/vault.key` (0600, user `acc`). It's created at install, never in the database, and backed up
  with the other secrets (deploy/backup), not with the database alone. It is written to a temporary file and linked
  into place (`deploy/lib/vault-key` at install, `Cipher._create` in accd), so a crash never leaves an empty key. The
  installer refuses to make one while `acc.db` already holds keys (a database restored without `secrets/`): restore
  `vault.key` from the same backup instead.
- Being honest about what this protects: the database on its own (a copy, a backup without the secrets folder, an
  export) reveals no vault values (model endpoint keys and TOTP secrets are other tables, still plain text). Someone with root on the main machine can read everything, as with any self-hosted
  secret store.

## Not leaking
- Values never appear in API responses, logs, the audit log, launch specs, session exports, search or Activity.
- **Redaction:** before ACC stores or shows text an agent produced (transcripts for search, session exports,
  Activity's "doing now" text, Changes diffs shown in the UI, hook payloads), any value of a key the session had is
  replaced with `«key: NAME»`. Values shorter than 8 characters are left alone, since they would match too much.
  Also matched: the value percent-encoded (`quote`, `quote_plus`), in base64 (both alphabets, at all three byte
  alignments, so inside a longer encoded string such as an HTTP Basic `user:value` credential), and cut short (a
  prefix of at least 12 characters and half the value, as a truncated log line leaves it). Not matched: a value split
  across lines, or encoded some other way (hex, a JSON `\u` escape, compressed).
- The copy accd keeps of a remote session's hook spool (`data/remote-spool/<id>/events.jsonl`) is redacted line by
  line as it is written; only whole lines are copied, and the node's offset is kept beside it (`offset`). The raw
  `events.jsonl` in a run folder is the agent account's own file and isn't rewritten.
- **Replaced and deleted keys (D22):** a running session keeps the value it was given until its next launch, and its
  transcript keeps it for good, while history is re-indexed from transcripts whenever a session's activity or state
  moves. So replacing a key's value, or deleting the key, first moves the old ciphertext (still sealed with the key's
  id and variable) to `vault_retired` if any session lists the key, and redaction uses those values too. Retention:
  a retired value is dropped once no session lists its key (sessions are archived, not deleted, so in practice it
  stays as long as their history does). Replacing a key in ACC doesn't revoke the old credential at its provider;
  do that there.
- Guardrail: reading `session.env` directly is blocked (it joins the secrets rule). Using a variable in a command
  (`curl -H "Authorization: Bearer $OPENAI_API_KEY"`) is fine, and that's what keys are for.
- An API response that would carry a value fails a test: one test walks every route and model and checks for a
  canary value.

## Adding keys
- **Settings → Keys:** add one (name, variable, value, sharing, note). Add and Edit offer common variable names
  grouped by provider, each with a short description; visible stored variables are marked **you have one** but
  remain selectable. **Custom** shows the existing free-text field with the same normalization and validation.
  Suggestions and the client-side name pattern live in `web/src/lib/envVariables.ts`; New session reuses their
  descriptions in its key summary. These are suggestions, not provider configuration or automatic credentials:
  GitLab/Linear connections still select a vault key explicitly, and `GOOGLE_APPLICATION_CREDENTIALS` is a path
  to a file that must already exist on the session's machine, not the file contents.
- **Paste a `.env`** is unchanged: it's parsed into a
  preview of names and variables, the values are never shown back, and you choose which to add. Comments and
  `export` are allowed, and quotes are handled.
- **Project page → Keys:** the keys given to this project, with Give and Remove. Archived projects show it too.
- **New session:** shows which variables the session will get (names only).

## Audit
`vault.add`, `vault.replace`, `vault.rename`, `vault.share`, `vault.remote`, `vault.delete`, `vault.give`,
`vault.take`, plus a `vault.use` per launch that lists the key names (never values).

## Infrastructure credentials (DNS phase 1, decision 0010)

Infrastructure Keys use a separate `infrastructure_credential` table, not an ordinary private/shared `vault_key`.
Their IDs cannot resolve through project/group grants, MCP, issue sources, session environment/context, exports or
remote delivery. Only signed-in browser owners can list their metadata or mutate them; each mutation confirms the
current password, with throttling and normal CSRF/Origin checks. There is no reveal/value-read/export route.

The AES-GCM associated data additionally binds provider, immutable certificate/Publishing purpose, field identity
and revision. Separate certificate and Publishing entries are required. The shared data-key creation guard counts
both namespaces, including during installation; a restored database without its vault key cannot create a replacement
over existing infrastructure ciphertext. Replaced encrypted generations expire after 24 hours and are purged by
accd independently of session history. The infrastructure redactor includes current and retained previous values;
provider/helper errors use fixed categories, with no raw body/config/diagnostic text returned or audited.

Settings stores confirmed zone/provider bindings and desired/applied revisions, without duplicate values. Only the
certificate-purpose value crosses the authenticated private root-helper channel; Publishing's separate consumer
cutover remains phase 2. Root and accd remain trusted. Caddy necessarily receives certificate credentials, through
root-owned 0600 generations and a systemd EnvironmentFile read as root. Caddy subprocesses for adaptation/validation
and reload explicitly run as caddy, without supplementary groups; the fixed controlled restart remains privileged.

Migration is explicit, idempotent and write-only: read only the fixed legacy files after descriptor/owner/mode/link
checks, never source them, preserve separate values and never overwrite an earlier import. Import/save alone do not
activate Caddy or delete legacy files. Prepare the managed persistence drop-in (with optional legacy fallback) before apply, then verify staging
issuance, production health and reboot persistence; remove the legacy certificate file only after the 24-hour rollback window and revoke
old tokens at the provider. Publishing's legacy file remains until its phase 2 cutover. Old backups need retention
review. Fresh installations keep internal CA without a DNS token.
