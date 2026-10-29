# 0042: Container qualification uses real CLIs and the existing launcher authority

Renumbered on 2026-10-24 from 0013 to 0042.

Date: 2026-10-23. Status: implemented as an opt-in harness; **not runtime-qualified**.

## Context

Phase 0 can prove rootless runtime availability but cannot prove provider hooks
or the production launcher. The deployed helper accepts only its registered
broker's MainPID and protected control-plane grants. A standalone agent script
cannot legitimately impersonate that broker. Default production sessions must
remain unchanged while the owner collects evidence.

## Decision

Use an owner-invoked, non-root `acc` driver to create unique temporary grants and
call the installed broker's existing RPC operations. Run the driver and its
PYTHONPATH code from reviewed root-owned source, with protected plan/result paths;
workspace data in the plan does not become privileged Python source. A separate root-owned
`qualification-` image policy maps the existing Codex provider slot to the
image's fixed Python qualification entrypoint. The helper protocol, broker,
production policy and `ACC_CONTAINERS` flag are unchanged.

Run real pinned Claude, Codex and Copilot executors against a container-loopback
scripted model transport. Require the exact guard custom denial, absent forbidden
marker, successful allowed command and post-tool event. Never count direct
helper invocation as provider qualification. No real credentials or model service
are required by this offline fixture.

Every mandatory item is pass/fail/skip with a reason. GO requires all mandatory
items in both generations and trusted launcher/cleanup evidence. Missing,
malformed and skipped results are NO-GO. Keep the immutable-effective-inputs gate:
root-owned bounded/hash-checked input snapshots are mounted read-only, with only
exact events/statusline outputs writable. HOME, Codex hooks/trust and Claude/Copilot
machine-policy hooks are immutable image files. Claude/Copilot config/state directories
are separate tmpfs paths; Codex uses explicit state links. Real qualification with
hostile user hook-disable settings must still demonstrate the mandatory guard. Active kernel mutation attempts
are required, rather than inferring immutability from helper binaries.

## Consequences

The owner can run the harness without enabling containers for normal projects.
The selected multi-stage image has been built and run locally in the user manager
(1.47 GiB). Local mode always skips trusted-launcher evidence. Provider hooks and Codex trust now pass. Nested proc mounting and Chrome chroot
failures keep the recorded result NO-GO; no proc-unmask, seccomp-unconfined or
capability workaround is permitted merely to produce a pass. The replay transports
are integration fixtures requiring owner validation with these exact CLI versions.
Session recreation does not qualify broker restart/reboot, live authentication,
credential refresh, two-session adversarial staging, hard disk quotas, collector
or later platform/network/GPU gates. A scoped GO is never production approval.

## Alternatives considered

- Calling the root helper directly from an agent: violates MainPID authentication.
- Adding a qualification bypass/root protocol: enlarges privileged authority.
- Calling hook scripts directly: does not demonstrate provider invocation/trust.
- Requiring live model APIs in `network=none`: incompatible with the agreed
  network gate and unnecessarily introduces provider credentials into fixtures.
