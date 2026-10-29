# Organizations stable identity migration plan

Phase 0 only, 2026-10-24. No schema revision, data migration, rename, sharing, SSO or org activation is shipped.
Read [Organizations](ORGANIZATIONS.md) and
[Groups](GROUPS.md#permissions) before implementing this plan.

## Current identity boundaries

| Entity | Current identity | Migration |
|---|---|---|
| User | Integer `user.id`; mutable-looking username is also an ownership/actor key throughout ACC. Renaming is not offered; retired names cannot be reused. | Add immutable principal UUID, unique installation-scoped legacy integer mapping, status and authorization revision. Username becomes a display/login alias. Preserve retirement tombstones forever until a separate approved reuse policy. |
| Project | Integer `project.id` and unique slug; most relational references already use the integer. Backup identity falls back to `slug@created_at`. Paths, audit targets, catalog/agent inputs and Git metadata carry slugs. | Add opaque project UUID with explicit legacy mapping; backfill all integer FKs and untyped references. Preserve legacy API/path aliases during compatibility. Pin existing backup identity before any slug change. |
| Session | Opaque `session.id`; mutable `name` is presentation, native CLI ID is a different provider identity. | Keep existing opaque ID; qualify by installation/site on export. Never resolve a session by its display name or native ID alone. |
| Group | Opaque `acc_group.id`; name is presentation. Membership/project ownership already use group ID and integer user ID. | Preserve group ID; map user membership FKs to principal ID. Do not conflate organization teams, groups, orchestrator Team, or MultiRunEntry.group_id (a multirun). |

A UUID here is an opaque identifier, not a capability. Every API, websocket, worker and broker still checks scope.
The dry-run proposes deterministic UUIDs using a **saved installation namespace** and legacy primary key;
this makes repeat reports comparable without inferring identity from email, display name or account name.
Use a distinct namespace for each installation. A future transactional migration persists assignments once;
New identities after cutover must receive fresh UUIDs (for example UUIDv4), never a UUID derived from a possibly recycled integer key. Persist assignments before retry. Never generate a new principal on each login.
`@api` becomes a separate service principal, never a local human user; system actors need explicit actor types.
Unknown historical actors stay unresolved, not assigned to whoever now has a similar name.

## Relational username inventory

These are plain strings, not enforced User foreign keys. The dry-run reports null/resolved/unresolved counts
for every listed column present in the copy. Null authority is preserved (for example host-wide Inbox items
and group projects), never defaulted to the owner. `publish_request.requested_by` can be an agent/session
identifier: classify by provenance rather than blindly treating it as a username.

| Table | Principal columns |
|---|---|
| `project` | `owner` |
| `session` | `owner` |
| `multirun` | `owner` |
| `node` | `created_by` |
| `setting` | `updated_by` |
| `audit_log` | `actor` |
| `retired_user` | `removed_by` |
| `invite` | `created_by` |
| `attention_item` | `owner` |
| `publish_request` | `owner`, `decided_by` |
| `tool_request` | `owner`, `decided_by` |
| `installed_tool` | `owner` |
| `port_reservation` | `owner` |
| `push_subscription` | `username` |
| `model_endpoint` | `created_by` |
| `vault_key` | `owner` |
| `vault_grant` | `given_by` |
| `schedule` | `owner` |
| `acc_group` | `created_by` |
| `placement` | `person`, `approved_by` |
| `group_merge_approval` | `approved_by` |
| `group_key_grant` | `given_by` |
| `infrastructure_credential` | `owner` |

## References outside relational foreign keys

| Surface and implementation | Mutable references / required treatment |
|---|---|
| `auth.py`, `api/context.py`, `api/auth.py`, `api/users.py`, `api/passkeys.py` | Principal.username, login lookup, audit actors/targets, account removal/transfer, invite creator, recovery; change authorization comparisons to principal IDs while retaining entered aliases and historical labels. AuthSession/Passkey/Invite integer user FKs already avoid name joins. Never change WebAuthn user handles/RP IDs during backfill. |
| `api/groups.py`, `api/group_exchange.py`, `tools.py`, `vault.py`, `schedules.py`, `api/*` | Ownership queries, current-role lookups and group-exchange user resolution use usernames; update every producer/consumer together. Keep group-only and personal recovery authority distinct. Group role membership remains explicit; an org admin gets no implicit private access. |
| `setting.key` | `budget.<username>`, `tools.person.<username>`, `risk_labels.<username>`, `phone_banner.<username>`; create namespaced principal-ID keys and dual-read old keys temporarily, never regex-replace arbitrary keys. Project policy keys (`tools.project.<id>`) need project UUID mapping too. |
| `project.compose_pin`, `publish_request.context`, `audit_log.detail/target`, observations/events | Embedded actor, port owner, project slug, session title and composite targets are schema-specific. Add typed IDs alongside immutable captured labels; do not rewrite historical evidence. Audit target may be host/key/project/username, so preserve unknowns and use a sidecar migration map. |
| LaunchSpec.inputs/files, Session launch/routing snapshots, Schedule.launch, Team grants, MultiRun choices | Frozen launches contain owner/account, project slug/path and provider/native session metadata. Keep original bytes/digests; attach an ID binding sidecar for authorization, require a new launch when policy tightens. Validate schedule and pending-command project/person bindings before resuming. |
| `gitbackup.py`, `group_git.py`, `group_recovery.py`, backup manifests and `backup_server.py` | Legacy `slug@created_at` identity, integer-ID repository paths, private-group paths hashed from username, placement person and identity strings. Preserve explicit backup_identity; version repository binding metadata and keep alias mapping for historical snapshots. Never rename a backup directory without collision/restore verification. |
| Broker operations, supervisor, node links, hooks and request spool (`src/acc/broker/`, `src/acc/node/`, `bin/acc-*`) | Frozen environment variables, account/path selectors, session refs, launch receipts and agent `requested_by` strings; add versioned ID fields with authenticated bindings, not an unchecked caller-supplied username. OS account names remain placement attributes, not identities to rename. |
| Catalog YAML/JSON actor fields, export/download names, notifications and push payloads | Captured display labels are historical; add typed principal/project/session/group fields where used for routing. Recheck recipient access when delivering, not only when composing. No mass rewrite of arbitrary text. |
| Web API types/components and external CLI integrations | Usernames, project slugs, session/group names used in selectors, URLs, labels, API payloads and cached query keys. Keep labels; send IDs for authority. Version schemas, invalidate cached capabilities and scope queries by immutable ID. Route `/vault/people/{username}` needs a compatibility alias and a new ID-based endpoint. Workspace `{folder}` remains a confined filesystem selector, not project ownership. |

The source index below enumerates matching locations for implementation review. Generic `name` text in prompts,
Git refs and third-party artifacts is not an identity foreign key. Opaque JSON and external consumers cannot be
proven complete by textual scanning: each format above needs a versioned adapter and qualification fixture before cutover.

## Dry-run (a database copy only)

Obtain a consistent SQLite copy using the existing backup/capture tooling, not a raw copy of an active WAL file.
Keep the copy/report private: the report includes legacy identity mappings. From this checkout:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m acc.organization_ids /secure/example-copy/acc.db \
  --namespace e41c9c3b-4f72-4ba4-9834-e42af726a754 > /secure/example-copy/id-plan.json
```

Use your installation's saved namespace in place of the example. The command opens the input with SQLite
`mode=ro`, snapshots it with SQLite backup into memory, and only reads that snapshot. There is no apply flag.
Exit 2 means unresolved principal references; identity collisions raise an error. Missing inputs are never
created. The report lists assignments, preserved session/group IDs, affected principal-column row counts,
ID foreign-key targets/counts and name-keyed settings. Nulls do not grant authority. Manual-review surfaces
are always reported; exit 0 means no detected relational orphan, **not migration approval**.
PostgreSQL is the company pilot dependency; this SQLite-only analysis does not claim PostgreSQL migration
qualification. Use `acc-db-transfer`/the HA track's database tooling and an isolated PostgreSQL clone for that gate.

## Future expand / verify / contract sequence

1. Capture/version the full backup, recovery kit, database schema, source build, namespace and ID mapping.
   Rehearse [recovery](ORGANIZATIONS-RECOVERY.md) before touching authoritative data.
2. Add nullable stable IDs and mapping tables in an additive migration with org mode off. Snapshot counts,
   FK integrity and existing access decisions; no default group/team/SSO binding or new grants.
3. Backfill active/retired users and service identities; halt on unresolved or colliding ownership. Backfill
   projects, relational references, typed settings and versioned non-DB bindings in bounded resumable batches.
   Enforce one-to-one mappings and transaction/checkpoint consistency. Preserve names as display snapshots.
4. Dual-write typed IDs and legacy references, compare shadow authorization decisions and deny unexplained
   differences. Cover API, websocket, queued approvals, schedule resume, backup/private recovery, key delivery,
   node reconnect, search, export, push and in-flight commit/effect boundaries. Revoke stale capabilities.
5. Qualify restart/retry/concurrency and SQLite + PostgreSQL migration on copies; run all existing personal
   ownership tests and group viewer/member/maintainer tests. Verify byte-identical frozen launch files,
   no widened project/key visibility, retained dirty work and historical backup restoration.
6. Only with explicit owner activation and tested recovery owners switch authority to typed IDs. Stop writes
   while reconciling the final delta. Keep compatibility aliases and the reverse map for a defined window.
7. Drop legacy authority columns only in a later approved release after consumers and rollback window expire.
   After company work starts, rollback requires preserving that work and reconciling revocations, not simply
   running an Alembic downgrade. No downgrade or migration is implemented in phase 0.

## Source review index

Generated search scope: `src/acc`, `bin`, `web/src`, excluding historical migrations; username/owner/person,
project slug, session name and group name references. Line numbers identify the phase-0 baseline, not a promise
that future additions are covered. Re-run the search during implementation and review schema-specific JSON.

| File | Lines to review |
|---|---|
| `src/acc/backup_server.py` | 653, 657, 660, 681 |
| `src/acc/dns_helper.py` | 125, 138, 162, 168 |
| `src/acc/node/config.py` | 64 |
| `src/acc/organization_ids.py` | 18, 21, 22, 23, 24, 25, 27, 47, 54, 86 |
| `src/acc/server/api/accounts.py` | 82, 90 |
| `src/acc/server/api/auth.py` | 51, 58, 62, 91, 108, 109, 110, 111, 112, 113, 117, 122, 129, 142, 156, 158, 165, 168, 206, 219, 237, 239, 241, 243, 248, 255, 258, 263, 285, 310, 325 |
| `src/acc/server/api/backups.py` | 97, 120, 137 |
| `src/acc/server/api/board.py` | 28 |
| `src/acc/server/api/catalog.py` | 100, 119, 125, 130, 137, 156, 175, 184, 189, 191, 195, 201, 202, 207, 216, 221, 230, 234, 244, 247, 250, 253, 257, 258, 263, 264 |
| `src/acc/server/api/certificate.py` | 68 |
| `src/acc/server/api/containers.py` | 35, 38, 39, 44, 50, 56, 59, 65, 77 |
| `src/acc/server/api/context.py` | 68, 75, 109, 121, 122, 129, 159, 166, 167, 172 |
| `src/acc/server/api/dns.py` | 57, 63, 83, 116, 137, 181, 196, 378 |
| `src/acc/server/api/endpoints.py` | 209, 215, 216, 223, 227, 231 |
| `src/acc/server/api/files.py` | 34, 47, 49, 52, 53, 151, 154 |
| `src/acc/server/api/group_exchange.py` | 91, 134, 136, 198, 209, 211, 213, 215, 248, 252, 261, 265, 311, 319, 325, 332, 353, 361, 372, 376, 391, 406, 414, 421, 429, 430, 440, 446, 447, 480, 507, 528, 531, 533, 534, 538, 549, 556, 566, 574, 584, 588, 589, 607, 627 |
| `src/acc/server/api/groups.py` | 97, 101, 102, 112, 133, 139, 143, 161, 164, 203, 287, 288, 300, 309, 319, 320, 322, 326, 327, 337, 344, 355 |
| `src/acc/server/api/host_backups.py` | 41, 45, 59, 63, 70, 74, 78, 80, 84, 91, 94, 101, 103, 108, 110, 115, 117, 122, 124, 129, 131 |
| `src/acc/server/api/inbox.py` | 44, 57, 68 |
| `src/acc/server/api/issue_sources.py` | 76, 87, 99, 101, 107, 123, 124, 130, 136, 145, 192, 196, 209, 228 |
| `src/acc/server/api/multiruns.py` | 78, 79, 96, 97, 132, 136, 182, 189 |
| `src/acc/server/api/nodes.py` | 216, 223, 239, 241, 254, 267, 274, 281, 317, 324, 362, 372 |
| `src/acc/server/api/operations.py` | 35 |
| `src/acc/server/api/packages.py` | 117, 172, 195, 219, 223 |
| `src/acc/server/api/passkeys.py` | 52, 156, 162, 172, 186, 189, 193, 195, 210, 238, 246, 288, 289, 315, 325, 328, 353, 364, 367, 376, 382, 387, 389, 399, 401, 409, 412, 423, 424, 435, 436, 438, 439, 444 |
| `src/acc/server/api/ports.py` | 31, 34, 35, 57, 59, 64, 69, 71, 82 |
| `src/acc/server/api/projects.py` | 77, 79, 97, 112, 139, 160, 161, 222, 224, 290, 300, 330, 336, 347, 355, 365 |
| `src/acc/server/api/publishing.py` | 27, 34, 40, 47, 51, 55, 99, 100, 110, 112, 119, 123, 132, 134, 135 |
| `src/acc/server/api/pulls.py` | 74, 84, 107, 228, 234, 242, 245 |
| `src/acc/server/api/push.py` | 39, 53, 56, 58, 61, 64, 72, 75, 83, 86, 94 |
| `src/acc/server/api/schedules.py` | 40, 53, 64, 65, 66, 110, 126, 129, 144, 152, 153, 173, 176, 184, 185 |
| `src/acc/server/api/schemas.py` | 210, 231, 243, 259, 310, 311, 323 |
| `src/acc/server/api/sessions.py` | 77, 124, 160, 173, 179, 215, 219, 265, 298, 299, 311, 314, 325, 329, 363, 434, 444, 539, 543, 547, 569, 570, 575, 584, 601, 651, 655, 662, 716, 735, 740, 743, 753, 754, 760, 769, 778, 809 |
| `src/acc/server/api/settings_routes.py` | 53, 59, 60, 69, 76, 91, 94, 97, 105, 107, 108, 121, 124, 136, 139 |
| `src/acc/server/api/terminal.py` | 32, 33, 39, 43, 48, 59 |
| `src/acc/server/api/tools.py` | 39, 41, 56, 64, 72, 77, 79, 80, 81, 82, 89, 91, 92, 98, 99 |
| `src/acc/server/api/updates.py` | 35, 39, 55, 60, 61, 64, 66, 71, 87, 91, 104 |
| `src/acc/server/api/users.py` | 72, 80, 92, 97, 101, 102, 122, 133, 140, 143, 145, 146, 158, 163, 169, 174, 181, 183, 188, 193, 199, 201, 205, 208, 209, 216, 221, 237, 238, 239, 240, 242, 244, 245, 246, 254, 262, 263, 264, 274, 275, 281, 283, 288, 289, 291, 292, 295, 303 |
| `src/acc/server/api/vault.py` | 67, 68, 79, 85, 96, 105, 113, 125, 140, 200, 202, 209, 223, 224, 237, 247, 255, 258, 259, 261, 262, 266, 268, 270, 271 |
| `src/acc/server/attention.py` | 27, 31, 32, 44, 56 |
| `src/acc/server/auth.py` | 67, 68, 133, 134, 145, 146 |
| `src/acc/server/automation.py` | 134 |
| `src/acc/server/backups.py` | 95 |
| `src/acc/server/budgets.py` | 3, 66, 69, 72, 78, 82, 84, 87, 88, 125, 126, 129, 140, 143, 151, 152, 154, 165, 173, 174, 177, 187, 191, 194, 196, 200 |
| `src/acc/server/containers.py` | 56, 76 |
| `src/acc/server/export.py` | 13, 21, 24 |
| `src/acc/server/gitbackup.py` | 104 |
| `src/acc/server/ha.py` | 161, 227 |
| `src/acc/server/ha_runtime.py` | 87, 94, 96, 107, 115, 121 |
| `src/acc/server/history.py` | 148 |
| `src/acc/server/infrastructure.py` | 122, 131 |
| `src/acc/server/ingest.py` | 206, 222, 234, 260 |
| `src/acc/server/issue_sources.py` | 100, 123, 151, 161, 166, 296, 351, 459 |
| `src/acc/server/local_workspace.py` | 56 |
| `src/acc/server/models.py` | 20, 75, 128, 251, 272, 285, 289, 343, 428, 466, 572 |
| `src/acc/server/passkeys.py` | 19, 36 |
| `src/acc/server/polling.py` | 103, 105, 151, 152, 182, 183, 209, 211, 313, 315, 322, 323, 325, 326, 365, 368 |
| `src/acc/server/preview.py` | 80 |
| `src/acc/server/publish.py` | 172, 174, 176, 193 |
| `src/acc/server/publish_addresses.py` | 99 |
| `src/acc/server/pulls.py` | 261 |
| `src/acc/server/reconcile.py` | 209, 234, 321, 355, 413 |
| `src/acc/server/risk_labels.py` | 37, 38 |
| `src/acc/server/schedules.py` | 103, 127, 183, 190, 212 |
| `src/acc/server/supervisor.py` | 189, 199, 211, 212, 215, 220, 227, 247, 250, 260, 331, 901 |
| `src/acc/server/teams.py` | 191 |
| `src/acc/server/tools.py` | 30, 53, 54, 57, 58, 64, 74, 101, 118, 120, 121, 216, 220 |
| `src/acc/server/vault.py` | 264, 265, 268, 279, 299, 421, 423, 505, 506 |
| `bin/acc-guard` | 1484 |
| `web/src/App.tsx` | 60, 71 |
| `web/src/components/CommandPalette.tsx` | 16 |
| `web/src/components/Containers.tsx` | 77, 78, 93 |
| `web/src/components/GroupPublished.tsx` | 20 |
| `web/src/components/IssueSources.tsx` | 51 |
| `web/src/components/Keys.tsx` | 223, 316 |
| `web/src/components/Layout.tsx` | 46, 67, 68, 70, 162, 163, 208, 209, 253, 257 |
| `web/src/components/MachinePolicy.tsx` | 72, 74, 75, 95 |
| `web/src/components/Machines.tsx` | 202 |
| `web/src/components/NewSessionDialog.tsx` | 205 |
| `web/src/components/People.tsx` | 12, 91, 92, 97, 112, 125, 140, 141, 150, 155, 156, 157, 158, 160, 162, 164, 165, 167, 175, 178, 204 |
| `web/src/components/Tools.tsx` | 111 |
| `web/src/components/session-bits.tsx` | 124, 148, 163 |
| `web/src/lib/queries.ts` | 41, 42 |
| `web/src/lib/types.ts` | 184, 185 |
| `web/src/pages/Audit.tsx` | 28, 41 |
| `web/src/pages/Auth.tsx` | 31, 59, 76, 88, 111, 116, 128, 136, 151, 155, 160 |
| `web/src/pages/Groups.tsx` | 11, 18, 19, 20, 44, 54, 64, 72, 94, 103, 110, 112 |
| `web/src/pages/MultiRunPage.tsx` | 101 |
| `web/src/pages/Projects.tsx` | 92, 128 |
| `web/src/pages/Providers.tsx` | 132, 274 |
| `web/src/pages/Publishing.tsx` | 25, 81, 131 |
| `web/src/pages/Schedules.tsx` | 48, 63 |
| `web/src/pages/SessionPage.tsx` | 313, 320, 358, 415, 425 |
| `web/src/pages/Settings.tsx` | 117 |
| `web/src/pages/WikiPage.tsx` | 16 |
| `web/src/pages/Workspaces.tsx` | 107, 145 |
