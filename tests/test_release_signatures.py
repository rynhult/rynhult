"""Real, isolated GnuPG verification with throwaway keys and no network."""
import pytest


def test_good_signature_reports_trusted_primary(release_signer):
    from acc.release_signatures import verify
    body = b'abc  acc-1.0.0.tar.gz\n'
    result = verify(body, release_signer.sign(body))
    assert result == {'status': 'verified', 'primary': release_signer.fingerprint, 'name': 'Rynhult Releases'}


def test_wrong_key_with_identical_uid_is_not_trusted(release_signer, signing_keys):
    from acc.release_signatures import SignatureError, verify
    other = signing_keys()
    body = b'checksums'
    with pytest.raises(SignatureError):
        verify(body, other.sign(body))


def test_tampered_sums_refused(release_signer):
    from acc.release_signatures import SignatureError, verify
    with pytest.raises(SignatureError):
        verify(b'tampered', release_signer.sign(b'original'))


def test_expired_signing_subkey_is_refused(signing_keys, tmp_path, monkeypatch):
    from acc import release_signatures as signatures
    signer = signing_keys(at=1577836800, expiry='1d')
    key = tmp_path / 'expired.asc'
    key.write_bytes(signer.public())
    monkeypatch.setattr(signatures, 'KEY_FILE', key)
    with pytest.raises(signatures.SignatureError, match='expired|revoked|invalid'):
        signatures.verify(b'old release', signer.sign(b'old release'))


def test_revoked_signing_subkey_is_refused(release_signer, monkeypatch):
    from acc import release_signatures as signatures
    body = b'release before revocation'
    signature = release_signer.sign(body)
    release_signer.run('--command-fd', '0', '--edit-key', release_signer.fingerprint,
                       data=b'key 1\nrevkey\ny\n0\nTest revocation\n\ny\nsave\n')
    signatures.KEY_FILE.write_bytes(release_signer.public())
    with pytest.raises(signatures.SignatureError, match='expired|revoked|invalid'):
        signatures.verify(body, signature)


def test_rotated_key_only_after_trusted_bundle_is_replaced(release_signer, signing_keys):
    from acc import release_signatures as signatures
    successor = signing_keys()
    body = b'new release'
    signature = successor.sign(body)
    with pytest.raises(signatures.SignatureError):
        signatures.verify(body, signature)
    # This represents an anchor shipped inside a release verified with the old key;
    # network material can never append to the installed key file.
    transition = release_signer.public() + successor.public()
    assert signatures.verify(transition, release_signer.sign(transition))['primary'] == release_signer.fingerprint
    signatures.KEY_FILE.write_bytes(transition)
    assert signatures.verify(body, signature)['primary'] == successor.fingerprint
    assert signatures.verify(body, release_signer.sign(body))['primary'] == release_signer.fingerprint


@pytest.fixture
def signed_release(tmp_path, release_signer):
    import hashlib

    from acc import bundle
    source = tmp_path / 'release-source'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="1.0.0"\n')
    archive = bundle.build(source, tmp_path / 'bundles').read_bytes()
    sums = f'{hashlib.sha256(archive).hexdigest()}  acc-1.0.0.tar.gz\n'.encode()
    prefix = 'https://api.github.com/repos/example/acc/releases/assets/'
    bodies = {'1': sums, '2': release_signer.sign(sums), '3': archive}
    latest = {'latest': '1.0.0', 'assets': {'SHA256SUMS': prefix+'1', 'SHA256SUMS.asc': prefix+'2',
                                         'acc-1.0.0.tar.gz': prefix+'3'}}
    calls = []

    def get(url, *args, **kwargs):
        calls.append(url.rsplit('/', 1)[-1])
        return bodies[calls[-1]]
    return latest, bodies, calls, get


def test_download_requires_signature_before_fetching_archive(tmp_path, signed_release):
    from acc.updates import download_release
    latest, bodies, calls, get = signed_release
    bodies['1'] += b'tampered\n'
    with pytest.raises(ValueError, match='signature'):
        download_release('example/acc', latest, tmp_path / 'stage', get=get)
    assert '3' not in calls


def test_missing_signature_requires_explicit_one_shot_override(tmp_path, signed_release):
    from acc.updates import download_release
    latest, bodies, calls, get = signed_release
    del latest['assets']['SHA256SUMS.asc']
    with pytest.raises(ValueError, match='signature'):
        download_release('example/acc', latest, tmp_path / 'refused', get=get)
    assert '3' not in calls
    result = {}
    tree = download_release('example/acc', latest, tmp_path / 'explicit', get=get,
                            allow_unsigned=True, verification=result)
    assert (tree / 'pyproject.toml').is_file()
    assert result['signature']['status'] == 'unsigned_override'


def test_override_cannot_bypass_invalid_signature(tmp_path, signed_release):
    from acc.updates import download_release
    latest, bodies, calls, get = signed_release
    bodies['2'] = b'not a signature'
    with pytest.raises(ValueError, match='signature'):
        download_release('example/acc', latest, tmp_path / 'stage', get=get, allow_unsigned=True)
    assert '3' not in calls


def test_good_signature_still_requires_correct_archive_hash(tmp_path, signed_release):
    from acc.updates import download_release
    latest, bodies, _, get = signed_release
    bodies['3'] += b'tampered'
    with pytest.raises(ValueError, match='checksum|SHA|hash'):
        download_release('example/acc', latest, tmp_path / 'stage', get=get)


def test_good_release_records_verification(tmp_path, signed_release, release_signer):
    from acc.updates import download_release
    latest, _, _, get = signed_release
    result = {}
    tree = download_release('example/acc', latest, tmp_path / 'stage', get=get, verification=result)
    assert (tree / 'pyproject.toml').is_file()
    assert result['signature']['primary'] == release_signer.fingerprint


