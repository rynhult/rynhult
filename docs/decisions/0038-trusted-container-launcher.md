# 0038: Trusted container launcher authority

Renumbered on 2026-10-24 from 0012 to 0038.

Date: 2026-10-23. **Status: implemented** (owner answers of 2026-10-23 on [CONTAINER-LAUNCHER](../CONTAINER-LAUNCHER.md)).

## Context

Rootless Podman can't map a session's user namespace from inside the broker, which runs with `NoNewPrivileges`
(the Phase 0 finding). The approved note asks for a small root helper with a fixed protocol, and says UID alone
can't identify a broker: an account's broker and its agents share that UID. It asks for "a separate broker control
identity or an equivalent protected, session-bound control-plane authorization channel" before the endpoint is used.

## Decision

- **One root process per request** on `/run/acc-containers.sock` (systemd `Accept=yes`), standard library only,
  installed to `/usr/local/sbin` like the other root helpers. The request has exactly six fields; no argv, paths,
  mounts, environment, images or unit properties.
- **Two independent checks of authority.** (1) The caller is the broker: its `SO_PEERCRED` uid is the account's and
  its pid is the `MainPID` systemd reports for `acc-broker@<account>.service`; agents are the broker's descendants,
  never its main process, and Yama (`ptrace_scope` 1, Ubuntu's default; checked on this machine) keeps them
  from ptracing it. The qualification run should confirm the value on each host. (2) The session is authorized by the control plane:
  accd (user `acc`) writes a grant for this session and generation into a folder only `acc` can write. The grant
  supplies account, image policy, mounts, command and limits. A compromised agent can neither write a grant nor be
  the broker's main process; a compromised broker can only launch what accd granted for its own account.
- **Podman runs as the agent**, in a system unit made with `systemd-run --uid` and `Delegate=yes`, never as root.
- **Limits live in accd** (Settings, per agent account, audited) and travel in the grant; the helper re-validates
  ranges and refuses missing values. `null` is "no limit", the owners' default.
- **No fallback**: any refusal is the launch's error. Reconcile, not the broker, is the backstop for cleanup.

## Consequences

The broker needs no new privilege and keeps `NoNewPrivileges`. The agent account and broker still share a UID, so
an agent that could make the broker's main process send arbitrary requests could ask for a stop or status of its own
sessions, never a launch accd didn't grant. The control plane must keep `grants/` unreadable and unwritable to agents
(0750 `acc:acc`). Disk is enforced by polling (within a minute) because session checkouts are host bind mounts
without quotas. The broker now runs git in container checkouts only with a config allowlist (the container can write
`.git/config`).

## Alternatives considered

- sudo rules or a generic `systemd-run` wrapper: the broker would choose argv and properties.
- A separate broker control user: a larger change to every broker unit and node installer; the grant plus `MainPID`
  gives the same separation for this endpoint now. Revisit if brokers ever run more than one main process.
- `systemd-run --user` through the agent's user manager in production: user managers aren't an authorization
  boundary and agents can talk to them. Kept for qualification only (`qualify_containers.py --user-manager`).
- accd calling the helper directly: the owner approved "only an account's broker may ask"; the broker also owns the
  tmux pane the container attaches to.

## Security review amendment (2026-10-23)

Mount descriptors now feed PID 1's private staging binds rather than only a post-start identity check. Host Git
reads use bounded no-follow snapshots and host Git writes are refused. Cleanup never removes a container by
forgeable names or labels: `--rm` handles normal exit and leftovers require owner review. Operations are serialized
per session. The root helper has its own aggregate resource slice and separate bounded refusal/lifecycle audit
streams, ingested even with the launch flag off. The default network is enforced as `none`. See `docs/reviews/2026-10-23-security-review-container-launcher.md` in the internal checkout for evidence and remaining qualification gates.
