"""Versioned offline recovery. No restored code or configuration is ever executed.

Local directory and enrolled Linux REST transports share repository operations.
The receiver never receives decryption keys. All main-side work runs unprivileged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .backup_policy import defaults, full_due
from .maintenance import git_lock, lease, secrets_lock

FORMAT = 1
MAX_FILES = 100000
MAX_BYTES = 20 << 30
RETENTION = {'hourly': 48, 'daily': 30, 'monthly': 12}
EXCLUSIONS = ['remote-spool', 'run', 'node private keys', 'workspaces and dirty trees',
              'provider transcripts/logins', 'containers/volumes', 'published application data']
ID = re.compile(r'^[0-9a-f]{32}$')
SNAPSHOT = re.compile(r'^[0-9a-f]{64}$')


class Refused(ValueError):
    pass


# Capture refusals the job worker matches (a kept stage may be what used up the budget).
STAGING_FULL = 'Not enough free disk space on the main machine to prepare the backup. Free some space, then back up again.'
OVER_LIMIT = 'Rynhult data is over the backup size or file limit.'


class RepositoryLocked(Refused):
    """Only allow-listed lock metadata may leave the restic boundary."""
    def __init__(self, operation, locks=()):
        self.locks = list(locks)
        details = '; '.join(f"host {l['hostname']}, PID {l['pid']}, age {l['age_seconds']}s, "
                            f"{'exclusive' if l['exclusive'] else 'non-exclusive'}" for l in self.locks)
        super().__init__(f"Backup storage is locked by another backup run (the backup tool's {operation} step stopped). " +
                         (details or 'Lock details unavailable') + '. Wait for that run to finish, or select Remove stale lock.')


class ResticFailed(Refused):
    """restic itself failed or timed out: the step and exit code, nothing the owner can act on without guidance."""


class BackendBusy(Refused):
    """This main is already running a repository command on the target."""


class RolledBack(Refused):
    """The target no longer lists the last generation this main verified."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def private_write(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_path(name):
    p = PurePosixPath(name)
    if (not name or '\\' in name or '\0' in name or p.is_absolute() or
            any(x in ('', '.', '..') for x in name.split('/')) or
            not (name == 'acc.db' or (len(p.parts) > 1 and p.parts[0] in ('secrets', 'catalog', 'git', 'config')))):
        raise Refused('unsupported payload path')
    if p.parts[0] == 'config' and name != 'config/review.json':
        raise Refused('unsupported configuration')
    return p


