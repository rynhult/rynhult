#!/usr/bin/python3 -I
"""Assemble only runtime files and their ELF dependencies from the pinned builder."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXCLUDES = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', 'test', 'tests', 'idlelib', 'tkinter',
                                  'turtledemo', 'ensurepip', 'pydoc_data', 'README*', 'CHANGELOG*', 'docs', 'man', 'voice', 'webview', 'pvrecorder', 'foundry-local-sdk')


ACL = ('system.posix_acl_access', 'system.posix_acl_default')
STICKY = ('tmp', 'var/tmp')  # world-writable mount points with the sticky bit (covered by a tmpfs at run time)
# The only commands on the image's PATH. The image's shell scripts (acc-hook) may use nothing else.
COMMANDS = ('sh dash bash env cat date cut touch readlink sleep mkdir chmod cp mv rm ln ls pwd '
            'find sort sha256sum head tail sed grep awk uname kill ps tar gzip git git-shell '
            'git-upload-pack git-receive-pack python3 node').split()


def copy_tree(source, target):
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, ignore=EXCLUDES)


def entries(root):
    yield root
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            yield Path(folder) / name


def normalize(root):
    """Image bytes belong to root: no group/other write, set-id bit or ACL survives from the base or the build host.

    The pinned base ships Chromium 0777, and a build folder with default ACLs turns new files' group bits into rwx.
    The read-only rootfs is the boundary; this keeps it from being the only one."""
    for path in entries(root):
        if path.is_symlink():
            continue
        for attribute in ACL:
            try:
                os.removexattr(path, attribute, follow_symlinks=False)
            except OSError:
                pass
        mode = path.lstat().st_mode & 0o7777
        os.chmod(path, 0o1777 if path in [root / name for name in STICKY] else mode & ~0o7022, follow_symlinks=False)


def unsafe_entries(root):
    found = []
    for path in entries(root):
        if path.is_symlink():
            continue
        name = '/' + str(path.relative_to(root)) if path != root else '/'
        mode = path.lstat().st_mode & 0o7777
        try:
            acl = any(attribute in os.listxattr(path, follow_symlinks=False) for attribute in ACL)
        except OSError:
            acl = False
        if acl or (mode != 0o1777 if name.lstrip('/') in STICKY else mode & 0o7022):
            found.append(name)
    return found


# Everything Podman 4.9 (libpod) and crun 1.14 would otherwise create on the rootfs at run time. The published rootfs
# is bind-mounted read-only before Podman starts, so a missing one fails the launch (e.g. "creating /etc/mtab symlink:
# permission denied", exit 126):
# - libpod symlinks /etc/mtab unless it exists (container_internal_common.go: any existing entry is accepted);
# - libpod bind-mounts its generated /etc/hosts, /etc/hostname and /etc/resolv.conf, and /run/.containerenv (the
#   launcher passes --read-only-tmpfs=false, so /run is not a tmpfs), and crun must find each target;
# - crun mounts proc, sysfs, the /dev tmpfs (shm, pts and mqueue are created inside it) and the launcher's tmpfs
#   mounts (/tmp, /run/credentials) on existing directories;
# - with --read-only and the default --read-only-tmpfs=true (the CT-05 probe in
#   scripts/container_qualification/image_store.py), libpod adds tmpfs mounts at /run, /tmp and /var/tmp, so crun needs
#   /var/tmp ("crun: mkdir `/var`: Permission denied", owner run attempt 8); /var/run links to ../run as on the host;
# - the session's binds (/run/acc-container/contract.json, the run folder under /srv/acc/run, the checkout under
#   /srv/acc/workspaces) land on the launcher's small tmpfs mounts at /srv/acc/run and /srv/acc/workspaces, which crun
#   can create deeper mount points in.
MOUNT_DIRECTORIES = ('proc', 'dev', 'sys', 'run', 'tmp', 'var', 'var/tmp', 'run/acc-container', 'run/credentials',
                     'srv/acc/run', 'srv/acc/workspaces', 'run/acc-credentials', 'run/acc-egress')
MOUNT_FILES = ('etc/hosts', 'etc/hostname', 'etc/resolv.conf', 'run/.containerenv', 'run/acc-container/contract.json',
               'run/acc-credentials/auth.json', 'run/acc-egress/socket')
# Phase-2 adapter targets (decision 0072), present in every image so the launcher can bind its sealed credential file and
# egress socket over them. Without an adapter they keep this placeholder: Codex's ~/.codex/auth.json links to the
# credential target, and an empty file there would not parse.
ADAPTER_PLACEHOLDERS = ('run/acc-credentials/auth.json', 'run/acc-egress/socket')
MOUNT_LINKS = {'etc/mtab': '../proc/self/mounts', 'var/run': '../run'}
# Relative, as every link the build makes: it resolves inside the rootfs wherever the rootfs is looked at (security review
# SV-2: an absolute target would follow the host's /usr/local/bin when the rootfs is read outside a container).
IMAGE_LINKS = {'srv/acc/bin': '../../usr/local/bin'}


def mount_targets(root):
    for folder in MOUNT_DIRECTORIES:
        (root / folder).mkdir(parents=True, exist_ok=True)
    for name in STICKY:
        (root / name).chmod(0o1777)
    for name in MOUNT_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).touch(mode=0o444 if name.startswith('run/acc-container/') else 0o644)
    for name, target in MOUNT_LINKS.items():
        if (root / name).is_symlink() or (root / name).exists():
            (root / name).unlink()
        (root / name).symlink_to(target)
    for name in ADAPTER_PLACEHOLDERS:
        (root / name).write_text('{}\n')
        (root / name).chmod(0o444)
    for folder in ('run/acc-container', 'run/acc-credentials', 'run/acc-egress'):
        (root / folder).chmod(0o555)


def assemble(root):
    root.mkdir()
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        (root / 'usr' / name).mkdir(parents=True, exist_ok=True)
        (root / name).symlink_to('usr/' + name)
    queue, scanned = [], set()

    def copy_file(source):
        source = Path(source)
        source = source.parent.resolve() / source.name
        dest = root / str(source).lstrip('/')
        if dest.is_symlink() or dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            dest.symlink_to(os.readlink(source))
            if source.exists():
                copy_file(source.resolve())
        else:
            shutil.copy2(source, dest)
            with source.open('rb') as file:
                if file.read(4) == b'\x7fELF':
                    queue.append(source)

    def tree(source):
        source = Path(source)
        dest = root / str(source).lstrip('/')
        copy_tree(source, dest)
        for copied in dest.rglob('*'):
            if copied.is_file() and not copied.is_symlink():
                original = source / copied.relative_to(dest)
                with original.open('rb') as file:
                    if file.read(4) == b'\x7fELF':
                        queue.append(original)

    for command in COMMANDS:
        path = shutil.which(command)
        if path:
            copy_file(path)
    for path in ('/usr/lib/python3.12', '/usr/lib/git-core', '/usr/share/git-core/templates',
                 '/etc/ssl/certs', '/etc/fonts', '/etc/claude-code', '/usr/share/fonts/truetype/dejavu',
                 '/usr/share/common-licenses', '/home/acc', '/opt/acc/src/acc/providers',
                 '/opt/acc/qualification', '/opt/acc/copilot', '/opt/acc/runtime'):
        if Path(path).exists():
            tree(path)
    for site in glob.glob('/usr/local/lib/python3.12/*-packages/yaml'):
        tree(site)
    for package in ('@anthropic-ai/claude-code-linux-x64', '@openai/codex-linux-x64'):
        tree('/opt/acc/cli/node_modules/' + package)
    browsers = glob.glob('/ms-playwright/chromium-*/chrome-linux*')
    if len(browsers) != 1:
        raise RuntimeError('one full Chromium runtime required')
    tree(browsers[0])
    locales = root / browsers[0].lstrip('/') / 'locales'
    if locales.exists():
        for locale in locales.iterdir():
            if locale.name != 'en-US.pak':
                locale.unlink()
    for pattern in ('/usr/lib/x86_64-linux-gnu/libnss_dns.so.2', '/usr/lib/x86_64-linux-gnu/libnss_files.so.2'):
        for path in glob.glob(pattern):
            copy_file(path)
    for path in ('/etc/nsswitch.conf', '/etc/localtime', '/etc/ld.so.cache', '/etc/ssl/openssl.cnf', '/usr/local/LICENSE',
                 '/opt/acc/src/acc/__init__.py', '/opt/acc/src/acc/env.py', '/opt/acc/claude-settings.json', '/opt/acc/state-directories.json',
                 '/opt/acc/state-files.json', '/opt/acc/system-packages.txt', '/opt/acc/python-packages.txt',
                 '/opt/acc/cli/package-lock.json', '/opt/acc/cli/requirements.txt'):
        if Path(path).exists():
            copy_file(path)
    for name in ('acc_env.py', 'acc-env.sh', 'acc-guard', 'acc-hook', 'acc-request', 'acc-tool-policy.py', 'acc-mcp-team', 'acc-statusline', 'acc-session', 'claude', 'codex', 'copilot'):
        copy_file('/usr/local/bin/' + name)
    while queue:
        binary = queue.pop()
        if binary in scanned:
            continue
        scanned.add(binary)
        libraries = subprocess.run(['/usr/bin/ldd', str(binary)], capture_output=True, text=True)
        text = libraries.stdout + libraries.stderr
        if 'not found' in text:
            raise RuntimeError('missing ELF dependency for ' + str(binary))
        for library in re.findall(r'(?<!\S)(/[^\s()]+)', text):
            if Path(library).is_file():
                copy_file(library)
    mount_targets(root)
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\nacc:x:1000:1000:ACC:/home/acc:/bin/sh\n')
    (root / 'etc/group').write_text('root:x:0:\nacc:x:1000:\n')
    for name, target in IMAGE_LINKS.items():
        (root / name).symlink_to(target)
    normalize(root)
    unsafe = unsafe_entries(root)
    if unsafe:
        raise RuntimeError('writable or set-id image entries: ' + ', '.join(unsafe[:10]))
    hashes = []
    for file in sorted((root / 'opt/acc').rglob('*')):
        if file.is_file() and not file.is_symlink():
            digest = hashlib.file_digest(file.open('rb'), 'sha256').hexdigest()
            hashes.append(digest + '  /' + str(file.relative_to(root)))
    (root / 'opt/acc/SHA256SUMS').write_text('\n'.join(hashes) + '\n')
    size = sum(file.stat().st_size for file in root.rglob('*') if file.is_file() and not file.is_symlink())
    (root / 'opt/acc/rootfs-size.json').write_text(json.dumps({'uncompressed_file_bytes': size,
                                                            'elf_objects': len(scanned)}))


if __name__ == '__main__':
    assemble(Path(sys.argv[1]))
