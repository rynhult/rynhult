# Container phase 0 qualification

For a fresh owner/root qualification window, the
[owner kit](../containers-qualification/README.md) wraps preflight, reversible
setup, root-captured image proof and this trusted-launcher driver. The manual
procedures here remain the reference; do not mix manual mutations into kit state.

This is a disconnected experiment, not a session launcher or a qualified image.
Run from this checkout as the agent account being qualified, never as root.
No live ACC data, other accounts, runtime stores or sockets are needed.
The default is OFF and exits 2 without running Podman:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py
PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py --enable-prototype
```

The enabled command uses a temporary rootless storage/run/config directory,
measures `podman info`, then seeds a private checkout of committed HEAD through a
bundle. It prints JSON, deletes scratch, and exits 1 (no-go). No images are pulled,
services started, machine settings changed or real credentials read. `go_phase_1`
stays false: this runner cannot certify real provider authentication or hooks.
Inside an ACC session the runner sits beneath the broker's `NoNewPrivileges`, where
`newuidmap` fails (the Phase 0 finding). `--user-manager` reruns it as a transient
service of the account's own lingering user manager (`systemd-run --user`), a new
process tree outside that restriction. An owner enables linger for the account
first (`sudo loginctl enable-linger <account>`); without it the runner refuses.
The report's `launcher` says which context ran (`user-manager` or `direct`) and
`apparmor_restrict_unprivileged_userns` records the sysctl at the time:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py --enable-prototype --user-manager
```