def copy_regular(src, dst, budget):
    """O_NOFOLLOW plus fstat: links, devices and changing sizes cannot escape the budget."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as inp:
        info = os.fstat(inp.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused('unsupported file type')
        if info.st_size > budget[0] or budget[1] <= 0:
            raise Refused(OVER_LIMIT)
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with dst.open('xb') as out:
            os.chmod(dst, 0o600)
            while chunk := inp.read(1 << 20):
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise Refused(OVER_LIMIT)
                out.write(chunk)
        budget[1] -= 1


def copy_tree(src, dst, budget, exclusions, prefix):
    if src.is_symlink():
        raise Refused('component root is a link')
    if not src.exists():
        return
    for root, dirs, files in os.walk(src, followlinks=False):
        for name in list(dirs):
            path = Path(root) / name
            if path.is_symlink():
                exclusions.append(f'{prefix}/{path.relative_to(src)}')
                dirs.remove(name)
        for name in files:
            path = Path(root) / name
            if name.endswith('.lock') or not stat.S_ISREG(path.lstat().st_mode):
                exclusions.append(f'{prefix}/{path.relative_to(src)}')
                continue
            copy_regular(path, dst / path.relative_to(src), budget)


def inventory(stage):
    result = {}
    for root, dirs, files in os.walk(stage, followlinks=False):
        if any((Path(root) / d).is_symlink() for d in dirs):
            raise Refused('links are refused')
        for name in files:
            p = Path(root) / name
            relative = p.relative_to(stage).as_posix()
            if relative == 'manifest.json':
                continue
            safe_path(relative)
            if not stat.S_ISREG(p.lstat().st_mode):
                raise Refused('links and special files are refused')
            result[relative] = {'size': p.stat().st_size, 'sha256': digest(p)}
    if len(result) > MAX_FILES or sum(f['size'] for f in result.values()) > MAX_BYTES:
        raise Refused('The backup is over its size or file limits.')
    return result


def validate_db(path, key_path):
    from .server.vault import Cipher
    with sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True) as db:
        if db.execute('pragma integrity_check').fetchall() != [('ok',)] or db.execute('pragma foreign_key_check').fetchall():
            raise Refused("Rynhult's database failed its integrity check.")
        schema = db.execute('select version_num from alembic_version').fetchone()[0]
        pages = db.execute('pragma page_count').fetchone()[0]
        cipher = Cipher(key_path)
        # Validate every current/retired value, without exposing plaintext or key identifiers.
        for table in ('vault_key', 'vault_retired'):
            if not db.execute('select 1 from sqlite_master where name=?', (table,)).fetchone():
                continue
            cols = {r[1] for r in db.execute(f'pragma table_info({table})')}
            key = 'key_id' if 'key_id' in cols else 'id'
            try:
                for row in db.execute(f'select {key}, variable, ciphertext from {table}'):
                    cipher.open(*row)
            except Exception:
                raise Refused("The database and the vault key don't belong together (pairing check failed).") from None
        if db.execute("select 1 from sqlite_master where name='infrastructure_credential'").fetchone():
            from .server.infrastructure import aad_for
            try:
                for id_, provider, purpose, revision, ciphertext, retired in db.execute(
                        'select id, provider, purpose, revision, ciphertext, retired from infrastructure_credential'):
                    cipher.open(id_, aad_for(provider, purpose, revision), ciphertext)
                    for old in json.loads(retired or '[]'):
                        cipher.open(id_, aad_for(provider, purpose, old['revision']), old['ciphertext'])
            except Exception:
                raise Refused("The database and the vault key don't belong together (pairing check failed).") from None
    return {'schema': schema, 'pages': pages}


def catalog_overlay_revision(files):
    """Bind the revision check to the captured overlay, not mutable installed built-ins.

    The signed inventory already authenticates these bytes. Keep the full effective
    catalog revision as capture metadata and for legacy manifests' strict validation.
    """
    return hashlib.sha256(canonical({name: info for name, info in files.items()
                                     if name.startswith('catalog/')})).hexdigest()


def validate_catalog(stage, catalog_root=None):
    from .server.catalog import load
    catalog_root = catalog_root or Path(__file__).resolve().parents[2] / 'catalog'
    try:
        catalog = load(catalog_root, stage / 'catalog')
        with sqlite3.connect(f'file:{stage / "acc.db"}?mode=ro&immutable=1', uri=True) as db:
            keys = {row[0] for row in db.execute('select id from vault_key')}
        from .server.vault import REF
        for item in catalog.mcps.values():
            for value in (item.meta.get('env') or {}).values():
                match = REF.fullmatch(str(value))
                if match and match[1] not in keys:
                    raise Refused('A catalog entry uses a vault key that no longer exists. Fix or remove that entry, then back up again.')
        return catalog.revision
    except Refused:
        raise
    except Exception:
        raise Refused('The catalog failed its check. Fix the catalog entries, then back up again.') from None


def validate_git(stage):
    """Sanitize git config before asking git to parse untrusted data; never execute restored hooks."""
    from .server.gitbackup import REPO_CONFIG, _git, install_hook
    results = []
    for head in (stage / 'git').rglob('HEAD') if (stage / 'git').exists() else ():
        repo = head.parent
        if not (repo / 'config').is_file():
            continue
        (repo / 'objects').mkdir(mode=0o700, exist_ok=True)
        (repo / 'refs').mkdir(mode=0o700, exist_ok=True)
        # Parse identity as data. Do not let git honor include paths, fsck config, hooks, alternates or filters.
        import configparser
        config = configparser.RawConfigParser(strict=False)
        try:
            config.read(repo / 'config')
            identity = config.get('acc', 'project', fallback='')
        except configparser.Error:
            raise Refused('invalid repository configuration') from None
        if not re.fullmatch(r'[A-Za-z0-9@._:+-]{0,200}', identity):
            raise Refused('invalid repository identity')
        for alternate in ('objects/info/alternates', 'objects/info/http-alternates'):
            if (repo / alternate).exists():
                raise Refused('external git objects refused')
        shutil.rmtree(repo / 'hooks', ignore_errors=True)
        (repo / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = true\n')
        for key, value in (*REPO_CONFIG, ('acc.project', identity), ('gc.auto', '0')):
            if _git('--git-dir', str(repo), 'config', key, value).returncode:
                raise Refused('repository configuration failed')
        check = _git('--git-dir', str(repo), 'fsck', '--full', timeout=600)
        if check.returncode:
            raise Refused('repository integrity failure')
        install_hook(repo)
        refs = _git('--git-dir', str(repo), 'for-each-ref', '--format=%(refname) %(objectname)').stdout.splitlines()
        results.append({'path': repo.relative_to(stage).as_posix(), 'identity': identity, 'refs': refs,
                        'captured_at': time.time()})
    return results


def repository_bindings(stage, repositories):
    from datetime import datetime
    with sqlite3.connect(f'file:{stage / "acc.db"}?mode=ro&immutable=1', uri=True) as db:
        projects = {}
        for pid, slug, created, stable, group_id in db.execute('select id, slug, created_at, backup_identity, group_id from project'):
            projects[pid] = stable or f"{slug}@{datetime.fromisoformat(created).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        group_projects = dict(db.execute('select id, group_id from project where group_id is not null'))
        from .server import auth
        private_people = {(pid, auth.digest(person)[:32]) for pid, person in db.execute('select project_id, person from placement where person is not null')}
        for repo in repositories:
            match = re.fullmatch(r'git/([0-9]+)\.git', repo['path'])
            pid = int(match[1]) if match else None
            expected = projects.get(pid)
            repo['scope'] = 'personal'
            shared = re.fullmatch(r'git/(?:exchange/([0-9]+)\.git|group-backups/([0-9]+)/[0-9a-f]{32}/repo\.git)', repo['path'])
            private = re.fullmatch(r'git/private-groups/([0-9]+)/([0-9a-f]{32})/[0-9a-f]{32}/repo\.git', repo['path'])
            if shared or private:
                pid = int((shared[1] or shared[2]) if shared else private[1])
                group_id = group_projects.get(pid)
                expected = f'{group_id}:{projects[pid]}' if group_id and pid in projects else None
                repo['scope'] = 'group-published' if shared else 'placement-private'
                if private:
                    expected = f'{expected}:{private[2]}' if expected and (pid, private[2]) in private_people else None
            repo['project_id'] = pid
            repo['recoverable_orphan'] = expected is None or expected != repo['identity']
            row = db.execute('select value from observation where key=?', (f'backup.{pid}',)).fetchone() if pid else None
            repo['last_upstream_push_at'] = json.loads(row[0]).get('last_at') if row else None
    return repositories


class Service:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir / 'backup-service'
        self.staging = self.root / 'staging'
        self.transport = None
        self.s3_credentials = None
        self.lock_audit = None

    def initialize(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.staging.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink() or self.staging.is_symlink():
            raise Refused('The backup folders on the main machine must be real folders, not links.')
        os.chmod(self.root, 0o700)
        os.chmod(self.staging, 0o700)
        with lease(self.root / '.coordinator'):
            if not (self.root / 'identity.json').exists():
                key = Ed25519PrivateKey.generate()
                private_write(self.root / 'signing.key', key.private_bytes_raw())
                private_write(self.root / 'identity.json', canonical({'installation_id': uuid.uuid4().hex,
                    'execution_machine_id': uuid.uuid4().hex,
                    'public_key': base64.b64encode(key.public_key().public_bytes_raw()).decode()}))

    def kit(self):
        from . import __version__
        identity = json.loads((self.root / 'identity.json').read_bytes())
        return {'format': FORMAT, **identity, 'public_key_fingerprint': hashlib.sha256(
            base64.b64decode(identity['public_key'])).hexdigest(), 'targets': [{**t, 'password': (self.root / 'keys' / t['id']).read_text()} for t in self.targets()],
            'instructions': 'Keep in a password manager plus one offline copy. Fence the old main before recovery. '
            'Obtain matching Rynhult and restic from trusted releases, never the backup target. Select an explicit '
            'backup ID from your independent drill record. Restore with acc-restore; keep startup hold until review.',
            'release_source': self.settings.source_url, 'manifest_format': FORMAT,
            'acc_version': __version__, 'restic_version': RESTIC_VERSION,
            'key_epoch': 1, 'created_at': time.time()}

    def sign(self, stage, manifest):
        key = Ed25519PrivateKey.from_private_bytes((self.root / 'signing.key').read_bytes())
        private_write(stage / 'manifest.json', canonical({'manifest': manifest,
            'signature': base64.b64encode(key.sign(canonical(manifest))).decode()}))

    def capture(self):
        from sqlalchemy.engine import make_url
        if make_url(self.settings.db_url).get_backend_name() != "sqlite":
            raise Refused("Rynhult backups support only its built-in SQLite database. Back up PostgreSQL with its own tools.")
        from . import __version__
        from .server.catalog_store import catalog_lock
        self.initialize()
        with lease(self.root / '.coordinator'), lease(self.settings.data_dir / '.capture-update.lock'):
            stage = self.staging / uuid.uuid4().hex
            stage.mkdir(mode=0o700)
            retained = sum(p.stat().st_size for p in self.staging.rglob('*') if p.is_file())
            budget, excluded = [min(MAX_BYTES - retained, (shutil.disk_usage(self.root).free - (64 << 20)) // 4), MAX_FILES], []
            started = time.time()
            try:
                with ExitStack() as locks:
                    # Catalog before the barrier, the order its writers use (catalog, then database): a running edit
                    # makes the capture wait (bounded by catalog_lock's timeout) while no DB writer is held off.
                    locks.enter_context(catalog_lock(self.settings.catalog_local_dir))
                    copying = locks.enter_context(ExitStack())  # until secrets/ is copied: see secrets_lock
                    copying.enter_context(lease(secrets_lock(self.settings.data_dir)))
                    barrier = locks.enter_context(ExitStack())
                    barrier.enter_context(lease(self.settings.data_dir / '.maintenance.lock'))
                    if (self.settings.data_dir / '.update-in-progress').exists():
                        raise Refused("Rynhult was updating, so this backup didn't run. Back up again after the update.")
                    with sqlite3.connect(self.settings.data_dir / 'acc.db') as src:
                        size = src.execute('pragma page_count').fetchone()[0] * src.execute('pragma page_size').fetchone()[0]
                        if size > budget[0]:
                            raise Refused(STAGING_FULL)
                        src.execute("insert into setting(key,value,updated_by,updated_at) values (?,?,'@backup',CURRENT_TIMESTAMP) "
                                    "on conflict(key) do update set value=excluded.value,updated_at=excluded.updated_at",
                                    ('backup.capture_checkpoint', json.dumps({'id': stage.name, 'at': started})))
                        src.commit()
                        with sqlite3.connect(stage / 'acc.db') as dst:
                            src.backup(dst, pages=256)
                            dst.execute('pragma journal_mode=DELETE')
                    os.chmod(stage / 'acc.db', 0o600)
                    budget[0] -= (stage / 'acc.db').stat().st_size
                    barrier.close()
                    # Keys are immutable/create-once: copying after the snapshot includes
                    # every key it can reference; later additions are harmless. Catalog
                    # edits remain serialized independently of database writers.
                    copy_tree(self.settings.data_dir / 'secrets', stage / 'secrets', budget, excluded, 'secrets')
                    copying.close()
                    copy_tree(self.settings.catalog_local_dir, stage / 'catalog', budget, excluded, 'catalog')
                    db_info = validate_db(stage / 'acc.db', stage / 'secrets/vault.key')
                    catalog_revision = validate_catalog(stage, self.settings.catalog_dir)
                    boundary = time.time()
                with lease(git_lock(self.settings.git_dir)):
                    copy_tree(self.settings.git_dir, stage / 'git', budget, excluded, 'git')
                repositories = repository_bindings(stage, validate_git(stage))
                private_write(stage / 'config/review.json', canonical({
                    'allowed_origins': self.settings.allowed_origins, 'source_url': self.settings.source_url,
                    'node_ca_file_configured': self.settings.node_ca_file is not None,
                    'prerequisites': ['Independent TLS/private CA escrow and stable endpoint review required'],
                    'execution_state': 'Main agent state is excluded; back up projects separately.'}))
                files = inventory(stage)
                manifest = {'format': FORMAT, 'restore_tool': FORMAT, 'backup_id': stage.name,
                    'installation_id': self.kit()['installation_id'], 'execution_machine_id': self.kit()['execution_machine_id'],
                    'version': __version__, 'build': (self.settings.update_source / '.acc-build').read_text().strip()
                    if (self.settings.update_source / '.acc-build').is_file() else None, 'started_at': started, 'finished_at': time.time(),
                    'db_secrets_catalog_interval': [started, boundary], 'checkpoint': stage.name,
                    'database': db_info, 'catalog_revision': catalog_revision,
                    'catalog_overlay_revision': catalog_overlay_revision(files), 'vault_key_epoch': digest(stage / 'secrets/vault.key') if
                    (stage / 'secrets/vault.key').exists() else None, 'repository_key_epoch': 1,
                    'repositories': repositories, 'exclusions': excluded, 'coverage_exclusions': EXCLUSIONS,
                    'retention': RETENTION, 'targets': {t['id']: {'repository_id': t['repository_id'],
                        'key_epoch': t['key_epoch']} for t in self.targets()}, 'files': files}
                self.sign(stage, manifest)
                return stage
            except BaseException:
                shutil.rmtree(stage)
                raise

    def discard_stages(self, keep=()):
        """Remove retained stages except `keep` names. A stage kept after a failed upload is a plaintext copy of data
        that is still live and that every newer capture supersedes; kept forever, they used up the capture budget
        (MAX_BYTES) until every capture failed. Returns (stages, bytes) removed."""
        removed, size = 0, 0
        with lease(self.root / '.coordinator'):
            if not self.staging.is_dir():
                return removed, size
            for stage in self.staging.iterdir():
                if stage.name in keep or not ID.fullmatch(stage.name) or stage.is_symlink() or not stage.is_dir():
                    continue
                size += sum(p.lstat().st_size for p in stage.rglob('*') if p.is_file() and not p.is_symlink())
                shutil.rmtree(stage)
                removed += 1
        return removed, size

    def newest_stage(self):
        """The complete stage (signed manifest, written last by capture()) that finished last, or None. A folder left by
        a capture killed midway has no manifest and is never the one kept; mtimes say nothing about completeness."""
        finished = {}
        for stage in self.staging.iterdir():
            manifest = stage / 'manifest.json'
            if not ID.fullmatch(stage.name) or stage.is_symlink() or manifest.is_symlink() or not manifest.is_file():
                continue
            try:
                m = json.loads(manifest.read_bytes())['manifest']
                if m['backup_id'] == stage.name:
                    finished[stage] = float(m['finished_at'])
            except (ValueError, TypeError, KeyError):
                continue
        return max(finished, key=finished.get, default=None)


    def targets(self):
        path = self.root / 'targets.json'
        return [defaults(t) for t in json.loads(path.read_bytes())] if path.exists() else []


    def add_s3_target(self, endpoint, bucket, region, credential_id):
        from .backup_s3 import location
        endpoint, bucket, region = location(endpoint, bucket, region)
        self.initialize()
        with lease(self.root / '.intent'), lease(self.root / '.coordinator'):
            target_id = uuid.uuid4().hex
            private_write(self.root / 'keys' / target_id, base64.b64encode(os.urandom(32)))
            prefix = 'acc/' + self.kit()['installation_id'] + '/' + target_id
            target = {'id': target_id, 'kind': 's3', 'endpoint': endpoint, 'bucket': bucket, 'region': region,
                      'prefix': prefix, 'credential_id': credential_id, 'repository_id': None, 'key_epoch': 1,
                      'repository': 's3:' + endpoint + '/' + bucket + '/' + prefix,
                      **defaults({}), 'enabled': False, 'setup_verified_at': None, 'created_at': time.time(),
                      'recovery_instructions': 'Use independent provider-account recovery to issue a read-only S3 key. '
                      'Export ciphertext with version-aware provider tools if current objects are hidden or overwritten; '
                      'then restore using --repository with the exported local repository. Maintenance/prune credentials '
                      'must remain on an independent trusted host outside Rynhult.'}
            private_write(self.root / 'targets.json', canonical([*self.targets(), target]))
            return target

    @contextmanager
    def s3(self, target):
        from .backup_s3 import S3
        if not self.s3_credentials:
            raise Refused("Rynhult can't read the S3 writer credentials. Save them again in Target setup.")
        with S3(target, self.s3_credentials(target)) as client:
            yield client

    def add_node_target(self, node_id, node_name, max_size_bytes=None):
        from .backup_targets import default_max_size, target_max_size
        max_size_bytes = target_max_size({"max_size_bytes": default_max_size() if max_size_bytes is None else max_size_bytes})
        # Enrollment uses token_hex(8); node IDs are independent of backup UUIDs.
        if not re.fullmatch(r'[0-9a-f]{16,32}', node_id) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', node_name):
            raise Refused('Choose an enrolled machine.')
        self.initialize()
        with lease(self.root / '.coordinator'):
            if any(t.get('node_id') == node_id for t in self.targets()):
                raise Refused('This machine already has a backup target.')
            target_id = uuid.uuid4().hex
            private_write(self.root / 'keys' / target_id, base64.b64encode(os.urandom(32)))
            private_write(self.root / 'transport' / target_id, base64.b64encode(os.urandom(32)))
            target = {'id': target_id, 'kind': 'node', 'node_id': node_id, 'node_name': node_name,
                      'repository': 'enrolled:' + node_name, 'repository_id': None, 'key_epoch': 1,
                      **defaults({}), 'enabled': False, 'max_size_bytes': max_size_bytes, 'setup_verified_at': None, 'created_at': time.time()}
            private_write(self.root / 'targets.json', canonical([*self.targets(), target]))
            return target

    def target_setup(self, target_id):
        from .backup_targets import setup_commands
        target = self.target(target_id)
        if target['kind'] != 'node':
            raise Refused('Setup commands exist only for machine targets.')
        token = (self.root / 'transport' / target_id).read_bytes()
        # htpasswd SHA is safe here only because this is a random 256-bit token,
        # never a human password. Neither the token nor repository key is shown.
        hashed = '{SHA}' + base64.b64encode(hashlib.sha1(token).digest()).decode()
        return setup_commands(target, self.kit()['installation_id'], hashed)

    def target(self, target_id):
        target = next((t for t in self.targets() if t['id'] == target_id), None)
        if target is None:
            raise Refused('That backup target was not found. Reload Host → Backups and try again.')
        return target

    @contextmanager
    def backend(self, target, binary):
        key = self.root / 'keys' / target['id']
        if target['kind'] == 'local':
            yield self.configure_backend(LocalTarget(target['repository'], key, binary), target)
        elif target['kind'] == 's3' and self.s3_credentials:
            from .backup_s3 import S3Target, location
            location(target['endpoint'], target['bucket'], target['region'])
            with tempfile.TemporaryDirectory(prefix='s3-job-', dir=self.root) as tmp, lease(Path(tmp) / '.active', timeout=1):
                credentials = Path(tmp) / 'credentials.json'
                private_write(credentials, canonical(self.s3_credentials(target)))
                yield self.configure_backend(S3Target(target, key, credentials, binary), target)
        elif target['kind'] == 'node' and self.transport:
            with self.transport(target) as url:
                yield self.configure_backend(RestTarget(url, key, self.root / 'transport' / target['id'], target['id'], binary), target)
        else:
            raise Refused("Rynhult can't reach this target's machine. Check that it is connected in Host → Machines, then select Check setup.")

    def configure_backend(self, backend, target):
        backend.expected_repository_id = target.get('repository_id')
        if self.lock_audit:
            backend.lock_audit = lambda detail: self.lock_audit(target['id'], detail)
        return backend

    def unlock(self, target_id, *, binary=Path('/usr/local/bin/restic'), actor='system'):
        target = self.target(target_id)
        with lease(self.root / f'.upload-{target_id}', timeout=1), self.backend(target, binary) as backend:
            return backend.remove_stale_locks(automatic=False, actor=actor)

    def check_target(self, target_id, *, binary=Path('/usr/local/bin/restic')):
        self.target(target_id)
        with lease(self.root / f'.upload-{target_id}', timeout=1):
            return self._check_target(target_id, binary=binary)

    def _check_target(self, target_id, *, binary):
        with lease(self.root / '.intent'), lease(self.root / '.coordinator'):
            target = self.target(target_id)
            if target['kind'] not in ('node', 's3'):
                raise Refused('Only machine and S3 targets have Check setup.')
            if target['kind'] == 'node' and getattr(self, 'receiver_check', None):
                self.receiver_check(target)
            # Invalidate old setup evidence before IO; a failed recheck never leaves a trusted receipt.
            if target['kind'] == 's3':
                target.update(setup_verified_at=None, immutability=None, enabled=False)
                private_write(self.root / 'targets.json', canonical([target if t['id'] == target_id else t for t in self.targets()]))
                result_path = self.root / f'result-{target_id}.json'
                if result_path.exists():
                    result = json.loads(result_path.read_bytes())
                    result.pop('immutable_until', None)
                    result.pop('immutability_checked_at', None)
                    private_write(result_path, canonical(result))
            with self.backend(target, binary) as backend:
                if not target['repository_id']:
                    try:
                        backend.command('cat', 'config')
                    except RepositoryLocked:
                        raise
                    except Refused:
                        backend.command('init', '--repository-version', '2')
                repo_id = backend.repository_id()
                if target['repository_id'] and repo_id != target['repository_id']:
                    raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
                backend.command('check', '--read-data')
                # An actual authenticated REST mutation probe proves append-only behavior.
                if target['kind'] == 'node':
                    backend.probe_append_only()
                else:
                    with self.s3(target) as client:
                        # Mutable or unsupported providers can still capture ciphertext, but never claim protection.
                        target['capabilities'] = client.capabilities()
                        from .backup_s3 import encrypted_probe
                        probe_snapshot = encrypted_probe(backend)
                        backend.command('check', '--read-data')
                        try:
                            target['immutability'] = client.probe(probe_snapshot)
                        except Refused:
                            target['immutability'] = None
                target.update(repository_id=repo_id, setup_verified_at=time.time())
                targets = [target if t['id'] == target_id else t for t in self.targets()]
                private_write(self.root / 'targets.json', canonical(targets))
            return target


    def add_local_target(self, repository, *, binary=Path('/usr/local/bin/restic')):
        self.initialize()
        repository = Path(repository)
        if repository.exists():
            raise Refused('new target requires a fresh directory')
        # Never allow capture staging, secrets or source trees as repositories.
        if repository.is_relative_to(self.settings.data_dir.parent):
            raise Refused('repository must be outside the installation tree')
        with lease(self.root / '.coordinator'):
            target_id = uuid.uuid4().hex
            password = self.root / 'keys' / target_id
            private_write(password, base64.b64encode(os.urandom(32)))
            backend = LocalTarget(repository, password, binary)
            try:
                backend.command('init', '--repository-version', '2')
                backend.command('check')
                target = {'id': target_id, 'kind': 'local', 'repository': str(repository),
                          'repository_id': backend.repository_id(), 'key_epoch': 1, 'enabled': False,
                          **defaults({}), 'created_at': time.time()}
                targets = self.targets()
                targets.append(target)
                from .server.db import make_engine, make_sessionmaker
                from .server.models import AuditLog
                engine = make_engine(self.settings.db_url)
                try:
                    with make_sessionmaker(engine)() as db:
                        db.add(AuditLog(actor='@local-admin', action='backup.target_add', target=target_id, detail={'kind': 'local'}))
                        db.commit()
                finally:
                    engine.dispose()
                private_write(self.root / 'targets.json', canonical(targets))
                return target
            except BaseException:
                password.unlink(missing_ok=True)
                raise


    def verify(self, target_id, backup_id, *, snapshot_id=None, binary=Path('/usr/local/bin/restic')):
        if not ID.fullmatch(backup_id) or (snapshot_id is not None and not SNAPSHOT.fullmatch(snapshot_id)):
            raise Refused('Choose a backup from the list first.')
        target = self.target(target_id)
        with lease(self.root / f'.upload-{target_id}', timeout=1), self.backend(target, binary) as backend:
            if backend.repository_id() != target['repository_id']:
                raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
            backend.command('check', '--read-data')
            with tempfile.TemporaryDirectory(prefix='verify-', dir=self.root) as tmp:
                manifest = restore(self.kit(), target_id, backup_id, Path(tmp) / 'restored', binary=binary,
                                   backend=backend, snapshot_id=snapshot_id)
            result = {'backup_id': backup_id, 'snapshot_id': snapshot_id, 'captured_at': manifest['started_at'],
                      'restore_tested_at': time.time()}
            path = self.root / f'result-{target_id}.json'
            if path.exists():
                latest = json.loads(path.read_bytes())
                if latest.get('backup_id') == backup_id and (snapshot_id is None or latest.get('snapshot_id') == snapshot_id):
                    latest.update(restore_tested_at=result['restore_tested_at'], verified_at=result['restore_tested_at'],
                                  full_backup_id=backup_id, full_snapshot_id=latest.get('snapshot_id'))
                    private_write(path, canonical(latest))
            return result

    def history(self, target_id, *, binary=Path('/usr/local/bin/restic')):
        """Authenticated generations. A compromised main can append a snapshot reusing an old backup ID, so
        duplicates are flagged and owners select by snapshot ID; a missing last-verified snapshot means the
        target withheld or rolled back history."""
        target = self.target(target_id)
        if not target.get('repository_id'):
            raise Refused('Finish Target setup and select Check setup first.')
        with self.backend(target, binary) as backend:
            if backend.repository_id() != target['repository_id']:
                raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
            values = json.loads(backend.command('snapshots', '--json', recover=False))  # a read never removes locks
        items = [{'snapshot_id': s['id'], 'backup_id': tag, 'time': s['time']}
                 for s in values for tag in s.get('tags', []) if ID.fullmatch(tag)]
        for item in items:
            item['duplicate'] = sum(i['backup_id'] == item['backup_id'] for i in items) > 1
        last = self.root / f'result-{target_id}.json'
        last = json.loads(last.read_bytes()).get('snapshot_id') if last.exists() else None
        return {'items': items, 'verified_missing': bool(last) and all(i['snapshot_id'] != last for i in items)}

    def back_up(self, target_id, *, binary=Path('/usr/local/bin/restic'), stage=None):
        target = self.target(target_id)
        if target['kind'] == 's3' and not target.get('setup_verified_at'):
            raise Refused('The S3 credentials changed. Select Check setup in Target setup, then back up again.')
        if not target.get('repository_id'):
            raise Refused('Finish Target setup and select Check setup first.')
        owns_stage = stage is None
        stage = stage or self.capture()
        # Separate upload lease: capture can proceed independently, no mutation barrier during encryption/IO.
        with lease(self.root / f'.upload-{target_id}', timeout=1), self.backend(target, binary) as backend:
            if target['kind'] == 's3' and self.target(target_id).get('setup_verified_at') != target.get('setup_verified_at'):
                raise Refused('The S3 setup changed during the backup. Select Check setup in Target setup, then back up again.')
            if backend.repository_id() != target['repository_id']:
                raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
            m = json.loads((stage / 'manifest.json').read_bytes())['manifest']
            if m['targets'].get(target_id, {}).get('repository_id') != target['repository_id']:
                raise Refused('This backup was prepared for different backup storage. Back up again.')
            previous = self.root / f'result-{target_id}.json'
            last = {}
            uploaded = None
            if previous.exists():
                # Freshness: a target serving an older copy of the repository hides newer generations.
                last = json.loads(previous.read_bytes())
                listed = json.loads(backend.command('snapshots', '--json', '--tag', last['backup_id']))
                if all(snap['id'] != last['snapshot_id'] for snap in listed):
                    raise RolledBack('target history rolled back: the last verified generation is missing')
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                existing = json.loads(backend.command('snapshots', '--json', '--tag', stage.name))
                existing = [snap for snap in existing if stage.name in snap.get('tags', [])]
                if len(existing) > 1:
                    raise Refused('More than one backup on the target has this backup\'s ID, so it is ambiguous. Check the target before backing up again.')
                if existing:
                    snapshot = existing[0]['id']
                else:
                    archive = Path(tmp) / 'capture.tar'
                    pack(stage, archive)
                    with archive.open('rb') as inp:
                        lines = backend.command('backup', '--json', '--host', self.kit()['installation_id'],
                            '--tag', stage.name, '--stdin', '--stdin-filename', 'acc-capture.tar', input_file=inp).splitlines()
                    summary = next(json.loads(line) for line in reversed(lines) if json.loads(line).get('message_type') == 'summary')
                    snapshot = summary['snapshot_id']
                    uploaded = summary.get('data_added_packed', summary.get('data_added'))
                retained = None
                if target['kind'] == 's3':
                    try:
                        with self.s3(target) as client:
                            from .backup_s3 import WINDOW, encrypted_probe
                            client.probe(encrypted_probe(backend))
                            retained = client.retained_versions()
                            if retained['until'] < time.time() + WINDOW:
                                retained = None
                    except Refused:
                        retained = None
                # Structural/metadata check every capture; full data read and restore only when due.
                full = full_due(target, last, time.time())
                backend.command('check', *(['--read-data'] if full else []))
                if full:
                    kit = self.kit()
                    restored = Path(tmp) / 'verified'
                    authenticated = restore(kit, target_id, stage.name, restored, binary=binary, backend=backend,
                                            snapshot_id=snapshot)
                    if canonical(authenticated) != canonical(m):
                        raise Refused('The target holds different contents under this backup\'s ID. Check the target before backing up again.')
                checked_at = time.time()
            result = {'backup_id': stage.name, 'snapshot_id': snapshot, 'captured_at': m['started_at'],
                      'checked_at': checked_at, 'check_kind': 'full' if full else 'metadata',
                      'verified_at': checked_at if full else last.get('verified_at'),
                      'restore_tested_at': checked_at if full else last.get('restore_tested_at'),
                      'full_backup_id': stage.name if full else last.get('full_backup_id', last.get('backup_id')),
                      'full_snapshot_id': snapshot if full else last.get('full_snapshot_id', last.get('snapshot_id')),
                      'uploaded_bytes': uploaded, 'bytes': sum(f['size'] for f in m['files'].values())}
            if retained is not None:
                try:
                    with self.s3(target) as client:
                        current = client.retained_versions()
                        # Independent writer policy must deny version deletion and retention overrides.
                        if current['versions'] != retained['versions']:
                            raise Refused('S3 repository versions changed during authenticated readback')
                        until = min(retained['until'], current['until'])
                        if until < time.time() + WINDOW:
                            raise Refused('S3 repository retention is shorter than 30 days at completion')
                        result['retention_objects'] = len(current['versions'])
                        result['immutable_until'] = until
                        result['immutability_checked_at'] = time.time()
                        result['version_bound_retention'] = True
                except Refused:
                    # Capture is authenticated but cannot claim immutable retention.
                    result.pop('immutable_until', None)
            private_write(self.root / f'result-{target_id}.json', canonical(result))
            # Staging is disposable after the scheduled check; full restore evidence is dated separately.
            if owns_stage:
                shutil.rmtree(stage)
            return result

def validate_and_install(stage, kit, backup_id, destination):
    """Verify the entire restore unit before atomically publishing a fresh quarantined tree."""
    from . import __version__
    try:
        if not ID.fullmatch(backup_id) or kit['format'] != FORMAT:
            raise Refused('The recovery kit or the backup ID is not valid.')
        if (stage / 'manifest.json').is_symlink() or (stage / 'manifest.json').stat().st_size > 32 << 20:
            raise Refused("This backup's list of contents is too large or unsafe, so it can't be restored.")
        doc = json.loads((stage / 'manifest.json').read_bytes())
        m = doc['manifest']
        Ed25519PublicKey.from_public_bytes(base64.b64decode(kit['public_key'], validate=True)).verify(
            base64.b64decode(doc['signature'], validate=True), canonical(m))
        if (m['format'] != FORMAT or m['restore_tool'] != FORMAT or m['backup_id'] != backup_id or
                m['installation_id'] != kit['installation_id'] or m['version'] != __version__):
            raise Refused('This backup was made by a different Rynhult installation or version. Test a backup made since the last update, or restore with the matching version.')
        build_file = Path(__file__).resolve().parents[2] / '.acc-build'
        if m.get('build') and (not build_file.is_file() or build_file.read_text().strip() != m['build']):
            raise Refused('Restoring this backup needs the same Rynhult build that made it.')
        for name in m['files']:
            safe_path(name)
        files = inventory(stage)
        if files != m['files']:
            raise Refused("This backup's files don't match its signed list, so it can't be trusted.")
        key = stage / 'secrets/vault.key'
        if (digest(key) if key.exists() else None) != m['vault_key_epoch']:
            raise Refused("This backup's vault key doesn't match its signed list, so it can't be trusted.")
        effective_revision = validate_catalog(stage)  # profiles, items and vault references still must be valid
        if 'catalog_overlay_revision' in m:
            if catalog_overlay_revision(files) != m['catalog_overlay_revision']:
                raise Refused("This backup's saved catalog doesn't match its signed list, so it can't be trusted.")
        elif effective_revision != m['catalog_revision']:
            raise Refused("This backup's catalog doesn't match its signed list, so it can't be trusted.")  # legacy signed captures retain their original check
        db_info = validate_db(stage / 'acc.db', key)
        if db_info != m['database']:
            raise Refused("This backup's database doesn't match its signed list, so it can't be trusted.")
        from alembic.script import ScriptDirectory

        from .server.db import alembic_config
        if db_info['schema'] != ScriptDirectory.from_config(alembic_config('sqlite://')).get_current_head():
            raise Refused('This backup\'s database is from a different Rynhult version. Restore it with the matching version.')
        with sqlite3.connect(f'file:{stage / "acc.db"}?mode=ro&immutable=1', uri=True) as db:
            checkpoint = db.execute("select value from setting where key='backup.capture_checkpoint'").fetchone()
            if not checkpoint or json.loads(checkpoint[0]).get('id') != backup_id:
                raise Refused("This backup's database isn't the one the backup recorded, so it can't be trusted.")
    except Refused:
        raise
    except Exception:
        raise Refused("The recovery kit doesn't match this backup, or the backup is damaged.") from None
    if (destination.exists() or destination.is_symlink() or not destination.parent.is_dir() or
            any(p.is_symlink() for p in destination.parents)):
        raise Refused('restore requires a fresh destination')
    scratch = Path(tempfile.mkdtemp(prefix='.restore-', dir=destination.parent))
    try:
        (scratch / 'data').mkdir(mode=0o700)
        copy_regular(stage / 'acc.db', scratch / 'data/acc.db', [MAX_BYTES, MAX_FILES])
        for component, target in [('secrets', 'data/secrets'), ('catalog', 'catalog-local'), ('git', 'git'), ('config', 'review')]:
            copy_tree(stage / component, scratch / target, [MAX_BYTES, MAX_FILES], [], component)
        validate_git(scratch)
        private_write(scratch / 'data/RESTORE-HOLD', canonical({'backup_id': backup_id,
            'restored_at': time.time(), 'reason': 'Offline recovery: fence, review authority and local workloads before release'}))
        with sqlite3.connect(scratch / 'data/acc.db') as db:
            db.execute('delete from auth_session')
            db.execute('delete from invite')
            db.execute("insert into audit_log(ts,actor,action,target,detail) values (CURRENT_TIMESTAMP,?,?,?,?)",
                       ('@offline-recovery', 'backup.restore', backup_id, json.dumps({'backup_id': backup_id, 'quarantine': True})))
            db.commit()
        # Pending intents remain inert under the hold; no ordinary API or scheduler is installed.
        os.rename(scratch, destination)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return m


RESTIC_VERSION = '0.19.1'
AUTO_LOCK_AGE, MANUAL_LOCK_AGE = 14400, 1800  # seconds: twice the command deadline; restic's own stale timeout
MAX_LOCKS, METADATA_CALL_SECONDS, METADATA_BUDGET_SECONDS = 16, 60, 180
MAX_CHILDREN, MAX_CHILD_BYTES = 64, 16384


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


class LocalTarget:
    """Local/mounted directory only. Root transport config belongs to the local administrator."""
    command_timeout = 7200
    lease_timeout = 1
    expected_repository_id = None
    lock_audit = None

    def __init__(self, repository, password_file, binary=Path('/usr/local/bin/restic')):
        self.repository, self.password_file, self.binary = Path(repository), Path(password_file), Path(binary)
        if not self.repository.is_absolute() or any(p.is_symlink() for p in [self.repository, *self.repository.parents]):
            raise Refused('local repository requires a non-symlink absolute directory')
        from .restic_process import run
        if run([str(binary), 'version'], env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C'}, timeout=10).stdout.split()[:2] != [b'restic', RESTIC_VERSION.encode()]:
            raise Refused(f'Rynhult needs backup tool restic {RESTIC_VERSION}. Install that version, then try again.')

    def env(self):
        return {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C',
               'RESTIC_PASSWORD_FILE': str(self.password_file), 'RESTIC_CACHE_DIR': str(self.password_file.parent / 'cache')}

    def repo_args(self):
        return ['--repo', str(self.repository)]

    def command_lease(self):
        # Stable across backend instances and transient node forwarder URLs.
        return lease(self.password_file.with_name('.' + self.password_file.name + '.restic.lock'),
                     timeout=self.lease_timeout)

    def _raw_command(self, *args, input_file=None, output_file=None, output_limit=None, timeout=None, record=True, cleanup_timeout=None):
        from .restic_process import run
        child = {}

        def started(pid):
            child.update(pid=pid, boot=boot_id(), started=time.time(), ended=None)
            self._record_child(child)

        try:
            result = run([str(self.binary), *self.repo_args(), '--limit-upload', '10240',
                          '--limit-download', '10240', *args], env=self.env(), stdin=input_file,
                         stdout=output_file or subprocess.PIPE, timeout=timeout or self.command_timeout,
                         output_limit=output_limit, idle_timeout=30 if output_limit is not None else None,
                         started=started if record else None, cleanup_timeout=cleanup_timeout)
        except TimeoutError:
            raise ResticFailed(f"The backup tool's {args[0]} step timed out and was stopped.") from None
        except OverflowError:
            raise Refused('The backup is larger than the restore limit.') from None
        finally:
            if child:  # run() reaps the child before returning or raising
                child['ended'] = time.time()
                self._record_child(child)
        if result.returncode == 11:
            raise RepositoryLocked(args[0])
        if result.returncode:
            # Never copy stderr, even a truncated line: it can contain repository
            # credentials, decrypted names and transport URLs. Match known phrases
            # and emit our own fixed vocabulary only. 130 is context cancellation
            # in pinned restic, not proof that this parent sent SIGINT.
            stderr = (result.stderr or b'').lower()
            if b'unable to refresh lock' in stderr or b'failed to refresh lock in time' in stderr:
                detail = '; repository lock refresh failed; check receiver connectivity and storage'
            elif b'context canceled' in stderr:
                detail = '; operation cancelled inside restic; signal or lock-refresh cause not established'
            elif result.returncode == 130:
                detail = '; cancellation cause unavailable'
            else:
                detail = ''
            raise ResticFailed(f"The backup tool's {args[0]} step failed (exit {result.returncode}){detail}.")
        return result.stdout

    # ---- this main's own restic children: the only proof that a same-host lock is ours ----

    def _children_path(self):
        return self.password_file.with_name('.' + self.password_file.name + '.restic-children.json')

    def children(self):
        try:
            value = json.loads(self._children_path().read_bytes()[:MAX_CHILD_BYTES])
        except (OSError, ValueError):
            return []  # unknown: only the age threshold applies
        return [c for c in value if isinstance(c, dict) and type(c.get('pid')) is int
                and isinstance(c.get('boot'), str) and isinstance(c.get('started'), (int, float))
                and (c.get('ended') is None or isinstance(c.get('ended'), (int, float)))] if isinstance(value, list) else []

    def _record_child(self, child):
        # Commands on a target are serialized by command_lease. A lost write only means an unrecorded child, whose
        # lock then waits for the age threshold: never a reason to remove a lock.
        try:
            rest = [c for c in self.children() if (c['pid'], c['started']) != (child['pid'], child['started'])]
            private_write(self._children_path(), json.dumps([*rest, child][-MAX_CHILDREN:]).encode())
        except OSError:
            pass

    def ours_and_dead(self, lock):
        """A matching hostname is never proof: another machine (an HA standby, a restored clone, a container) can share
        it. The lock is ours only if its PID and creation time fall within the lifetime of a child this main started
        and has reaped, or of one from an earlier boot or a crashed run whose PID is gone."""
        if lock['hostname'] != socket.gethostname():
            return False
        boot, created = boot_id(), lock['created_at']
        for child in self.children():
            if child['pid'] != lock['pid'] or created < child['started'] - 2:
                continue
            if child['ended'] is not None:
                if created <= child['ended'] + 2:
                    return True
                continue
            if created > child['started'] + self.command_timeout + 120:
                continue  # no child of ours outlives its command deadline
            if child['boot'] != boot:
                return True
            try:
                os.kill(child['pid'], 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass  # permission/unknown process state is not proof of death
        return False

    def removable(self, lock, *, automatic):
        """Automatic: four hours (twice the command deadline). Owner: restic's own 30 minutes (live restic refreshes
        its lock every five). Younger locks only if ours_and_dead."""
        return lock['age_seconds'] > (AUTO_LOCK_AGE if automatic else MANUAL_LOCK_AGE) or self.ours_and_dead(lock)

    # ---- lock metadata: bounded in count, size and time ----

    def _metadata(self, *args, limit=4096, deadline=None):
        # Bound diagnostic output on disk as well as in status/audit records.
        left = (deadline - time.monotonic()) if deadline else METADATA_CALL_SECONDS
        if left <= 0:
            raise Refused('lock metadata unavailable')
        with tempfile.TemporaryFile() as out:
            try:
                # These readers never acquire repository locks, so no lock-removal grace is needed.
                # Include cleanup in the metadata deadline even if the child ignores SIGINT.
                self._raw_command(*args, output_file=out, output_limit=limit,
                                  timeout=min(METADATA_CALL_SECONDS, left), record=False, cleanup_timeout=0)
            except Refused:
                raise Refused('lock metadata unavailable') from None
            out.seek(0)
            return out.read(limit)

    def _lock_error(self, operation):
        try:
            locks = self.locks()
        except InterruptedError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError, OSError, OverflowError):
            locks = []
        return RepositoryLocked(operation, locks)

    def locks(self):
        """No-lock is restricted to reading config/lock metadata, never backup/check/prune. At most MAX_LOCKS lock
        files of 4 KiB each, every read under METADATA_CALL_SECONDS and all of them under METADATA_BUDGET_SECONDS."""
        from datetime import datetime
        deadline = time.monotonic() + METADATA_BUDGET_SECONDS
        raw = self._metadata('list', 'locks', limit=8192, deadline=deadline)
        ids = raw.decode('ascii').split() if len(raw) <= 8192 else []
        if len(raw) > 8192 or len(ids) > MAX_LOCKS or any(not SNAPSHOT.fullmatch(i) for i in ids):
            raise Refused('lock metadata unavailable')
        result = []
        for identity in sorted(set(ids)):
            raw = self._metadata('cat', 'lock', identity, '--no-lock', deadline=deadline)
            if len(raw) > 4096:
                raise Refused('lock metadata unavailable')
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get('time'), str):
                raise Refused('lock metadata unavailable')
            host, pid, exclusive = data.get('hostname'), data.get('pid'), data.get('exclusive')
            stamp = datetime.fromisoformat(data['time'].replace('Z', '+00:00'))
            if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,253}', host)
                    or type(pid) is not int or not 0 < pid < 2**31 or type(exclusive) is not bool
                    or stamp.tzinfo is None or stamp.timestamp() > time.time() + 60):
                raise Refused('lock metadata unavailable')
            result.append({'id': identity, 'hostname': host, 'pid': pid, 'exclusive': exclusive,
                           'created_at': stamp.timestamp(), 'age_seconds': max(0, int(time.time()-stamp.timestamp()))})
        return result

    # ---- removal: exactly the lock IDs judged stale, never a restic-wide `unlock` ----

    def delete_lock(self, identity):
        """One lock file of the local repository, without following links."""
        repository = os.open(self.repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            folder = os.open('locks', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=repository)
            try:
                os.unlink(identity, dir_fd=folder)
            except FileNotFoundError:
                pass
            finally:
                os.close(folder)
        finally:
            os.close(repository)

    def _unlock(self, locks, *, automatic, actor):
        """Restic's `unlock` removes whatever it considers stale when it runs, including a lock created after this
        check, and it calls a same-host lock stale when it cannot signal the PID (EPERM included). So remove only the
        IDs judged removable, re-read right before removal. Lock files are written once (a refresh writes a new ID), so
        an ID judged stale never becomes a live process's lock."""
        if not self.lock_audit or not self.expected_repository_id:
            raise Refused('Stale locks can be removed only after Check setup has passed for this target.')
        candidates = [lock for lock in locks if self.removable(lock, automatic=automatic)]
        if not candidates:
            return {'removed': [], 'remaining': len(locks), 'locks': locks}
        if json.loads(self._metadata('cat', 'config', '--no-lock'))['id'] != self.expected_repository_id:
            raise Refused("The backup storage on this target isn't the one Rynhult set up. Check that the target still points at the same storage.")
        detail = {'automatic': automatic, 'actor': actor, 'locks': candidates}
        self.lock_audit({**detail, 'phase': 'requested'})  # fail closed before mutation if audit fails
        try:
            current = {lock['id']: lock for lock in self.locks()}
            removed = []
            for lock in candidates:
                now = current.get(lock['id'])
                fields = ('hostname', 'pid', 'exclusive', 'created_at')
                if now is None or any(now[f] != lock[f] for f in fields) or not self.removable(now, automatic=automatic):
                    continue
                if not SNAPSHOT.fullmatch(lock['id']):
                    raise Refused('lock metadata unavailable')
                self.delete_lock(lock['id'])
                removed.append(lock['id'])
            remaining = self.locks()
        except Exception:
            self.lock_audit({**detail, 'phase': 'failed'})
            raise
        removed = sorted(set(removed) - {lock['id'] for lock in remaining})
        self.lock_audit({**detail, 'phase': 'complete', 'removed': removed, 'remaining': len(remaining)})
        return {'removed': removed, 'remaining': len(remaining), 'locks': remaining}

    @contextmanager
    def held(self):
        """The target's command lease; only failing to get it is 'busy' (a timeout inside is the command's own)."""
        context = self.command_lease()
        try:
            context.__enter__()
        except TimeoutError:
            raise BackendBusy('Another backup task on this target is in progress. Try again when it finishes.') from None
        try:
            yield
        finally:
            context.__exit__(None, None, None)

    def remove_stale_locks(self, *, automatic=False, actor='system'):
        with self.held():
            locks = self.locks()
            if automatic and (not locks or not all(self.removable(l, automatic=True) for l in locks)):
                raise Refused('lock is active or its state is unknown')
            return self._unlock(locks, automatic=automatic, actor=actor)

    def command(self, *args, input_file=None, output_file=None, output_limit=None, recover=True):
        """`recover=False` for reads (history): a lock is reported, never removed, by a read."""
        with self.held():
            position = input_file.tell() if input_file else None
            output_position = output_file.tell() if output_file else None
            try:
                return self._raw_command(*args, input_file=input_file, output_file=output_file, output_limit=output_limit)
            except RepositoryLocked:
                error = self._lock_error(args[0])
                locks = error.locks
                if (not recover or not locks or not self.lock_audit
                        or not all(self.removable(l, automatic=True) for l in locks)):
                    raise error from None
                try:
                    self._unlock(locks, automatic=True, actor='system')
                except (ValueError, KeyError, TypeError):
                    raise error from None
                if input_file:
                    input_file.seek(position)
                if output_file:
                    output_file.seek(output_position)
                    output_file.truncate()
                try:
                    return self._raw_command(*args, input_file=input_file, output_file=output_file, output_limit=output_limit)
                except RepositoryLocked:
                    raise self._lock_error(args[0]) from None  # one retry, never an unlock loop

    def repository_id(self):
        # Read-only identity is verifiable even while an exclusive lock exists.
        return json.loads(self.command('cat', 'config', '--no-lock'))['id']


