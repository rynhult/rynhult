"""All privileged operations use private temporary roots and a fake credential-aware runner."""
import json
import os
import stat

import pytest

from acc.dns_helper import DNSHelper, Refused, open_root

TOKEN = 'cloudflare-test-canary-1234567890'
NEW = 'cloudflare-replacement-9876543210'


@pytest.fixture
def helper(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    calls = []
    def runner(action, token):
        calls.append((action, token))
        return action != 'reload' or token != 'bad-token'
    # The reviewed release qualifies every provider here; root policy is what these tests vary.
    release = {'status': 'qualified', 'provider_qualification': {
        name: {'certificates': True, 'publishing': False, 'evidence': None} for name in ('cloudflare', 'azure', 'route53')}}
    h = DNSHelper(fd, {'zones': ['example.com']}, runner, owner=os.getuid(), release=lambda: release)
    yield h, root, calls
    os.close(fd)


def apply(h, token=TOKEN, revision=1):
    return h.handle(dict(action='apply', provider='cloudflare', zone='example.com', revision=revision,
                         fields={'api_token': token}))


def test_apply_restart_rollback_and_retention(helper):
    h, root, calls = helper
    first = apply(h)
    assert first['ok'] and first['generation']
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={TOKEN}\n'
    assert stat.S_IMODE((root / 'current.env').stat().st_mode) == 0o600
    assert apply(h, NEW, 2)['ok']
    assert h.handle({'action': 'rollback'})['ok']
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={TOKEN}\n'
    assert calls[-1] == ('reload', TOKEN)
    assert h.handle({'action': 'restart'})['ok']
    assert calls[-1] == ('restart', TOKEN)
    assert TOKEN not in json.dumps(first)


def test_failed_apply_restores_persisted_and_running_generation(helper):
    h, root, calls = helper
    assert apply(h)['ok']
    failed = apply(h, 'bad-token', 2)
    assert failed == {'ok': False, 'code': 'apply_failed'}
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={TOKEN}\n'
    assert calls[-1] == ('reload', TOKEN)


@pytest.mark.parametrize('target', ['current.env', 'state.json', '.lock', 'previous.env', 'candidate.env'])
def test_refuses_symlinks(helper, tmp_path, target):
    h, root, calls = helper
    victim = tmp_path / 'victim'
    victim.write_text('unchanged')
    (root / target).symlink_to(victim)
    with pytest.raises(Refused):
        apply(h)
    assert victim.read_text() == 'unchanged' and calls == []


def test_refuses_bad_mode_hardlinks_and_wrong_owner(helper, tmp_path):
    h, root, calls = helper
    current = root / 'current.env'
    current.write_text('unsafe')
    current.chmod(0o644)
    with pytest.raises(Refused):
        apply(h)
    current.chmod(0o600)
    os.link(current, tmp_path / 'alias')
    with pytest.raises(Refused):
        apply(h)
    (tmp_path / 'alias').unlink()
    h.owner = os.getuid() + 1
    with pytest.raises(Refused):
        apply(h)
    assert calls == []


def test_fixed_policy_and_bounded_fields(helper):
    h, root, calls = helper
    for message in [dict(action='apply', provider='azure', zone='example.com', revision=1, fields={'api_token': TOKEN}),
                    dict(action='apply', provider='cloudflare', zone='other.com', revision=1, fields={'api_token': TOKEN}),
                    dict(action='apply', provider='cloudflare', zone='example.com', revision=1,
                         fields={'api_token': TOKEN + '\nHOME=/tmp/attack'}),
                    {'action': 'restart', 'command': 'touch /tmp/attack'}]:
        with pytest.raises(Refused):
            h.handle(message)
    assert calls == []


def test_production_walk_refuses_writable_ancestors(tmp_path):
    with pytest.raises(Refused):
        open_root(tmp_path)  # /tmp is writable: never a production trust root


def test_expired_rollback_and_purge(helper):
    h, root, calls = helper
    assert apply(h)['ok']
    assert apply(h, NEW, 2)['ok']
    h.clock = lambda: 10**12
    with pytest.raises(Refused, match='expired'):
        h.handle({'action': 'rollback'})
    assert h.handle({'action': 'purge'})['ok']
    assert not (root / 'previous.env').exists()
    assert not (root / 'candidate.env').exists()


def test_migration_never_sources_shell_and_checks_metadata(helper):
    from acc.dns_helper import read_legacy
    h, root, calls = helper
    legacy = root / 'cloudflare.env'
    legacy.write_text(f'CF_API_TOKEN={TOKEN}\n')
    legacy.chmod(0o600)
    assert read_legacy(h.fd, owner=os.getuid()) == TOKEN
    legacy.write_text('CF_API_TOKEN=$(touch /tmp/should-not-exist)\n')
    with pytest.raises(Refused):
        read_legacy(h.fd, owner=os.getuid())
    legacy.unlink()
    legacy.symlink_to(root / 'current.env')
    with pytest.raises(Refused):
        read_legacy(h.fd, owner=os.getuid())


def test_binary_digest_is_checked_before_execution():
    import hashlib

    from acc.dns_helper import verify_artifact
    binary = b'not executed'
    manifest = {'status': 'qualified', 'artifacts': {'amd64': {'sha256': hashlib.sha256(binary).hexdigest()}}}
    assert verify_artifact(binary, manifest, 'amd64') == binary
    with pytest.raises(Refused):
        verify_artifact(binary + b'changed', manifest, 'amd64')
    with pytest.raises(Refused):
        verify_artifact(binary, {'status': 'pending', 'artifacts': {}}, 'amd64')


def test_pending_generation_refuses_another_apply(helper):
    h, root, calls = helper
    (root / 'state.json').write_text('{"pending":true}')
    (root / 'state.json').chmod(0o600)
    with pytest.raises(Refused, match='recovery'):
        apply(h)
    assert calls == []


def test_symlink_race_between_validation_and_write_is_refused(helper, tmp_path):
    h, root, calls = helper
    outside = tmp_path / 'outside'
    outside.write_text('intact')
    def race(action, token):
        if action == 'validate':
            (root / 'current.env').symlink_to(outside)
        return True
    h.runner = race
    with pytest.raises(Refused):
        apply(h)
    assert outside.read_text() == 'intact'


def test_atomic_binary_install_requires_committed_digest(helper):
    import hashlib

    from acc.dns_helper import install_binary
    h, root, calls = helper
    data = b'qualified-test-artifact'
    manifest = {'status': 'qualified', 'artifacts': {'amd64': {'sha256': hashlib.sha256(data).hexdigest()}}}
    install_binary(h.fd, data, manifest, 'amd64', owner=os.getuid())
    assert (root / 'caddy').read_bytes() == data
    assert stat.S_IMODE((root / 'caddy').stat().st_mode) == 0o755
    with pytest.raises(Refused):
        install_binary(h.fd, b'tampered', manifest, 'amd64', owner=os.getuid())
    assert (root / 'caddy').read_bytes() == data


def test_configuration_lock_serializes_independent_callers(helper):
    import threading

    from acc.dns_helper import caddy_lock
    h, root, calls = helper
    started, acquired = threading.Event(), threading.Event()
    def other():
        started.set()
        with caddy_lock(h.fd, owner=os.getuid()):
            acquired.set()
    with caddy_lock(h.fd, owner=os.getuid()):
        thread = threading.Thread(target=other)
        thread.start()
        assert started.wait(2)
        assert not acquired.wait(.05)
    thread.join(2)
    assert acquired.is_set()


def test_configuration_lock_wait_is_bounded(helper):
    from acc.dns_helper import caddy_lock
    h, root, calls = helper
    with caddy_lock(h.fd, owner=os.getuid()):
        with pytest.raises(Refused, match='busy'):
            with caddy_lock(h.fd, owner=os.getuid(), timeout=.01):
                pytest.fail('concurrent configuration lock was granted')


def test_binary_switch_crash_keeps_current_executable(helper, monkeypatch):
    import hashlib

    from acc.dns_helper import install_binary
    h, root, calls = helper
    old, new = b'old-verified-binary', b'new-verified-binary'
    manifest = lambda data: {'status': 'qualified', 'artifacts': {'amd64': {'sha256': hashlib.sha256(data).hexdigest()}}}
    install_binary(h.fd, old, manifest(old), 'amd64', owner=os.getuid())
    rename = os.rename
    def crash(source, target, **kwargs):
        if target == 'caddy':
            raise OSError('simulated power loss before atomic switch')
        return rename(source, target, **kwargs)
    monkeypatch.setattr(os, 'rename', crash)
    with pytest.raises(OSError):
        install_binary(h.fd, new, manifest(new), 'amd64', owner=os.getuid())
    assert (root / 'caddy').read_bytes() == old


def test_caddy_subprocess_has_no_root_identity(monkeypatch):
    """Native setuid cannot run in this unprivileged suite; inspect its subprocess boundary."""
    import subprocess
    from types import SimpleNamespace

    from acc.dns_helper import bounded_command
    calls = []
    def child(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', child)
    assert bounded_command(['/usr/local/bin/caddy', 'adapt'], {}, run_as_caddy=True) == b''
    assert calls[0]['user'] == 'caddy' and calls[0]['group'] == 'caddy' and calls[0]['extra_groups'] == []
    assert bounded_command(['/usr/bin/systemctl', 'restart', 'caddy.service'], {}) == b''
    assert calls[1]['user'] is None


def test_first_migration_apply_seeds_legacy_rollback(helper):
    h, root, calls = helper
    h.legacy_reader = lambda: TOKEN  # fixed legacy root-file reader in production
    assert apply(h, NEW)['ok']
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={NEW}\n'
    assert h.handle({'action': 'rollback'})['ok']
    assert (root / 'current.env').read_text() == f'CF_API_TOKEN={TOKEN}\n'
    assert calls[-1] == ('reload', TOKEN)


@pytest.mark.parametrize('provider', ['azure', 'route53'])
def test_multicloud_generations_are_typed_and_rollback_is_complete(helper, provider):
    from test_dns_providers_p2 import AWS_FIELDS, AZURE_FIELDS
    h, root, calls = helper
    h.policy['providers'] = ['cloudflare', 'azure', 'route53']
    fields = AZURE_FIELDS if provider == 'azure' else AWS_FIELDS
    assert apply(h)['ok']
    outcome = h.handle(dict(action='apply', provider=provider, zone='example.com', revision=2, fields=fields))
    assert outcome['ok']
    assert calls[-1] == ('reload', {'provider': provider, 'fields': fields})
    assert h.handle({'action': 'restart'})['ok']
    assert h.handle({'action': 'rollback'})['ok']
    assert calls[-1] == ('reload', TOKEN)
    assert TOKEN not in json.dumps(outcome)


def test_route53_json_uses_explicit_region_and_hosted_zone():
    from test_dns_providers_p2 import AWS_FIELDS

    from acc.dns_credentials import caddy_provider
    rendered = caddy_provider('route53', AWS_FIELDS)
    assert rendered['region'] == 'us-east-1'
    assert rendered['hosted_zone_id'] == 'ZEXAMPLE'
    assert 'profile' not in rendered


def test_installed_helper_imports_credentials_in_isolated_python(tmp_path):
    import subprocess
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'src/acc'
    for name, target in [('dns_helper.py', 'helper.py'), ('dns_credentials.py', 'credentials.py')]:
        (tmp_path / target).write_text((source / name).read_text())
    # Invalid argv stops before root paths even if CI runs as root; imports must still work under systemd's -I.
    result = subprocess.run(['/usr/bin/python3', '-I', str(tmp_path / 'helper.py'), '--invalid'],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.strip().startswith('acc-dns: refused')
