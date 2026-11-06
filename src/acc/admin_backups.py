"""Root-console folder provisioning; never imported by the HTTP service."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from .backup_server import Refused
from .server.local_backups import refuse_volatile, repository_path


def service_settings():
    """Child settings come only from the explicit environment supplied by root."""
    from .env import Environment
    from .server.config import Settings
    return Settings.from_env(Environment({key: os.environ[key] for key in
                                          ('ACC_DATA_DIR', 'ACC_DATABASE_URL') if key in os.environ}))


def service_environment(env_file=Path('/etc/default/accd')):
    """Read root-only defaults once; export only backup state and database settings."""
    from .env import Environment
    from .node.config import read_env_file
    from .server.config import Settings
    settings = Settings.from_env(Environment(read_env_file(env_file) if env_file.exists() else {}))
    return {'PATH': '/usr/local/bin:/usr/bin:/bin',
            'ACC_DATA_DIR': str(settings.data_dir), 'ACC_DATABASE_URL': settings.db_url}


def trusted_directory(info, uid):
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Refused('Storage and all parents must be root-owned and not group/other writable.')


def open_folder(path, uid):
    """Pin every component without following symlinks, including during root writes."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        trusted_directory(os.fstat(fd), uid)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            trusted_directory(os.fstat(fd), uid)
        return fd
    except BaseException:
        os.close(fd)
        raise


