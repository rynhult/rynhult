# Disposable Arch desktop qualification

This is a destructive, headless test **inside rootless Podman only**. It does not
qualify a physical desktop, a derivative kernel/LSM, real provider auth, signing,
or per-session containers. The guest runs shipped native-mode desktop sessions.
Do not run the probe on the host or use real provider CLIs/credentials.

Build a fresh x86-64 developer `.deb` using `desktop/package.sh`, then prepare the
unchanged Arch recipe from it with `desktop/package-arch.sh`. This repackages the
bundled runtime; makepkg does not compile Tauri or download application binaries.
Use an ordinary account with working rootless Podman, delegated cgroup v2 and an
existing user manager. Do not install host prerequisites, enable linger, change
sysctls/LSM policy or reset a shared Podman store to make qualification work.

The ACC broker's NoNewPrivileges may prevent newuidmap. The existing container
qualification convention runs a bounded transient test job through the account's
already running user manager, without changing the broker policy. A private store
and empty registry auth keep this run separate from normal Podman state:

```bash
set -euo pipefail
scratch=$(mktemp -d /tmp/rynhult-arch-q.XXXXXX)
chmod 700 "$scratch"
mkdir "$scratch/config" "$scratch/tmp"
printf '{"auths":{}}\n' > "$scratch/auth.json"
chmod 600 "$scratch/auth.json"
uid=$(id -u)
serial=0
podman_q() {
  serial=$((serial + 1))
  XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemd-run --user --working-directory="$PWD" --quiet --wait --pipe --collect \
    --unit="arch-q-$$-$serial" -p Delegate=yes -p CPUQuota=200% -p MemoryMax=3G \
    -p TasksMax=512 -p RuntimeMaxSec=900 -- \
    env -i PATH=/usr/bin:/bin XDG_RUNTIME_DIR="/run/user/$uid" \
    XDG_CONFIG_HOME="$scratch/config" CONTAINERS_CONF=/dev/null \
    CONTAINERS_STORAGE_CONF=/dev/null REGISTRY_AUTH_FILE="$scratch/auth.json" \
    podman --root="$scratch/storage" --runroot="$scratch/run" \
      --tmpdir="$scratch/tmp" --storage-driver=vfs --events-backend=none "$@"
}
podman_q info --format=json > "$scratch/info.json"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["host"]["security"]["rootless"]' "$scratch/info.json"
# Set deb to the freshly built package, not an older build with another source tree.
desktop/package-arch.sh "$deb" "$scratch/package"
cat "$scratch/package/PKGBUILD"
sha256sum "$scratch/package/PKGBUILD" "$scratch/package/rynhult-desktop.tar.gz"
podman_q build -t localhost/arch-desktop-qualification \
  -f desktop/tests/arch-phase1.Containerfile desktop/tests
podman_q image inspect localhost/arch-desktop-qualification > "$scratch/image.json"
podman_q run --pull=never -d --name "arch-q-$$" --systemd=always --cgroupns=private \
  --cap-add=SYS_ADMIN --memory=2g --cpus=2 --pids-limit=512 --network=none \
  --stop-timeout=20 localhost/arch-desktop-qualification /sbin/init > "$scratch/cid"
cid=$(tail -1 "$scratch/cid")
podman_q exec "$cid" mkdir -p /opt/qualification /build
podman_q cp "$scratch/package/PKGBUILD" "$cid:/build/"
podman_q cp "$scratch/package/rynhult-desktop.tar.gz" "$cid:/build/"
podman_q cp desktop/tests/linux_phase1_probe.py "$cid:/opt/qualification/"
podman_q cp desktop/tests/linux_fake_provider.py "$cid:/opt/qualification/"
podman_q cp tests/fake_cli.py "$cid:/opt/qualification/fake-claude"
podman_q cp tests/fake_cli.py "$cid:/opt/qualification/fake-codex"
podman_q exec "$cid" bash -euc '
  test "$(cat /proc/1/comm)" = systemd
  useradd --create-home --uid 2000 --shell /bin/bash package-builder
  chown -R package-builder:package-builder /build
  cd /build
  setpriv --reuid 2000 --regid "$(id -g package-builder)" --init-groups \
    env HOME=/home/package-builder makepkg --cleanbuild --noconfirm
  pacman -U --noconfirm /build/rynhult-linux-spike-*.pkg.tar.zst
  pacman -Qkk rynhult-linux-spike
  ln -s /usr/lib/rynhult-linux-spike /opt/qualification/resources
  python /opt/qualification/linux_phase1_probe.py qualify
  python /opt/qualification/linux_phase1_probe.py uninstall-arch
'
podman_q cp "$cid:/var/lib/desktop-qualification/report.json" "$scratch/report.json"
```

SYS_ADMIN exists only inside the rootless guest namespace for systemd's
ProtectHome mounts. No privileged/rootful container, host namespace/directory/
socket bind, ports or host capability is used. Dependencies download only during
image build; the qualification guest has network none. The probe refuses a
host/rootful/non-systemd environment before creating the two owners and agents.
The package builder's UID2000 is separate from the owners' UID1000/1001.

The shipped broker is a **system socket/service with User=agent**. The owner PAM
login activates the owner's `systemd --user` manager; logout stops that manager
while both fake sessions and the broker survive. Reopening verifies credential
retention and streams the same provider PIDs through the real PTY WebSocket.
File ACL denial and socket SO_PEERCRED denial are independently exercised.

Uninstall first requires API-stopped providers, stops only the owned broker
units, records the dedicated tmux server PID and closes it through its exact
test socket. It refuses any unexpected agent process or surviving process. The installed setup
`uninstall --apply` executes with a fixture intercepting its UID-kill command:
that intercept proves the target UID is empty and records a no-op. No pattern or
UID kill executes. This does not qualify forced termination of running agents.
All other installed uninstall commands run normally. Both owners' units,
sockets and manifests must disappear; accounts remain locked/expired and work,
fake credentials and profile remain. Pacman then removes every packaged file.

Stop on failure; save the report when available, phase/main stderr, journal,
package hashes, makepkg output, `pacman -Q` and `pacman -Qkk` output before cleanup.
Never repair partial setup in place; use a fresh guest after fixing a defect.
Stop/remove **only the recorded container ID**. Delete the entire private scratch
store using rootless `podman unshare` when subordinate ownership requires it.
Never reset the normal store or stop a shared pause helper. A task-created helper
may be stopped only by its recorded PID after checking start time/namespace and
proving there are no unrelated namespace users. Collect transient jobs too.

Arch caveats: use `pacman -Syu`, never a partial `-Sy` upgrade. Capture resolved
image digest and exact package versions: the base digest does not pin repository
state. Do not add Arch repositories to Manjaro or override a derivative's keyring,
repository order, kernel or LSM. Physical GPU/WebKit, login manager, cgroup
policy and derivative package resolution remain owner-machine gates.
