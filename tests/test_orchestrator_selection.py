"""Slice 3 selector: synthetic grants, readiness and percentage budgets only."""
import copy

import pytest

from acc.server.orchestrator import selection as sel


def choices():
    return [{'agent': agent, 'profile': 'backend', 'inputs': {'provider': agent, 'profile': 'backend',
             'permission_mode': 'acceptEdits', 'sandbox': 'workspace-write', 'approval': 'on-request',
             'skills': ['python'], 'options': {}}} for agent in ('claude', 'codex', 'copilot')]


def inventory():
    return {('local', agent): {'logged_in': True, 'models': [{'id': 'small', 'price_class': 'cheap'},
                                                          {'id': 'large', 'price_class': 'expensive'}]}
            for agent in ('claude', 'codex', 'copilot')}


def ranked(**changes):
    return sel.rank({'role': 'backend', 'task_type': 'implementation', 'effort': 'implement',
                     'skills_wanted': ['python'], 'languages': ['python'], 'risk': 'low'},
                    choices(), ['local'], inventory(), **changes)


def test_seed_is_an_ordered_allowlist_not_an_agent_recommendation():
    result = ranked()
    assert result['state'] == 'proposed'
    winner = result['candidates'][result['chosen']]
    assert winner['agent'] == 'codex' and winner['model'] == 'small'
    assert all(c['agent'] != 'copilot' or not c['eligible'] for c in result['candidates'])
    assert 'allow-list' in result['reason']
    assert result == ranked()


@pytest.mark.parametrize('task,expected', [('design','claude'), ('security_review','claude'),
                                        ('docs','copilot'), ('small_fix','copilot'), ('review','claude')])
def test_owner_habits_are_seeded(task, expected):
    need = {'role': 'backend', 'task_type': task, 'effort': 'implement', 'skills_wanted': [],
            'languages': [], 'risk': 'low'}
    assert sel.rank(need, choices(), ['local'], inventory())['candidates'][0]['agent'] == expected


def test_exact_profile_policy_can_deny_or_pin_models_but_never_expand_the_grant():
    policy = {'version': 1, 'rules': [{'profile': 'backend', 'task_type': 'implementation',
              'choices': [{'agent': 'claude', 'models': ['large']}]}]}
    result = ranked(preferences=policy)
    winner = result['candidates'][result['chosen']]
    assert (winner['agent'], winner['model']) == ('claude', 'large')
    original = choices(); original[0]['inputs']['model'] = 'small'
    assert sel.rank({'task_type':'implementation','effort':'implement'}, original, ['local'], inventory(),
                    preferences=policy)['state'] == 'refused'
    policy['rules'][0]['choices'] = []
    assert ranked(preferences=policy)['state'] == 'refused'


@pytest.mark.parametrize('change', ['offline', 'signed_out', 'paused', 'read_only', 'budget'])
def test_hard_filters_are_recorded_and_cannot_be_scored_away(change):
    allowed, ready, extra = choices(), inventory(), {}
    if change == 'offline': ready = {}
    if change == 'signed_out':
        for row in ready.values(): row['logged_in'] = False
    if change == 'paused': extra['paused'] = {'claude': True, 'codex': True}
    if change == 'read_only':
        for row in allowed:
            row['inputs'].update(permission_mode='plan', sandbox='read-only')
    if change == 'budget': extra['remaining'] = {'claude': 0.01, 'codex': 0.01}
    result = sel.rank({'task_type':'implementation', 'effort':'implement'}, allowed, ['local'], ready, **extra)
    assert result['state'] == 'refused' and result['chosen'] is None
    assert result['candidates'] and all(c['rejections'] for c in result['candidates'])


def test_budget_fallback_is_cheapest_fitting_and_snapshot_has_explicit_units():
    result = ranked(remaining={'codex': 0.01, 'claude': 1.0})
    chosen = result['candidates'][result['chosen']]
    assert chosen['agent'] == 'claude' and chosen['model'] == 'small'
    assert chosen['est_cost']['unit'] == 'usage_percent'
    assert 'heuristic' in chosen['est_cost']['source']


def test_read_only_need_never_chooses_a_more_powerful_profile():
    assert sel.rank({'task_type':'design', 'effort':'plan'}, choices(), ['local'], inventory())['state'] == 'refused'


