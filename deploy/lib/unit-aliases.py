#!/usr/bin/python3 -I
"""Add systemd aliases without enabling units, starting jobs or replacing objects."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

# Only reviewed services and activation units. Slices cannot have aliases.
UNITS = (
    'accd.service', 'accd.socket', 'acc-broker@.service', 'acc-broker@.socket', 'acc-node.service',
    'acc-backup.service', 'acc-backup.timer', 'acc-backup-receiver.service',
    'acc-dns.service', 'acc-dns.socket', 'acc-dns-purge.service', 'acc-dns-purge.timer',
    'acc-containers.socket', 'acc-containers@.service',
    'acc-containers-reconcile.service', 'acc-containers-reconcile.timer',
    'acc-publish.service', 'acc-publish.path', 'acc-accounts.service', 'acc-accounts.path',
    'acc-packages.service', 'acc-packages.path', 'acc-packages-daily.service', 'acc-packages-daily.timer',
    'acc-update.service', 'acc-update.path', 'acc-hoststate.service', 'acc-hoststate.timer',
    'acc-root-session.service', 'acc-telemetry.service',
)


# Other places systemd loads units from. An alias in /etc would shadow a different unit of the same name there.
SEARCH_DIRS = ('/run/systemd/system', '/run/systemd/transient', '/usr/local/lib/systemd/system',
               '/usr/lib/systemd/system', '/lib/systemd/system')


def install_aliases(directory: Path, *, check_only=False, search_dirs=None, logical_directory=None):
    directory = directory.absolute()
    if search_dirs is None:
        search_dirs = SEARCH_DIRS if directory == Path('/etc/systemd/system') else ()
    others = [Path(d) for d in search_dirs if Path(d).absolute() != directory]
    pending = []
    for name in UNITS:
        source = directory / name
        if not os.path.lexists(source):
            continue
        if not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError(f'unit conflict: {source}; masked/linked units require manual review')
        alias = directory / ('rynhult' + name[3:])
        expected = (name, str(source))
        if logical_directory is not None:
            # Offline snapshots retain systemctl's absolute host-namespace links.
            expected += (str(Path(logical_directory) / name),)
        if '@.' in name:
            # Template aliases must not hide a custom/masked instance. systemctl
            # enable creates instance aliases pointing at the same old template.
            for dropin in directory.glob(alias.name.replace('@.', '@*.') + '.d'):
                raise ValueError(f'unit conflict: {dropin}; review new-name instance drop-ins')
            for pattern in (name.replace('@.', '@*.'), alias.name.replace('@.', '@*.')):
                for instance in directory.glob(pattern):
                    if instance.name in (name, alias.name):
                        continue
                    if not instance.is_symlink() or os.readlink(instance) not in expected:
                        raise ValueError(f'unit conflict: {instance}; review custom/masked instance')
        for other in others:
            if '@.' in name:
                for pattern in (name.replace('@.', '@*.'), alias.name.replace('@.', '@*.') + '*'):
                    for instance in other.glob(pattern):
                        if instance.name in (name, alias.name):
                            continue
                        raise ValueError(f'unit conflict: {instance}; review vendor/runtime instance or drop-in')
            for path in (other / alias.name, other / (alias.name + '.d')):
                if os.path.lexists(path):
                    raise ValueError(f'unit conflict: {path}; an alias would shadow this unit')
        if os.path.lexists(str(alias) + '.d'):
            raise ValueError(f'unit conflict: {alias}.d; review new-name drop-ins')
        if os.path.lexists(alias):
            if alias.is_symlink() and os.readlink(alias) in expected:
                continue
            raise ValueError(f'unit conflict: {alias}; refusing to overwrite')
        pending.append((alias, name))
    if check_only:
        return
    # Preflight the complete set before creating anything. symlink fails closed
    # if a destination appears between preflight and creation; never use ln -sf.
    for alias, name in pending:
        alias.symlink_to(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit-dir', type=Path, default=Path('/etc/systemd/system'))
    parser.add_argument('--check', action='store_true', help='Preflight only; do not create aliases')
    args = parser.parse_args()
    try:
        install_aliases(args.unit_dir, check_only=args.check)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
