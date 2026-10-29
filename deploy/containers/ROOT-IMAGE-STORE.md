# Owner installation of the reviewed session image

The [qualification kit](../../scripts/containers-qualification/README.md) automates
this import/probe sequence for a **new** store, with an exclusive manifest and
rollback inventory. It reuses `verify_image_archive.py` and `image_store.py`.
Existing stores are refused; use this manual reference only in a separately
reviewed window. Never mix manual store/policy edits with a kit-owned run.

These are owner commands, run from a retained administrative login. They are not
agent or installer actions. Keep containers disabled and stop container sessions
before changing a store. Never rebuild or load into a published store while it
is in use. The store uses root's real uid/gid mappings; never chown it to an agent
or subordinate uid, or import with `podman unshare`. Keep the reviewed archive,
image ID and archive SHA-256 in the owner's review record.

Execution supports **systemd 255**, kernel user namespaces and the installed root
namespace bootstrap/guard. The service briefly starts as root in `/`; the guard
creates a root-owned full identity user namespace, delegates only its unit's cgroup
controls, drops to the agent with no supplementary groups or active capabilities,
and verifies that identity before executing rootless Podman. Unsupported or failed setup refuses launch; there is
no downgrade to the account's existing Podman namespace.
Do not upgrade the host as part of these commands without separate owner approval.

