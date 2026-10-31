"""Owner-invoked signed local-bundle updates, sharing the normal update transaction.

No release discovery, downloads or unsigned exception. The privileged entry point
loads this module only from the owner's reviewed, root-protected new source kit.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import re
from pathlib import Path

from . import bundle
from .env import Environment, environment
from .release_signatures import verify
from .update_helper import Transaction, read_json, write_json
from .updates import BUILD, release_checksum, version


def bounded(path: Path, limit: int) -> bytes:
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Release metadata exceeds the size limit.')
    return raw


def configure_environment(settings_file: Path, backup_file: Path, data: Path):
    """Use the installed backend and backup encryption settings, never shell-source them."""
    from .node.config import read_env_file
    settings = Environment(read_env_file(settings_file))
    if Path(settings.get('ACC_DATA_DIR', str(data))) != data:
        raise ValueError('This upgrade requires the standard data directory; use an owner-managed recovery plan.')
    environment['ACC_DATABASE_URL'] = settings.get('ACC_DATABASE_URL', '')
    try:
        backup = Environment(read_env_file(backup_file))
    except FileNotFoundError:
        backup = Environment({})
    # Match the backup service's encryption, without importing unrelated service
    # environment or allowing configuration to redirect the update commands.
    environment['ACC_BACKUP_PASSPHRASE_FILE'] = backup.get('ACC_BACKUP_PASSPHRASE_FILE', '')


class OfflineTransaction(Transaction):
    def __init__(self, *args, archive=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.archive = Path(archive) if archive is not None else None
        self.old_build = bundle.installed_identity(self.source)

    def backup(self):
        if version(self.target) <= version(bundle.version_of(self.source)):
            raise ValueError('Choose a newer release; use the explicit rollback command for recovery.')
        super().backup()

    def download(self):
        sums = bounded(self.archive.parent / 'SHA256SUMS', 1 << 20)
        self.state['signature'] = verify(sums, bounded(self.archive.parent / 'SHA256SUMS.asc', 128 << 10))
        checksum = release_checksum(sums, self.target)
        if self.archive.name != f'acc-{self.target}.tar.gz' or self.archive.stat().st_size > 256 << 20:
            raise ValueError('Choose the matching source archive (maximum 256 MiB).')
        self.stage.mkdir(mode=0o700)
        tree = bundle.unpack(self.archive, self.stage / 'unpacked', self.target, checksum)
        if bundle.version_of(tree) != self.target:
            raise ValueError('Release source version mismatch.')
        stamp = bounded(tree / '.acc-build', 65).decode('ascii').strip()
        if not BUILD.fullmatch(stamp):
            raise ValueError('Release build identity is missing or invalid.')
        # The signed checksum authenticated all archive bytes. Do not recompute
        # identity with exclusion rules that might differ between releases.
        self.expected_build = stamp
        return tree

    def after_success(self):
        journal = self.root / 'offline'
        journal.mkdir(mode=0o700, exist_ok=True)
        write_json(journal / f'{self.id}.json', {
            'id': self.id, 'version': self.target, 'build': self.expected_build,
            'from_version': self.state['from_version'], 'from_build': self.old_build,
            'backup_day': self.saved.name, 'rolled_back': False,
        })
        self.state['detail'] = f'Updated; rollback ID {self.id}. Previous source and full backup retained.'

    def restore(self):
        super().restore()
        target, expected = self.target, getattr(self, 'expected_build', None)
        try:
            self.target = self.state['from_version']
            self.expected_build = self.old_build
            self.health()
        finally:
            self.target, self.expected_build = target, expected


def rollback(root, source, data, identifier, config, *, transaction_class=OfflineTransaction, run=None):
    """Explicit, single-use rollback using the same DB/source restore as failed updates."""
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('Invalid rollback ID.')
    path = root / 'offline' / f'{identifier}.json'
    record = read_json(path)
    if (record.get('rolled_back') or record['version'] != bundle.version_of(source)
            or record['build'] != bundle.installed_identity(source) or (source / '.git').exists()):
        raise ValueError('Rollback does not match the current installation or was already used.')
    day = record['backup_day']
    if datetime.date.fromisoformat(day).isoformat() != day:
        raise ValueError('Invalid backup date.')
    job = transaction_class(root, source, data, record['version'], config, run=run)
    # Retain a fresh backup of today's state before reverting its schema/data.
    from .maintenance import update_hold
    with update_hold(data):
        Transaction.backup(job)
        job.saved = root / 'backups' / identifier / day
        if not (job.saved / 'acc.db').is_file():
            raise ValueError('Retained database backup is missing.')
        job.previous = source.with_name(f'{source.name}.previous-{identifier}')
        if not job.previous.is_dir() or job.previous.is_symlink():
            raise ValueError('Retained source is missing or unsafe.')
        job.stage.mkdir(mode=0o700)
        job.activated = job.install_started = True
        job.state['from_version'] = record['from_version']
        job.old_build = record['from_build']
        job.restore()
        record['rolled_back'] = True
        write_json(path, record)
        job.state.update(ok=True, running=False, rolled_back=True, detail='Previous source and database restored.')
        job.save()
    return job.state


def main(argv=None):
    parser = argparse.ArgumentParser(description='Upgrade a bundle install from signed local release files.')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--bundle', type=Path)
    action.add_argument('--rollback', metavar='ID')
    parser.add_argument('--version')
    parser.add_argument('--yes', action='store_true', help='confirm dashboard restart and possible database rollback')
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or not args.yes:
        parser.error('Run as root with --yes after reviewing the upgrade Guide and taking a backup.')
    if args.bundle and not args.version:
        parser.error('--bundle requires --version')
    root, source, data = Path('/srv/acc/updates'), Path('/srv/acc/app'), Path('/srv/acc/data')
    # Match systemd EnvironmentFile semantics without sourcing shell code. In
    # particular, never mistake a leftover SQLite DB for a PostgreSQL backup.
    try:
        configure_environment(Path('/etc/default/accd'), Path('/etc/default/acc-backup'), data)
    except (OSError, ValueError):
        print('Upgrade refused. Check the installed database and backup configuration; standard paths are required.')
        return 1
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    config = {'group': 'acc'}
    with (root / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.rollback:
                result = rollback(root, source, data, args.rollback, config)
            else:
                result = OfflineTransaction(root, source, data, args.version, config, archive=args.bundle).apply()
        except (OSError, ValueError):
            print('Upgrade/recovery refused. Check the signed files, retained backups and installation settings.')
            return 1
    print(result['detail'])
    if result.get('rollback_error'):
        print(result['rollback_error'])
    return 0 if result.get('ok') else 1
