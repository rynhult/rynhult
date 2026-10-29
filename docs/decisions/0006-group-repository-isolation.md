# Group repository isolation

Date: 2026-10-22. **Status: approved 2026-10-22 with the answers in 0008 (private decision record) (each group project has owners who approve merges); authority and same-host private exchange phases 1–2 implemented; group execution remains gated until phase 3.**

## Context

Group collaborators need a common project while retaining separate agent
accounts, provider logins and private work. Today's project binds one account,
path and machine. Linked worktrees share Git administration; writable shared
metadata would cross the account boundary, and filesystem ACLs do not solve
collaboration from separate machines.

## Proposed decision

Use private per-person/per-machine clones, each with per-session worktrees.
Publish selected committed refs through a service-owned ACC Git exchange.
Keep its fetch/publish authority separate from append-preserving backups.
Expose a sanitized, advisory ACC work board, not teammates' transcripts,
terminals, dirty files or keys. Full model, protocol, alternatives, migration
and approval questions: [GROUPS](../GROUPS.md).

## Consequences

More disk use, explicit sync and a new authenticated Git exchange are required.
Shared code is still untrusted executable input; private clones do not sandbox
it. Claims cannot guarantee exclusivity. Group key grants require their owner's
consent, and offline revocation cannot erase already delivered credentials.

## Alternatives considered

A writable bare repository with cross-account linked worktrees shares config,
hooks and administrative state. Unix groups with setgid/default ACLs also expose
dirty files in shared trees. Neither preserves the chosen account boundary or
works across machines without another protocol. Implementation is authorized by the owner answers in decision 0008. Phase 1
records authority and placements but leaves group execution disabled until private
clone isolation and Git exchange are qualified.
