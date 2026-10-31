"""Release channel selection; no network access."""
import json

import pytest

from acc import updates


def test_rc_highest_not_list_order_and_drafts_skipped():
    rows = [{'tag_name': 'v0.15.0-rc.2', 'prerelease': True},
            {'tag_name': 'v9.0.0', 'draft': True},
            {'tag_name': 'v0.15.0-rc.1', 'prerelease': True}]
    result = updates.check('example/app', '0.15.0-rc.1', get=lambda *a: json.dumps(rows).encode())
    assert result['latest'] == '0.15.0-rc.2' and result['available']


def test_stable_opt_in():
    get = lambda *a: json.dumps([{'tag_name': 'v1.0.0-rc.2', 'prerelease': True}]).encode()
    with pytest.raises(updates.ReleaseSourceError, match='no stable release'):
        updates.check('example/app', '0.14.0', get=get)
    assert updates.check('example/app', '0.14.0', get=get, include_prereleases=True)['available']


@pytest.mark.parametrize('missing', [False, True])
def test_list_404_checks_repository(missing):
    calls = []
    def get(url, *args):
        calls.append(url)
        if '/releases?' in url or missing:
            raise updates.ReleaseSourceError('repository_missing')
        return b'{}'
    with pytest.raises(updates.ReleaseSourceError) as error:
        updates.check('example/app', '1.0.0', get=get)
    assert error.value.kind == ('repository_missing' if missing else 'no_release')
    assert calls[-1] == 'https://api.github.com/repos/example/app'


@pytest.mark.parametrize('rows,kind', [([], 'no_release'), ([{'draft': True, 'tag_name': 'v2.0.0'}], 'no_release')])
def test_no_published_release(rows, kind):
    with pytest.raises(updates.ReleaseSourceError) as error:
        updates.check('example/app', '1.0.0', get=lambda *a: json.dumps(rows).encode())
    assert error.value.kind == kind


def test_rate_limit_does_not_probe_repository():
    calls = []
    def get(url, *args):
        calls.append(url)
        raise updates.ReleaseSourceError('rate_limited')
    with pytest.raises(updates.ReleaseSourceError) as error:
        updates.check('example/app', '1.0.0', get=get)
    assert error.value.kind == 'rate_limited' and len(calls) == 1


async def test_preference_defaults_persistence_and_stale_observation(api, monkeypatch):
    from acc.server import updating
    service = api.app.state.ctx.updates
    monkeypatch.setattr(updating, '__version__', '1.0.0')
    assert service.defaults()['include_prereleases'] is False
    service.store({'latest': '1.1.0-rc.1'})
    assert not service.state()['available']
    response = await api.put('/api/updates/config', json={'repository': 'example/app', 'include_prereleases': True})
    assert response.status_code == 200 and response.json()['include_prereleases'] is True
    assert service.observation() == {}
    seen = []
    def check(*args, **kwargs):
        seen.append(args[4])
        return {'latest': '1.1.0-rc.1'}
    monkeypatch.setattr(updating, 'check', check)
    assert (await api.post('/api/updates/check')).json()['available']
    assert seen == [True]
    assert (await api.put('/api/updates/config', json={'repository': 'example/app', 'include_prereleases': False})).status_code == 200
    assert not service.state()['available']
    monkeypatch.setattr(updating, '__version__', '1.1.0-rc.1')
    assert service.defaults()['include_prereleases'] is True
    service.store({'latest': '1.1.0-rc.2'})
    assert not service.state()['available']  # Explicit Stable overrides the RC-install default.
    assert (await api.put('/api/updates/config', json={'repository': 'example/app', 'include_prereleases': 'yes'})).status_code == 422


def test_check_can_report_rc_but_default_apply_helpers_refuse(tmp_path):
    rows = [{'tag_name': 'v0.15.0-rc.2', 'prerelease': True}]
    report = updates.check('example/app', '0.15.0-rc.1', get=lambda *a: json.dumps(rows).encode())
    assert report['available'] and report['latest'] == '0.15.0-rc.2'
    assert not updates.newer(report['latest'], '0.15.0-rc.1')
    assert not updates.available(report, '0.15.0-rc.1', None)
    assert not updates.available({'latest': report['latest'], 'latest_build': 'b' * 64}, report['latest'], 'a' * 64)
    with pytest.raises(ValueError, match='stable'):
        updates.validate_request({'action': 'update', 'version': report['latest']})
    def never_fetch(*args, **kwargs):
        pytest.fail('candidate apply must refuse before network or staging')
    with pytest.raises(ValueError, match='stable'):
        updates.download_release('example/app', report, tmp_path / 'stage', get=never_fetch)
    assert not (tmp_path / 'stage').exists()


async def test_candidate_is_not_queued_or_audited_as_an_apply(api, monkeypatch):
    service = api.app.state.ctx.updates
    monkeypatch.setattr(service, 'state', lambda: {'blocked': None, 'available': True, 'error': None, 'latest': '1.0.0-rc.2'})
    monkeypatch.setattr(service, 'config', lambda: {'repository': 'example/app', 'release_channel': 'stable'})
    monkeypatch.setattr(service, 'audit', lambda *a: pytest.fail('candidate queued'))
    response = await api.post('/api/updates/apply')
    assert response.status_code == 409 and 'stable' in response.json()['detail']
    assert not list((service.settings.updates_dir / 'requests').glob('*.json'))
