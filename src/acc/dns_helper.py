"""Root DNS credential delivery. Fixed policy/paths; no shell, caller commands or raw errors.

Installed independently under /usr/local/lib/acc-dns (root-owned ancestors).
The production entrypoint accepts no path/environment overrides. Tests inject directory
FDs and a runner: they cannot select a production target through IPC.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path

try:
    from acc.dns_credentials import (
        caddy_directive,
        caddy_provider,
        check_fields,
        credential_environment,
        parse_environment,
    )
except ImportError:  # -I deliberately omits the script directory from sys.path.
    import importlib.util
    # The helper and this fixed sibling are installed together in a root-owned, non-writable directory.
    # No IPC field, environment variable or argv selects this module path.
    _spec = importlib.util.spec_from_file_location('acc_fixed_dns_credentials', Path(__file__).with_name('credentials.py'))
    _credentials = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_credentials)
    caddy_directive = _credentials.caddy_directive
    caddy_provider = _credentials.caddy_provider
    check_fields = _credentials.check_fields
    credential_environment = _credentials.credential_environment
    parse_environment = _credentials.parse_environment

MAX_MESSAGE = 16384
WINDOW = 24 * 3600


class Refused(ValueError):
    """Sanitized refusal; never include a caller's value."""


def trusted(st, owner, directory=False):
    if st.st_uid != owner or (not directory and st.st_gid != owner) or st.st_mode & 0o022:
        raise Refused('unsafe_owner_or_mode')
    if directory:
        if not stat.S_ISDIR(st.st_mode):
            raise Refused('unsafe_directory')
    elif not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:
        raise Refused('unsafe_file')


def open_root(path):
    """Walk from / with directory FDs: symlink and writable ancestor refusal, no check/open gap."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('unsafe_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        trusted(os.fstat(fd), 0, True)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            trusted(os.fstat(fd), 0, True)
        return fd
    except (OSError, Refused):
        os.close(fd)
        raise Refused('unsafe_path') from None


@contextlib.contextmanager
def caddy_lock(directory_fd=None, *, owner=0, timeout=10):
    """Shared with acc-publish; root-owned non-writable parent, fixed no-follow lock."""
    own_directory = directory_fd is None
    directory = open_root('/run/acc-dns') if own_directory else os.dup(directory_fd)
    lock = None
    try:
        trusted(os.fstat(directory), owner, True)
        lock = os.open('caddy-config.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        trusted(os.fstat(lock), owner)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused("configuration_busy") from None
                time.sleep(.01)
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def release_qualifies(release, provider):
    """A provider's certificate path must be qualified in the reviewed release as well as allowed by root policy."""
    if not isinstance(release, dict) or release.get('status') != 'qualified':
        return False
    table = release.get('provider_qualification')
    if table is None:  # phase 1 manifests qualified a Cloudflare-only bundle
        return provider == 'cloudflare'
    entry = table.get(provider) if isinstance(table, dict) else None
    return isinstance(entry, dict) and entry.get('certificates') is True


def installed_release(policy_fd, *, owner=0):
    """The independently reviewed manifest beside root policy; missing means nothing is qualified."""
    try:
        return json.loads(DNSHelper(policy_fd, {}, None, owner=owner).read('release.json') or '{}')
    except ValueError:
        return {}


