# Containers qualification runbook

For the owner running the Linux qualification kit from a retained administrative
console, **not an ACC agent session**. This portable runbook describes the order
and recovery checks. `alex-agent` and the paths below are
examples; substitute the selected enrolled account and reviewed paths.

**Keep `ACC_CONTAINERS` off, both configured and running.** A passing collection
is only a scoped phase-1 result, with `production_qualified: false`. It does not
enable containers or approve production rollout.

## Before starting: ship and deploy matching code

The owner kit and the read-only-rootfs mount fixes are on main. Use one reviewed,
committed main checkout containing the kit, current image fixes and matching
launcher. Do not assemble an unreviewed mix of branches.

**Ship and deploy launcher changes first**, through the normal reviewed release
process. The kit does not update helpers or restart ACC services. Its preflight
compares the installed helpers with the reviewed source byte-for-byte; a newer
checkout beside an old deployed launcher is a hard failure, not a reason to
relax the check or copy a helper out of an agent worktree.

Verify the root-owned bundle and all its ancestors before executing its code.
They must have no agent/group/other write access, including ACL grants. Check
ownership before asking git to read the checkout's configuration. The clean
bundle must contain no untracked/ignored files or Python bytecode caches.

From the owner's administrative Bash console:

```bash
sudo -i
set -euo pipefail
umask 077
cd /
REVIEWED=/srv/acc/reviewed/phase1
REVIEWED_COMMIT=REPLACE_WITH_REVIEWED_COMMIT
KIT="$REVIEWED/scripts/containers-qualification"
ACCOUNT=alex-agent
EVIDENCE=/root/containers-qualification-example
mkdir -m 0700 "$EVIDENCE"
PLAN="$EVIDENCE/plan.json"
STATE=/var/lib/acc/containers-qualification-kit

namei -l "$REVIEWED"
test -z "$(find "$REVIEWED" \( ! -user root -o -perm /022 \) -print -quit)"
test "$(git -C "$REVIEWED" rev-parse HEAD)" = "$REVIEWED_COMMIT"
test -z "$(git -C "$REVIEWED" status --porcelain --ignored)"
cmp "$REVIEWED/deploy/containers/acc-containers" /usr/local/sbin/acc-containers
cmp "$REVIEWED/deploy/containers/acc-container-exec" /srv/acc/containers/acc-container-exec
sha256sum "$KIT"/*.sh "$KIT/kit.py" "$KIT/manifest.json" \
  "$REVIEWED"/deploy/containers/{verify_image_archive.py,storage.conf,acc-container-exec,acc-containers} \
  "$REVIEWED"/deploy/containers/{take_archive.py,setup-build-account,setup_build_account.py} \
  "$REVIEWED"/deploy/apparmor/{acc-podman,acc-podman-runtime} \
  "$REVIEWED/scripts/container_qualification/image_store.py" \
  "$REVIEWED/scripts/qualify_containers.py" > "$EVIDENCE/reviewed-source.sha256"
cat "$KIT/manifest.json"
```

Replace the commit placeholder with the independently reviewed commit, not
whatever HEAD happens to be. For a bundle without git metadata, compare the
complete file hashes with the list made during review instead. `namei`/`find`
do not replace the ACL review. Run each stage separately and stop on any error.
If a failed block exits the shell, return through the retained owner console
and re-establish its variables before inspecting evidence.

The hyphenated `scripts/containers-qualification/` is the owner kit. The existing
underscored `scripts/container_qualification/` contains the lower-level harness;
its local/user-manager mode cannot replace trusted-launcher qualification.

## 1. Prepare the account

Create/enroll the non-root `*-agent` account through the normal owner account
workflow first. Its registered broker must already allow the plan's repository
and workspace root. The separate `acc` control account and protected grants
directory must also be provisioned by the normal launcher installation.

Stop this account's other native and container sessions. Resolve residual
workloads by their exact recorded IDs, never with pattern kills. A quiet account
is a qualification prerequisite: process sampling cannot exclude a short-lived
writer between samples. Do not use the kit to create accounts, install packages,
edit subordinate IDs or repair delegation.