class RestTarget(LocalTarget):
    """REST ciphertext transport; service credentials never appear in argv."""
    def __init__(self, url, password_file, credential_file, username, binary):
        # Reuse the pinned binary check without treating a URL as a local path.
        super().__init__(Path('/nonexistent'), password_file, binary)
        self.repository = 'rest:' + url
        self.url, self.credential_file, self.username = url, credential_file, username

    def env(self):
        return {**super().env(), 'RESTIC_REST_USERNAME': self.username,
                'RESTIC_REST_PASSWORD': self.credential_file.read_text()}

    def repo_args(self):
        return [*super().repo_args(), '--option', 'rest.connections=1']

    def receiver_request(self, method, path, body=None):
        """One authenticated request to the receiver, relative to the repository: (status, at most 1 MiB of body)."""
        import http.client
        import socket
        from urllib.parse import urlsplit
        auth = base64.b64encode((self.username + ':' + self.credential_file.read_text()).encode()).decode()
        if self.url.startswith('http+unix://'):
            # restic's form: http+unix:///run/socket:/repository/
            socket_path, prefix = self.url[len('http+unix://'):].split(':', 1)
            class Connection(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX)
                    self.sock.settimeout(self.timeout)
                    self.sock.connect(socket_path)
            conn = Connection('localhost', timeout=30)
        else:
            parts = urlsplit(self.url)
            prefix = parts.path
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
        try:
            conn.request(method, prefix + path, body=body, headers={'Authorization': 'Basic ' + auth})
            response = conn.getresponse()
            return response.status, response.read(1 << 20)
        finally:
            conn.close()

    def delete_lock(self, identity):
        """One lock object: pinned rest-server --append-only permits deleting locks and nothing else."""
        status, _ = self.receiver_request('DELETE', 'locks/' + identity)
        if status not in (200, 204, 404):
            raise Refused(f'The backup receiver refused to remove the lock (HTTP {status}).')

    def probe_append_only(self):
        # A disposable, valid encrypted snapshot: even a misconfigured mutable
        # receiver can only delete this probe, never a retained generation.
        with tempfile.TemporaryFile() as probe:
            probe.write(os.urandom(32))
            probe.seek(0)
            lines = self.command('backup', '--json', '--tag', 'acc-setup-probe', '--stdin',
                                 '--stdin-filename', 'setup-probe', input_file=probe).splitlines()
        summary = next(json.loads(line) for line in reversed(lines) if json.loads(line).get('message_type') == 'summary')
        snapshot = summary['snapshot_id']
        if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
            raise Refused("Check setup's test backup on the receiver isn't valid. Check the receiver machine, then try again.")

        def request(method, body=None):
            return self.receiver_request(method, 'snapshots/' + snapshot, body)
        status, ciphertext = request('GET')
        if status != 200 or not ciphertext:
            raise Refused("Check setup couldn't read its test backup back from the receiver. Check the receiver machine, then try again.")
        if request('POST', ciphertext)[0] != 403 or request('DELETE')[0] != 403:
            raise Refused('The receiver let a test backup be overwritten or deleted, so it is not append-only. Run the Target setup commands on that machine again.')
        if request('GET') != (200, ciphertext):
            raise Refused('Check setup\'s test backup changed on the receiver during the check. Check the receiver machine, then try again.')


