# Owner container qualification kit (Linux)

This packages the owner window in [CONTAINERS.md](../../docs/CONTAINERS.md#qualification-run)
and [ROOT-IMAGE-STORE.md](../../deploy/containers/ROOT-IMAGE-STORE.md).
**Nothing is enabled by default.** It installs no packages, upgrades no runtime,
restarts no ACC service, never pulls an image and never sets `ACC_CONTAINERS`.
Use a retained administrative console, outside an agent session. The payload runs
as the selected enrolled agent; the trusted driver runs as `acc`, never root.
Root only provisions the reviewed store/configuration and captures the host proof.

Read `manifest.json`, `kit.py` and all four shell entrypoints before using them.
The shell entrypoints use `set -euo pipefail` and `exec /usr/bin/python3 -I -B`;
Python checks every subprocess exit, with bounded waits and a minimal environment.
There is no `--force`, test-root override, package installer or automatic retry.
This kit is tested without root using fake commands; **an owner run is still
required to qualify the real kernel, systemd, AppArmor and Podman combination.**

## 1. Review and prepare inputs

First complete the normal reviewed launcher installation, account enrollment and
[unprivileged image build/review](../../deploy/containers/README.md). Those actions
are separate from this kit. Retain the reviewed OCI archive and independent
archive SHA-256 and config/image ID. Remove only the reviewed local image copy
from the agent's build store using the image runbook; do not force image removal.
Stop that account's other sessions, native and container. Keep an owner console
available to smoke-test existing Docker stacks, Chrome and native Codex afterward.

**Building from the owner console:** provision the dedicated `rynhult-build`
account and use `deploy/containers/build-image` exactly as the
[image runbook](../../deploy/containers/README.md#build-and-review-locally) shows.
The builder never runs sessions. The helper supplies its own private
`XDG_RUNTIME_DIR` and `--cgroup-manager=cgroupfs`; no build-account user manager,
login or lingering is needed. The qualification session account's existing
runtime/lingering remains separate.

**Never kill `catatonit`.** It is rootless Podman's pause process. Killing it leaves
`pause.pid` naming a dead process, and every later Podman call for that account fails
with "invalid internal status". To reset it, run as the account (no agent session
running):

```bash
runuser -u alex-agent -- /usr/bin/env -i XDG_RUNTIME_DIR=/run/user/$(id -u alex-agent) \
  PATH=/usr/bin:/bin /usr/bin/podman system migrate
```

Use a **reviewed root-owned checkout/bundle**, including its ancestors and every
source file, without group/other write permissions or ACL grants. Do not execute
a script from an agent-writable checkout as root. The preflight enforces this and refuses bytecode caches; use a fresh committed
checkout/bundle. The collector also disables bytecode writes and uses a private
cache prefix for the control driver. Reviewing the script must happen *before* invoking it. Have the owner provision
missing tools/IDs/delegation separately; the kit will only describe those gaps.
The exact Podman/crun versions in the plan are owner-reviewed installed versions,
not a claim that an arbitrary version has already been qualified.

From the administrative Bash console (all paths/names below are examples):

Verify the bundle with system tools **before running anything from it**: the kit's
own protection check runs inside `kit.py` and can't vouch for a `kit.py` someone
changed after your review. Check ownership first, because `git` reads the
checkout's `.git/config` (which can name commands). Then confirm it is exactly the
commit you reviewed with no extra or ignored files, and record the hashes of
everything root executes or installs. Without git, compare those hashes against the
list you made during review instead of the two `git` lines.

```bash
sudo -i
set -euo pipefail
REVIEWED=/srv/acc/reviewed/phase1
REVIEWED_COMMIT=0000000000000000000000000000000000000000   # the commit you reviewed
KIT="$REVIEWED/scripts/containers-qualification"
namei -l "$REVIEWED"                                       # every ancestor root-owned, no group/other write
test -z "$(find "$REVIEWED" \( ! -user root -o -perm /022 \) -print -quit)"
test "$(git -C "$REVIEWED" rev-parse HEAD)" = "$REVIEWED_COMMIT"
test -z "$(git -C "$REVIEWED" status --porcelain --ignored)"
sha256sum "$KIT"/*.sh "$KIT"/kit.py "$KIT"/manifest.json \
  "$REVIEWED"/deploy/containers/{verify_image_archive.py,storage.conf,acc-container-exec,acc-containers} \
  "$REVIEWED"/deploy/containers/{take_archive.py,setup-build-account,setup_build_account.py} \
  "$REVIEWED"/deploy/apparmor/{acc-podman,acc-podman-runtime} \
  "$REVIEWED"/scripts/container_qualification/image_store.py "$REVIEWED"/scripts/qualify_containers.py
cat "$KIT/manifest.json"
/usr/bin/podman --version
/usr/bin/crun --version
# New file; noclobber avoids overwriting another run's plan.
(set -o noclobber; cat "$KIT/plan.example.json" > /root/containers-qualification-plan.json)
chmod 0600 /root/containers-qualification-plan.json
sudoedit /root/containers-qualification-plan.json
```

Replace **every** placeholder, including exact numeric runtime versions such as
`4.9.3` and `1.14.4` (examples only). `archive` must be a regular protected file
under root-owned ancestors, not a symlink or a writable `/tmp` input. `repo` must
be a clean committed repository already allowed by that account's broker; `root`
must be its existing allowed workspace root. No keys or credentials belong in
the plan. The account must be an enrolled non-root `*-agent` account.

## 2. Read-only preflight

```bash
"$KIT/preflight.sh" --plan /root/containers-qualification-plan.json --prepare \
  | tee /root/containers-qualification-preflight.json
```

This reads files and queries service/version information; it never runs `podman
info`, creates storage, starts a service or changes a setting. It prints JSON
with every check's name/status/detail, the archive/image pins and source hashes.
A non-root administrative reader can run preflight, but unreadable root state is
reported as missing; the complete check requires the owner console.

Checks include: source/installed-helper hashes; systemd >=255; exact reviewed
Podman/crun versions; all required executables; AppArmor enabled and loaded
profiles; caller `NoNewPrivs: 0`; non-administrative agent groups; Yama >=1; restricted-userns sysctl; nonoverlapping subordinate UID/GID
ranges of at least 65536; cgroup-v2 CPU/memory/PID controllers; linger, user-manager
delegation and bus; installed active launcher/broker/reconciler; the protected
control-account grant directory; archive checksum, policy and protected image
store. Preflight also reads the account's rootless Podman `pause.pid` (files only,
never `podman info`). If it names a process that exited or isn't the account's
catatonit, the **rootless Podman state** row is missing, with the `podman system migrate`
command above.

A quiet-account census rejects other account processes, including residual probes
outside launcher units, and names a stray pause process (catatonit) separately, with
the same fix. It allows only the registered broker, the user manager's `init.scope`,
and the account's own session bus. That bus is socket-activated once rootless Podman
uses the systemd cgroup manager: `/usr/bin/dbus-daemon --session`, or
`/usr/bin/dbus-broker(-launch)`, owned only by the account, in exactly
`user@UID.service/app.slice/dbus.service` or `session.slice/dbus.service`. Anything
else, including a lookalike binary or cgroup, fails closed. As with the existing driver, sampling cannot prove that
a process did not exist between samples; stop competing workloads first.

The image probe runs Podman as the account, which leaves its pause process
(catatonit) in a `podman-pause-*.scope` of the account's user manager and starts the
session bus (qualification attempt 13). Before the launcher test, collect settles
the account. First a census: nothing may run but the broker, `init.scope`, the bus
and pause processes, or nothing is stopped. Then `podman system migrate` with the
probe's own environment (it ends the pause process through its `pause.pid`), only
when that `pause.pid` names a catatonit in a `podman-pause-*.scope` of the
account's user manager: migrate stops all of the account's containers and kills
whatever that file names. Then `systemctl --user stop dbus.socket
dbus.service`, then a strict census (broker and `init.scope` only, no bus) and no
`podman-pause-*.scope` left. Anything that survives stops collection before the
driver runs, named with its fix.
Settle also checks what the driver's census takes on trust from the broker: no
tmux configuration in the account's home (`~/.tmux.conf`,
`~/.config/tmux/tmux.conf`), which the broker's tmux server would read and which
could choose the trusted pane's shell, and a broker started as `python -I`, which
imports nothing from the account's home (its `WorkingDirectory`).
It checks both configured and running `ACC_CONTAINERS` are off, without
printing the daemon's other environment values.