class GoogleCredentialFiles:
    """Fixed root-owned, setgid-caddy directory; only hashed credential generation names.

    Caddy can read these files but cannot replace them. Environment generations remain root-only.
    Tests supply directory FDs; production opens only the installed fixed directory.
    """
    def __init__(self, directory_fd, *, owner=0, group):
        self.fd, self.owner, self.group = directory_fd, owner, group
        st = os.fstat(self.fd)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_gid != group or
                stat.S_IMODE(st.st_mode) != 0o2750):
            raise Refused('unsafe_credential_directory')

    def read(self, name, *, temporary=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused('unsafe_credential_file') from None
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != self.owner or st.st_gid != self.group or
                    st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != (0o600 if temporary else 0o640) or
                    st.st_size > MAX_MESSAGE):
                raise Refused('unsafe_credential_file')
            return os.read(fd, MAX_MESSAGE + 1).decode()
        finally:
            os.close(fd)

    @staticmethod
    def name(token):
        if not isinstance(token, dict) or token['provider'] != 'googleclouddns':
            return None
        return caddy_provider(token['provider'], token['fields'])['gcp_application_default'].rsplit('/', 1)[-1]

    def prepare(self, token):
        name = self.name(token)
        if name is None:
            return
        content = token['fields']['service_account_json']
        existing = self.read(name)
        if existing is not None:
            if existing != content:
                raise Refused('credential_generation_mismatch')
            return
        temp = '.candidate-' + secrets.token_hex(16)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o640)
                os.fsync(stream.fileno())
            os.rename(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=self.fd)

    def retain(self, tokens):
        import re
        keep = {self.name(token) for token in tokens}
        # Validate every destination before removing any. Unrelated files are never touched.
        remove = []
        for name in os.listdir(self.fd):
            if re.fullmatch(r'[0-9a-f]{64}\.json', name):
                self.read(name)
                if name not in keep:
                    remove.append(name)
            elif re.fullmatch(r'\.candidate-[0-9a-f]{32}', name):
                # A process death can leave a root-private temporary file (or a chmod-completed one).
                try:
                    self.read(name, temporary=True)
                except Refused:
                    self.read(name)
                remove.append(name)
        for name in remove:
            os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)