def pack(stage, archive):
    import tarfile
    with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as out:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and not path.is_symlink():
                out.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
    os.chmod(archive, 0o600)


def unpack(archive, destination):
    """Two-pass validation then manual extraction: no archive-supplied permissions/links/owners."""
    import tarfile
    # Raw USTAR headers only. Avoid PAX/GNU metadata being allocated by tarfile before limits apply.
    seen, total = set(), 0
    with archive.open('rb') as raw:
        while True:
            header = raw.read(512)
            if header == b'\0' * 512:
                break
            if len(header) != 512:
                raise Refused('truncated archive')
            try:
                kind = header[156:157]
                size = int(header[124:136].rstrip(b'\0 ').strip() or b'0', 8)
                name = header[:100].split(b'\0')[0].decode()
                prefix = header[345:500].split(b'\0')[0].decode()
                name = f'{prefix}/{name}' if prefix else name
            except (ValueError, UnicodeError):
                raise Refused('invalid archive header') from None
            if kind not in (b'0', b'\0') or size < 0:
                raise Refused('only regular USTAR files accepted')
            if name != 'manifest.json':
                safe_path(name)
            elif size > 32 << 20:
                raise Refused('manifest exceeds limit')
            total += size
            if (name in seen or any(str(p) in seen for p in PurePosixPath(name).parents) or
                    any(existing.startswith(name + '/') for existing in seen) or len(seen) >= MAX_FILES or total > MAX_BYTES):
                raise Refused('archive limits or duplicate file')
            seen.add(name)
            raw.seek((size + 511) // 512 * 512, 1)
    if 'manifest.json' not in seen:
        raise Refused('missing manifest')
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, 'r:') as tar:
            for member in tar:
                # Compare the second parser's interpretation with the validated raw headers.
                if member.name not in seen or not member.isfile() or member.pax_headers or member.sparse:
                    raise Refused('unexpected archive metadata')
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as inp, target.open('xb') as out:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(inp, out)
    except BaseException:
        shutil.rmtree(destination)
        raise