`--prepare` allows only explicitly marked `setup` gaps: linger (and its absent
manager), reviewed AppArmor profiles, userns restriction, and new image store.
It still lists those gaps. Exit 0 means the hard prerequisites passed, **not GO**.
Any `missing` row exits 1 and stops the copy-paste block. Fix exactly the reported
prerequisite through a separate owner-reviewed change, then rerun preflight.
Without `--prepare`, every row must pass. Unknown/unsupported state fails closed.

## 3. One reversible setup

```bash
"$KIT/setup.sh" --plan /root/containers-qualification-plan.json
"$KIT/preflight.sh" --plan /root/containers-qualification-plan.json \
  | tee /root/containers-qualification-ready.json
```

Setup refuses **any** existing kit state, `/var/lib/acc/session-images` or
`/run/acc-image-import`, including dangling symlinks. Existing published stores
are not upgrades for this script: stop and use the manual runbook/owner review.
It never deletes an account-local image or edits subordinate IDs/cgroup policies.
It refuses a half-installed distribution runtime-profile pair and refuses to
replace mismatched/unloaded existing ACC AppArmor profiles. Existing matching
loaded profiles remain untouched. The reviewed helper update must already be
installed; the kit will not overwrite it or restart its units.

The exclusive root-only journal is at
`/var/lib/acc/containers-qualification-kit/manifest.json`. Setup records intent
before each setting/file operation, snapshots original configuration bytes,
ownership, mode, xattrs and timestamps, and records the created trees' exact
inode inventories. It loads only new reviewed AppArmor profiles (with
`--skip-cache`, so no parser cache is left behind), enables linger
only if off, sets userns restriction only from 0 to 1 and writes its scoped sysctl
file. It copies and verifies the reviewed archive (including layers/diff IDs),
imports into a new root store, publishes an independent rootfs without hardlinks
back to the mount, refuses device nodes (in the archive's layers and again in the
copy), strips setuid/setgid bits and file capabilities while the store is still
0700, removes ACLs and protects ownership/modes. Root **never
executes image code**. It preserves policy entries, updates the default's image
pin and adds the separate network-none `qualification-phase1` policy.
The kit prints review hashes, and retains a staged archive for audit/recovery.
Do not edit the reviewed source, policy or store during this window.

