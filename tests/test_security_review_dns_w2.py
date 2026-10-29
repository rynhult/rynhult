"""Security review of DNS providers wave 2 (docs/reviews/2026-10-24-review-dns-wave-2.md)."""
import json
import os
from pathlib import Path

import pytest

from acc.dns_credentials import check_fields

ACCOUNT = {
    'type': 'service_account', 'project_id': 'example-project', 'private_key_id': '0123456789abcdef0123456789abcdef01234567',
    'private_key': '-----BEGIN PRIVATE KEY-----\nexample-secret-key-material\n-----END PRIVATE KEY-----\n',
    'client_email': 'dns@example-project.iam.gserviceaccount.com', 'client_id': '123456789012345678901',
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token',
    'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
    'client_x509_cert_url': 'https://www.googleapis.com/robot/v1/metadata/x509/dns%40example-project.iam.gserviceaccount.com',
    'universe_domain': 'googleapis.com',
}


def fields(text):
    return dict(project_id='example-project', managed_zone='example-zone', zone_name='example.com',
                service_account_json=text)


def test_srd3_01_a_complete_google_key_file_is_accepted():
    assert check_fields('googleclouddns', fields(json.dumps(ACCOUNT)))


SMUGGLED = {
    # Go's encoding/json matches keys case-insensitively and the last match wins: Caddy's Google library would
    # read an external-account credential that reads local files and posts them to a caller-chosen endpoint.
    'case-variant type': json.dumps(ACCOUNT)[:-1] + ', "Type": "external_account", "token_url": "https://sts.example.com/",'
                         ' "credential_source": {"file": "/proc/self/environ"}}',
    'case-variant token endpoint': json.dumps(ACCOUNT)[:-1] + ', "Token_URI": "https://token.example.com/"}',
    'duplicate key': json.dumps(ACCOUNT)[:-1] + ', "token_uri": "https://token.example.com/"}',
    'escaped key folding': json.dumps(ACCOUNT)[:-1] + ', "to\\u212aen_uri": "https://token.example.com/"}',
    'unknown field': json.dumps({**ACCOUNT, 'service_account_impersonation_url': 'https://iam.example.com/'}),
    'credential source': json.dumps({**ACCOUNT, 'credential_source': {'url': 'http://169.254.169.254/'}}),
    'non-string value': json.dumps({**ACCOUNT, 'client_id': 123456789012345678901}),
    'other auth endpoint': json.dumps({**ACCOUNT, 'auth_uri': 'https://auth.example.com/'}),
    'other certificate URL': json.dumps({**ACCOUNT, 'client_x509_cert_url': 'https://certs.example.com/x'}),
    'not an object': json.dumps([ACCOUNT]),
}


@pytest.mark.parametrize('case', SMUGGLED)
def test_srd3_01_google_key_file_refuses_fields_a_go_parser_reads_differently(case):
    with pytest.raises(ValueError) as error:
        check_fields('googleclouddns', fields(SMUGGLED[case]))
    assert 'example-secret-key-material' not in str(error.value)


def test_srd3_01_root_helper_refuses_a_smuggled_credential_before_writing_a_file(tmp_path):
    from acc.dns_helper import DNSHelper, GoogleCredentialFiles, Refused
    env, files = tmp_path / 'env', tmp_path / 'google'
    env.mkdir(mode=0o700)
    files.mkdir()
    files.chmod(0o2750)
    env_fd = os.open(env, os.O_RDONLY | os.O_DIRECTORY)
    files_fd = os.open(files, os.O_RDONLY | os.O_DIRECTORY)
    ran = []
    try:
        helper = DNSHelper(env_fd, {'zones': ['example.com'], 'providers': ['googleclouddns']},
            lambda action, token: ran.append(action) or True, owner=os.getuid(),
            credential_files=GoogleCredentialFiles(files_fd, owner=os.getuid(), group=os.getgid()),
            release=lambda: {'status': 'qualified', 'provider_qualification': {'googleclouddns': {'certificates': True}}})
        with pytest.raises(Refused):
            helper.handle(dict(action='apply', provider='googleclouddns', zone='example.com', revision=1,
                               fields=fields(SMUGGLED['case-variant type'])))
        assert not ran and not list(files.iterdir())
    finally:
        os.close(env_fd)
        os.close(files_fd)


def test_srd3_02_digitalocean_certificate_token_guidance_omits_update():
    # Caddy's DNS-01 solver only appends and deletes challenge records; Publishing also updates.
    root = Path(__file__).parents[1]
    for path in ('web/src/components/DNSSettings.tsx', 'docs/wiki/User-Guide/DNS-Providers.md', 'docs/DNS-PROVIDERS.md'):
        text = (root / path).read_text()
        assert 'domain:write' not in text, path
        assert 'domain:read, domain:create and domain:delete' in text, path
