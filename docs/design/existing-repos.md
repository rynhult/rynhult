# Use existing repositories where they are

Status: **proposal for owner approval; design only**. Date: 2026-10-30.
Base: main `5e81a727`. Intended integration: the proposed rc.3 Security settings
work, not a separate exception to it. No feature, migration, helper or permission
change is implemented by this document.

## Problem, existing behavior and success

A person has an existing checkout, for example `/home/alex/src/demo-api`, and
cannot move it into Rynhult's default workspaces folder. The person wants to
register it, review what access agents receive, and start work without rerunning
an installer or discovering filesystem permissions through launch failures.

Today **Register folder** records a path without moving it. Brokers separately
check configured allowed roots; Linux agents usually run as a distinct account
such as `alex-agent`, which also needs OS filesystem access. Root lists can be
set with `acc-agent-add --roots` or the node installer. These are two controls:
adding a broker root does not grant Unix permissions, and granting an ACL does
not authorize a project in the dashboard. Existing `import_mode` is
`managed | readonly`; `managed_checkout` separately records creation provenance
for safe deletion. Do not overload either field to mean “under the default root.”

Success: an owner adds one existing repo for one machine/account, sees and
approves the exact access changes, registers it without changing its location,
remotes, Git identity or working files, and can immediately launch a supported
session. Removal prevents later broker use and offers a separately described OS
access undo. A member or injected agent cannot approve either expansion.

The six requirement groups below come from `brief-existingrepos.md`. Proposed
defaults are recommendations, not accepted owner decisions. The approved direction
is a Security tab with per-guardrail toggles and Unrestricted mode; its detailed
scope and implementation still require agreement.

## R1 — one owner surface for allowed folders

**Proposed default:** **Settings → Security → Allowed folders** owns the editor.
**Host → [machine] → Agent accounts** links into that same editor with machine and
account selected. No second implementation of these settings on Host.

Show machine name and identity, account name, current broker connection/version,
policy source and effective revision. List each allowed folder with **Configured**,
**Access checked**, **Needs access**, **Pending application**, or **Unavailable**,
plus dependent projects/sessions. Never render a connection error as an empty list.
Keep the existing new-project destination explicit: adding/removing another folder
does not silently change the first/default workspace root.

**Add folder** opens the standard non-dismissable form dialog. The owner selects
an enrolled machine and an existing assigned *agent* account, enters one absolute
folder, and selects **Check folder**. Check is a bounded metadata/access check, not
recursive content indexing. An owner can enter a known folder even if the agent
cannot traverse it; only the proposed privileged metadata check may diagnose why.
Members cannot browse other homes or check arbitrary paths. No arbitrary remote
filesystem browser, shell command field or password field is introduced.

The preview identifies the canonical folder and owner, proposed root scope,
filesystem/ACL capability, affected projects and required ancestor traversal.
Prefer the exact repository directory; explain that allowing its parent also
allows future child repositories. **Allow folder** changes only broker policy.
If OS access is missing, offer the separate R2 **Grant access** step. Agent
requests may raise a need card; they cannot invoke the mutation or self-approve.

**Remove** shows projects, schedules and active sessions that depend on that root,
including roots overlapping it. Offer cancellation or **Stop affected sessions and
remove**. Freeze new launches, resumes, schedules and helper starts in the affected
scope; require confirmed stops before removal, or leave it pending. Existing
processes do not lose their OS access merely because broker configuration changes.
Re-evaluate overlapping grants: a child still covered by another root is explicitly
shown as still allowed. Do not delete or unregister any project implicitly.

All mutations require an authenticated owner, normal CSRF/origin protection and
recent reauthentication. Members may see an actionable “Ask an owner to allow this
folder” for their own project, without other users' roots or metadata. Session/MCP,
agent-account and installation tokens cannot mutate Security settings. The root
worker's narrow protocol has no direct agent socket or sudo entry.

### Root helper and durable policy