class DNSHelper:
    def __init__(self, directory_fd, policy, runner, *, owner=0, clock=time.time, legacy_reader=None, release=None, credential_files=None):
        self.fd, self.policy, self.runner, self.owner, self.clock = directory_fd, policy, runner, owner, clock
        self.legacy_reader = legacy_reader
        self.credential_files = credential_files
        # Read per apply, so a reviewed manifest change takes effect without trusting a cached copy. None: refuse.
        self.release = release or (lambda: {})

    def read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused('unsafe_file') from None
        try:
            trusted(os.fstat(fd), self.owner)
            data = os.read(fd, MAX_MESSAGE + 1)
            if len(data) > MAX_MESSAGE:
                raise Refused('oversize_file')
            return data.decode('utf-8')
        finally:
            os.close(fd)

    def write(self, name, content):
        self.read(name)  # refuse existing unsafe destinations, even though rename would replace the link
        tmp = '.candidate-' + secrets.token_hex(16)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=self.fd)

    def handle(self, message):
        trusted(os.fstat(self.fd), self.owner, True)
        if stat.S_IMODE(os.fstat(self.fd).st_mode) != 0o700:
            raise Refused('unsafe_directory')
        self.read('.lock')
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
        try:
            trusted(os.fstat(lock), self.owner)
            fcntl.flock(lock, fcntl.LOCK_EX)
            # All destinations are checked before any external effect.
            current = self.read('current.env')
            rollback = self.read('previous.env')
            self.read('candidate.env')
            if current:
                self.token(current)
            state = json.loads(self.read('state.json') or '{}')
            action = message.get('action')
            if action == 'apply':
                if state.get('pending'):
                    raise Refused('recovery_required')
                if set(message) != {'action', 'provider', 'zone', 'revision', 'fields'}:
                    raise Refused('invalid_message')
                fields = message['fields']
                if (message['provider'] not in self.policy.get('providers', ['cloudflare']) or
                        message['zone'] not in self.policy['zones'] or
                        type(message['revision']) is not int or message['revision'] < 1):
                    raise Refused('invalid_binding')
                try:
                    fields = check_fields(message['provider'], fields)
                except (ValueError, TypeError):
                    raise Refused('invalid_binding') from None
                if fields.get('zone_name', message['zone']) != message['zone']:
                    raise Refused('invalid_binding')
                if not release_qualifies(self.release(), message['provider']):
                    raise Refused('provider_unqualified')
                previous = current
                if previous is None and self.legacy_reader is not None:
                    legacy_token = self.legacy_reader()
                    if legacy_token is not None:
                        previous = f'CF_API_TOKEN={legacy_token}\n'
                        self.token(previous)
                token = fields['api_token'] if message['provider'] == 'cloudflare' else {
                    'provider': message['provider'], 'fields': fields}
                generation = secrets.token_hex(16)
                candidate = self.environment(token)
                if len(candidate.encode()) > MAX_MESSAGE:
                    raise Refused('invalid_binding')
                if message['provider'] == 'googleclouddns' and self.credential_files is None:
                    raise Refused('credential_delivery_unavailable')
                if self.credential_files is not None:
                    self.credential_files.prepare(token)
                # Reload receives candidate environment, not the old service environment.
                if not self.runner('validate', token):
                    return {'ok': False, 'code': 'validation_failed'}
                self.write('candidate.env', candidate)
                if previous:
                    self.write('previous.env', previous)
                pending = {'generation': generation, 'previous_generation': (
                           state.get('generation') or ('legacy' if previous else None)),
                           'previous_until': self.clock() + WINDOW, 'pending': True,
                           'revision': message['revision'], 'zone': message['zone']}
                self.write('state.json', json.dumps(pending))
                self.write('current.env', candidate)
                if not self.runner('reload', token):
                    # Keep the existing rollback generation: overwriting it would make a later rollback a no-op
                    # that still reports the older generation.
                    self.restore(previous, state, rollback=rollback)
                    return {'ok': False, 'code': 'apply_failed'}
                pending['pending'] = False
                self.write('state.json', json.dumps(pending))
                return {'ok': True, 'generation': generation, 'issuance': 'unverified'}
            if set(message) != {'action'}:
                raise Refused('invalid_message')
            if action == 'rollback':
                previous = self.read('previous.env')
                if not previous or self.clock() > state.get('previous_until', 0):
                    raise Refused('rollback_expired')
                token = self.token(previous)
                if not self.runner('validate', token):
                    return {'ok': False, 'code': 'validation_failed'}
                self.write('current.env', previous)
                if not self.runner('reload', token):
                    self.restore(current, state)
                    return {'ok': False, 'code': 'rollback_failed'}
                self.write('state.json', json.dumps({'generation': state.get('previous_generation')}))
                # Rollback is one step: the abandoned credential and the now-current copy have no further use.
                self.discard('previous.env', 'candidate.env')
                return {'ok': True, 'generation': state.get('previous_generation')}
            if action == 'restart' and current and not state.get('pending'):
                return {'ok': bool(self.runner('restart', self.token(current))), 'generation': state.get('generation')}
            if action == 'recover' and state.get('pending'):
                previous = self.read('previous.env')
                self.restore(previous, {'generation': state.get('previous_generation')})
                return {'ok': True, 'generation': state.get('previous_generation')}
            if action == 'purge':
                if state.get('pending') or self.clock() <= state.get('previous_until', 0):
                    raise Refused('rollback_active')
                self.discard('previous.env', 'candidate.env')
                return {'ok': True}
            raise Refused('invalid_action')
        except OSError:
            raise Refused('filesystem_refused') from None
        finally:
            try:
                if self.credential_files is not None:
                    tokens = [self.token(content) for name in ('current.env', 'previous.env', 'candidate.env')
                              if (content := self.read(name))]
                    self.credential_files.retain(tokens)
            finally:
                os.close(lock)

    environment = staticmethod(credential_environment)

    @staticmethod
    def token(content):
        try:
            return parse_environment(content)
        except (ValueError, TypeError):
            raise Refused('invalid_credential_file') from None

    def discard(self, *names):
        for name in names:
            self.read(name)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def restore(self, previous, state, rollback=...):
        """Back to `previous`; the refused or crashed candidate is removed. `rollback` replaces previous.env."""
        if previous:
            self.write('current.env', previous)
            if not self.runner('reload', self.token(previous)):
                raise Refused('recovery_required')
        else:
            self.discard('current.env')
        if rollback is None:
            self.discard('previous.env')
        elif rollback is not ...:
            self.write('previous.env', rollback)
        self.discard('candidate.env')
        self.write('state.json', json.dumps(state))


def verify_artifact(data, manifest, architecture):
    """No execution before verification. Manifest is independently reviewed, never fetched with a binary."""
    artifact = manifest.get('artifacts', {}).get(architecture)
    if (not artifact or manifest.get('status') != 'qualified' or len(data) > 100 * 1024 * 1024 or
            hashlib.sha256(data).hexdigest() != artifact['sha256']):
        raise Refused('unapproved_binary')
    return data


