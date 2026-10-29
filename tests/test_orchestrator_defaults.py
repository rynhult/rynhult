"""Default-on rollout: absent choices inherit defaults; explicit refusals survive."""
import pytest

from acc.env import names
from acc.server.admin_features import CURRENT, FEATURES, KEY, Features
from acc.server.config import Settings
from acc.server.models import Setting
from acc.server.orchestrator.switch import enabled, slice_enabled

FLAGS = ('ACC_ORCHESTRATOR', 'ACC_ORCHESTRATOR_V2', 'ACC_ORCHESTRATOR_V2_DELIVERY')


@pytest.fixture(autouse=True)
def default_environment(isolated_orchestrator_environment, monkeypatch):
    for flag in FLAGS:
        for name in names(flag):
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('context', [False, True])
def test_unset_switches_enable_every_slice_without_opt_in(context):
    token = CURRENT.set(Features(Settings()) if context else None)
    try:
        assert enabled()
        assert slice_enabled('ACC_ORCHESTRATOR_V2')
        assert slice_enabled('ACC_ORCHESTRATOR_V2_DELIVERY')
        from acc.server import orchestrator
        assert {'worklist', 'plan', 'select', 'report', 'reserve', 'gate'} <= set(orchestrator.team_actions())
        assert FEATURES['orchestrator'].default
        assert not FEATURES['orchestrator'].preview
    finally:
        CURRENT.reset(token)


@pytest.mark.parametrize(('overrides', 'choice', 'want'), [
    ({}, False, (False, False, False)),
    ({'ACC_ORCHESTRATOR': '0'}, True, (False, False, False)),
    ({'ACC_ORCHESTRATOR': '1'}, False, (True, True, True)),
    ({'ACC_ORCHESTRATOR_V2': '0'}, True, (True, False, True)),
    ({'ACC_ORCHESTRATOR_V2_DELIVERY': '0'}, True, (True, True, False)),
    ({'ACC_ORCHESTRATOR_V2': '1', 'ACC_ORCHESTRATOR_V2_DELIVERY': '1'}, False, (False, False, False)),
    ({'ACC_ORCHESTRATOR': '1', 'RYNHULT_ORCHESTRATOR': '0'}, True, (False, False, False)),
    ({'ACC_ORCHESTRATOR_V2': '1', 'RYNHULT_ORCHESTRATOR_V2': '0'}, True, (True, False, True)),
])
def test_only_explicit_overrides_change_inheritance(monkeypatch, overrides, choice, want):
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    token = CURRENT.set(Features(Settings(), {'orchestrator': choice}))
    try:
        assert (enabled(), slice_enabled(FLAGS[1]), slice_enabled(FLAGS[2])) == want
    finally:
        CURRENT.reset(token)


@pytest.mark.parametrize('saved', [None, {'choices': {'schedules': False}, 'revision': 3},
                                  {'choices': {'orchestrator': False}, 'revision': 4}])
async def test_existing_installation_inherits_only_when_owner_never_chose(api, saved):
    # A stored choice is the migration-free distinction. No backfill, schema change,
    # startup write or materialized default may turn an owner's explicit Off into On.
    with api.app.state.db() as db:
        if saved is not None:
            db.add(Setting(key=KEY, value=saved))
            db.commit()
        features = Features(Settings())
        features.load(db)
        expected = saved is None or 'orchestrator' not in saved['choices']
        assert features.enabled('orchestrator') is expected
        row = db.get(Setting, KEY)
        assert (row.value if row else None) == saved
        token = CURRENT.set(features)
        try:
            assert enabled() is expected
            assert slice_enabled(FLAGS[1]) is expected
            assert slice_enabled(FLAGS[2]) is expected
        finally:
            CURRENT.reset(token)


async def test_default_on_does_not_opt_into_running_branch_code(api):
    state = (await api.get('/api/admin-features')).json()
    row = next(row for row in state['features'] if row['id'] == 'orchestrator')
    assert row['enabled'] and not row['preview'] and not row['locked']
    budget = await api.get('/api/gates/budget')
    assert budget.status_code == 200, budget.text
    assert budget.json()['enabled'] is False
    refused = await api.put('/api/gates/budget', json={'enabled': True})
    assert refused.status_code == 409 and 'branch' in refused.text
    assert (await api.get('/api/gates/budget')).json()['enabled'] is False
