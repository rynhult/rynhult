"""Private root credential generations for the file-based Google certificate candidate."""
import json
import os
import stat

import pytest
from test_dns_wave2 import FIELDS

from acc.dns_helper import DNSHelper, Refused


def test_google_files_follow_apply_rollback_and_purge(tmp_path):
    from acc.dns_credentials import caddy_provider
    from acc.dns_helper import GoogleCredentialFiles
    env = tmp_path / 'env'
    files = tmp_path / 'google'
    env.mkdir(mode=0o700)
    files.mkdir(mode=0o2750)
    files.chmod(0o2750)
    env_fd = os.open(env, os.O_RDONLY | os.O_DIRECTORY)
    files_fd = os.open(files, os.O_RDONLY | os.O_DIRECTORY)
    custody = GoogleCredentialFiles(files_fd, owner=os.getuid(), group=os.getgid())
    calls = []
    def runner(action, token):
        path = caddy_provider('googleclouddns', token['fields'])['gcp_application_default']
        candidate = files / path.rsplit('/', 1)[-1]
        assert candidate.read_text() == token['fields']['service_account_json']
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o640
        calls.append(action)
        return True
    helper = DNSHelper(env_fd, {'zones': ['example.com'], 'providers': ['googleclouddns']}, runner,
        owner=os.getuid(), credential_files=custody, release=lambda: {'status': 'qualified',
            'provider_qualification': {'googleclouddns': {'certificates': True}}})
    first = dict(FIELDS['googleclouddns'])
    second = dict(first, service_account_json=first['service_account_json'].replace('example-key', 'replacement-key'))
    def apply(fields, revision):
        return helper.handle(dict(action='apply', provider='googleclouddns', zone='example.com', fields=fields, revision=revision))
    try:
        assert apply(first, 1)['ok']
        assert len(list(files.iterdir())) == 1
        assert apply(second, 2)['ok']
        assert len(list(files.iterdir())) == 2
        assert helper.handle({'action': 'rollback'})['ok']
        assert len(list(files.iterdir())) == 1
        assert helper.handle({'action': 'restart'})['ok']
        assert apply(second, 3)['ok']
        helper.clock = lambda: 10**12
        assert helper.handle({'action': 'purge'})['ok']
        assert len(list(files.iterdir())) == 1
        assert 'example-key' not in json.dumps(calls)
    finally:
        os.close(env_fd)
        os.close(files_fd)


@pytest.mark.parametrize('unsafe', ['symlink', 'mode', 'hardlink'])
def test_google_file_delivery_refuses_unsafe_existing_targets(tmp_path, unsafe):
    from acc.dns_credentials import caddy_provider
    from acc.dns_helper import GoogleCredentialFiles
    root = tmp_path / 'google'
    root.mkdir(mode=0o2750)
    root.chmod(0o2750)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    fields = FIELDS['googleclouddns']
    token = {'provider': 'googleclouddns', 'fields': fields}
    name = caddy_provider('googleclouddns', fields)['gcp_application_default'].rsplit('/', 1)[-1]
    victim = tmp_path / 'victim'
    victim.write_text('unchanged')
    target = root / name
    if unsafe == 'symlink':
        target.symlink_to(victim)
    elif unsafe == 'hardlink':
        os.link(victim, target)
    else:
        target.write_text('unchanged')
        target.chmod(0o666)
    try:
        with pytest.raises(Refused):
            GoogleCredentialFiles(fd, owner=os.getuid(), group=os.getgid()).prepare(token)
        assert victim.read_text() == 'unchanged'
    finally:
        os.close(fd)


@pytest.mark.parametrize('failure', ['validate', 'reload', 'unqualified'])
def test_failed_google_generation_never_retires_the_active_file(tmp_path, failure):
    from acc.dns_credentials import caddy_provider
    from acc.dns_helper import GoogleCredentialFiles
    env, files = tmp_path / 'env', tmp_path / 'files'
    env.mkdir(mode=0o700)
    files.mkdir(mode=0o2750)
    files.chmod(0o2750)
    env_fd = os.open(env, os.O_RDONLY | os.O_DIRECTORY)
    files_fd = os.open(files, os.O_RDONLY | os.O_DIRECTORY)
    first = dict(FIELDS['googleclouddns'])
    second = dict(first, service_account_json=first['service_account_json'].replace('example-key', 'refused-key'))
    def runner(action, token):
        return token['fields'] == first or action != failure
    release = {'status': 'qualified', 'provider_qualification': {'googleclouddns': {'certificates': True}}}
    helper = DNSHelper(env_fd, {'zones': ['example.com'], 'providers': ['googleclouddns']}, runner,
        owner=os.getuid(), credential_files=GoogleCredentialFiles(files_fd, owner=os.getuid(), group=os.getgid()),
        release=lambda: release)
    def apply(fields, revision):
        return helper.handle(dict(action='apply', provider='googleclouddns', zone='example.com', fields=fields, revision=revision))
    try:
        assert apply(first, 1)['ok']
        if failure == 'unqualified':
            release['provider_qualification']['googleclouddns']['certificates'] = False
            with pytest.raises(Refused, match='provider_unqualified'):
                apply(second, 2)
        else:
            assert apply(second, 2)['ok'] is False
        active = caddy_provider('googleclouddns', first)['gcp_application_default'].rsplit('/', 1)[-1]
        assert [path.name for path in files.iterdir()] == [active]
        assert (files / active).read_text() == first['service_account_json']
        assert helper.token((env / 'current.env').read_text())['fields'] == first
    finally:
        os.close(env_fd)
        os.close(files_fd)