```bash
getent passwd "$ACCOUNT"
id "$ACCOUNT"
QUAL_UID=$(id -u "$ACCOUNT")
AGENT_HOME=$(getent passwd "$ACCOUNT" | cut -d: -f6)
loginctl show-user "$ACCOUNT" -p Linger -p State
systemctl status "user@$QUAL_UID.service" \
  "acc-broker@$ACCOUNT.service" acc-containers.socket acc-containers-reconcile.timer
```

Session qualification needs a usable `/run/user/UID` outside broker
`NoNewPrivileges`; the separate image builder creates its own private runtime.
Missing runtime/account prerequisites need a separate reviewed owner action.
Record that baseline: setup cannot undo changes made beforehand.
The kit can enable missing linger during setup, and explicitly waits for the
user manager to start; do not substitute a sleep after `enable-linger`.

## 2. Build as the dedicated build account

Security review 8 SR8-3 requires **`rynhult-build`**, never the selected session
account or root. From the retained owner console, follow the one-time account
setup and validation in `deploy/containers/README.md` first. The builder runs no
sessions, has no supplementary groups, linger, active user manager or scheduled
jobs, and needs read/traverse access to the protected reviewed checkout. Never
add it to an agent group. Confirm `ps -u rynhult-build` shows no unrelated work.

Start from `/`, not the owner's inaccessible `/root` working directory. Use the
reviewed helper with a new output directory for every attempt:

```bash
BUILD_OUTPUT=/var/lib/rynhult-build/qualification-attempt-01
runuser -u rynhult-build -- /usr/bin/env -i PATH=/usr/bin:/bin \
  HOME=/var/lib/rynhult-build LANG=C.UTF-8 \
  "$REVIEWED/deploy/containers/build-image" "$BUILD_OUTPUT" \
  2>&1 | tee "$EVIDENCE/build.log"
```

No login shell, user manager or session runtime is required for the builder.
The helper supplies `--cgroup-manager=cgroupfs` and a private throwaway store and
runtime; it builds and saves `acc-session.oci.tar`, its checksum, `image.txt`
and `review/`, then cleans its store. It never uses the session account's store.
If cleanup reports residual storage, preserve the exact diagnostic and follow
its reviewed build-account cleanup instructions; do not conceal the residue.

Review the Containerfile, pinned inputs, filesystem inventory and licenses using
`deploy/containers/README.md` in the reviewed checkout. That reference describes
inspection without executing image code. Builds download pinned build inputs;
the kit itself never pulls an image.

## 3. Archive, review and pin

The helper already saved the OCI archive as `rynhult-build`. Review its output
privately and record the **full image/config ID and independently reviewed archive
SHA-256**, not just a tag. Do not source builder-owned files or infer approval
from a checksum beside the archive.

After approving those exact bytes, set the pins from the review record. Transfer
to the existing private root evidence directory without following builder-owned
links, then verify the copy:

```bash
BUILD_ARCHIVE="$BUILD_OUTPUT/acc-session.oci.tar"
IMAGE_ID=sha256:REPLACE_WITH_REVIEWED_64_HEX_CONFIG_DIGEST
ARCHIVE_SHA256=REPLACE_WITH_INDEPENDENTLY_REVIEWED_64_HEX_ARCHIVE_SHA256
ARCHIVE="$EVIDENCE/reviewed.oci.tar"
/usr/bin/python3 -I "$REVIEWED/deploy/containers/take_archive.py" \
  "$BUILD_ARCHIVE" "$ARCHIVE"
/usr/bin/python3 -I "$REVIEWED/deploy/containers/verify_image_archive.py" \
  "$ARCHIVE" "$ARCHIVE_SHA256" "$IMAGE_ID"
stat -c '%U %G %a %n' "$ARCHIVE"
```

Expected: verification succeeds and the new copy is `root root 600`. Stop on any
failure. The session account must have no local image/layers shadowing the
protected store. The dedicated builder does not put them there; review any
residue from older attempts separately. Keep the archive separately; do not
manually import it or create the store before kit setup.

**Never kill `catatonit`**, rootless Podman's pause process. Doing so leaves a
stale `pause.pid` and later commands fail with "invalid internal status". With
this account's workloads stopped, `podman system migrate` as that account is the
reviewed reset before the census. It is not a build step. Do not delete PID
files or kill whatever PID a stale file happens to name.

