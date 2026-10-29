#!/usr/bin/python3 -I
"""Root-only owner tool: encrypted device-authority backup and restore (uninstalled).

  acc-device-ca-recovery backup <new backup file>
  acc-device-ca-recovery restore <backup file> <authority SHA-256 fingerprint>
  acc-device-ca-recovery rollover stage|promote|finish

The authority directory and passphrase file are fixed; neither accd nor the signer's
stdin contract can reach this tool. Output is the public fingerprint only.
"""
import importlib.util
import os
import stat
import sys
from pathlib import Path

PASSPHRASE = Path('/etc/acc-device-ca/backup-passphrase')
EDGE_POLICY = Path('/etc/acc-remote-access')  # the edge helper's root-provisioned authority files


def signer():
    path = Path(__file__).resolve().with_name('acc-device-ca.py')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o002
            or (info.st_mode & 0o020 and (os.geteuid() == 0 or info.st_gid != os.getegid()))):
        raise ValueError('unsafe device authority signer')
    if os.geteuid() == 0:
        for parent in path.parents:
            info = parent.stat(follow_symlinks=False)
            sticky = stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if info.st_uid != 0 or (info.st_mode & 0o022 and not sticky):
                raise ValueError('unsafe device authority signer directory')
    spec = importlib.util.spec_from_file_location('acc_device_ca', path)
    module = importlib.util.module_from_spec(spec)
    # Compile the checked source, not a potentially pre-existing bytecode cache.
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def passphrase(ca, path):
    raw = ca.private_read(path.parent, path.name).split(b'\n', 1)[0].strip()
    if len(raw) < 16:
        raise ValueError('backup passphrase is too short')
    return raw


def run(argv, *, state=None, secret=PASSPHRASE, helper=EDGE_POLICY):
    ca = signer()
    state = state or ca.STATE
    if argv[:1] == ['backup'] and len(argv) == 2 and Path(argv[1]).is_absolute():
        blob = ca.export_backup(state, passphrase(ca, secret))
        fd = os.open(argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(fd)
        return ca.fingerprint(ca.x509.load_pem_x509_certificate(ca.private_read(state, 'ca.pem')))
    if argv[:1] == ['restore'] and len(argv) == 3 and Path(argv[1]).is_absolute():
        fd = os.open(argv[1], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            blob = stream.read((1 << 24) + 1)
        return ca.restore_backup(blob, passphrase(ca, secret), state, argv[2])
    if argv[:1] == ['rollover'] and len(argv) == 2 and argv[1] in ('stage', 'promote', 'finish'):
        if argv[1] == 'stage':
            return ca.rollover_stage(state)
        return getattr(ca, 'rollover_' + argv[1])(state, helper)
    raise ValueError('usage: backup <file> | restore <file> <fingerprint> | rollover stage|promote|finish')


def main():
    try:
        if os.geteuid() != 0:
            raise ValueError('run as root')
        print(run(sys.argv[1:]))
        return 0
    except Exception as error:
        # Messages are fixed strings from this tool; never key material, passphrases or tracebacks.
        message = str(error) if isinstance(error, ValueError) and 'PRIVATE' not in str(error) else 'refused'
        sys.stderr.write(f'device authority recovery refused: {message}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
