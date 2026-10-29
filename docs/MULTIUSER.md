# Multi-user architecture

This is the portable summary of the October 2026 multi-user implementation.
Deployment diaries, account inventories and host preparation records are private.
For current behavior, see [People and sign-in](wiki/User-Guide/People-and-Sign-in.md)
and [Security](wiki/Internal/Security.md).

## Trust model

- Administrators are trusted. Account separation is not a security boundary
  between people who already have administrative control of the same host.
- Agents are not trusted. An agent must not gain root, reach the dashboard's
  database or secrets, or access another account's sessions and provider logins.
- Provider subscriptions belong to a Linux account. Each person's agents run
  under a separate unprivileged agent account with its own broker and home.
- Services and agent workloads are separate. Sharing a project checkout does
  not grant an agent access to the service account's container runtime.

## Broker and host boundaries

Broker sockets live in protected directories. The dashboard checks the peer
credentials and records the executing account on each session. Separate run
folders, homes and tmux servers keep account state apart; accounts, not session
names or per-session secrets readable by the same Unix user, are the boundary.

Agent accounts have no administrative, container-runtime or privileged
supplementary groups. Explicit filesystem grants share only selected project
paths. Resource limits apply to the account's workload cgroup, and the Host page
reports account use.

Root helpers restrict the operations they accept instead of treating a
dashboard request as unrestricted root authority. Event ingestion validates
record shapes, native IDs and input sizes. Files in session folders are opened
without following links, with regular-file and size checks. Provider secrets do
not belong in command lines or launch previews.

## Users, ownership and shared resources

Owner and member roles determine dashboard visibility and authority. Each user
has an agent account. Projects, sessions, workspaces, keys, notifications and
requests retain ownership checks; the first owner receives pre-existing rows
during the historical migration.

Invite/reset links are expiring and single-use, with only their hashes stored.
Removing a user retires the username rather than making it available for a new
identity. Sessions are revoked on the relevant account/security changes.

Members request publishing; owners approve it. Project port reservations avoid
collisions. Shared catalog access does not permit members to rewrite the
installed catalog. Provider login and notification state remain personal.

## Group authority

Groups are an independent opt-in axis. Owners create/archive groups and appoint
maintainers. Membership changes are revision-fenced and preserve the required
maintainer and project-owner invariants. A personal project, transcript, key or
checkout does not become shared merely because its owner joins a group.

See [Groups](GROUPS.md) for the current execution and revocation boundaries.