## 4. Prepare the baseline policy and plan

Before preflight, the owner must provide a protected root-owned version-1
`/etc/acc/containers/policy.json` with an `images.default` object. On a fresh
installation, review `deploy/containers/policy.example.json` as the starting
shape; create the policy as root, mode 0644, under protected ancestors. Preserve
every existing entry and provider mapping. Do not blindly overwrite a policy.

Do **not** manually add `qualification-phase1`: prepare preflight refuses an
existing entry. Setup snapshots the baseline, updates its default image pin,
and adds the separate qualification entry with the reviewed image,
`network: none` and `providers: {"codex": "/usr/bin/python3"}`. Rollback restores
the baseline. Do not mix the older manual image-import/policy mutations into a
kit-managed attempt.

```bash
(set -o noclobber; cat "$KIT/plan.example.json" > "$PLAN")
chmod 0600 "$PLAN"
sudoedit "$PLAN"
/usr/bin/podman --version
/usr/bin/crun --version
```

Replace every placeholder. The plan has exactly these eight string fields:

| Field | Required value |
|---|---|
| `account` | The selected enrolled `alex-agent` account |
| `archive` | The protected `$ARCHIVE` path, not the builder's writable output |
| `archive_sha256` | The reviewed 64-hex archive checksum |
| `image` | The reviewed `sha256:` image/config ID |
| `podman_version`, `crun_version` | Exact reviewed installed numeric versions, not assumed supported versions |
| `repo` | An allowed clean, committed repository, for example `/srv/acc/workspaces/demo-api` |
| `root` | Its existing broker-allowed root, for example `/srv/acc/workspaces` |

Enter actual values in JSON, not shell variable names. No provider keys,
credentials or live-session data belong in the plan.

## 5. Run prepare preflight

```bash
"$KIT/preflight.sh" --plan "$PLAN" --prepare \
  | tee "$EVIDENCE/preflight-prepare.json"
```

This is read-only: it does not run `podman info`, create storage or start a
service. Exit 0 permits only explicitly marked reversible `setup` gaps, such as
missing linger, reviewed profiles, restricted-userns setting or the new image
store. It is **not GO**. Any `missing` row blocks setup.

Resolve each hard prerequisite separately, then repeat only this read-only
preflight. In particular, a helper hash mismatch requires shipping/deploying
the reviewed launcher, not skipping that row. Wrong ownership, stale Podman
state, absent delegation/bus, unsupported runtime versions and competing account
processes are not reasons to weaken the checks.

## 6. Setup once, then strict preflight

```bash
"$KIT/setup.sh" --plan "$PLAN" 2>&1 | tee "$EVIDENCE/setup.log"
"$KIT/preflight.sh" --plan "$PLAN" \
  | tee "$EVIDENCE/preflight-ready.json"
```

The second preflight has **no `--prepare`**: every row must pass. Setup journals
changes before making them, verifies the copied archive and layers, publishes
the protected store and independent pinned rootfs, strips unsafe modes and
capabilities, and provisions only its reviewed reversible settings. Root never
executes image code. Smoke-test existing host workloads after AppArmor/sysctl
changes, including Docker stacks, Chrome and native Codex.

Setup refuses existing kit state, image store or import runroot, including
dangling symlinks. It is not an upgrade command and cannot be repeated to repair
a failed attempt. Do not change reviewed source, policy or image files after
setup; collection and uninstall depend on their recorded state.

## 7. Collect once and preserve evidence

```bash
"$KIT/collect.sh" 2>&1 | tee "$EVIDENCE/collect.log"
```

Collection first captures a root-owned host proof via the agent and cleans its
exact temporary namespace unit, then runs the trusted-launcher driver as `acc`.
The real pinned CLIs use synthetic loopback transports, not live credentials.
It requires all mandatory rows in both session generations.

A nonzero exit stops the block. From the retained console, inspect the report
even after failure; **do not rerun collect**. Ordinary probe/driver failures
retain `report.json`. If no report exists, preserve the manifest and command
output and investigate rather than inventing a result.