def install_binary(directory_fd, data, manifest, architecture, *, owner=0):
    """Root installer only: bounded, verified bytes; atomic switch retaining a trusted last-good binary."""
    verify_artifact(data, manifest, architecture)
    trusted(os.fstat(directory_fd), owner, True)
    def check(name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        except OSError:
            raise Refused('unsafe_binary') from None
        try:
            st = os.fstat(fd)
            if (st.st_uid != owner or st.st_gid != owner or not stat.S_ISREG(st.st_mode) or
                    st.st_mode & 0o022 or st.st_nlink != 1):
                raise Refused('unsafe_binary')
        finally:
            os.close(fd)
        return True
    exists = check('caddy')
    check('caddy.previous')
    name = '.caddy-' + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
        if exists:
            # Copy last-good first: renaming it away would leave no executable after a crash.
            old_fd = os.open('caddy', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            backup = '.previous-' + secrets.token_hex(16)
            try:
                old_st = os.fstat(old_fd)
                if (old_st.st_uid != owner or old_st.st_gid != owner or not stat.S_ISREG(old_st.st_mode) or
                        old_st.st_nlink != 1 or old_st.st_mode & 0o022 or old_st.st_size > 100 * 1024 * 1024):
                    raise Refused('unsafe_binary')
                backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=directory_fd)
                with os.fdopen(backup_fd, 'wb') as output, os.fdopen(old_fd, 'rb', closefd=False) as source:
                    output.write(source.read(100 * 1024 * 1024 + 1))
                    output.flush()
                    os.fchmod(output.fileno(), 0o755)
                    os.fsync(output.fileno())
                os.rename(backup, 'caddy.previous', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(old_fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(backup, dir_fd=directory_fd)
        os.rename(name, 'caddy', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory_fd)


def read_legacy(directory_fd, *, owner=0, missing_ok=False):
    """The one fixed legacy file, strict assignment parser, never a shell/.env interpreter."""
    reader = DNSHelper(directory_fd, {}, None, owner=owner)
    text = reader.read('cloudflare.env')
    if text is None:
        if missing_ok:
            return None
        raise Refused('legacy_missing')
    return reader.token(text)


def bounded_command(argv, env, *, data=None, timeout=20, run_as_caddy=False):
    """Unlinked memory file and child output limit; errors cannot enter logs or IPC."""
    import resource
    import subprocess
    fd = os.memfd_create('acc-dns-private', os.MFD_CLOEXEC)
    def bounds():
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
    try:
        result = subprocess.run(argv, input=data, stdout=fd, stderr=subprocess.DEVNULL,
                                env=env, timeout=timeout, preexec_fn=bounds, check=False,
                                user="caddy" if run_as_caddy else None,
                                group="caddy" if run_as_caddy else None, extra_groups=[] if run_as_caddy else None)
        if result.returncode:
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 2 * 1024 * 1024)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        os.close(fd)


class CaddyRunner:
    """Root-owned binary/config are fixed by deployment, never selected over IPC."""
    def __init__(self):
        self.adapted = None
        self.deadline = time.monotonic() + 20

    def command(self, argv, env, *, data=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        return bounded_command(argv, env, data=data, timeout=remaining, run_as_caddy=argv[0] == "/usr/local/bin/caddy")

    def __call__(self, action, token):
        provider = token['provider'] if isinstance(token, dict) else 'cloudflare'
        fields = token['fields'] if isinstance(token, dict) else {'api_token': token}
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/var/lib/caddy',
               'ACC_DNS_CONFIG': caddy_directive(provider, fields)}
        if provider == 'cloudflare':
            env['CF_API_TOKEN'] = token
        binary = '/usr/local/bin/caddy'
        # Reopen trusted ancestors each operation and checksum before executing even list-modules.
        directory = open_root('/usr/local/bin')
        try:
            fd = os.open('caddy', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                st = os.fstat(fd)
                if st.st_uid != 0 or st.st_gid != 0 or not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022:
                    return False
                manifest_dir = open_root('/etc/acc-dns')
                try:
                    manifest = json.loads(DNSHelper(manifest_dir, {}, None).read('release.json') or '{}')
                finally:
                    os.close(manifest_dir)
                import platform
                arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
                data = os.read(fd, 100 * 1024 * 1024 + 1)
                verify_artifact(data, manifest, arch)
                # Ancestors are root-owned and non-writable; replacement is a trusted operator action.
            finally:
                os.close(fd)
        finally:
            os.close(directory)
        if action == 'validate':
            config = self.command([binary, 'adapt', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'], env)
            if config is None:
                return False
            try:
                parsed = json.loads(config)
                if parsed.get('admin', {}).get('listen') != 'unix//run/caddy/admin.sock|0660':
                    return False
                if provider != 'cloudflare':
                    def providers(node):
                        if isinstance(node, dict):
                            for k, v in node.items():
                                if k == 'provider' and isinstance(v, dict) and 'name' in v:
                                    yield v
                                yield from providers(v)
                        elif isinstance(node, list):
                            for v in node:
                                yield from providers(v)
                    found = list(providers(parsed))
                    expected = caddy_provider(provider, fields)
                    if not found or any(any(p.get(k) != v for k, v in expected.items()) for p in found):
                        return False
            except (ValueError, AttributeError):
                return False
            if self.command([binary, 'validate', '--config', '-'], env, data=config) is None:
                return False
            self.adapted = config
            return True
        if action == 'reload':
            # Adapt again with precisely this token, including when restoring an older generation.
            if not self('validate', token):
                return False
            return self.command([binary, 'reload', '--config', '-', '--address',
                                    'unix//run/caddy/admin.sock'], env, data=self.adapted) is not None
        if action == 'restart':
            if not self('validate', token):
                return False
            return self.command(['/usr/bin/systemctl', 'restart', 'caddy.service'],
                                {'PATH': '/usr/bin:/bin'}) is not None
        return False


def respond(raw, helper, legacy_fd):
    """One request line. Any failure, including hostile nesting from the peer, is the same fixed refusal."""
    runner = helper.runner
    try:
        if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
            raise Refused('invalid_message')
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise Refused('invalid_message')
        if message == {'action': 'import'}:
            result = {'ok': True, 'fields': {'api_token': read_legacy(legacy_fd)}}
        else:
            if isinstance(runner, CaddyRunner):
                runner.deadline = time.monotonic() + 20
            with caddy_lock():
                result = helper.handle(message)
    except Exception:  # noqa: BLE001  (never a raw error, and never end the root service)
        result = {'ok': False, 'code': 'refused'}
    finally:
        if isinstance(runner, CaddyRunner):
            runner.adapted = None
    return json.dumps(result).encode() + b'\n'


def main():
    """Socket activated root service. Only the acc UID may connect; root policy remains authoritative."""
    import pwd
    import socket
    import struct
    import sys
    if sys.argv[1:] not in ([], ['--purge']) or os.geteuid() != 0:
        raise SystemExit('acc-dns: refused. systemd starts this service; by hand, run it only as root with --purge.')
    policy_fd = open_root('/etc/acc-dns')
    directory = open_root('/etc/caddy/acc-dns')
    legacy_fd = open_root('/etc/caddy')
    google_fd = open_root('/etc/caddy/acc-dns-google')
    credential_files = GoogleCredentialFiles(google_fd, group=pwd.getpwnam('caddy').pw_gid)
    try:
        policy = json.loads(DNSHelper(policy_fd, {}, None).read('policy.json') or '{}')
        if sys.argv[1:] == ['--purge']:
            try:
                DNSHelper(directory, policy, None, credential_files=credential_files).handle({'action': 'purge'})
            except Refused:
                pass  # active rollback windows and incomplete generations are retained
            return
        allowed_uid = pwd.getpwnam('acc').pw_uid
        listener = socket.socket(fileno=3)  # exclusively supplied by systemd's fixed socket unit
        helper = DNSHelper(directory, policy, CaddyRunner(),
                           legacy_reader=lambda: read_legacy(legacy_fd, missing_ok=True),
                           release=lambda: installed_release(policy_fd), credential_files=credential_files)
        while True:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(30)
                _, uid, _ = struct.unpack('@iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != allowed_uid:
                    continue
                with contextlib.suppress(OSError):
                    with connection.makefile('rb') as stream:
                        raw = stream.readline(MAX_MESSAGE + 1)
                    connection.sendall(respond(raw, helper, legacy_fd))
    finally:
        for fd in (policy_fd, directory, legacy_fd, google_fd):
            os.close(fd)


if __name__ == '__main__':
    main()
