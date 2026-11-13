"""Presentation isolation operates only on the disposable browser database."""
from types import SimpleNamespace

import pytest
from e2e_presentation_state import PresentationState
from sqlalchemy.orm import sessionmaker

from acc.server.admin_features import Features
from acc.server.config import Settings
from acc.server.db import make_engine, write_intent
from acc.server.models import Setting


@pytest.fixture
def state(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path}/state.db')
    Setting.__table__.create(engine)
    db = sessionmaker(engine)
    settings = Settings()
    settings = SimpleNamespace(features=Features(settings))
    return PresentationState(db, settings)

def put(state, key, value):
    with state.db() as db:
        db.merge(Setting(key=key, value=value))
        db.commit()

def test_reset_restores_all_presentation_groups_and_publishes_flags(state):
    for key in ['setup_level.alex', 'ui_header.alex', 'theme.person.alex']:
        put(state, key, {'enabled': False, 'choice': 'changed', 'level': 2})
    put(state, 'admin_features', {'choices': {'publishing': False}, 'revision': 12})
    with state.db() as db:
        state.settings.features.load(db)
    assert not state.settings.features.enabled('publishing')
    assert state.settings.features.choices['publishing'] is False
    assert set(state.read()) == {'admin_features.publishing', 'setup_level.alex', 'ui_header.alex', 'theme.person.alex'}
    state.reset()
    assert state.read() == {}
    assert state.settings.features.enabled('publishing')

def test_reset_does_not_change_security_or_account_settings(state):
    put(state, 'guardrails', {'rules': {'push_main': True}})
    put(state, 'phone_banner.alex', {'dismissed': True})
    state.reset()
    with state.db() as db:
        assert db.get(Setting, 'guardrails').value == {'rules': {'push_main': True}}
        assert db.get(Setting, 'phone_banner.alex').value == {'dismissed': True}

def test_default_valued_rows_and_revision_counters_are_not_leaks(state):
    from acc.server.api.settings_routes import default_header
    put(state, 'ui_header.alex', default_header())
    put(state, 'theme.person.alex', {'choice': None, 'revision': 9, 'previous': 'old'})
    put(state, 'admin_features', {'choices': {'publishing': True}, 'revision': 8})
    assert state.read() == {}

def test_owned_reset_leaves_other_changes_visible_to_detector(state):
    put(state, 'ui_header.alex', {'enabled': False})
    put(state, 'theme.person.blair', {'choice': {'package_id': 'example'}})
    state.reset(['ui_header'])
    assert state.read() == {'theme.person.blair': {'package_id': 'example'}}
    with pytest.raises(ValueError, match='Unknown presentation group'):
        state.reset(['guardrails'])

def test_layouts_and_file_references_are_reset_without_product_p2_models(state):
    from sqlalchemy import JSON, Column, Integer, MetaData, String, Table
    metadata = MetaData()
    layout = Table('personal_layout', metadata, Column('id', Integer, primary_key=True), Column('layout', JSON))
    refs = Table('layout_file_ref', metadata, Column('id', String, primary_key=True))
    with state.db() as db:
        metadata.create_all(db.get_bind())
        db.execute(layout.insert(), [{'id': 1, 'layout': {'columns': []}}, {'id': 2, 'layout': None}])
        db.execute(refs.insert(), {'id': 'fixture-file'})
        db.commit()
    assert state.read() == {'personal_layout': 1, 'layout_file_ref': 1}
    state.reset(['layouts'])
    assert state.read() == {}

async def test_fixture_capability_is_required_and_response_names_only(state):
    import httpx
    from e2e_presentation_state import install
    from fastapi import FastAPI
    app = FastAPI()
    app.state.db = state.db
    app.state.ctx = SimpleNamespace(settings=state.settings)
    token = install(app)
    put(state, 'ui_header.alex', {'enabled': False, 'private': 'must not appear'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://example.lan') as client:
        assert (await client.get('/e2e-test/presentation')).status_code == 403
        assert (await client.post('/e2e-test/presentation', json={})).status_code == 403
        assert 'ui_header.alex' in state.read()
        headers = {'x-e2e-isolation': token}
        result = await client.get('/e2e-test/presentation', headers=headers)
        assert result.json() == {'changed': ['ui_header.alex'], 'before': ['ui_header.alex'], 'previous_test': None}
        assert (await client.post('/e2e-test/presentation', headers=headers, json={'groups': ['security']})).status_code == 400
        assert (await client.post('/e2e-test/presentation', headers=headers, json={})).json() == {
            'changed': [], 'before': ['ui_header.alex'], 'previous_test': None}

def test_reset_uses_one_connection_under_real_request_write_intent(state):
    put(state, 'ui_header.alex', {'enabled': False})
    @write_intent
    def reset_in_request():
        state.reset()
    reset_in_request()
    assert state.read() == {}

async def test_hook_drift_is_returned_before_reset_with_previous_test(state):
    import httpx
    from e2e_presentation_state import install
    from fastapi import FastAPI
    app = FastAPI()
    app.state.db = state.db
    app.state.ctx = SimpleNamespace(settings=state.settings)
    token = install(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://example.lan',
                                 headers={'x-e2e-isolation': token}) as client:
        await client.post('/e2e-test/presentation', json={'test': 'previous.spec.ts > case'})
        put(state, 'ui_header.alex', {'enabled': False})
        result = await client.post('/e2e-test/presentation', json={'test': 'next.spec.ts > case'})
        assert result.json() == {'changed': [], 'before': ['ui_header.alex'], 'previous_test': 'previous.spec.ts > case'}
        assert state.read() == {}