```bash
/usr/bin/python3 -I -B -m json.tool "$STATE/report.json"
sha256sum "$STATE/report.json"
install -m 0600 "$STATE/report.json" "$EVIDENCE/report.json"
install -m 0600 "$STATE/manifest.json" "$EVIDENCE/manifest-before-uninstall.json"
```

Exit **0** means the requested step passed; **1** means preflight blocked or
collection NO-GO; **2** means unsafe input/state, wrong identity or a failed
setup/uninstall. Missing, malformed or skipped mandatory checks remain NO-GO.
Even a scoped pass leaves credentials/refresh, broker/host restart, hostile
simultaneous staging, disk exhaustion/quotas, Internet/LAN policy, GPU, WSL and
Mac qualification open.

## 8. Uninstall between attempts

Preserve results, stop qualification workloads using their exact recorded
session/unit IDs, and resolve residual processes. From the same protected
reviewed kit used for this attempt:

```bash
"$KIT/uninstall.sh" 2>&1 | tee "$EVIDENCE/uninstall.log"
/usr/bin/python3 -I -B -m json.tool "$STATE/manifest.json"
install -m 0600 "$STATE/manifest.json" "$EVIDENCE/manifest-uninstalled.json"
sha256sum "$EVIDENCE/report.json" "$EVIDENCE/manifest-uninstalled.json"
```

Uninstall prevalidates all files, metadata, settings and inventoried trees before
rollback. It restores only its changes, never kills unknown processes and refuses
unknown additions, mounts, drift, incomplete cleanup or pending operations.
On refusal, retain state for a reviewed owner recovery action. Never clear
`pending`, force removal, delete the journal or overwrite edits to retry.

The canonical kit directory retains the root-only report, staged archive and
`uninstalled` tombstone. Driver checkouts/run folders also remain for review.
Before another attempt, have the owner review and retire that completed kit
state into protected evidence storage; a successful uninstall alone does not
make setup reusable. Do not move a failed/pending journal aside to evade refusal.
Only then deploy any newly reviewed changes, start a new evidence directory and
plan, and repeat the sequence. Do not modify the old reviewed bundle while its
attempt still needs rollback.

Keep the archive, independent pins, source hashes, both preflights, command logs,
report, before/after manifests and exact recorded units together in the private
owner evidence directory and its protected backup, **not `/tmp`, an agent
worktree or the public wiki**. Preserve original evidence before producing any
sanitized review copy.

## Common failure modes

| Symptom | Action |
|---|---|
| Build has no systemd login session | Use the dedicated build account's `build-image` helper, which supplies cgroupfs and a private runtime; do not change production runtime settings. |
| "Invalid internal status", stale `pause.pid`, or census names `catatonit` | Stop the selected account's workloads and run its `podman system migrate`. Never kill `catatonit` or trust a stale PID. |
| Session bus appears after Podman uses systemd | Leave the account's legitimate session bus running. The reviewed census allows only `/usr/bin/dbus-daemon --session`, `/usr/bin/dbus-broker` or `/usr/bin/dbus-broker-launch`, with every UID matching the account, in exactly its `user@<uid>.service/app.slice/dbus.service` or `session.slice/dbus.service`. Lookalikes elsewhere still fail. |
| Installed helper differs from reviewed source | Ship and deploy the reviewed launcher changes first. The kit does not install them; no hash-check bypass or in-place helper patch. |
| `namespace_unshare` exits 125 / "Error: setting up the process" | Use the current diagnostics and run from `/`. Podman's namespace child may not enter an inherited `/root`; inspect `checks.image_store` and its bounded `podman`/`podman_debug` output rather than assuming the cause. |
| `held_rootfs_run` fails on `/proc/PID/fd`, `/etc/mtab` or another missing target | Use the reviewed probe and matching image/launcher fixes. The probe checks the protected rootfs path against its held inode and live container root; the image must ship static mount targets and the launcher must supply reviewed tmpfs parents for dynamic binds. Rebuild, review and repin after image changes. Never make the protected rootfs writable or weaken namespace/seccomp checks. |

The owner kit README and manifest define the current evidence contract.
Host-specific attempts and internal security reviews remain private; neither a
staged attempt nor this runbook grants production approval.
See [Operations](/Internal/Operations#container-qualification-evidence) and
[Security](/Internal/Security#container-launcher) for the surrounding boundary.