This qualifies the runtime context only. Production sessions never use the user
manager: they go through the root launcher (`deploy/containers/acc-containers`).
The full owner runbook is in [CONTAINERS](../../docs/CONTAINERS.md#qualification-run).

Run the unit tests separately with the required checkout import path:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_container_prototype.py
```

For the reviewed image built from `deploy/containers/Containerfile`, run the
real image probes in the delegated user manager without changing the broker flag:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python scripts/qualify_containers.py \
  --enable-prototype --user-manager --image sha256:FULL_64_HEX_IMAGE_ID
```

The image must already exist locally; no pull occurs. Optional `--image-store` and
`--image-runroot` select an isolated local store. Alternatively `--image-archive`
imports a reviewed OCI archive into a disposable store. Tags are refused. The
runner executes real provider dispatch against synthetic loopback APIs, active
input mutation attempts, Option A policy/Chrome and outside-mount write refusal, actual kernel limits,
network-none and two generations' cleanup. It reports every mandatory row with a
reason. No real credentials are accepted. Bounded synthetic startup diagnostics
are included when a CLI fails. Do not weaken outer seccomp/AppArmor to obtain a pass. Owner-selected Option A
disables only inner sandboxes under the verified immutable ACC contract; native
sessions retain their sandboxes.

**This local mode always skips `trusted_launcher` and stays NO-GO.** It cannot
replace the registered broker/root-launcher driver below. A readonly immutable
hook boundary is necessary even when a CLI fails to start with that boundary.

Before closing phase 0 an owner must provide an authorized execution context
outside broker `NoNewPrivileges` with delegated cgroup v2, then record:

1. Rootless keep-id maps and workspace ownership under the intended systemd and
   restricted AppArmor policy. Do not disable the broker restriction or global
   Ubuntu user-namespace restrictions. Compare a direct account login and the
   dedicated launcher context; the current runner does not create that service.
2. Real Claude, Codex and Copilot guard block and post-tool event, immutable
   effective hook settings, Codex trust and the selected container sandbox boundary. Stop on a mandatory
   hook failure. Replace direct file spooling with the session collector before
   running production agents.
3. Real provider-specific short-lived/narrowed copies issued only through ACC,
   refresh/rotation races, resume reauthorization and tmpfs teardown. Do not copy
   canonical writable refresh stores or trust session-returned configuration.
4. Multiple warm container startup timings plus steady/peak cgroup memory,
   including the approved container sandbox/browser workloads. The runner reports failed runtime
   probe time and child RSS separately; neither is container startup or memory.
5. Two-session isolation and hostile Git/config tests from the design; runtime
   fallback qualification, enforceable private/LAN IPv4/IPv6 egress denial with
   explicit project grants, and first-release NVIDIA Linux/WSL qualification.

The offline probe's `--network=none` is deliberately stricter than the approved
Internet-enabled policy. Do not substitute unrestricted rootless networking for
an external deny policy. Native exceptions remain owner-only, per-project, with
a **not isolated** badge when implemented. No automatic native fallback exists
in this experiment; today's production session launch is unchanged.

## Phase 1 through the trusted launcher

The phase-0 commands above are retained for diagnosis. **They cannot return GO.**
Phase 1 uses `--enable-prototype --phase-1-plan PLAN.json`, invoked by the owner
as the **acc control account** (never root, never the agent). It writes a unique
protected grant, calls the already-installed registered broker, and that broker
calls the trusted launcher. No user-manager substitute, MainPID impersonation,
new root protocol operation, production flag, database row or normal policy is
needed. All commands in this section are **owner-run**, not installer steps.

### Build and authorize (owner, root where marked)

1. Keep `ACC_CONTAINERS` off. The host described on 2026-10-23 already has linger,
   the user-namespace restriction and the named AppArmor profiles loaded. Record
   those settings; **do not change them for this run**. From an administrative
   login outside the ACC broker, check `cat /proc/sys/kernel/yama/ptrace_scope`
   (at least 1) and `systemctl status acc-containers.socket
   acc-containers-reconcile.timer acc-broker@alex-agent.service`. Substitute the
   selected account and checkout paths below. The image code runs as the agent;
   only the owner changes the root policy.
2. Build and review the local image using [the image runbook](../../deploy/containers/README.md).
   Build with `deploy/containers/build-image OUTPUT_DIR`, outside an ACC session (`NoNewPrivs: 0`), started with
   `sudo -u rynhult-build` as the image runbook shows, while that dedicated account runs nothing else (security review 8, SR8-3):
   it refuses while the account has other processes, including its broker, or has lingering, user timers, a
   crontab or `at` jobs. The session agent still needs its normal launcher runtime/lingering;
   the dedicated builder is separate, has no broker and never needs a user manager.
   The owner provisions it with `setup-build-account`; the build helper provides
   a private `XDG_RUNTIME_DIR` and cgroupfs. Do not repurpose the session agent for builds.
   It builds in a private throwaway store (explicit `--root`/`--runroot` under
   `/tmp/acc-ib.*`, no additional store, so it also works before the protected store
   exists) and removes it on exit. Nothing is left in the account's own store, where a
   layer with the same ID would shadow the protected store and be refused (qualification
   attempt 11). Before qualifying, run `python3 -I scripts/container_qualification/image_store.py
   --check-store sha256:FULL_64_HEX_IMAGE_ID` as the agent; the host probe repeats this
   check and fails with the exact cleanup commands if an older build left such a layer. The commands use the run
   root and tmp dir Podman recorded in that store's `libpod/db.sql`; when those can't be read, or aren't plain paths in the
   account's runtime directory or home, none is printed.
   Session images must be installed in the root-owned read-only additional image
   store. Follow the exact [owner import and host qualification commands](../../deploy/containers/ROOT-IMAGE-STORE.md)
   before proceeding. Never load the session image into the account's writable
   store: even a matching image ID there is refused. The root-captured host proof
   must be less than one hour old when the driver runs. Installation also
   publishes the pinned flattened execution tree. The probe uses `--rootfs`,
   verifies the live process-root inode and read-only setting, and requires
   `execution_source: held-rootfs`; older image-lookup proofs are not accepted.
   The runtime supports systemd 255 through the protected root namespace bootstrap
   and guard; Podman still executes as the agent with a full identity map.
   The owner proof also requires denied same-UID proc-root/fd and namespace access,
   natively and through `podman unshare` (`namespace_boundary: denied`).

3. **Root:** use `sudoedit /etc/acc/containers/policy.json`. Preserve every
   existing entry, including the normal default; add this separate entry under
   `images`, replacing the image placeholder with the inspected ID:

   ```json
   "qualification-phase1": {
     "image": "sha256:FULL_64_HEX_IMAGE_ID",
     "network": "none",
     "providers": {"codex": "/usr/bin/python3"}
   }
   ```

   The policy must remain root-owned and not group/other writable (0644).
   The special map starts the image's fixed qualification Python entrypoint;
   the driver refuses the normal policy, a mismatched ID, networking or map.
   Existing helper and broker services are used without restarting them or accd.
   If `grants/` has not been provisioned by the launcher installation, stop and
   complete that reviewed installation rather than making it agent-writable.
4. Save an owner-reviewed plan, without keys or provider credentials. `repo`
   is a clean, committed, reviewed source under the selected broker's allowed
   roots; `root` is an existing allowed workspace root. The broker seeds a new
   private checkout. The driver must be able to read this file as `acc`:

   ```sh
   QUALIFICATION_SOURCE=/srv/acc/reviewed/phase1
   cat > "$QUALIFICATION_SOURCE/qualification-plan.json" <<'JSON'
   {
     "account": "alex-agent",
     "repo": "/srv/acc/workspaces/demo-api",
     "root": "/srv/acc/workspaces",
     "policy_id": "qualification-phase1",
     "image": "sha256:FULL_64_HEX_IMAGE_ID"
   }
   JSON
   chmod 0644 "$QUALIFICATION_SOURCE/qualification-plan.json"
   ```

### Run and read the result

**Stop every other session of the payload account first** (native and container).
The payload reports through that account's own checkout, and its challenge is in
world-readable argv, so any other process of the account could write a forged
report. The driver samples the account's processes throughout each generation:
anything outside the launcher unit, the broker's main process and the account's
user manager, or a process table it can't read (hidepid), fails `trusted_launcher`
and stops the run (security review CT-03). Sampling can miss a writer that lives
only between samples; a quiet account is the precondition, the census its check.

Before the first launch the census is strict: nothing in the broker's own service
cgroup but the broker, so also no tmux server (`tmux -L acc kill-server` as the
account once its sessions are gone). During the run it attributes to the broker
only its direct children, the single tmux server it started, and the pane PID its
launch answer named (with that pane's descendants); other panes, tmux jobs and a
second detached process there are foreign. A failure names each process: PID, a
fixed label (`Podman pause scope`, `session bus`, `broker cgroup`, `user manager
unit`, `login session`, `system service`, `other`) and its cgroup with anything but
plain path characters escaped; the report's `foreign_processes` lists the same.
Why this can't forge evidence: docs/security/2026-10-25-review-kit-census-quiescence.md.


The payload's report must carry the container uid the launcher maps the account to (keep-id; 1000) and this run's
nonce; a report with the nonce and another uid stops the run at once. The driver reads that uid from the installed
launcher's one `CONTAINER_UID` line (`/usr/local/sbin/acc-containers`, root-owned, not group- or world-writable),
which also builds the launcher's `--userns` and `--user` flags; a launcher without it stops the run before any grant
under `launcher_host`. A failed hook row lists the hook events
it saw, the denials by rule and both markers, and, when it fails, the last part of the CLI's own output (attempt 16's
Codex row exited 0 and said nothing). The Copilot row first requires the image wrapper to refuse, with exit 78 and no
model request, each setting Copilot would obey to drop its hooks: the repository's `.github/copilot/settings.json` and
`settings.local.json`, and the session's own `settings.json`. A run that stops early names the step and its error in
`restart_stop_cleanup` and `stopped_at` (attempt 16 said only `RuntimeError`); failing payload rows never stop it, and
of a broker refusal only the launcher's fixed code is kept.

Before any grant, the driver also checks two host settings the launch needs (attempt 14 found both only at the real
launch): `/run/acc-ct-attach` is missing or root's 0755 folder (not a link), and `acc-containers@.service` and
`acc-launcher.slice` are installed with `TasksMax` of at least 256 and 4128 (16 connections at 256, plus reconcile's 32). Otherwise it stops with `launcher_host` naming
what differs and the fix.

Use a reviewed root-owned source checkout or checksum-verified bundle extracted
under root ownership, including every ancestor of the source directory. The agent
must not be able to replace the driver or its PYTHONPATH code. Keep the plan/result
in that protected directory too. The plan's `repo` remains ordinary workspace data.
The session handoff includes a prepared bundle, checksums and exact owner commands.
From the owner shell, switch to the protected control account (Python refuses root):

```sh
sudo -u acc env PYTHONPATH="$QUALIFICATION_SOURCE/src" /srv/acc/app/.venv/bin/python \
  "$QUALIFICATION_SOURCE/scripts/qualify_containers.py" --enable-prototype \
  --phase-1-plan "$QUALIFICATION_SOURCE/qualification-plan.json" > "$QUALIFICATION_SOURCE/qualification-result.json"
qualification_exit=$?
cat "$QUALIFICATION_SOURCE/qualification-result.json"
printf 'qualification exit: %s\n' "$qualification_exit"
/srv/acc/app/.venv/bin/python -m json.tool "$QUALIFICATION_SOURCE/qualification-result.json"
```

Exit 0 means **every mandatory row passed**, through the trusted launcher, in
both fresh generations. Exit 1 means NO-GO, including unavailable prerequisites;
exit 2 means disabled or root/options refused. Each mandatory row is `pass`,
`fail` or `skip` with a reason. Missing/malformed rows cannot yield GO. A skip
never means supported. Expect up to about 12 minutes for two generations when
provider timeouts fire. The per-generation elapsed time includes all probes;
it is **not container startup latency**. Failed cleanup reserves capacity and
must be investigated; it is never relabeled clean. The driver attempts stop
on partial launch and revokes its grant even on failure, allowing installed
reconciliation to retry. It preserves the checkout and run folder for review.
Synthetic qualification failures include bounded CLI startup diagnostics. No real credentials enter this run.

### What is exercised

| Mandatory row | Passing evidence |
| --- | --- |
| `trusted_launcher` | Registered broker's launch RPC confirms the expected running launcher unit for the protected grant and policy |
| `claude_hooks`, `codex_hooks`, `copilot_hooks` | Real pinned CLI consumes a loopback scripted-model tool call; ACC records an exact custom pre-tool denial; forbidden marker is absent; allowed command creates its marker and records a real post-tool event; CLI exits successfully |
| `codex_hook_trust` | The real noninteractive Codex hook run succeeds using the existing version-gated ACC bootstrap; no synthetic hash-only success or global trust bypass |
| `codex_bundled_bwrap` | Under a verified ACC contract: pass with reason **bubblewrap: not applicable (container is the sandbox)**; no nested sandbox claim |
| `chrome_sandbox` | **Chrome: --no-sandbox inside container**, live renderer with outer NNP/seccomp; native validation still requires Chrome sandbox/PID isolation |
| `network_none` | Only loopback exists, no external IPv4 route, no IPv6 gateway/non-loopback route; model traffic stays inside that namespace |
| `per_session_limits` | Kernel cgroup-v2 CPU quota, memory, swap and PID controls match the unit and aggregate slice (2 CPUs, 4 GiB, 512 tasks; two-session aggregate) |
| `restart_stop_cleanup` | Stop both generations through the broker; launcher confirms unit termination and automatic container removal; a new payload nonce proves recreation |
| `container_boundary` | Verified immutable launcher contract and actual outside-mount writes refused with EROFS |
| `immutable_inputs` | Helpers, effective provider hooks/trust and guard policy, and their parent folders, cannot be replaced by the payload |

The replay fixture implements Anthropic Messages, OpenAI Responses and OpenAI
Chat Completions streams. It sends commands through **the actual CLI tool
executor**, not by calling hooks itself. Unknown advertised tools/wire formats
fail; unsupported transport behavior is a probe failure, not a fake provider
pass. It uses documented [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway),
[Codex custom Responses providers](https://github.com/openai/codex/blob/main/codex-rs/responses-api-proxy/README.md),
and [Copilot BYOK/offline mode](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models).
These are synthetic protocol fixtures, **not provider API/authentication,
refresh-token or online model qualifications**. No real tokens enter this run.
The container's own report remains untrusted session data, not remote attestation.

### Known NO-GO and scope limits

Install the reviewed launcher update before the trusted run: it copies bounded, no-follow,
hash-checked input files into a root-owned snapshot, binds that directory read-only and
binds only events/statusline as writable outputs. HOME, Codex hooks/trust, Claude machine-policy hooks and Copilot's
hooks file are immutable image files. Separate Claude/Copilot config directories and explicit Codex state links
reach tmpfs; Copilot's config directory links its hooks to the image copy, and its wrapper refuses to start without
them (docs/CONTAINERS.md). Real dispatch must still block when user settings request disableAllHooks, and each
CLI runs inside a scratch repository whose `.claude/`, `.codex/config.toml` and Copilot settings try to
disable hooks (security review CT-01). `immutable_inputs` also inspects image modes behind the read-only
mount: no group/other-writable or set-id entry (CT-02). Qualification actively tries
write/chmod/parent creation against every protected input.

The local user-manager image run exercises the runtime but always skips `trusted_launcher`
and stays NO-GO. All three provider hooks, Codex trust and Option A Chrome now pass locally.
The approved policy does not require nested proc/chroot sandboxes: the container is
the sandbox. Removing proc masks, dropping seccomp or adding capabilities is not
part of Option A; outer restrictions and outside-mount write refusal remain mandatory. The local result does not establish installed systemd private staging.

This driver recreates a **session**, not the broker service or host. It does not
qualify broker crash/reboot recovery, simultaneous session/swap-and-restore or
submount attacks, disk/inode exhaustion (the helper still polls disk), hard disk
quotas, PTY/attach safety, collector authentication/backpressure, credentials,
private/LAN Internet-enabled policy, NVIDIA, WSL or Mac. Those security-review
and later-phase gates remain open even if this scoped report eventually says GO.
No production rollout or feature enablement follows automatically from a report.

After review, root may remove only the `qualification-phase1` policy entry;
preserve normal policies. Delete the temporary plan and move the JSON result
into the owner's evidence archive. Inspect a failed unit by the exact unit/session
ID in the report and the launcher's journal/audit. Never delete containers by
labels or name guesses to make cleanup pass. Retained private checkouts/run
folders are owner-reviewed evidence, not automatically force-cleaned work.


### Owner Option A

The root helper creates a private immutable contract; image-only wrappers at the
pinned binary paths use it to select the container as sandbox. Environment or
repository flags cannot select this mode. No contract preserves native defaults.
Proc masks, seccomp, dropped capabilities, network none and immutable inputs stay
unchanged. The local prototype emulates the contract and always skips the real
trusted-launcher item; every mandatory item must pass for GO. Decision 0043
and the session handoff describe the policy, exact alex-agent qualification image
policy and one-time acc-owned grants once this branch is deployed. The deployed
control-account driver creates/revokes fresh scoped grants for both generations;
never install an agent-writable or permanent wildcard grant.