Propose a dedicated, root-installed `acc-folder-policy` worker, dispatched through
the existing server-to-root request pattern and the authenticated machine link.
Do not rerun the whole account/node installer or expose arbitrary root commands.
Its request is a bounded structured record: operation ID, enrolled machine ID,
assigned account ID/UID, expected revision, add/remove/check, literal path, preview
identity/digest and expiry. Resolve account mapping on the target from trusted
configuration; a client cannot supply a privileged UID or a different services
account. The request contains no secrets or shell text. Validate again at execution.

The root-owned machine policy is enforcement authority; the DB is a revisioned
mirror of desired/applied state. Initially import the existing root list without
broadening it. Flag legacy unsafe entries for review; do not silently rewrite or
claim they satisfy the new validator. Keep installer compatibility with the same
policy writer so it cannot overwrite later UI changes using stale defaults.

Write only a dedicated root-owned policy/config fragment using exclusive staging,
0600 permissions, fsync, same-directory atomic rename and parent fsync. Preserve
unrelated broker settings. Use an operation lock and compare-and-swap revision;
apply only the whole reviewed root set. Broker reads trusted applied policy,
acknowledges that revision, and rechecks policy on every filesystem operation.
If live reload is unsupported, require a scoped account-broker restart only after
all its sessions have stopped. Show pending until target and broker acknowledge;
an offline machine never shows a speculative change as applied. Retries are
idempotent, and failure retains the prior policy. A reconnect with a different
machine identity invalidates the preview.

### Path validation that remains mandatory

Validation belongs on the machine that owns the filesystem, not on main's view of
another machine's path. The policy writer must:

- Require an existing absolute directory, bounded bytes/components and UTF-8;
  reject NUL/control characters, `..`, shell expansion, `~`, environment syntax,
  empty components and colon/newline config injection. Serialize as structured
  paths; never turn a list into shell or environment-file text without escaping.
- Open directory components relative to a pinned root with no-follow semantics;
  retain descriptors through mutation. Reject user-controlled symlink components
  and linked leaves, including raced replacement. Permit platform system aliases
  only via a small qualified alias map (for example macOS `/var`), resolving under
  the pinned host root with trusted owners. Compare actual device/inode identities
  at preview/apply; a text prefix or resolve-once/reopen sequence is insufficient.
- Deny `/`, system directory trees (`/etc`, `/root`, `/proc`, `/sys`, `/dev`, `/run`,
  `/boot`, `/usr`, `/bin`, `/sbin`, `/lib*`), mount roots, temporary roots, service
  accounts' homes, and the Rynhult application, run, state, backup, config and
  data/secrets trees on that machine. Discover configured locations, not just
  `/srv/acc/data`. Deny any proposed *ancestor* that would expose a protected tree,
  including `/srv`, `/var`, `/opt` and `/home` as broad roots.
- Allow an exact subtree of the explicitly associated human owner's home, such as
  `/home/alex/src/demo-api`, but never that entire home or another user's home.
  Reject credential/config trees (`.ssh`, `.gnupg`, `.aws`, provider auth/config,
  browser profiles, etc.) as grant roots. Same rule for an agent's home: only a
  dedicated repo subtree, not the home/auth tree. Other-user shared repositories
  require explicit host-administrator provisioning, not an owner UI shortcut.
- Require repository content to be owned by the associated human or target agent
  for automatic grants; otherwise diagnose and defer to host administration.
  Reject special files, hard-linked regular files, bind-mount aliases of protected
  paths and filesystem/mount crossings for automatic recursive access changes.
  Capture mount identity and refuse if safe ancestry/alias checks are unavailable.
  Do not claim this can discover every administrator-created copy of a secret.
- Treat Git administration separately: inspect `.git` directories/files without
  running checkout code; resolve linked-worktree common metadata and object/ref
  stores against explicitly approved policy. Refuse outside common dirs,
  alternates and metadata links unless separately approved and validated. Never
  run Git, repository hooks/filters, npm or a repo-supplied executable as root.

