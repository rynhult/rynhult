# Folder-policy worker — uninstalled, qualification pending

This branch installs or starts nothing. The worker, socket and service templates
are review material, **not a production enablement procedure**. Do not set
`qualified: true`, `shared_group_qualified: true` or `policy_qualified: true` on a real machine. Existing installer roots continue unchanged
until a separately provisioned target worker acknowledges a reviewed change.

The service runs fixed root-owned Python sources with `/usr/bin/python3 -I`, not
an editable application venv. The bootstrap pins its package directory and checks
ownership, types and writable bits. The root worker clears its environment and
accepts one bounded JSON request from the provisioned main/node **service UID**.
An agent UID, installation token or dashboard member cannot approve a mutation.
The node control channel carries the same record over its authenticated enrollment;
old/unprovisioned targets remain unavailable. macOS and WSL Windows drives have
no automatic grant support. No mount, interop or privacy configuration is changed.

## Provisioned inputs (not caller-controlled)

The proposed fixed locations are `/etc/acc/folders/worker.json` (root, 0600),
`/var/lib/acc-folder-policy` (root, 0700), the service-only socket
`/run/acc-folder-policy/helper.sock`, and immutable modules under
`/usr/local/lib/acc-folders/acc`. The account broker socket comes only from the root-owned account mapping
(`broker_socket`), never from IPC. Its parent/leaf are opened without symlinks and
its peer must be root or the mapped agent UID. The usual main socket is
`/run/acc-broker/<assigned-account>.sock`; node prefixes must be provisioned explicitly.
The fixed units are `acc-broker@<account>.service` and `.socket`; other service
layouts remain unsupported until separately qualified.

The root mapping contains `machine_id`, numeric `service_uid`, literal booleans
`qualified`, `shared_group_qualified` and `policy_qualified` (all false), and `accounts`. Each account maps its actual numeric UID,
associated human `owner_uid`, `broker_socket`, `broker_unit`,
`broker_socket_unit`, the exact human/agent `homes`, the complete discovered
installation/service `protected` directories, and existing ordered `roots`. The
first root is preserved as the default workspace. Never provision root/services
as an agent. The worker rechecks the account's passwd UID at execution. A production
provisioner still needs to derive and validate the associated human/group mapping
and all configured app/state/run/backup/config/service-home locations; guessed
`/srv/acc` paths are insufficient.

Root policy and ACL ledgers are exclusive 0600 staging files, fsynced and renamed
within the private directory, with parent fsync and an operation flock. Root
publication uses a **root-peer-only** broker frame; it is absent from the ordinary
RPC method map. The broker serializes publication against operations and checks
expected revisions. `ACC_FOLDER_POLICY_REQUIRED=1` would make a provisioned broker
refuse filesystem work after restart until root reloads it. Recovery must reload
and re-acknowledge, never return a cached success. Terminals and stop remain usable.

## Reviewed wire records

Every record binds `operation_id` (32 lowercase hex), `machine_id`, assigned
`account`, and expected `revision`; no UID, environment, executable, unit or socket
comes from a request. Extra fields refuse.

- Policy preview/publication requires literal `policy_qualified: true`; missing,
  false or truthy non-booleans refuse before preparing a privileged change.
- `policy_preview`: complete `roots`, `allowed_folders` boolean. Stores a five-minute
  metadata/inode preview without changing OS access. New roots require strict
  target-local validation; old installer roots are explicitly marked legacy.
- `policy_apply`: `digest`. Revalidates preview/identities, stages pending intent,
  publishes the complete root set, and records only the broker-acknowledged revision.
  Same-operation retries are idempotent and reload after a broker restart.
- `policy_recover`: original `digest`, target `recovery_digest`; `revision` is the
  abandoned publication's revision. A recently verified owner separately reviews
  the target's acknowledged roots/switch and confirms recovery. Under the worker
  lock, this closes the old intent before reloading only that acknowledged policy
  above every known abandoned/recovery revision. It remains available after a
  mapping change or withdrawal of policy qualification; it grants no OS access.
  The consent binds acknowledged state, current mapping and recovery progress;
  a changed target requires a fresh review. A lost reply is idempotent, and a fresh
  review after a refused recovery advances the revision instead of trapping it.
- `preview`: `path`, `method: named_acl | shared_group`, `write` boolean. Bounded no-follow metadata
  and ACL walk, no Git, hooks, filters, repository programs or contents as root.
- `apply`: `digest`. OS mutation requires literal qualification true. Uses pinned
  descriptors, Linux fd xattrs (no setfacl/chmod fallback), explicit mask checks,
  private progress ledger and ancestor reference counts. No execute is added to
  non-executable regular files; ancestors receive traverse only, no defaults.
- `undo`: `digest`. Restores only matching recorded post-grant inode/permission
  states; later changes, unknown descendants and uncertain syscalls require repair.
  It never deletes repository content or wipes unrelated ACL defaults.
- `status`: returns safe operation/revision/qualification state, not raw ACL ledgers.
  It expires policy previews before mapping checks and answers after a mapping
  change, reporting whether the old operation can complete. `policy_recovery: true`
  identifies the recovery-capable worker; new folder publications require it.
  The acknowledged roots/switch and `recovery_digest` form the owner recovery
  preview. Roots are never taken from the agent broker's ping for recovery.