Stop on any error. Setup does not retry or claim to undo an operation whose
outcome is unknown. A failed command leaves an audited `failed` journal for the
uninstall/recovery procedure below. A second setup is always refused.

## 4. Collect the real qualification evidence

```bash
"$KIT/collect.sh"
/usr/bin/python3 -I -B -m json.tool \
  /var/lib/acc/containers-qualification-kit/report.json
sha256sum /var/lib/acc/containers-qualification-kit/report.json
```

The collector reruns ready preflight, checks all recorded state/source hashes,
and refuses any existing report, prior collection attempt or account proof.
It creates one uniquely named temporary namespace-probe service, captures
`image_store.py` directly from the agent (at most 4096 bytes of output; more, or a
timeout, kills it), validates every field and writes a proof built by root from
those fields with root's capture time, then stops
that exact service. The bootstrap uses the installed namespace guard and checks
that it has dropped to the correct account before probing. The probe tests host
and `podman unshare` write refusal plus held-rootfs and namespace boundaries.
No result copied out of an agent session is accepted as root evidence.

Only after the proof passes and cleanup succeeds does the collector run the
existing trusted-launcher driver as `acc`. It writes the separate public
qualification plan under the protected qualification directory. It runs the
synthetic real-provider, hooks/trust, immutable inputs, container boundary,
network-none, kernel limits and two-generation stop/recreation checks. No live
provider credentials or Rynhult agent tools are invoked by this kit.