Mandatory root-helper restrictions survive R5's toggle and Unrestricted mode.
A broker path guard is not a filesystem sandbox: native agent processes can
access anything their OS account can access, irrespective of broker root lists.

### Audit and failures

Record request, preview, approval, applied revision, refusal, conflict, partial
access change, rollback and removal. Suggested actions: `security.folders.*` and
`security.access.*`. Include actor, machine ID, account/UID, operation ID, canonical
root, identity/digest, old/new policy revision, method, permission-change counts,
affected sessions and outcome. Record security-toggle changes separately.
Do not log file contents, `.env` values, raw ACL manifests or Git URLs containing
credentials. Detailed ACL rollback manifests are encrypted/private service state;
owner-only audit paths must not leak into member events or agent launch context.
Show a concise safe failure, retain the draft, and offer a fresh preview after
identity or permission changes. Partial grants have an explicit repair/undo state.

## R2 — guided Grant access

**Proposed default:** no OS changes when existing access already works; otherwise
named-user ACLs for the one agent, on Linux filesystems that pass ACL qualification.
The alternative is a dedicated shared group with setgid and default ACLs. Do not
add an agent to the human's primary group, sudo/admin/docker groups, or unrelated
existing groups. Do not use recursive chmod 777, chown the checkout to the agent,
or give directory write permission merely to make an ancestor traversable.

Before approval show the root, account, read/write choice, affected item count,
parent traversal entries and exact permission diff. Default to read/write only
when the owner explicitly chooses agent editing. Read-only registration offers a
read/traverse grant. An execution budget caps traversal at 100,000 entries, depth
64 and 30 seconds per preview; exceeding it requests a narrower directory/manual
provisioning, never a partial successful preview. Recheck the preview at apply.

Display this consequence before **Grant access**:

> alex-agent will be able to read everything in this folder, including .env files,
> keys and untracked files. Write access also permits changes to Git metadata and
> hooks. Secret-read guardrails are a safety net, not a filesystem boundary. Moving
> secrets out of this folder is safer. Removing access cannot recall copied data.

Do not scan/read secret contents to produce this warning. Default ACLs apply to
new children, but applications can create restrictive modes or replace/move files
with different ACLs; access must be checked again rather than promised forever.

### Exact privileged operations proposed

These are **design contracts**, not commands to execute for this task. All values
below come from a reviewed target-local preview, never shell interpolation.
Use a fixed root worker with a sanitized environment and fixed executable paths.
It walks pinned descriptors, only within the approved inode manifest, skips no
errors, does not follow links and refuses mount/hardlink/special-file surprises.
Root performs only the following operations:

| Method | Operations |
|---|---|
| Either method | Snapshot numeric owner/group/mode and access/default ACLs into a service-only rollback ledger. For ancestors below a protected boundary, add only the required named-agent `--x` traverse entry, never read/list/write or a default ACL. Refuse broad traversal changes if they would grant access to unrelated private trees. Root does not create missing project directories. |
| Named-user ACL | On each approved directory, add access `u:<agent-uid>:rwx` (read-only: `r-x`) and a matching default entry. On each regular file, add `rw-` (read-only: `r--`), retaining execute only where already executable. Apply explicit access/default masks computed from the preview. Preserve every existing principal's effective rights; if widening a mask would awaken another masked grant, stop and require a revised/manual plan. |
| Shared group | Create one purpose-specific unprivileged group, e.g. `ryn-repo-<id>`, using fixed-argv `groupadd -- <validated-name>`. Add exactly the associated human and agent using fixed-argv `usermod --append --groups <group> -- <account>`. For approved inodes only, use `fchown(fd, -1, gid)` to change group, then `fchmod` to preserve existing mode while adding directory setgid. Add a named group access/default ACL for the reviewed rights, with the same mask checks. Do not grant execute to previously non-executable files or alter user ownership. Refuse special/setuid files. |
| Policy | Atomically update only the dedicated root-owned folder policy/ledger; invoke a fixed service-manager reload/restart for the mapped broker unit if required. No user-selected unit names, executable paths, hooks or service environment changes. |