def remove_repository(repository, parent_info, repository_info, uid):
    """Remove only the empty directory created by root, through a trusted parent."""
    fd = open_folder(repository.parent, uid)
    try:
        pinned, current = os.fstat(fd), repository.parent.lstat()
        identity = lambda info: (info.st_dev, info.st_ino)
        if identity(pinned) != identity(parent_info) or identity(current) != identity(pinned):
            raise Refused('Storage changed during rollback.')
        child = os.stat(repository.name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISDIR(child.st_mode) or identity(child) != identity(repository_info):
            raise Refused('Repository changed during rollback.')
        os.rmdir(repository.name, dir_fd=fd)
    finally:
        os.close(fd)


def add_folder(folder, same_disk=False, *, data=None,
               dropins=Path('/etc/systemd/system/accd.service.d'), run=subprocess.run, account=None,
               env_file=Path('/etc/default/accd')):
    if os.getuid() != 0 or os.geteuid() != 0:
        raise Refused('Requires real and effective root on the host.')
    if any(ord(c) < 32 for c in folder):
        raise Refused('Control characters are not allowed in paths.')
    child_env = service_environment(env_file)
    data = Path(child_env['ACC_DATA_DIR']) if data is None else data
    repository = repository_path(folder, data, same_disk)
    user = account or pwd.getpwnam('acc')
    fd = open_folder(repository.parent, user.pw_uid)
    try:
        pinned, current = os.fstat(fd), repository.parent.stat()
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise Refused('Storage changed during validation; try again.')
        os.mkdir(repository.name, mode=0o700, dir_fd=fd)
        repository_info = os.stat(repository.name, dir_fd=fd, follow_symlinks=False)
        os.chown(repository.name, user.pw_uid, user.pw_gid, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    config = dropins / ('backup-' + repository.name + '.conf')
    created = False
    def as_acc(option, value):
        return run(['/usr/sbin/runuser', '-u', 'acc', '--', sys.executable, '-I', '-m',
                    'acc.admin_backups', option, value], check=True, capture_output=True, text=True, env=child_env)
    try:
        dropins.mkdir(mode=0o755, parents=True, exist_ok=True)
        value = str(repository).replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"').replace('%', '%%')
        config_fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        created = True
        with os.fdopen(config_fd, 'w') as out:
            out.write('[Service]\nReadWritePaths="-' + value + '"\n')
        run(['systemctl', 'daemon-reload'], check=True)
        run(['systemctl', 'restart', 'accd.service'], check=True)
        result = json.loads(as_acc('--provision', str(repository)).stdout)
        result['first_job'] = json.loads(as_acc('--wait-job', result['first_job']['id']).stdout)
        if result['first_job'].get('state') in ('failed', 'cancelled'):
            raise Refused('First backup failed; folder setup was rolled back.')
        return result
    except BaseException as error:
        failures = []
        for action in (lambda: config.unlink(missing_ok=True) if created else None,
                       lambda: as_acc('--rollback', str(repository)),
                       lambda: remove_repository(repository, pinned, repository_info, user.pw_uid),
                       lambda: run(['systemctl', 'daemon-reload'], check=True),
                       lambda: run(['systemctl', 'restart', 'accd.service'], check=True)):
            try:
                action()
            except Exception as rollback_error:
                failures.append(type(rollback_error).__name__)
        if failures:
            raise Refused('Setup failed; rollback incomplete: ' + ', '.join(failures)) from error
        raise


def rollback(repository):
    from .backup_server import Service, canonical, private_write
    from .maintenance import lease
    if os.geteuid() == 0:
        raise Refused('Rollback must run as the service account.')
    repository = Path(repository)
    if not repository.name.startswith('rynhult-') or len(repository.name) != 40:
        raise Refused('Invalid provisioned repository.')
    service = Service(service_settings())
    with lease(service.root / '.coordinator'):
        targets = service.targets()
        removed = [t for t in targets if t.get('repository') == str(repository)]
        private_write(service.root / 'targets.json', canonical([t for t in targets if t not in removed]))
        for target in removed:
            (service.root / 'keys' / target['id']).unlink(missing_ok=True)
        if repository.is_symlink():
            raise Refused('Repository must not be a symlink.')
        if repository.exists():
            # The root-owned parent prevents acc from removing the repository itself.
            # Keep the directory for root's inode-checked, non-recursive rmdir.
            with os.scandir(repository) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path)
                    else:
                        os.unlink(entry.path)
    return {'rolled_back': True}


def provision(repository):
    from .backup_server import Service
    from .server.backup_jobs import BackupJobs
    from .server.db import make_engine, make_sessionmaker
    if os.geteuid() == 0:
        raise Refused('Repository provisioning must run as the service account.')
    refuse_volatile(Path(repository).parent)
    settings = service_settings()
    # Preserve the empty directory already bound into accd's mount namespace.
    target = Service(settings).add_local_target(repository, prepared=True)
    engine = make_engine(settings.db_url)
    try:
        jobs = BackupJobs(settings, make_sessionmaker(engine))
        job = jobs.request(target['id'], 'capture', '@local-admin')
        return {'target_id': target['id'], 'first_job': job, 'enabled': False}
    finally:
        engine.dispose()


def wait_job(job_id):
    from .server.backup_jobs import BackupJobs
    from .server.db import make_engine, make_sessionmaker
    settings = service_settings()
    engine = make_engine(settings.db_url)
    try:
        jobs = BackupJobs(settings, make_sessionmaker(engine))
        deadline = time.monotonic() + 300
        while True:
            job = next((j for j in jobs.jobs() if j['id'] == job_id), None)
            if job is None:
                raise Refused('Queued backup job not found.')
            if job['state'] not in ('queued', 'running') or time.monotonic() >= deadline:
                return job
            time.sleep(2)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback', help=argparse.SUPPRESS)
    parser.add_argument('--wait-job', help=argparse.SUPPRESS)
    parser.add_argument('--provision', help=argparse.SUPPRESS)
    parser.add_argument('command', nargs='?', choices=['add-folder'])
    parser.add_argument('folder', nargs='?')
    parser.add_argument('--same-disk', action='store_true')
    args = parser.parse_intermixed_args()
    try:
        if args.rollback:
            result = rollback(args.rollback)
        elif args.wait_job:
            result = wait_job(args.wait_job)
        elif args.provision:
            result = provision(args.provision)
        elif args.command and args.folder:
            result = add_folder(args.folder, args.same_disk)
        else:
            parser.error('expected add-folder [--same-disk] -- /absolute/folder')
        print(json.dumps(result))
        if not args.provision and not args.wait_job and not args.rollback:
            print('First backup requested. Check its result in Host → Backups; export a new recovery kit. Scheduled backups remain off.')
    except Exception as error:
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        parser.exit(1, f'Folder setup failed: {reason}. Check storage, permissions and the unit drop-in before retrying.\n')


if __name__ == '__main__':
    main()