## C2 access lifecycle

Access has its own `access-<account>.json` revision; it never advances the broker's
folder-policy revision. Apply/Undo stop the mapped socket and broker only after
all other account processes have stopped. Any remaining UID process refuses the
mutation. Main's durable pending operation freezes launch/resume/helper/schedule
and normal filesystem admission before IPC. Broker service restart failure leaves
partial/pending state rather than success; re-acknowledge any existing root policy.

The default ACL method and shared-group method each need independent literal
qualification. Shared groups use generated `ryn-repo-<operation16>` names and a
reviewed unused GID (60000–64999), fixed absolute `groupadd/usermod/gpasswd` argv,
no shell or request-selected executable. The worker journals creation and each
membership separately. It never adopts or deletes an existing group. An uncertain
syscall needs administrator repair, not guessed cleanup. Each disjoint grant gets
its own group: no group is reused across grants/accounts. Ancestor ACLs remain
reference-counted. Group ownership changes preserve the old owning group's
previous effective rights with an explicit named ACL entry.

Verification forks only fixed built-in metadata/open checks as the numeric agent
UID with freshly resolved groups. It reads/writes no file contents and executes
no repository program. Applied means that manifest passed these checks. Undo is
CAS against recorded post-grant metadata; unknown children or later permission
changes remain repair conflicts. Known helper-added memberships are removed only
when the grant has no outstanding inode conflicts. Empty created groups are
retained, since this bounded worker cannot prove no external inode uses that GID.
No groupdel or global filesystem scan is performed by the product.

`cancel` tombstones an unstarted preview under the same worker lock: a late apply
cannot follow cancellation. It refuses once any mutation might have started.
Lost replies use `status`; the server never interprets a prior applied grant as
confirmation of a new Undo. Original owners can recover already-approved grants
after fresh sign-in; unapproved old-browser previews remain unusable.

The service template intentionally creates **no per-connection mount namespace**:
PrivateTmp/ProtectSystem/ProtectKernel* bind/remount isolation would invalidate
stored mount identities across preview/apply processes. The root boundary instead
uses fixed immutable code, peer authentication, trusted mapping, mandatory path
bounds and retained descriptors. Qualify this tradeoff independently. Do not add
mount-namespace hardening without also designing stable cross-request identities.

## Qualification blockers / enablement boundary

This repository still supplies no automatic installer/enabler. Do not enable any
qualification flag on a real machine based only on unprivileged unit/browser
results. The coordinator must review installation ownership/mapping discovery,
installer coexistence, startup policy reload, HA authority, systemd/socket behavior
and mounts in disposable Linux/WSL VMs. Full R6 provider/worktree/platform evidence
from the earlier handoff remains required. macOS/Windows drives stay existing-access
only. No recursive grant can promise revocation of open descriptors or copies.

## Focused root qualification

**Only in a throwaway root VM, never this shared host or the live installation.**
From this branch's checkout with its development venv available:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q -n 1 --disposable-root-vm \
  tests/test_folder_root_qualification.py \
  tests/test_folder_group_root_qualification.py
```

The named-ACL cases use disposable numeric UIDs only. The group cases create two
uniquely named no-home accounts and one purpose group, then remove only their own
identities/tree/group. They start/stop no service. Run them sequentially (`-n 1`)
to avoid GID allocation races between disposable fixtures. Without root and the
explicit flag all four cases skip. They do not independently qualify deployment,
systemd stop/restart, mount namespaces, every crash point or the full R6 matrix.
No root tests or worker installation were run by the builder.

### Disposable-VM account-command exception

With the captured disposable-root opt-in **and actual EUID 0**, HostGuard permits
only the qualification fixture's fixed argv for `/usr/sbin/useradd`, `userdel`,
`groupadd`, `groupdel`, `usermod` and `gpasswd`, plus the lifecycle's existing
Debian `/usr/bin/gpasswd` path. No PATH lookup, shell text, extra flags or arbitrary
accounts/groups are allowed. Users must be `accq[ha]<8 lowercase hex>`, UIDs
61000–61999, primary GID 65534, nologin, no home. Purpose groups must be
`ryn-repo-<16 lowercase hex>`, GIDs 60000–64999. Membership/cleanup requires
creation argv authorized in this pytest worker. Fixtures separately require
successful creation or a confirmed private creation ledger before cleanup;
unknown/pre-existing groups are never adopted for cleanup. The exception never
authorizes systemctl, sudo, providers or other host control. Without both root
and captured consent all these real account commands, including gpasswd, refuse.

Coordinator evidence at `5814deaf`: named-ACL VM qualification **2 passed**; group
cases failed at the old command tripwire. This exception fixes that setup blocker;
the group qualification must be rerun in the disposable VM, not this shared host.

### Main/node service channel configuration

A node advertises the folder-policy capability only when its trusted enrollment
configuration explicitly sets `ACC_FOLDER_POLICY_SOCKET` to the qualified
target-local helper socket (normally `/run/acc-folder-policy/helper.sock`).
An unset value disables both discovery and requests; merely installing a socket
does not enable node access. This path is local service configuration, never part
of a primary's control-frame payload. Tests provision a disposable fake path.
The root peer check and bounded request protocol still apply.