ACL operations must use descriptor-based ACL APIs (`acl_get_fd`, `acl_set_fd`, or
qualified OS-specific equivalents, including a safe default-ACL implementation).
The familiar Linux command equivalents are `setfacl -m u:<uid>:<rights>`,
`setfacl -m d:u:<uid>:<rights>` and group equivalents, with explicit reviewed masks.
**Do not implement these as root `setfacl -R <mutable-path>` or `chgrp -R`.** A
component swap between checking and traversal must never redirect root mutation.
The [ACL utility documentation](https://man7.org/linux/man-pages/man1/setfacl.1.html)
explains mask recalculation and that unsupported ACL filesystems can fall back to
mode changes before returning failure. The worker must probe capability first
and refuse such fallback; silently broadening mode bits is unacceptable.

A preview authorizes specific directory/file identities, not arbitrary later
objects at their names. Recheck before every mutation and stop on detected rename
or permission drift; do not let an untrusted request supply an ACL restore file.
Live external writers cannot be serialized by Rynhult's lock: show that the grant
is not a single atomic recursive transaction, use a progress ledger and reconcile
or undo partial work. Do not promise race-free bulk rollback without testing it.

Group membership takes effect only in newly started processes. Freeze account
launches and require its existing sessions to stop before refreshing that broker
and verifying memberships. Do not silently restart active terminals. Named ACLs
avoid that membership change but still need post-apply access verification as the
agent. Root never executes a project program as the access test.

### Undo and revocation

**Remove allowed folder** and **Undo access grant** are separate choices. Removal
always names whether OS access remains. Undo first freezes affected launches,
requires confirmed stops, and removes the helper's own grants using the protected
ledger. Restore an existing inode's prior ACL/group/mode only when its identity
and current permission state still match the recorded post-grant state. Otherwise
report a conflict; never restore stale ACLs over a person's later changes.
For newly created descendants, remove only attributable agent/group entries after
a bounded no-follow walk; do not invent a prior group/mode. List unowned changes
that need host-administrator repair rather than claiming a complete undo.

Reference-count ancestor grants and purpose groups across projects/accounts.
Remove membership with fixed-argv `gpasswd --delete <account> <group>` only when
this feature added it and no remaining grant depends on it; delete a helper-created
group only after no inode/other grant depends on it. Never delete a pre-existing
group. Do not use `setfacl -b` or wipe unrelated defaults. ACL masks receive the
same effective-rights preservation check during undo.

An agent may retain access via other groups, existing ACLs, ownership, Windows
permissions, open descriptors or copied data. Removing an ACL does not revoke a
previous read, and an account with write access may already have made changes.
Report measured remaining access, require stopping all affected account processes
for meaningful revocation, and label unconfirmed stops/revocation honestly.
No rollback deletes repository files, branches or untracked work.

## R3 — Import existing folder without moving it

Extend **New project** with **Existing folder**, reusing the current Register folder
flow rather than creating a competing importer. Steps: select machine/account,
enter path, check policy/access, review mode and Git state, register, then optionally
trust and launch. A member can register only an already allowed/access-capable
folder for their own account; only an owner can continue into R1/R2 approval.
Return to the import draft after access application, including offline/retry cases.

Keep two separate choices in the review summary:

| Choice | Behavior |
|---|---|
| Location: **Use where it is** (default) | Store its verified path; no move, clone, initialization, commit or cleanup of source. `managed_checkout=false`, including when agent editing is enabled. |
| Location: **Make a separate checkout** | Optional explicit alternative in the existing clone workflow. Explain that a clone transfers committed history, not dirty/untracked files. Never copy a whole private folder as an implicit access workaround. |
| Access: **Review only** | Existing `readonly` mode; read-only sessions, no stacks, worktrees/identity mutations, writes or automatic export into this folder. OS read access is still a prerequisite, not guaranteed isolation. |
| Access: **Let agents work here** | Existing `managed` capability mode; explicit trust still required. Does not transfer filesystem ownership or deletion provenance to Rynhult. |

Use plain wording; “managed” is an internal capability, not permission to delete
a person's imported checkout. Unregister/delete removes only its Rynhult record;
the original checkout and externally located linked worktrees remain untouched.
Agent worktrees created by Rynhult have explicit independent provenance and a
separate cleanup review. Archive never changes filesystem permissions.

Read Git status and remotes using hardened owning-account broker operations,
never as root. Redact embedded credentials before UI/log/audit; do not rewrite
existing remotes or strip credentials destructively. Show a warning and an explicit
repair action. Registration does not fetch, push, initialize Git, run hooks or
change global/local identity. Non-Git and unborn repositories can register, but
worktree/helper launch explains the missing commit and offers a distinct approved
initialization/first-commit action. Dirty/untracked work is shown without staging.
Treat repo hooks, submodules, filters and config as untrusted until explicit trust.

**Proposed default for editing sessions:** own worktree in the account's already
allowed dedicated workspace, e.g. `/srv/acc/workspaces/alex/.worktrees/<id>`, keeping
the human's checkout/branch unchanged. The source stays where it is; linked
worktrees share objects, refs and per-worktree metadata in its common `.git`, so
this still needs approved Git-metadata write access. Explain this before launch.
An owner may instead select a worktree directory under the explicitly allowed
source folder; no implicit sibling directory writes. Validate source/common Git
metadata and destination independently, and preserve existing worktrees.

**Work directly in this folder** is an explicit alternative, warning about the
human's dirty files and concurrent branch changes. Refuse incompatible concurrent
Rynhult writers; warn that external editor/Git processes are outside that lock.
Do not stash/reset/clean on launch. Git metadata path links must be pinned and
validated at mutation, including object/ref children, not only at registration.
Provider sandboxes get only qualified required paths; preserve Codex's narrowed
worktree-admin/objects/refs/logs grants, never broaden to hooks/config or a home.
Container sessions mount only the approved checkout/own copy and required validated
metadata; they do not inherit arbitrary host roots. Refuse unsupported combinations.

## R4 — platform behavior

**Linux main/nodes:** first qualified implementation. Use per-account root policy,
dedicated service/agent UIDs and the target-local privileged helper. No root agent,
sudo grant or arbitrary capability expansion. Network/ACL-less filesystems require
manual provisioning and an honest unsupported access-grant state, not chmod fallback.

**macOS node:** current user-mode node may share the person's UID; therefore “grant
access” cannot manufacture separation that does not exist. Show **Runs as alex;
not isolated from your files** and retain macOS privacy/TCC protections. First
release may edit folder policy through a qualified user-owned local policy service
but offers **Already accessible / Open local setup instructions**, not a remote
root ACL worker. An owner must not be able to use remote privileged access to
another Mac user's home. No Linux setfacl/group commands on macOS; a separate
proposal/qualification is required for native ACL inheritance, rollback, separate
UID operation and TCC-denied folders. Do not invoke sudo or disable privacy prompts.

**WSL node, Linux filesystem:** same qualified Linux protocol within the selected
WSL distribution; label the distribution and machine identity. Never infer a path
in another distribution from an identical `/home/...` string.

**WSL Windows drives (`/mnt/c`, etc.):** detect filesystem/mount type, not merely
that prefix. The [WSL permission documentation](https://learn.microsoft.com/en-us/windows/wsl/file-permissions)
explains Windows permission translation and optional metadata; Linux chmod/ACL
success cannot be advertised as Windows isolation. Allow narrowly scoped in-place
registration only when already accessible, owner explicitly accepts **Windows drive;
not isolated**, and machine isolation policy permits it. No automated recursive
ACL/grant or Windows ACL changes in the first release; prefer a separate Linux
checkout when compatible with the person's workflow. Do not require moving the
original. Worktree filesystem compatibility must pass qualification or be refused.

If dedicated-WSL isolation disabled automount or drive access, show **Blocked by
machine isolation** with a link to its separate owner controls. Adding a folder,
disabling Allowed folders or choosing Unrestricted must not remount drives,
reenable interop, edit wsl.conf, dismiss its warning or restart WSL. Any isolation
change remains a distinct owner-approved operation; the
[WSL configuration documentation](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
explains that settings apply after distribution restart. Recheck after reconnect;
never assume a pending isolation change has applied. Preserve decision 0073:
v1.0 is the Linux server used in browsers, with existing Linux/macOS/WSL nodes;
this design does not add a native Windows application or new platform qualification.

## R5 — fit the Security tab and Unrestricted mode

One **Allowed folders** guardrail, enabled by default, with a machine/account
policy editor beneath it. Explain **Limits the folders Rynhult's account service
will operate on. It does not sandbox native agent processes.** Keep **Secret
reads**, provider permission mode, OS access, project trust and node isolation as
separate controls; choosing one never implicitly changes another.

| Effective setting | Broker operations | Privileged access/helper operations |
|---|---|---|
| Allowed folders on | Require an applied allowed root for the requested machine/account; validate descriptor containment on each operation. | R1/R2 strict validation and explicit approval always required. |
| Allowed folders off | Owner explicitly permits broker operations on other OS-accessible repository paths for this scope, still enforcing mandatory protected-path/metadata checks and project authorization. No privilege escalation or OS grant. | Same strict validation; arbitrary paths never become eligible for root mutations. |
| Unrestricted | A named preset of the approved configurable guardrails, visibly showing its effective switches. It may switch off Allowed folders only through the same owner confirmation and revisioned enforcement. | Same strict validation, authz, audit, service isolation and owner approval; no arbitrary root execution or grant. |

**Proposed default scope:** toggle applies per machine/agent account, with an owner
summary of all affected projects. Do not let an agent, profile, schedule or task
select Unrestricted; no per-session widening over owner policy. A future Security
spec may choose a narrower scope, but cannot silently broaden this one. Store saved
roots independently, so turning protection back on restores them. No wildcard `/`
root is written to represent Off. Display the exception persistently in Security
and affected launch summaries with words and an audit entry; normal enabled state
is quiet. Disabling does not trigger ACL changes or trust a project.

Use recent reauthentication plus a consequence confirmation through `useConfirm`,
not a modal for every ordinary read. The confirmation names machine/account and
what remains protected. Existing running agents retain their original provider/
launch grants; a setting change does not widen a running session silently. Scope
revision fences prevent launches/resumes during an unapplied transition. Tightening
protection requires the stop/reconcile path from R1. Re-enabling a guard does not
recall data already read or revoke unrelated OS rights.

The mandatory broker denylist for system and Rynhult service credentials/metadata
is distinct from the configurable Secret reads shell guardrail. A repository
.env file is subject to that separate guardrail and its OS permissions, not a
promise of an immutable broker-wide secret filter. Native agents with sufficient OS access remain
able to bypass prompt-based secret-read checks, even with Allowed folders on.
The UI must not call this a filesystem jail or promise that Unrestricted is safe.

## R6 — threat model and acceptance plan

Trusted: the explicitly approving owner, target root/service installation and OS
account mapping. Untrusted: injected/hostile agent, member, repository contents,
paths and Git metadata, local concurrent writer, stale node observations and any
request originating from the private network without authentication. A compromised
node can lie about its own filesystem; it cannot select another machine/account's
policy or produce a main-authorized approval. Physical administrator/root compromise
and revoking copies already taken are outside this boundary.

| Threat / requirement | Required evidence before shipping |
|---|---|
| R1 authorization | Owner succeeds; anonymous/member/agent/MCP/installation tokens fail check/preview/apply/remove and toggles. Cross-account/machine IDs, CSRF, replay, expired preview and reconnect identity changes fail. Member errors expose no other-home existence or path listings. |
| R1 path escape | Table-driven rejection of system/secret paths, protected ancestors, other homes, malformed encodings, config delimiters, symlink chains, binds, mount crossings and linked Git metadata. Deterministic directory/metadata swaps at preview, open, grant and policy publication must not mutate an unapproved inode. |
| R1 durability | Inject disk-full/crash/reload failure; old root set remains authoritative until complete target acknowledgement. Duplicate operations idempotent, stale revisions conflict, old nodes refuse, offline pending honest. New-project default root unchanged. |
| R2 Linux access | Real disposable multi-UID fixtures prove read-only/read-write differences, ancestor traverse-only, nested files/default inheritance, umask and restrictive new files. Group activation only after stops. Mask changes do not awaken unrelated grants. Unsupported ACLs cannot change modes as fallback. |
| R2 rollback | Partial grant, changed/moved/deleted inodes, concurrent permission edits, hardlinks and nested mounts; ledger restores only unchanged managed entries. Shared grants not revoked accidentally; remaining access reported. Never remove repository content. |
| R3 happy path | Browser: existing repo outside default roots → owner allow → preview/grant → import where it is → trust/own-worktree launch with fake providers. Human dirty/untracked files, branch, remotes and identity remain unchanged; worktree commits work in qualified provider sandbox. |
| R3 misuse | Member request escalates only to owner; cancel/save/reload retains draft without mutation. Credential-bearing remote redacted. Outside common Git dir/alternates rejected. Readonly never launches editing helpers/exports. Delete/unregister preserves imported folder and external worktrees. |
| R4 platforms | Disposable Linux node and WSL ext4 multi-UID grant/undo; DrvFS with/without metadata and disabled automount/interop; existing-access-only Windows-drive flow. Real Mac privacy denial and same-UID warning, no Linux grant command. No fallback disables machine isolation. |
| R5 security integration | Every effective toggle/preset state tested against launch, resume, team helper, schedule, Files, preview/stack and Git paths. Owner off never grants OS access or relaxes root helper checks. Re-enable restores exact roots, transitions block stale grants, no agent-controlled widening. |
| Audit/resources/UI | Audit actor/revision/outcome complete without contents/credentials. Oversized/huge/deep trees refuse within budget. Fixed-argv commands resist leading dashes/Unicode/control injection. Keyboard/focus, phone layout, screen-reader status, error/retry/stale states use existing design primitives. |

Run implementation-specific Python suites with four workers and fake providers,
then focused browser specs on a disposable instance/own port. Root/OS-level tests
belong in disposable VMs, never the live installation. A browser grant simulation
alone is not permission/isolation qualification. The coordinator runs full gates.

## Proposed delivery and approval boundary

First coherent slice: owner Security editor, target Linux helper/policy revisioning,
named ACL and group grant previews/undo, and the in-place import journey. Include
existing-access-only Mac/Windows-drive paths with honest limitations. Do not mark
a platform's automatic grant supported until its multi-UID and race tests pass.
No new package installation, general file manager, arbitrary sudo command,
automatic secret relocation, repository move or global Git configuration editing.

Owner approval is needed for these **proposed defaults** before building: one
Security editor linked from Host; exact-repo roots; named-user ACL as the suggested
Linux method with shared group as an explicit alternative; policy toggle scoped
to machine/account; persistent worktree destination under the account's dedicated
workspace; existing-access-only Mac/DrvFS path rather than remote privilege setup.
The consent and undo limits above must be part of that approval, not hidden later.

After approval, the builder must update the Guide's Projects, Settings/Security,
Nodes, Install-Windows and relevant Mac instructions alongside the actual UI,
plus ownership/authz inventory and audit words. Do not publish proposed controls
as already available in the current Guide. Use existing DESIGN.md tokens,
Dialog/QueryState/ErrorNote/useConfirm primitives, status words, accessible focus
and phone layouts. Implementation may require a new decision record/schema; this
design authorizes neither a migration nor code. **Stop here for owner review.**
