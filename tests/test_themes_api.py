import pytest
from test_themes import archive, manifest

from acc.server.api.context import Principal
from acc.server.models import AuditLog, Setting


@pytest.fixture
def person(api):
    from acc.server import auth
    from acc.server.models import User

    class People(list):
        def __setitem__(self, index, who):
            with api.app.state.db() as db:
                user = db.query(User).filter_by(username=who.username).one_or_none()
                if user is None:
                    user = User(username=who.username, password_hash='unused', totp_secret='unused')
                    db.add(user)
                user.role, user.account = who.role, who.account
                db.flush()
                token, csrf = auth.create_session(db, user, 'theme fixture')
                db.commit()
            api.headers.pop('Authorization', None)
            api.headers['x-acc-csrf'] = csrf
            api.cookies.set('acc_session', token)
            super().__setitem__(index, who)

    identity = People([None])
    identity[0] = Principal('alice', 'owner', 'alice-agent', 1)
    return identity


async def install(api):
    review = await api.post('/api/catalog/themes/review', content=archive(), headers={'Content-Type': 'application/zip'})
    assert review.status_code == 200, review.text
    body = {'token': review.json()['token']}
    result = await api.post('/api/catalog/themes/install', json=body)
    assert result.status_code == 200, result.text
    return review.json(), body


async def test_review_install_is_separate_idempotent_and_catalog_parity(api, person):
    legacy = (await api.get('/api/catalog')).json()
    review, body = await install(api)
    assert (await api.post('/api/catalog/themes/install', json=body)).status_code == 200
    state = (await api.get('/api/catalog/themes')).json()
    assert len(state['items']) == 1 and state['items'][0]['manifest']['id'] == 'example-theme'
    assert (await api.get('/api/catalog')).json() == legacy
    assert (await api.get('/api/catalog/themes/preference')).json()['choice'] is None
    with api.app.state.db() as s:
        assert len(list(s.query(AuditLog).filter(AuditLog.action == 'catalog.theme.install'))) == 1


async def test_apply_reset_per_person_cas_and_audit(api, person):
    await install(api)
    preview = await api.post('/api/catalog/themes/preview', json={'id': 'example-theme', 'digest': manifest()['digest']})
    assert preview.status_code == 200, preview.text
    change = {'token': preview.json()['token'], 'revision': 0, 'request_id': 'apply-once'}
    saved = await api.put('/api/catalog/themes/preference', json=change)
    assert saved.status_code == 200, saved.text
    assert (await api.put('/api/catalog/themes/preference', json=change)).json() == saved.json()
    assert (await api.put('/api/catalog/themes/preference', json={**change, 'request_id': 'stale'})).status_code == 409
    person[0] = Principal('bob', 'member', 'bob-agent', 2)
    assert (await api.get('/api/catalog/themes/preference')).json()['choice'] is None
    assert (await api.put('/api/catalog/themes/preference', json=change)).status_code in (403, 410)
    assert (await api.post('/api/catalog/themes/review', content=archive())).status_code == 403
    assert (await api.post('/api/catalog/themes/install', json={'token': 'unknown'})).status_code == 403
    person[0] = Principal('alice', 'owner', 'alice-agent', 1)
    reset = await api.put('/api/catalog/themes/preference', json={'revision': 1, 'request_id': 'reset-once'})
    assert reset.status_code == 200 and reset.json()['choice'] is None
    with api.app.state.db() as s:
        assert s.get(Setting, 'theme.person.alice').value['previous']['package_id'] == 'example-theme'
        assert s.query(AuditLog).filter(AuditLog.action == 'theme.apply').count() == 1
        assert s.query(AuditLog).filter(AuditLog.action == 'theme.reset').count() == 1


async def test_wrong_actor_stale_review_and_corruption_fall_back(api, person, env):
    review = await api.post('/api/catalog/themes/review', content=archive())
    person[0] = Principal('other', 'owner', 'other-agent', 2)
    assert (await api.post('/api/catalog/themes/install', json={'token': review.json()['token']})).status_code == 403
    person[0] = Principal('alice', 'owner', 'alice-agent', 1)
    # A legacy Catalog mutation invalidates the proposed typed review.
    # Direct store changes exercise the same shared catalog revision barrier.
    store = api.app.state.ctx
    from acc.server.catalog_store import CatalogStore
    CatalogStore(store.settings.catalog_dir, store.settings.catalog_local_dir).save_profile(
        'theme-test', {'name': 'Example', 'skills': [], 'agents': [], 'mcps': []}, create=True)
    assert (await api.post('/api/catalog/themes/install', json={'token': review.json()['token']})).status_code == 409
    await install(api)
    own = await api.post('/api/catalog/themes/preview', json={'id': 'example-theme', 'digest': manifest()['digest']})
    applied = await api.put('/api/catalog/themes/preference', json={'token': own.json()['token'], 'revision': 0, 'request_id': 'a'})
    assert applied.status_code == 200
    for path in (env['settings'].catalog_local_dir / 'packages' / 'themes').glob('*.zip'):
        path.write_bytes(b'corrupt')
    state = (await api.get('/api/catalog/themes/preference')).json()
    assert state['choice'] and state['effective'] is None and state['unavailable']
    assert (await api.get('/api/catalog')).status_code == 200