def restore(kit, target_id, backup_id, destination, *, binary=Path('/usr/local/bin/restic'), repository=None, backend=None,
            snapshot_id=None, prepare_replacement=False, reviewed_nodes=None, resume_sessions=()):
    if prepare_replacement and snapshot_id is None:
        raise Refused('replacement preparation requires the independently recorded snapshot ID')
    if (not isinstance(kit, dict) or kit.get('format') != FORMAT or not ID.fullmatch(backup_id) or
            (snapshot_id is not None and not SNAPSHOT.fullmatch(snapshot_id))):
        raise Refused('valid recovery kit and explicit backup ID required')
    try:
        target = next(t for t in kit['targets'] if t['id'] == target_id)
        password = target['password']
    except (KeyError, StopIteration, TypeError):
        raise Refused("This recovery kit doesn't include that target. Export a new recovery kit in Host → Backups.") from None
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise Refused('restore destination must be fresh')
    with tempfile.TemporaryDirectory(prefix='.acc-recovery-', dir=destination.parent) as name:
        scratch = Path(name)
        key = scratch / 'password'
        private_write(key, password.encode())
        backend = backend or LocalTarget(repository or target['repository'], key, binary)
        if backend.repository_id() != target['repository_id']:
            raise Refused("The backup storage doesn't match the recovery kit.")
        snapshots = json.loads(backend.command('snapshots', '--json', '--tag', backup_id))
        snapshots = [s for s in snapshots if backup_id in s.get('tags', [])]
        # Tags are chosen by whoever holds the writer credential: a compromised main can append an impostor
        # under an old backup ID. The recorded snapshot ID (a content hash) selects the genuine generation.
        if snapshot_id is not None:
            snapshots = [s for s in snapshots if s['id'] == snapshot_id]
        if len(snapshots) != 1:
            raise Refused("That backup isn't on the target, or its ID is ambiguous. Choose it by the snapshot ID you recorded.")
        snapshot = snapshots[0]['id']
        # Dump a single opaque USTAR blob, never let restic extract attacker paths. Enforce output limit while streaming.
        archive = scratch / 'capture.tar'
        _bounded_dump(backend, snapshot, archive)
        unpack(archive, scratch / 'payload')
        m = validate_and_install(scratch / 'payload', kit, backup_id, scratch / 'validated')
        if m.get('targets', {}).get(target_id, {}).get('repository_id') != target['repository_id']:
            raise Refused("This backup was signed for different backup storage, so it can't be trusted.")
        if prepare_replacement:
            from .backup_activation import plan_digest, prepare
            shutil.rmtree(scratch / 'validated')
            plan = prepare(scratch / 'payload', kit, backup_id, scratch / 'validated',
                           reviewed_nodes=reviewed_nodes, resume_sessions=resume_sessions)
            plan.update(repository_id=target['repository_id'], target_id=target_id, snapshot_id=snapshot)
            private_write(scratch / 'validated/plan.json', canonical(plan))
            m = {**m, 'replacement_plan_sha256': plan_digest(plan), 'new_main_id': plan['new_main_id']}
        os.rename(scratch / 'validated', destination)
        return m