def test_policy_validation_rejects_ambiguous_or_injected_choices():
    seed = sel.seed_preferences()
    assert sel.validate_preferences(seed) == seed
    for value in ({'version':True,'rules':[]}, {'version':1,'rules':[{'profile':'backend','task_type':'implementation',
                  'choices':[{'agent':'codex','models':['--dangerous']}]}]}):
        with pytest.raises(ValueError): sel.validate_preferences(value)
    changed = copy.deepcopy(seed); changed['rules'].append(changed['rules'][0])
    with pytest.raises(ValueError): sel.validate_preferences(changed)


async def test_readiness_uses_only_granted_machines_and_account_and_discards_diagnostics(api):
    from types import SimpleNamespace

    calls=[]
    async def call(method,**params):
        calls.append((method,params))
        if method=='auth_health':
            return {'codex':{'logged_in':True,'diagnostic':'private diagnostic'},
                    'claude':{'logged_in':False}}
        return {'codex':[{'id':'known'}], 'claude':[{'id':'other'}]}
    broker=SimpleNamespace(call=call,hub=SimpleNamespace(links={
        'worker':SimpleNamespace(closed=False),'offline':SimpleNamespace(closed=True)}))
    result=await sel.readiness(SimpleNamespace(broker=broker, db=api.app.state.db),'dana-agent',['worker','offline'],{'codex'})
    assert result=={('worker','codex'):{'logged_in':True,'models':[{'id':'known', 'image_input': None,
        'image_qualification': {'scope':'native', 'status':'unknown', 'checked_at':None, 'evidence':None}}]}}
    assert calls==[(method,{'account':'dana-agent','host':'worker'})
                   for method in ('auth_health','model_catalog')]


def test_reserved_migration_upgrades_and_rolls_back_without_other_slice_tables(tmp_path):
    # Slice 3's 0070 follows slice 1's 0069 (slice 2 needs no table), on the real chain after 0068.
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from acc.server.db import alembic_config
    config = alembic_config('sqlite:///'+str(tmp_path/'db.sqlite'))
    command.upgrade(config, '0069')
    engine = create_engine('sqlite:///'+str(tmp_path/'db.sqlite'))
    assert 'selection' not in inspect(engine).get_table_names()
    command.upgrade(config, '0070')
    assert 'selection' in inspect(engine).get_table_names()
    command.downgrade(config, '0069')
    assert 'selection' not in inspect(engine).get_table_names()
    command.upgrade(config, 'head')
    assert {'selection', 'gate_job', 'reservation'} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_empty_profile_allowlist_cannot_fall_back_to_permissive_wildcard():
    policy = {'version': 1, 'rules': [
        {'profile': '*', 'task_type': 'implementation', 'choices': [
            {'agent': 'claude', 'models': ['*']}, {'agent': 'codex', 'models': ['*']}]},
        {'profile': 'backend', 'task_type': 'implementation', 'choices': []},
    ]}
    result = ranked(preferences=policy)
    assert result['state'] == 'refused' and result['chosen'] is None
    assert all(not candidate['eligible'] for candidate in result['candidates'])
    assert all('profile/task allow-list excludes this provider/model' in candidate['rejections']
               for candidate in result['candidates'])


@pytest.mark.parametrize('failed_method', ['auth_health', 'model_catalog'])
async def test_readiness_failure_never_admits_provider_or_returns_private_diagnostic(failed_method):
    from types import SimpleNamespace

    from acc.server.broker_client import BrokerError

    async def call(method, **params):
        if method == failed_method:
            raise BrokerError('private sign-in diagnostic canary')
        return {'claude': {'logged_in': True}}

    supervisor = SimpleNamespace(broker=SimpleNamespace(call=call))
    assert await sel.readiness(supervisor, 'dana-agent', ['local'], {'claude'}) == {}


async def test_readiness_refuses_excess_granted_machines_before_broker_io():
    from types import SimpleNamespace

    from acc.server.supervisor_common import LaunchRefused

    async def call(*args, **kwargs):
        pytest.fail('over-limit selection must not contact a broker')

    with pytest.raises(LaunchRefused, match='at most 16'):
        await sel.readiness(SimpleNamespace(broker=SimpleNamespace(call=call)), 'dana-agent',
                            [f'worker-{i}' for i in range(17)], {'claude'})
