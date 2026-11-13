"""Qualified-only account extractor. No npm invocation and no network access.

Every publication step is a separate accd-driven RPC: the caller must journal its
intent before calling it and its observation before calling the next step.
Local operation memory is advisory; losing it never causes automatic reexecution.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import pwd
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from functools import cache
from pathlib import Path

from .. import install_artifact as artifact
from ..install_qualification import QUALIFIED_TOOL_EXECUTORS

PROTOCOL = 'account-install-admission-v1'
ADAPTER = 'npm-extractor-v1'
# The shared owner qualification registry is deliberately empty in this build.
PROVENANCE_FIELDS = {'request_digest', 'source', 'requesting_session', 'lead_id', 'team_id',
    'team_generation', 'project_id', 'grant_digest', 'security_revision', 'machine_id', 'account',
    'consent_id', 'consent_revision', 'issuer', 'registry_version', 'recipe_revision', 'qualification', 'managed_versions'}


def non_root_uid(uid):
    """Production identity fence, shared with accd; no RPC/environment override."""
    return type(uid) is int and uid > 0


def local_machine_id():
    if sys.platform != 'linux':
        raise artifact.InvalidArtifact('runtime_unqualified')
    fd = os.open('/etc/machine-id', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        value = os.read(fd, 257).strip()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022
                or not re.fullmatch(b'[0-9a-f]{32}', value)):
            raise artifact.InvalidArtifact('runtime_unqualified')
        return hashlib.sha256(value).hexdigest()
    finally:
        os.close(fd)


@cache
def _interpreter_digest():
    """Pin the running interpreter once per process; restart after upgrades."""
    with open(sys.executable, 'rb') as interpreter:
        return hashlib.file_digest(interpreter, 'sha256').hexdigest()


def qualification_fingerprint(recipe_digest, runtime_id):
    # Code and interpreter changes invalidate qualification without relying on a
    # mutable broker-local installation identity as proof of runtime bytes.
    python_digest = _interpreter_digest()
    return artifact.digest({'protocol': PROTOCOL, 'adapter': ADAPTER, 'runtime_id': runtime_id,
        'recipe_digest': recipe_digest, 'python': python_digest, 'platform': sys.platform,
        'machine': os.uname().machine, 'kernel': os.uname().release, 'machine_id': local_machine_id(),
        'adapter_code': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'inspector_code': hashlib.sha256(Path(artifact.__file__).read_bytes()).hexdigest()})


class AccountLock:
    """One nonblocking lock for delegated and legacy person account mutations."""
    def __init__(self, run_dir):
        self.path = Path(run_dir) / 'account-tools.lock'
        self.uid = os.geteuid()  # Lock ownership is bound to broker startup identity.
        self.mutex = threading.Lock()
        self.fd = None

    def acquire(self):
        if not self.mutex.acquire(blocking=False):
            raise artifact.InvalidArtifact('collision')
        fd = None
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self.uid or st.st_nlink != 1:
                raise artifact.InvalidArtifact('collision')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
        except BaseException as error:
            if fd is not None:
                os.close(fd)
            self.mutex.release()
            if isinstance(error, OSError):
                raise artifact.InvalidArtifact('collision') from None
            raise

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.mutex.release()

    @contextlib.contextmanager
    def held(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()


def _directory(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        _identity(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def parent_fd(root, relative):
    pieces = artifact.path(relative).split('/')
    fd = os.dup(root)
    try:
        for part in pieces[:-1]:
            child = _directory(fd, part)
            os.close(fd)
            fd = child
        yield fd, pieces[-1]
    finally:
        os.close(fd)


def _identity(fd):
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise artifact.InvalidArtifact('collision')
    return [st.st_dev, st.st_ino, st.st_uid]


def _noreplace(source_fd, source, destination_fd, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise artifact.InvalidArtifact('runtime_unqualified')
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise artifact.InvalidArtifact('collision')
        raise artifact.InvalidArtifact('executor_failed')


def observe(root, manifest):
    """Re-hash all declared paths; version strings alone never prove a no-op."""
    observations = []
    for entry in manifest:
        with parent_fd(root, entry['path']) as (parent, name):
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            result = {'path': entry['path'], 'mode': stat.S_IMODE(st.st_mode), 'uid': st.st_uid}
            if entry['type'] == 'link':
                if not stat.S_ISLNK(st.st_mode):
                    raise artifact.InvalidArtifact('footprint_mismatch')
                result.update(type='link', target=os.readlink(name, dir_fd=parent))
            elif entry['type'] == 'directory':
                if not stat.S_ISDIR(st.st_mode):
                    raise artifact.InvalidArtifact('footprint_mismatch')
                result['type'] = 'directory'
            else:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise artifact.InvalidArtifact('footprint_mismatch')
                    hashed, size = hashlib.sha256(), 0
                    while chunk := os.read(fd, 1024 * 1024):
                        size += len(chunk)
                        if size > entry['size']:
                            raise artifact.InvalidArtifact('footprint_mismatch')
                        hashed.update(chunk)
                    result.update(type='file', sha256=hashed.hexdigest(), size=size,
                                  mode=stat.S_IMODE(before.st_mode), uid=before.st_uid)
                finally:
                    os.close(fd)
            if any(result.get(k) != v for k, v in entry.items()):
                raise artifact.InvalidArtifact('footprint_mismatch')
            if result['uid'] != os.geteuid():
                raise artifact.InvalidArtifact('footprint_mismatch')
            observations.append(result)
    return observations


class Extractor:
    def __init__(self, config, lock, runtime_id, *, home=None):
        self.config, self.lock, self.runtime_id = config, lock, runtime_id
        # RPC input and the agent-held HOME environment never select the prefix.
        self.home = Path(pwd.getpwuid(os.geteuid()).pw_dir) if home is None else Path(home)
        self.operations = {}

    def _scope(self, envelope):
        fields = {'protocol', 'operation_id', 'nonce', 'uid', 'runtime_id', 'host', 'recipe_digest',
                  'lease_deadline', 'artifact_sha512', 'manifest_digest', 'metadata', 'provenance', 'limits'}
        if not isinstance(envelope, dict) or set(envelope) != fields:
            raise artifact.InvalidArtifact('runtime_unqualified')
        artifact.limits(envelope['limits'])
        provenance = envelope['provenance']
        if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
            raise artifact.InvalidArtifact('authority_changed')
        artifact.canonical(provenance)
        fingerprint = qualification_fingerprint(envelope['recipe_digest'], self.runtime_id)
        if (provenance['qualification'] != fingerprint or fingerprint not in QUALIFIED_TOOL_EXECUTORS or self.config.role != 'agents'
                or not non_root_uid(os.geteuid()) or type(envelope['uid']) is not int or envelope['uid'] != os.geteuid()
                or envelope['runtime_id'] != self.runtime_id or envelope['protocol'] != PROTOCOL
                or envelope['host'] != 'local'):
            raise artifact.InvalidArtifact('runtime_unqualified')
        if type(envelope['lease_deadline']) is not int or time.time() >= envelope['lease_deadline']:
            raise artifact.InvalidArtifact('lease_expired')
        return fingerprint

    def stage(self, envelope, blob):
        self._scope(envelope)
        operation_id = envelope['operation_id']
        if not isinstance(operation_id, str) or not re.fullmatch('[0-9a-f]{32}', operation_id):
            raise artifact.InvalidArtifact()
        if operation_id in self.operations:
            raise artifact.InvalidArtifact('authority_changed')
        self.lock.acquire()
        scratch = None
        scratch_fd = None
        started = time.monotonic()
        try:
            if hashlib.sha512(blob).hexdigest() != envelope['artifact_sha512']:
                raise artifact.InvalidArtifact()
            inspected, files = artifact.inspect(blob, envelope['metadata'], ceilings=envelope['limits'])
            if inspected['manifest_digest'] != envelope['manifest_digest']:
                raise artifact.InvalidArtifact('footprint_mismatch')
            home = self.home
            for cmd in inspected['bins']:
                artifact.command(cmd)
                for base in (home / '.local/bin', home / '.cargo/bin', home / 'go/bin', Path('/opt/homebrew/bin')):
                    if os.path.lexists(base / cmd):
                        raise artifact.InvalidArtifact('collision')
            scratch = Path(tempfile.mkdtemp(prefix='account-install-', dir=self.config.run_dir))
            os.chmod(scratch, 0o700)
            manifest = inspected['manifest']
            scratch_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            scratch_bytes = 0
            for entry in sorted(manifest, key=lambda e: (e['path'].count('/'), e['path'])):
                if time.monotonic() - started >= envelope['limits']['execution_seconds']:
                    raise artifact.InvalidArtifact('resource_limit')
                scratch_bytes += entry.get('size', 0)
                if scratch_bytes > envelope['limits']['scratch_bytes']:
                    raise artifact.InvalidArtifact('resource_limit')
                if entry['type'] == 'directory':
                    with parent_fd(scratch_fd, entry['path']) as (parent, name):
                        os.mkdir(name, mode=0o755, dir_fd=parent)
                else:
                    # Scratch is account-held; publication reopens with no-follow and hashes once.
                    with parent_fd(scratch_fd, entry['path']) as (parent, name):
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, entry['mode'], dir_fd=parent)
                    try:
                        remaining = memoryview(files[entry['path']])
                        while remaining:
                            written = os.write(fd, remaining)
                            if written <= 0:
                                raise artifact.InvalidArtifact('executor_failed')
                            remaining = remaining[written:]
                        os.fchmod(fd, entry['mode'])
                    finally:
                        os.close(fd)
            monitor_scratch(scratch, envelope['limits']['scratch_bytes'])
            os.close(scratch_fd)
            scratch_fd = None
            home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                home_identity = _identity(home_fd)
                if home_identity[2] != os.geteuid():
                    raise artifact.InvalidArtifact('authority_changed')
            finally:
                os.close(home_fd)
            self.operations[operation_id] = {'envelope': envelope, 'scratch': scratch,
                'manifest': manifest, 'plan': publication_plan(inspected), 'home_identity': home_identity, 'next': 0}
            return {'operation_id': operation_id, 'manifest': manifest,
                    'manifest_digest': inspected['manifest_digest'], 'plan': publication_plan(inspected),
                    'home_identity': home_identity}
        except BaseException:
            if scratch_fd is not None:
                os.close(scratch_fd)
            self.lock.release()
            # Retain scratch for person inspection; it is not evidence.
            raise

    def footprint(self, envelope, manifest):
        self._scope(envelope)
        if artifact.digest(manifest) != envelope['manifest_digest']:
            raise artifact.InvalidArtifact('footprint_mismatch')
        package = envelope['metadata']
        bins = artifact.metadata(package)
        for name in bins:
            for base in (self.home / '.cargo/bin', self.home / 'go/bin', Path('/opt/homebrew/bin')):
                if os.path.lexists(base / name):
                    raise artifact.InvalidArtifact('collision')
        plan = publication_plan({'package': package, 'manifest': manifest, 'bins': bins})
        with self.lock.held():
            home = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                home_identity = _identity(home)
                if home_identity[2] != os.geteuid():
                    raise artifact.InvalidArtifact('authority_changed')
                for index, entry in enumerate(plan):
                    if entry['type'] == 'directory':
                        continue
                    try:
                        with parent_fd(home, temporary_path(envelope['operation_id'], index, entry)) as (parent, name):
                            os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise artifact.InvalidArtifact('collision')
                base = '.local/lib/node_modules/' + package['name']
                try:
                    with parent_fd(home, base) as (parent, name):
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    for command in bins:
                        for directory in (self.home / '.local/bin', self.home / '.cargo/bin',
                                          self.home / 'go/bin', Path('/opt/homebrew/bin')):
                            if os.path.lexists(directory / command):
                                raise artifact.InvalidArtifact('collision')
                    return {'state': 'absent', 'home_identity': home_identity}
                expected = [{k: v for k, v in e.items() if k not in ('source', 'shared')}
                            for e in plan if not e.get('shared')]
                try:
                    with parent_fd(home, base) as (parent, name):
                        package_fd = _directory(parent, name)
                    try:
                        verify_package_tree(package_fd, manifest)
                    finally:
                        os.close(package_fd)
                    observations = observe(home, expected)
                except (OSError, artifact.InvalidArtifact):
                    raise artifact.InvalidArtifact('collision') from None
                return {'state': 'already_installed', 'home_identity': home_identity,
                        'footprint': observations, 'footprint_digest': artifact.digest(observations)}
            finally:
                os.close(home)

    def abort(self, operation_id):
        operation = self.operations.pop(operation_id, None)
        if operation:
            self.lock.release()
        return {'state': 'uncertain' if operation else 'unknown'}

    def publish_step(self, operation_id, manifest_digest, index):
        """One previously journaled intent. No local replay or auto-compensation."""
        operation = self.operations.get(operation_id)
        if operation is None:
            raise artifact.InvalidArtifact('transport_lost')
        self._publication_budget(operation)
        # Stage and finish scan the tree once each; per-step source rehashing
        # avoids a quadratic tree walk during publication.
        if (manifest_digest != operation['envelope']['manifest_digest']
                or type(index) is not int or index != operation['next']):
            raise artifact.InvalidArtifact('authority_changed')
        plan = operation['plan']
        if index >= len(plan):
            raise artifact.InvalidArtifact('authority_changed')
        entry = plan[index]
        home = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        source_root = os.open(operation['scratch'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(home) != operation['home_identity']:
                raise artifact.InvalidArtifact('authority_changed')
            with parent_fd(home, entry['path']) as (parent, name):
                parent_identity = _identity(parent)
                if entry['type'] == 'directory':
                    created = True
                    try:
                        os.mkdir(name, entry['mode'], dir_fd=parent)
                    except FileExistsError:
                        if not entry.get('shared'):
                            raise artifact.InvalidArtifact('collision') from None
                        check = _directory(parent, name)
                        try:
                            if os.fstat(check).st_uid != os.geteuid():
                                raise artifact.InvalidArtifact('collision')
                        finally:
                            os.close(check)
                        created = False
                    if created:
                        created_directory = _directory(parent, name)
                        try:
                            os.fchmod(created_directory, entry['mode'])
                            os.fsync(created_directory)
                        finally:
                            os.close(created_directory)
                    observed = {'created': created}
                else:
                    if entry['type'] == 'link':
                        artifact.command(name)
                        for base in (self.home / '.cargo/bin', self.home / 'go/bin', Path('/opt/homebrew/bin')):
                            if os.path.lexists(base / name):
                                raise artifact.InvalidArtifact('collision')
                    temporary = temporary_path(operation_id, index, entry).rsplit('/', 1)[-1]
                    temporary_identity = None
                    temporary_verified = False
                    try:
                        if entry['type'] == 'link':
                            os.symlink(entry['target'], temporary, dir_fd=parent)
                            created = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                            temporary_identity = [created.st_dev, created.st_ino, created.st_uid]
                            temporary_verified = True
                        else:
                            with parent_fd(source_root, entry['source']) as (source_parent, source_name):
                                source = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
                            destination = None
                            try:
                                st = os.fstat(source)
                                if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != entry['mode']
                                        or st.st_uid != os.geteuid() or st.st_size != entry['size']):
                                    raise artifact.InvalidArtifact('footprint_mismatch')
                                destination = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                      0o600, dir_fd=parent)
                                created = os.fstat(destination)
                                temporary_identity = [created.st_dev, created.st_ino, created.st_uid]
                                hashed, size = hashlib.sha256(), 0
                                while chunk := os.read(source, 1024 * 1024):
                                    self._publication_budget(operation)
                                    size += len(chunk)
                                    if size > entry['size']:
                                        raise artifact.InvalidArtifact('footprint_mismatch')
                                    hashed.update(chunk)
                                    remaining = memoryview(chunk)
                                    while remaining:
                                        written = os.write(destination, remaining)
                                        if written <= 0:
                                            raise artifact.InvalidArtifact('executor_failed')
                                        remaining = remaining[written:]
                                if size != entry['size'] or hashed.hexdigest() != entry['sha256']:
                                    raise artifact.InvalidArtifact('footprint_mismatch')
                                os.fchmod(destination, entry['mode'])
                                os.fsync(destination)
                                temporary_verified = True
                            finally:
                                os.close(source)
                                if destination is not None:
                                    os.close(destination)
                        # Reopen the named parent before no-clobber publication.
                        with parent_fd(home, entry['path']) as (current_parent, _):
                            if _identity(current_parent) != parent_identity:
                                raise artifact.InvalidArtifact('collision')
                        self._publication_budget(operation)
                        _noreplace(parent, temporary, parent, name)
                        observed = {'created': True}
                    finally:
                        if temporary_identity is not None and temporary_verified:
                            try:
                                current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                                if [current.st_dev, current.st_ino, current.st_uid] == temporary_identity:
                                    expected_temp = {k: v for k, v in entry.items() if k not in ('shared', 'source')}
                                    expected_temp['path'] = temporary_path(operation_id, index, entry)
                                    observe(home, [expected_temp])
                                    current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                                    if [current.st_dev, current.st_ino, current.st_uid] == temporary_identity:
                                        os.unlink(temporary, dir_fd=parent)
                            except (OSError, artifact.InvalidArtifact):
                                pass  # Unproved or changed temporary paths require person recovery.
                os.fsync(parent)
                with parent_fd(home, entry['path']) as (current_parent, _):
                    if _identity(current_parent) != parent_identity:
                        raise artifact.InvalidArtifact('footprint_mismatch')
                published = os.stat(name, dir_fd=parent, follow_symlinks=False)
                observed.update(path=entry['path'], parent_identity=parent_identity,
                                identity=[published.st_dev, published.st_ino, published.st_uid])
                operation['next'] += 1
                return observed
        finally:
            os.close(home)
            os.close(source_root)

    def _publication_budget(self, operation):
        self._scope(operation['envelope'])
        deadline = operation.setdefault('publication_deadline',
            time.monotonic() + operation['envelope']['limits']['verification_seconds'])
        if time.monotonic() >= deadline:
            raise artifact.InvalidArtifact('resource_limit')

    def compensate(self, operation_id, index, evidence):
        operation = self.operations.get(operation_id)
        if operation is None or type(index) is not int or not 0 <= index < operation['next']:
            raise artifact.InvalidArtifact('transport_lost')
        entry = operation['plan'][index]
        if not isinstance(evidence, dict) or evidence.get('path') != entry['path'] or evidence.get('created') is not True:
            raise artifact.InvalidArtifact('footprint_mismatch')
        home = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _identity(home) != operation['home_identity']:
                raise artifact.InvalidArtifact('footprint_mismatch')
            with parent_fd(home, entry['path']) as (parent, name):
                if _identity(parent) != evidence['parent_identity']:
                    raise artifact.InvalidArtifact('footprint_mismatch')
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if [st.st_dev, st.st_ino, st.st_uid] != evidence['identity']:
                    return {'state': 'changed'}
                expected = {k: v for k, v in entry.items() if k not in ('source', 'shared')}
                observe(home, [expected])
                after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
                    return {'state': 'changed'}
                if entry['type'] == 'directory':
                    try:
                        os.rmdir(name, dir_fd=parent)
                    except OSError as error:
                        if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                            return {'state': 'nonempty'}
                        raise
                else:
                    os.unlink(name, dir_fd=parent)
                os.fsync(parent)
                return {'state': 'removed'}
        except (FileNotFoundError, artifact.InvalidArtifact):
            return {'state': 'changed'}
        finally:
            os.close(home)

    def finish(self, operation_id):
        operation = self.operations.get(operation_id)
        if operation is None or operation['next'] != len(operation['plan']):
            raise artifact.InvalidArtifact('transport_lost')
        self._publication_budget(operation)
        monitor_scratch(operation['scratch'], operation['envelope']['limits']['scratch_bytes'])
        home = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            footprint = [{k: v for k, v in entry.items() if k not in ('shared', 'source')}
                         for entry in operation['plan'] if not entry.get('shared')]
            observed = observe(home, footprint)
        finally:
            os.close(home)
        self.operations.pop(operation_id)
        self.lock.release()
        return {'state': 'installed', 'footprint': observed, 'footprint_digest': artifact.digest(observed)}


def temporary_path(operation_id, index, entry):
    parent = entry['path'].rsplit('/', 1)[0]
    return parent + '/.acc-install-' + operation_id + '-' + str(index)


def publication_plan(inspected):
    package = inspected['package']['name']
    base = '.local/lib/node_modules/' + package
    shared = ['.local', '.local/bin', '.local/lib', '.local/lib/node_modules']
    if package.startswith('@'):
        shared.append('.local/lib/node_modules/' + package.split('/')[0])
    result = [{'path': p, 'type': 'directory', 'mode': 0o755, 'shared': True} for p in shared]
    result.append({'path': base, 'type': 'directory', 'mode': 0o755})
    for entry in sorted(inspected['manifest'], key=lambda e: (e['path'].count('/'), e['path'])):
        item = entry | {'path': base + '/' + entry['path']}
        if entry['type'] == 'file':
            item['source'] = entry['path']
        result.append(item)
    for name, target in sorted(inspected['bins'].items()):
        result.append({'path': '.local/bin/' + name, 'type': 'link', 'mode': 0o777,
                       'target': '../lib/node_modules/' + package + '/' + target})
    return result


class Protocol:
    """Bounded artifact transfer over existing accd→broker socket, no reverse RPC."""
    def __init__(self, config, lock, runtime_id, *, home=None):
        self.extractor = Extractor(config, lock, runtime_id, home=home)
        self.transfer = None

    def call(self, params):
        import base64
        try:
            if not isinstance(params, dict):
                raise artifact.InvalidArtifact()
            phase = params.get('phase')
            if phase == 'identity' and set(params) == {'phase', 'recipe_digest'}:
                fingerprint = qualification_fingerprint(params['recipe_digest'], self.extractor.runtime_id)
                if (fingerprint not in QUALIFIED_TOOL_EXECUTORS or self.extractor.config.role != 'agents'
                        or not non_root_uid(os.geteuid())):
                    raise artifact.InvalidArtifact('runtime_unqualified')
                return {'uid': os.geteuid(), 'runtime_id': self.extractor.runtime_id}
            if phase == 'footprint' and set(params) == {'phase', 'envelope', 'manifest'}:
                return self.extractor.footprint(params['envelope'], params['manifest'])
            if phase == 'prepare' and set(params) == {'phase', 'envelope'}:
                envelope = params['envelope']
                self.extractor._scope(envelope)
                if self.transfer is not None or self.extractor.operations:
                    raise artifact.InvalidArtifact('collision')
                expire_scratch(self.extractor.config.run_dir)
                self.transfer = {'envelope': envelope, 'bytes': bytearray()}
                return {'state': 'ready'}
            if phase == 'chunk' and set(params) == {'phase', 'operation_id', 'offset', 'data'}:
                transfer = self.transfer
                if (transfer is None or params['operation_id'] != transfer['envelope']['operation_id']
                        or type(params['offset']) is not int or params['offset'] != len(transfer['bytes'])
                        or not isinstance(params['data'], str) or len(params['data']) > 2 * 1024 * 1024):
                    raise artifact.InvalidArtifact()
                self.extractor._scope(transfer['envelope'])
                chunk = base64.b64decode(params['data'], validate=True)
                if len(transfer['bytes']) + len(chunk) > transfer['envelope']['limits']['compressed_bytes']:
                    raise artifact.InvalidArtifact('resource_limit')
                transfer['bytes'].extend(chunk)
                return {'offset': len(transfer['bytes'])}
            if phase == 'stage' and set(params) == {'phase', 'operation_id'}:
                transfer = self.transfer
                if transfer is None or params['operation_id'] != transfer['envelope']['operation_id']:
                    raise artifact.InvalidArtifact('transport_lost')
                self.transfer = None
                return self.extractor.stage(transfer['envelope'], bytes(transfer['bytes']))
            if phase == 'publish' and set(params) == {'phase', 'operation_id', 'manifest_digest', 'index'}:
                return self.extractor.publish_step(params['operation_id'], params['manifest_digest'], params['index'])
            if phase == 'compensate' and set(params) == {'phase', 'operation_id', 'index', 'evidence'}:
                return self.extractor.compensate(params['operation_id'], params['index'], params['evidence'])
            if phase == 'finish' and set(params) == {'phase', 'operation_id'}:
                return self.extractor.finish(params['operation_id'])
            if phase == 'abort' and set(params) == {'phase', 'operation_id'}:
                if self.transfer and self.transfer['envelope']['operation_id'] == params['operation_id']:
                    self.transfer = None
                return self.extractor.abort(params['operation_id'])
            raise artifact.InvalidArtifact('runtime_unqualified')
        except artifact.InvalidArtifact as error:
            return {'reason': error.reason}
        except Exception:
            return {'reason': 'executor_failed'}


def verify_package_tree(root_fd, manifest):
    """Include directories and unexpected links without following any of them."""
    expected = {entry['path'] for entry in manifest}
    found = set()

    def walk(parent, prefix=''):
        for name in os.listdir(parent):
            relative = prefix + name
            if relative not in expected:
                raise artifact.InvalidArtifact('collision')
            found.add(relative)
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                child = _directory(parent, name)
                try:
                    walk(child, relative + '/')
                finally:
                    os.close(child)
    walk(root_fd)
    if found != expected:
        raise artifact.InvalidArtifact('collision')


def monitor_scratch(root, ceiling=200 * 1024 * 1024):
    """Monitored byte/entry ceiling, not a kernel-enforced filesystem quota."""
    size, entries = 0, 0
    for _, directories, files, parent in os.fwalk(root, follow_symlinks=False):
        entries += len(directories) + len(files)
        if entries > 20_000:
            raise artifact.InvalidArtifact('resource_limit')
        for name in files:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            size += st.st_size
            if size > ceiling:
                raise artifact.InvalidArtifact('resource_limit')


def expire_scratch(run_dir):
    """Only advisory extractor scratch; durable service evidence is never touched."""
    if not shutil.rmtree.avoids_symlink_attacks:
        return
    cutoff = time.time() - 7 * 24 * 60 * 60
    for entry in Path(run_dir).iterdir():
        if not re.fullmatch('account-install-[a-z0-9_]{8}', entry.name):
            continue
        st = entry.lstat()
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and st.st_mtime < cutoff:
            shutil.rmtree(entry)