async def test_owner_unsigned_exception_is_one_shot_and_audited(api, tmp_path, monkeypatch):
    import dataclasses

    from sqlalchemy import select

    from acc.server.models import AuditLog
    service = api.app.state.ctx.updates
    installed = tmp_path / 'installed'
    installed.mkdir()
    service.settings = dataclasses.replace(service.settings, update_source=installed)
    (service.settings.updates_dir / 'requests').mkdir(parents=True, exist_ok=True)
    service.store({'latest': '99.0.0', 'available': True, 'signature': {'status': 'missing'}})
    refused = await api.post('/api/updates/apply')
    assert refused.status_code == 409 and 'signature' in refused.json()['detail']
    response = await api.post('/api/updates/apply', json={'allow_unsigned': True})
    assert response.status_code == 202, response.text
    import json
    requests = list((service.settings.updates_dir / 'requests').glob('*.json'))
    assert len(requests) == 1 and json.loads(requests[0].read_text())['allow_unsigned'] is True
    assert 'allow_unsigned' not in service.config()
    with service.db() as session:
        rows = list(session.scalars(select(AuditLog).where(AuditLog.action == 'update.request')))
        assert rows[-1].detail['allow_unsigned'] is True


async def test_invalid_signature_never_offers_an_exception(api, tmp_path, monkeypatch):
    import dataclasses
    service = api.app.state.ctx.updates
    installed = tmp_path / 'installed'
    installed.mkdir()
    service.settings = dataclasses.replace(service.settings, update_source=installed)
    (service.settings.updates_dir / 'requests').mkdir(parents=True, exist_ok=True)
    service.store({'latest': '99.0.0', 'available': True, 'signature': {'status': 'verified'},
                   'error': 'Release signature is invalid.'})
    response = await api.post('/api/updates/apply', json={'allow_unsigned': True})
    assert response.status_code == 409
    assert not list((service.settings.updates_dir / 'requests').glob('*.json'))


def test_missing_signature_exception_not_accepted_on_node_requests():
    from acc.update_helper import validate_node_request
    with pytest.raises(ValueError):
        validate_node_request({'action': 'update', 'version': '1.0.0', 'build': 'a'*64,
                               'sha256': 'b'*64, 'allow_unsigned': True})


def test_unsigned_override_request_is_strictly_boolean():
    from acc.updates import validate_request
    for value in [1, 'true', None, [], {}]:
        with pytest.raises(ValueError):
            validate_request({'action': 'update', 'version': '1.0.0', 'allow_unsigned': value})


def test_signature_design_is_private_in_all_source_exports():
    from acc.bundle import private_content
    assert private_content('docs/design/updater-signatures.md')
    assert not private_content('docs/design/DESIGN.md')


def test_release_check_authenticates_checksum_manifest(signed_release, release_signer):
    import json

    from acc.updates import signed_check
    latest, _, _, get_asset = signed_release
    metadata = {'tag_name': 'v1.0.0', 'assets': [{'name': name, 'url': url}
                                              for name, url in latest['assets'].items()]}

    def get(url, *args, **kwargs):
        return json.dumps([metadata]).encode() if url.endswith('/releases?per_page=30') else get_asset(url, *args, **kwargs)
    result = signed_check('example/acc', '0.14.0', get=get)
    assert result['signature']['primary'] == release_signer.fingerprint
    assert result['available'] is True


@pytest.mark.parametrize('failure', ['missing', 'network', 'invalid'])
def test_release_check_distinguishes_missing_from_bad_signature(signed_release, failure):
    import json

    from acc.updates import signed_check
    latest, bodies, _, get_asset = signed_release
    if failure == 'missing':
        del latest['assets']['SHA256SUMS.asc']
    elif failure == 'invalid':
        bodies['2'] = b'bad signature'
    metadata = {'tag_name': 'v1.0.0', 'assets': [{'name': name, 'url': url}
                                              for name, url in latest['assets'].items()]}

    def get(url, *args, **kwargs):
        if url.endswith('/releases?per_page=30'):
            return json.dumps([metadata]).encode()
        if url.endswith('/2') and failure == 'network':
            raise ValueError('could not reach release source')
        return get_asset(url, *args, **kwargs)
    if failure == 'missing':
        assert signed_check('example/acc', '0.14.0', get=get)['signature']['status'] == 'missing'
    else:
        with pytest.raises(ValueError):
            signed_check('example/acc', '0.14.0', get=get)


def test_attached_message_cannot_authenticate_different_sums(release_signer):
    from acc.release_signatures import SignatureError, verify
    attached = release_signer.run('--armor', '--sign', data=b'original manifest')
    with pytest.raises(SignatureError):
        verify(b'different manifest', attached)


async def test_unsigned_exception_cannot_queue_if_audit_write_fails(api, tmp_path, monkeypatch):
    import dataclasses
    service = api.app.state.ctx.updates
    installed = tmp_path / 'installed'
    installed.mkdir()
    service.settings = dataclasses.replace(service.settings, update_source=installed)
    requests = service.settings.updates_dir / 'requests'
    requests.mkdir(parents=True, exist_ok=True)
    service.store({'latest': '99.0.0', 'signature': {'status': 'missing'}})

    def unavailable(*args, **kwargs):
        raise ValueError('audit unavailable')
    monkeypatch.setattr(service, 'audit', unavailable)
    with pytest.raises(ValueError, match='audit unavailable'):
        service.request('owner', allow_unsigned=True)
    assert not list(requests.glob('*.json'))