async def test_token_principal_not_a_person(api):
    assert (await api.post('/api/catalog/themes/review', content=archive())).status_code == 403
    assert (await api.get('/api/catalog/themes/preference')).status_code == 403


async def test_install_audit_failure_does_not_publish_a_partial_catalog_change(api, person, monkeypatch):
    review = await api.post('/api/catalog/themes/review', content=archive())
    factory = api.app.state.db.class_
    original = factory.commit
    def commit(session):
        if any(isinstance(row, AuditLog) and row.action == 'catalog.theme.install' for row in session.new):
            raise RuntimeError('injected audit failure')
        return original(session)
    with monkeypatch.context() as patch:
        patch.setattr(factory, 'commit', commit)
        with pytest.raises(RuntimeError, match='injected audit failure'):
            await api.post('/api/catalog/themes/install', json={'token': review.json()['token']})
    state = await api.get('/api/catalog/themes')
    assert state.json()['items'] == []
    # Retry via a newly packaged archive with the same canonical identity. The
    # abandoned immutable blob must keep its actual raw-archive checksum.
    import io
    import json
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zipped:
        zipped.writestr('theme.json', json.dumps(manifest(), indent=2))
    await api.post('/api/catalog/themes/cancel', json={'token': review.json()['token']})
    fresh = await api.post('/api/catalog/themes/review', content=out.getvalue())
    result = await api.post('/api/catalog/themes/install', json={'token': fresh.json()['token']})
    assert result.status_code == 200, result.text
    assert not (await api.get('/api/catalog/themes')).json()['items'][0].get('unavailable')


async def test_crash_between_audit_and_publication_is_hidden_and_retryable(api, person, monkeypatch):
    from acc.server.catalog_themes import ThemePackages
    review = await api.post('/api/catalog/themes/review', content=archive())
    def crash(*args):
        raise OSError('injected publication failure')
    with monkeypatch.context() as patch:
        patch.setattr(ThemePackages, 'publish', crash)
        result = await api.post('/api/catalog/themes/install', json={'token': review.json()['token']})
        assert result.status_code == 409
    assert (await api.get('/api/catalog/themes')).json()['items'] == []
    retried = await api.post('/api/catalog/themes/install', json={'token': review.json()['token']})
    assert retried.status_code == 200
    assert len((await api.get('/api/catalog/themes')).json()['items']) == 1
    with api.app.state.db() as s:
        assert s.query(AuditLog).filter(AuditLog.action == 'catalog.theme.install').count() == 1


@pytest.mark.parametrize('revocation', ['demote', 'logout', 'remove'])
async def test_install_rechecks_authority_after_staging(api, env, monkeypatch, revocation):
    from test_auth import cookie_client, do_setup

    from acc.server.catalog_themes import ThemePackages
    from acc.server.models import AuthSession, User

    await do_setup(api, env)
    client = cookie_client(api)
    client.headers['x-acc-csrf'] = client.cookies.get('acc_csrf')
    review = await client.post('/api/catalog/themes/review', content=archive())
    assert review.status_code == 200, review.text
    token = review.json()['token']
    original = ThemePackages.install

    def stage_then_revoke(packages, *args):
        result = original(packages, *args)
        # Deterministic barrier: durable revocation completes after staging,
        # before the endpoint can authorize audit/publication.
        with api.app.state.db() as db:
            user = db.query(User).filter_by(username='casey').one()
            if revocation == 'demote':
                user.role = 'member'
            else:
                db.query(AuthSession).filter_by(user_id=user.id).delete()
                if revocation == 'remove':
                    db.delete(user)
            db.commit()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ThemePackages, 'install', stage_then_revoke)
        refused = await client.post('/api/catalog/themes/install', json={'token': token})
    assert refused.status_code == (403 if revocation == 'demote' else 401), refused.text
    with api.app.state.db() as db:
        assert db.query(AuditLog).filter_by(action='catalog.theme.install').count() == 0
    from acc.server.catalog_store import CatalogStore
    packages = ThemePackages(CatalogStore(env['settings'].catalog_dir, env['settings'].catalog_local_dir))
    assert packages.list()['items'] == []
    assert packages.receipt('casey', token) is None
