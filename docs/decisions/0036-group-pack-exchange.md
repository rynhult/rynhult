# Group exchange uses bounded, explicit commit packs

Renumbered on 2026-10-24 from 0010 to 0036.

Date: 2026-10-23. Status: accepted implementation of the approved
[Groups design](../GROUPS.md) and owner answers (private decision record).

## Context

Backup receive authorization permits append-only project recovery, not collaboration.
Source repositories belong to untrusted agents; their config, hooks, dangling objects
and uncommitted files must not enter shared views or another person's clone.

## Decision

Use authenticated Git HTTP upload-pack/receive-pack on the new exchange, backed
by per-request trusted staging and explicit commit packs at the trust boundary.
The existing authenticated API and peer-checked broker also provide bounded
pack helpers; neither transport exposes a filesystem remote or private checkout.
A 60-second, hashed operation capability binds group revision, project identity,
never-reused person identity, optional read placement, operation and exact ref
transition. Writers require their own reserved placement. Viewers can fetch with
no placement and cannot publish. Existing HTTPS/device/TLS policy applies to this
API; same-host brokers receive packs over their authenticated socket, without
remote URLs, Git credentials, new CA settings or the backup receiver's tokens.

Export through fresh Git metadata and an explicit source object directory; import
into temporary quarantine with strict object validation, after a declared-size scan
refuses decompression bombs. Promote only the selected commit's objects that the
destination does not already reach from its refs; never a second history copy. No arbitrary-object fetch,
contributor config, hooks, alternate object stores, ref namespaces, force or delete.
Apply subprocess time, memory, file, descriptor and output limits. Serialize Git
with the existing capture lease, taken before the database; fence current membership
in the database through each service-side Git write, and recheck it without SQLite's
writer for reads (security review groups2, 2026-10-23). Integration consumes an exact phase 1 approval
from a current named project maintainer. Exact-old-ref CAS remains a durable replay
fence if a crash separates Git from the database commit; reconcile ambiguous outcomes
by reading refs, never by overwriting them. Retain spent receipt rows so SQLite
cannot recycle their IDs into later approvals.

Each placement has a private clone root and **standalone session worktrees**. They
copy objects rather than sharing even same-person Git metadata. This deliberately
uses more storage than linked worktrees and avoids running a previous session's
modified config during checkout. Sync never checks out into an existing session;
there is no automatic merge, dependency install, submodule initialization or agent
launch. Recipient review before execution belongs to phase 3's launch gate.

Store group snapshots (published refs and safe metadata), private placement
recovery (explicit unpublished commits), and exchange repositories separately under
the existing Git capture root. The host backup server captures all three using its
existing retention, encryption and restore hold; group download authorization never
covers private recovery. Group restore creates a new branch and needs a fresh named
owner approval before reaching integration. No schema migration is required.

## Consequences

Clients can use standard Git push/fetch/clone with a scoped exchange grant, or
the documented ACC pack helpers. Advertisements and intermediate read negotiation
recheck current membership; a write grant is consumed on POST and a read grant
before its first pack is returned. Git protocol v0 is used initially. Receive-pack
updates only its exact granted ref in disposable staging, then promotes only the
granted history. Upload-pack sees only the selected published closure. The broker
helpers keep network credentials out of contributor-controlled Git configuration.

Initial limits deliberately suit small same-host projects:
8 MiB complete packs (at most 128 MiB per expanded object, 512 MiB per pack),
128 published refs, 128 MiB object stores (people's branches: 32 per person, at most
112 refs and 112 MiB, leaving the rest for maintainers), 1 MiB text views,
5,000 regular files per tree and a 64 MiB snapshot transfer budget. Local recovery
stores stop at 128 snapshots or their 512 MiB storage budget, reserving headroom
before each capture. Larger projects
fail explicitly and require a future reviewed streaming protocol. Local snapshots
are append-only; off-site pruning remains owner-only. This does not qualify Unix
account deployment or own-machine operation, retract previously copied commits,
sandbox published code, or enable group launches before phase 3.

## Alternatives

Serving the persistent exchange directly through unrestricted smart HTTP would
need careful upload-pack negotiation and leave rejected objects to consider.
Per-request staging limits both services to their authorized closure. Hidden refs
alone do not protect private objects. Shared writable Git metadata and reuse of
receive-only backup authorization remain rejected by the approved design.

Broker checkout also reserves expanded content before creating a working tree:
128 MiB of blob payload across paths (duplicate blobs count for every path),
5,000 total tree entries, 1,024-byte paths and 64 components. This bounds disk
amplification from small compressed packs; no oversized worktree is created.
