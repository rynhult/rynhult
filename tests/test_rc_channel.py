"""Owner-approved RC channel; fake sources and disposable state only."""
import json

import pytest

from acc import updates


def test_explicit_stable_channel_overrides_installed_rc():
    rows = [{'tag_name': 'v0.15.0-rc.3', 'prerelease': True}, {'tag_name': 'v0.14.0'}]
    result = updates.check('example/app', '0.15.0-rc.2', include_prereleases=False,
                           get=lambda *a: json.dumps(rows).encode())
    assert result['latest'] == '0.14.0' and not result['available']


def test_candidate_request_requires_channel_and_disallows_unsigned():
    data = {'action': 'update', 'version': '0.15.0-rc.3'}
    with pytest.raises(ValueError):
        updates.validate_request(data)
    assert updates.validate_request(data, include_prereleases=True) == data['version']
    with pytest.raises(ValueError, match='signature'):
        updates.validate_request({**data, 'allow_unsigned': True}, include_prereleases=True)


@pytest.mark.parametrize('override', [False, True])
def test_candidate_download_requires_signature_even_with_override(tmp_path, override):
    from acc.release_signatures import MissingSignature
    row = {'latest': '0.15.0-rc.3', 'assets': {'SHA256SUMS': 'https://api.github.com/repos/example/app/releases/assets/1'}}
    with pytest.raises(MissingSignature):
        updates.download_release('example/app', row, tmp_path / 'stage', allow_unsigned=override,
                                 include_prereleases=True, get=lambda *a, **kw: b'')
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('channel,latest,want', [
    ('stable', '0.15.0-rc.3', False), ('rc', '0.15.0-rc.3', True),
    ('rc', '0.15.0-rc.1', False), ('rc', '0.15.0', True), ('stable', '0.15.0', True),
])
def test_available_both_channels_no_downgrade(channel, latest, want):
    assert updates.available({'latest': latest}, '0.15.0-rc.2', None, channel == 'rc') is want


async def test_channel_owner_audit_and_cache_invalidation(api, monkeypatch):
    from sqlalchemy import select

    from acc.server import updating
    from acc.server.models import AuditLog
    service = api.app.state.ctx.updates
    monkeypatch.setattr(updating, '__version__', '0.15.0-rc.2')
    assert service.defaults()['release_channel'] == 'rc'
    service.store({'latest': '0.15.0-rc.3'})
    response = await api.put('/api/updates/config', json={'repository': 'example/app', 'release_channel': 'stable'})
    assert response.status_code == 200 and response.json()['release_channel'] == 'stable'
    assert service.observation() == {}
    with service.db() as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == 'update.configure').order_by(AuditLog.id.desc()))
        assert entry.detail['release_channel'] == 'stable'
    service.store({'latest': '0.15.0-rc.3'})
    assert not service.state()['available']
    assert (await api.put('/api/updates/config', json={'repository': 'example/app', 'release_channel': 'nightly'})).status_code == 422
    monkeypatch.setattr(updating, '__version__', '0.15.0')
    assert service.defaults()['release_channel'] == 'stable'


@pytest.mark.parametrize('latest,expected', [('0.15.0-rc.3', []), ('0.15.0', ['system'])])
async def test_automatic_never_installs_candidate(api, monkeypatch, latest, expected):
    import time
    service = api.app.state.ctx.updates
    await api.put('/api/updates/config', json={'repository': 'example/app', 'release_channel': 'rc', 'automatic': True, 'time': '00:00'})
    service.store({'latest': latest, 'checked_at': time.time()})
    called = []
    monkeypatch.setattr(service, 'request', lambda actor: called.append(actor))
    await service.tick()
    assert called == expected


@pytest.mark.parametrize('signature', ['verified', 'missing', 'unsigned_override'])
@pytest.mark.parametrize('unsigned', [False, True])
async def test_candidate_apply_mandatory_verified_signature(api, tmp_path, monkeypatch, signature, unsigned):
    import dataclasses
    service = api.app.state.ctx.updates
    source = tmp_path / 'installed'
    source.mkdir()
    service.settings = dataclasses.replace(service.settings, update_source=source)
    requests = service.settings.updates_dir / 'requests'
    requests.mkdir(parents=True, exist_ok=True)
    await api.put('/api/updates/config', json={'repository': service.authorized_repository(), 'release_channel': 'rc'})
    service.store({'latest': '99.0.0-rc.3', 'signature': {'status': signature}})
    response = await api.post('/api/updates/apply', json={'allow_unsigned': unsigned})
    if signature == 'verified' and not unsigned:
        assert response.status_code == 202
        queued = json.loads(next(requests.glob('*.json')).read_text())
        assert queued == {'action': 'update', 'version': '99.0.0-rc.3'}
    else:
        assert response.status_code == 409 and 'signature' in response.json()['detail'].lower()
        assert not list(requests.glob('*.json'))


@pytest.mark.parametrize('requested,wrong_build', [('0.15.0-rc.3', False), ('0.15.0-rc.4', False), ('0.15.0-rc.3', True)])
def test_node_candidate_is_only_the_enrolled_mains_exact_bundle(tmp_path, monkeypatch, requested, wrong_build):
    import hashlib

    from acc import bundle, update_helper
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="0.15.0-rc.2"\n')
    main = tmp_path / 'main'
    main.mkdir()
    (main / 'pyproject.toml').write_text('[project]\nversion="0.15.0-rc.3"\n')
    archive = bundle.build(main, tmp_path / 'bundles')
    config = tmp_path / 'node.env'
    config.write_text('ACC_NODE_PRIMARY=https://main.example.lan\nACC_NODE_ID=demo\nACC_NODE_TRUST=system\n')
    def fetch(url, **kwargs):
        assert url == 'https://main.example.lan/api/nodes/bundle'
        assert kwargs['context'].check_hostname
        return archive.read_bytes()
    monkeypatch.setattr(update_helper, 'fetch', fetch)
    monkeypatch.setattr(update_helper, 'check', lambda *a, **kw: pytest.fail('node must never select a release'))
    request = {'action': 'update', 'version': requested, 'build': 'f' * 64 if wrong_build else bundle.identity(main),
               'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    assert update_helper.validate_node_request(request) == requested
    job = update_helper.NodeTransaction(tmp_path / 'updates', source, tmp_path / 'data', requested,
                                       {**request, 'node_config': str(config), 'node_state': str(tmp_path / 'identity')})
    if requested == '0.15.0-rc.3' and not wrong_build:
        assert bundle.version_of(job.download()) == requested
    else:
        with pytest.raises(ValueError):
            job.download()


def test_github_prerelease_flag_with_stable_tag_stays_candidate():
    get = lambda *a: json.dumps([{'tag_name': 'v0.15.0', 'prerelease': True}]).encode()
    row = updates.check('example/app', '0.14.0', include_prereleases=True, get=get)
    assert row['prerelease'] is True
    assert not updates.available(row, '0.14.0', None)
    assert updates.available(row, '0.14.0', None, True)