If the host proof fails, `checks.image_store` in the report holds the probe's exit code,
the last 4 KiB of its stdout and the last 16 KiB of its stderr (terminal escapes and control characters
removed). The probe's last line, `image_store: FAILED <check>: <reason>`, names the step:
`namespace_service_exited`, `namespace_access`, `namespace_unshare`, `rootfs_ancestors`,
`held_rootfs_pin`, `held_rootfs_run`, `held_rootfs_runtime`, `held_rootfs_identity`, `image_entry_modes`,
`direct_write` or `unshare_write`. `namespace_service_exited` means the acc-cq-* unit had
already stopped; it says nothing about the host boundary. The probe runs the held root the
way the launcher does: Podman gets the protected store path (the launcher binds its held
descriptor into staging), and the probe refuses unless that path, before and after the run,
and the container's root as seen from the host are the same inode it holds open. When `podman unshare` itself
fails (not the check's own "allowed"), the probe reruns it once with `--log-level=debug`.
`podman` lists Podman's own stderr lines and `podman_debug` the last 30 debug lines.
Every kit command, and the probe's Podman calls, run from `/`: never from the owner console's
directory, which the agent's namespace may be unable to enter.

The root-only JSON report contains preflight, hashes, host proof, phase-1 rows,
errors and timestamps. It is written even on ordinary probe/driver failures.
Every mandatory row must pass; missing/skipped rows are NO-GO even if the summary
claims pass. Exit 0 means **scoped phase-1 pass**, with
`production_qualified: false`; it never authorizes production rollout. Provider
authentication/refresh, broker/host reboot, simultaneous hostile staging,
disk/inode exhaustion, Internet/LAN policy, NVIDIA, WSL and Mac remain the
[existing owner/security-review gates](../../scripts/container_qualification/README.md#known-no-go-and-scope-limits).
Local phase-0/user-manager diagnosis remains available through the old harness;
it cannot replace this root-captured trusted-launcher run.

Exit codes: **0** requested step passed; **1** preflight blocked or collection
NO-GO (an unexpected collector error is a recorded NO-GO); **2** unsafe input/state,
wrong identity, failed setup/uninstall or any unexpected error. The
entrypoint propagates errors. Under `set -euo pipefail`, a failed collection
stops before the display commands; read `report.json` from the retained console
before deciding on recovery. Never bypass a failed mandatory row.

## 5. Restore / recover

Stop any qualification workloads by their exact recorded session/unit IDs and
retain the report. Do not delete containers by labels or guessed names. Then:

```bash
"$KIT/uninstall.sh"
/usr/bin/python3 -I -B -m json.tool \
  /var/lib/acc/containers-qualification-kit/manifest.json
```

Uninstall first requires the same protected reviewed source as setup (it runs as
root too), then locks the journal, requires no active container units, and takes a process census of the selected account (registered broker and
user-manager init.scope only). Residual probes or native sessions stop rollback;
it never kills those processes. It validates **all** recorded files/settings/trees before making its first rollback change.
It refuses changed/replaced files, symlinks, unknown tree additions, active
mounts, incomplete probe cleanup and pending operations. It unloads only profiles
it loaded, restores original configuration/metadata and changed settings, and
removes only its inventory of created objects. Existing profiles and unrelated
policy entries are preserved. It never applies `sysctl --system`, disables
AppArmor, removes accounts/IDs or starts/stops ACC services. Linger is disabled
only if the kit enabled it; its user-manager lifecycle remains managed by logind.

If a command timed out, the machine lost power, or the journal says `pending`,
the script cannot prove whether that operation completed. **It stops rather than
removing unknown state.** Inspect the exact journal path/unit and kernel/service
state from the owner console and obtain a reviewed recovery action. Do not clear
`pending`, delete the journal, use `--force` or rerun setup to make a check pass.
Likewise, an owner edit during qualification requires review; uninstall never
overwrites it with an old snapshot. An uninstall that fails mid-rollback stops;
retain the manifest for reviewed recovery instead of blindly repeating it.

Evidence, the staged archive and the `uninstalled` journal remain root-only.
Driver checkouts/run folders are retained for owner review, as in the original
harness. Archive these explicitly after review. A new qualification window needs
a separately reviewed retirement of the old kit state; setup never silently
reuses it. Keeping setup for further qualification does not enable containers.
If accepted, separately review persistence/reboot and the remaining rollout gates.

## Development checks (no root)

```bash
shellcheck scripts/containers-qualification/*.sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q \
  tests/test_containers_qualification_kit.py tests/test_container_qualification.py \
  tests/test_container_exec_guard.py tests/test_container_prototype.py
```

Tests inject filesystem/commands through Python only. The production CLI exposes
no alternate filesystem root, command override or privilege-bypass environment.