def _bounded_dump(backend, snapshot, archive):
    # Use the same owned-child cleanup, repository serialization and lock recovery
    # as check/backup; retain the old size, idle and overall time bounds.
    with archive.open('xb') as out:
        os.chmod(archive, 0o600)
        backend.command('dump', snapshot, '/acc-capture.tar', output_file=out,
                        output_limit=MAX_BYTES + (MAX_FILES + 4) * 1024 + (32 << 20))


def main(argv=None):
    """Offline tool: never activates a restore or writes privileged installation paths."""
    import argparse
    parser = argparse.ArgumentParser(description='Rynhult phase-one backup and isolated offline restore')
    sub = parser.add_subparsers(dest='command', required=True)
    target = sub.add_parser('add-local', help='Local administrator: provision a fresh repository, initially disabled')
    target.add_argument('--data', type=Path, required=True)
    target.add_argument('--repository', type=Path, required=True)
    target.add_argument('--restic', type=Path, default=Path('/usr/local/bin/restic'))
    recover = sub.add_parser('restore', help='Restore into a fresh private tree with startup hold')
    recover.add_argument('--repository', type=Path, help='Owner-exported local ciphertext repository')
    recover.add_argument('--kit', type=Path, required=True)
    recover.add_argument('--target-id', required=True)
    recover.add_argument('--backup-id', required=True)
    recover.add_argument('--snapshot-id', help='Recorded snapshot ID; required when the backup ID is ambiguous')
    recover.add_argument('--destination', type=Path, required=True)
    recover.add_argument('--restic', type=Path, default=Path('/usr/local/bin/restic'))
    recover.add_argument('--prepare-replacement', action='store_true', help='Off by default: prepare a held replacement plan, never install or activate')
    recover.add_argument('--reviewed-nodes', type=Path, help='Private JSON mapping of independently verified node IDs to SHA-256 public-key fingerprints')
    recover.add_argument('--resume-session', action='append', default=[], help='Preapprove a reviewed remote session for a fresh manual Resume; never local')
    args = parser.parse_args(argv)
    try:
        if args.command == 'add-local':
            from .server.config import Settings
            service = Service(Settings(data_dir=args.data))
            result = service.add_local_target(args.repository, binary=args.restic)
            # No passwords, kit or private paths in output.
            print(json.dumps({'target_id': result['id'], 'enabled': False}))
        else:
            if args.kit.is_symlink() or args.kit.stat().st_size > 1 << 20:
                raise Refused('kit invalid or exceeds limit')
            kit = json.loads(args.kit.read_bytes())
            if (args.reviewed_nodes or args.resume_session) and not args.prepare_replacement:
                raise Refused('node/session selections require replacement preparation')
            if args.prepare_replacement and os.geteuid() == 0:
                raise Refused('replacement preparation must run unprivileged')
            from .backup_activation import _json
            reviewed = _json(args.reviewed_nodes) if args.reviewed_nodes else {}
            manifest = restore(kit, args.target_id, args.backup_id, args.destination, binary=args.restic, repository=args.repository,
                               snapshot_id=args.snapshot_id, prepare_replacement=args.prepare_replacement,
                               reviewed_nodes=reviewed, resume_sessions=args.resume_session)
            print(json.dumps({'backup_id': manifest['backup_id'], 'captured_at': manifest['started_at'],
                              'quarantine': True, 'exclusions': manifest['coverage_exclusions'],
                              **{key: manifest[key] for key in ('replacement_plan_sha256', 'new_main_id') if key in manifest}}))
        return 0
    except Exception as error:
        # No input, password, decrypted filenames or subprocess output in diagnostics; Refused reasons are fixed texts.
        reason = f' ({error})' if isinstance(error, Refused) else ''
        print(f'Rynhult backup operation refused{reason}; check the kit, explicit IDs (add --snapshot-id if a backup ID is ambiguous), trusted version, disk space and fresh destination.',
              file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