First finish the unprivileged build/review with `deploy/containers/build-image` (see the
[image runbook](README.md#build-and-review-locally)). It builds in a private throwaway store that it removes, so
nothing of the image is left in the dedicated builder's own store. Retain the archive and review pins.
Separately, for each session agent account using its normal qualification runtime,
confirm that no older agent-account build left a layer in that account's store.
Do not log into the builder or enable its user manager for this check:

```sh
cd /srv/acc/reviewed/phase1
python3 -I scripts/container_qualification/image_store.py \
  --check-store sha256:REPLACE_WITH_REVIEWED_64_HEX_CONFIG_DIGEST
```

If it reports layers, or an account image that reuses the pinned ID, run exactly the commands it prints. They use `deploy/containers/account-store.conf` and remove only
the images and containers holding those layers, without `--force`. A layer with the same ID in the account's store
would shadow the protected image, and the launcher refuses such shadows. The archive is retained separately for root
import.

The following shell runs as root. Replace the two explicit pins with the reviewed
values, and use a reviewed root-owned source checkout at the example path. Do not
compute the expected checksum from an unreviewed input. A new installation refuses
an existing store; upgrades should use a separate reviewed store and a maintenance
window. No session image is pulled by the launcher.

```sh
sudo -i
set -eu
REVIEWED=/srv/acc/reviewed/phase1
REVIEWED_ARCHIVE=/var/lib/rynhult-image-review-20261026-1/reviewed.oci.tar
ARCHIVE_SHA256=REPLACE_WITH_REVIEWED_64_HEX_ARCHIVE_SHA256
IMAGE_ID=sha256:REPLACE_WITH_REVIEWED_64_HEX_CONFIG_DIGEST
# Refuse unsafe existing paths; never repair an agent-writable directory with chmod/chown.
# Staging and the store must both be new, including on retry after an interrupted import.
python3 -I - "$REVIEWED" <<'PY'
import os
import stat
import sys
from pathlib import Path
def protected(path):
    for item in (path, *path.parents):
        info = item.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, item
        assert not {'system.posix_acl_access', 'system.posix_acl_default'} & set(os.listxattr(item)), item
for path in (Path('/var/lib'), Path('/run'), Path('/srv/acc/containers'), Path('/etc/acc/containers'), Path(sys.argv[1])):
    protected(path)
base = Path('/var/lib/acc')
try:
    base.mkdir(mode=0o755)
except FileExistsError:
    pass
protected(base)
(base / 'image-import').mkdir(mode=0o700)
(base / 'session-images').mkdir(mode=0o700)
Path('/run/acc-image-import').mkdir(mode=0o700)
qualification = Path('/etc/acc/containers/qualification')
try:
    qualification.mkdir(mode=0o755)
except FileExistsError:
    pass
protected(qualification)
PY
install -o root -g root -m 0600 "$REVIEWED_ARCHIVE" \
  /var/lib/acc/image-import/reviewed.oci.tar
cat > /var/lib/acc/image-import/storage.conf <<'CONF'
[storage]
driver = "overlay"
CONF
# This verifies the copied bytes, manifest/config, layer blobs and diff_ids, and that every read-only mount target
# (slim_rootfs MOUNT_*) survives all layers as the right kind (e.g. a RUN in the final stage removed /run/.containerenv).
python3 -I "$REVIEWED/deploy/containers/verify_image_archive.py" \
  /var/lib/acc/image-import/reviewed.oci.tar "$ARCHIVE_SHA256" "$IMAGE_ID"
env -i PATH=/usr/bin:/bin HOME=/root CONTAINERS_CONF=/dev/null \
  CONTAINERS_CONF_OVERRIDE=/dev/null \
  CONTAINERS_STORAGE_CONF=/var/lib/acc/image-import/storage.conf \
  podman --root /var/lib/acc/session-images --runroot /run/acc-image-import \
  --storage-driver overlay load --input /var/lib/acc/image-import/reviewed.oci.tar
ACTUAL_ID=$(env -i PATH=/usr/bin:/bin HOME=/root CONTAINERS_CONF=/dev/null \
  CONTAINERS_CONF_OVERRIDE=/dev/null \
  CONTAINERS_STORAGE_CONF=/var/lib/acc/image-import/storage.conf \
  podman --root /var/lib/acc/session-images --runroot /run/acc-image-import \
  --storage-driver overlay image inspect --format '{{.Id}}' "$IMAGE_ID")
test "${ACTUAL_ID#sha256:}" = "${IMAGE_ID#sha256:}"
# Materialize an independent execution tree from this verified image, never hardlink it.
# Root mounts the image for copying only; root never executes image code.
MOUNTED=$(env -i PATH=/usr/bin:/bin HOME=/root CONTAINERS_CONF=/dev/null \
  CONTAINERS_CONF_OVERRIDE=/dev/null \
  CONTAINERS_STORAGE_CONF=/var/lib/acc/image-import/storage.conf \
  podman --root /var/lib/acc/session-images --runroot /run/acc-image-import \
  --storage-driver overlay image mount "$IMAGE_ID")
mkdir -m 0700 /var/lib/acc/session-images/rootfs
mkdir -m 0700 /var/lib/acc/session-images/rootfs/.pending
cp -a --reflink=never "$MOUNTED/." /var/lib/acc/session-images/rootfs/.pending/
env -i PATH=/usr/bin:/bin HOME=/root CONTAINERS_CONF=/dev/null \
  CONTAINERS_CONF_OVERRIDE=/dev/null \
  CONTAINERS_STORAGE_CONF=/var/lib/acc/image-import/storage.conf \
  podman --root /var/lib/acc/session-images --runroot /run/acc-image-import \
  --storage-driver overlay image unmount "$IMAGE_ID"
python3 -I - "$IMAGE_ID" <<'PY'
import os
import sys
fd = os.open('/var/lib/acc/session-images/rootfs/.pending/.acc-image-id',
             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
with os.fdopen(fd, 'w') as stream:
    stream.write(sys.argv[1] + '\n')
PY
mv -T /var/lib/acc/session-images/rootfs/.pending \
  "/var/lib/acc/session-images/rootfs/${IMAGE_ID#sha256:}"
# No ACL may grant an agent write access. Do not dereference image symlinks.
setfacl -Rb /var/lib/acc/session-images
find /var/lib/acc/session-images -type d -exec setfacl -k {} +
chown -hR root:root /var/lib/acc/session-images
# The image is agent-built: before the store becomes world-readable, refuse any device node (an overlay
# whiteout, c 0 0, is not one) and strip setuid/setgid bits and file capabilities, explicitly rather than
# relying on chown. A failure here means stop: the store is still 0700. (The kit's harden() does the same.)
python3 -I - <<'PY'
import os, stat
for folder, dirs, files in os.walk('/var/lib/acc/session-images', followlinks=False):
    for name in dirs + files:
        path = os.path.join(folder, name)
        info = os.lstat(path)
        if stat.S_ISBLK(info.st_mode) or (stat.S_ISCHR(info.st_mode) and info.st_rdev != 0):
            raise SystemExit('device node in imported image; stop: ' + path)
        if stat.S_ISREG(info.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
                if 'security.capability' in os.listxattr(fd):
                    os.removexattr(fd, 'security.capability')
            finally:
                os.close(fd)
PY
chmod -R a+rX,go-w /var/lib/acc/session-images
# Publish the launcher's exact reviewed additionalimagestores profile.
install -o root -g root -m 0644 "$REVIEWED/deploy/containers/storage.conf" \
  /srv/acc/containers/storage.conf
install -o root -g root -m 0644 "$REVIEWED/deploy/containers/acc-container-exec" \
  /srv/acc/containers/acc-container-exec
# Preserve all policy entries; atomically pin the default to the inspected ID.
python3 -I - "$IMAGE_ID" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
path = Path('/etc/acc/containers/policy.json')
fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(fd) as stream:
    info = os.fstat(stream.fileno())
    assert stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    policy = json.load(stream)
assert policy['version'] == 1 and isinstance(policy['images']['default'], dict)
policy['images']['default']['image'] = sys.argv[1]
fd, name = tempfile.mkstemp(prefix='.policy-', dir=path.parent)
with os.fdopen(fd, 'w') as stream:
    json.dump(policy, stream, indent=2)
    stream.write('\n')
    os.fchmod(stream.fileno(), 0o644)
os.replace(name, path)
PY
```

The pinned `image` value in the root-owned policy is the expected image ID, not
a tag or the archive checksum. Keep `/etc/acc/containers`, `/srv/acc/containers`
and `/var/lib/acc` root-owned without agent-write ACLs. The import configuration
has no additional store; the launch configuration lists only
`/var/lib/acc/session-images` under `[storage.options].additionalimagestores`.
Root's store is consumed read-only by rootless Podman. Root must not run images
from it. The launcher also requires the complete pinned `rootfs/<config-digest>`
tree and its `.acc-image-id` publication marker. It holds that directory open,
binds it read-only into unique systemd staging, then uses plain `podman --rootfs`
without `:O`. There is no second image-name/ID lookup at execution time: importing
a same-ID image after verification cannot replace the execution source.
Missing or mismatched trees fail closed; there is no image-mode fallback.
Keep-id maps the host agent to the image's existing `acc` user (UID/GID 1000);
passwd injection is disabled, so the protected tree never needs account edits.
The service also requires a new root-owned full-identity user namespace. Its
guard verifies the namespace identity, full UID/GID maps and root ownership
before executing Podman. A namespace-private tmpfs supplies fresh graphroot,
runroot, temporary state and `XDG_RUNTIME_DIR`. This prevents reuse of a hostile
account pause namespace or state, and blocks host same-UID proc-root/fd aliases.
Podman uses cgroupfs within the already delegated, systemd-limited unit. The unit
also gets an empty read-only `/run/user`, so Podman can't reach the account's
user-manager bus. Without that, Podman moved its pause process out of the unit:
godbus finds the bus at `/run/user/<uid>/bus` from the uid, not from the private
`XDG_RUNTIME_DIR` (qualification attempt 10; see docs/CONTAINERS.md).
If `setfacl` is unavailable, stop and have the owner provision the tool;
do not skip ACL removal. This task does not install tools.

## Prove outside-container immutability

Stop the account's other sessions. Use the reviewed, root-owned script, and
capture its stdout directly into a root-only temporary file. Do not copy a report
produced by an agent session. The command starts a short, networkless keep-id
container from the held protected rootfs, checks its actual process-root inode
and read-only setting, then tries opening **every regular execution-tree file** for writing from the host and
again through `podman unshare`. It never writes or truncates a file. A writable
file, failed/unavailable unshare, a missing tree, or a keep-id root copy into
account storage fails. Cleanup removes only the probe's unique container.
It also requires denied access to a root-owned test service's proc-root,
stdin descriptor and user/mount namespaces, natively and through `podman unshare`.
Missing processes or unavailable namespace support are failures, not denials.

```sh
# Still in the root administrative shell above; same reviewed IMAGE_ID.
# This directory was created/validated by the preflight above, not repaired with chmod.
test -d /etc/acc/containers/qualification
PROBE_UNIT="acc-ct05-namespace-$(python3 -I -c 'import uuid; print(uuid.uuid4().hex)')"
# The protected guard's bootstrap is also used by the production service.
# This probe needs no cgroup delegation because it only sleeps.
systemd-run --quiet --collect --unit="$PROBE_UNIT" --uid=0 --gid=0 \
  --working-directory=/ --expand-environment=no --property=PrivateUsers=no \
  --property=PrivateMounts=yes --property=Type=exec /usr/bin/python3 -I -c '
import os, runpy, sys
ns = os.stat("/proc/self/ns/user")
guard = runpy.run_path("/srv/acc/containers/acc-container-exec")
guard["enter_namespace"](int(sys.argv[1]), int(sys.argv[2]), (ns.st_dev, ns.st_ino))
os.execl("/usr/bin/sleep", "sleep", "120")
' "$(id -u alex-agent)" "$(id -g alex-agent)"
trap 'systemctl stop "$PROBE_UNIT"' EXIT
NAMESPACE_PID=$(systemctl show --property=MainPID --value "$PROBE_UNIT")
test "$NAMESPACE_PID" -gt 1
# Type=exec acknowledges Python startup, not completed namespace setup.
# Wait for sleep, then require the expected account identity before probing.
READY=no
for ATTEMPT in $(seq 1 100); do
  if test "$(cat /proc/"$NAMESPACE_PID"/comm 2>/dev/null)" = sleep; then
    READY=yes
    break
  fi
  sleep 0.1
done
test "$READY" = yes
test "$(stat -c %u /proc/"$NAMESPACE_PID")" = "$(id -u alex-agent)"
PROOF=$(mktemp /etc/acc/containers/qualification/.alex-agent.XXXXXX)
if runuser -u alex-agent -- env -i HOME=/home/alex-agent PATH=/usr/bin:/bin \
  XDG_RUNTIME_DIR=/run/user/$(id -u alex-agent) \
  /usr/bin/python3 -I "$REVIEWED/scripts/container_qualification/image_store.py" \
  "$IMAGE_ID" "$NAMESPACE_PID" > "$PROOF"; then
  chmod 0644 "$PROOF"
  mv -T "$PROOF" /etc/acc/containers/qualification/alex-agent.json
else
  rm -f "$PROOF"
  exit 1
fi
systemctl stop "$PROBE_UNIT"
trap - EXIT
```

Run the trusted-launcher qualification as `acc` within one hour. Its new mandatory
`image_store` row consumes only a fresh root-captured proof for the selected
account and image ID; payload reports cannot mark that row passed. Regenerate the
proof after every store or image change. Old image-lookup proofs are rejected:
the new proof must include `execution_source: held-rootfs` and
`namespace_boundary: denied`. A local diagnostic run skips this row
and cannot return GO. Keep-id and read-only shared-store support depends on the
runtime/filesystem: a copy into writable storage is a NO-GO, never grounds to
relax the probe or use the account store. No root-store runtime qualification was
performed during this implementation.
