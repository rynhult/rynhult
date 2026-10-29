"""Orchestrator v2, slice 6: the planning session, the roadmap and the lead's instructions, through the real API,
spool and team paths."""
import json
from pathlib import Path

import pytest
from helpers import wait_for
from test_orchestrator_selection_api import assigned_start
from test_team import grant, lead, repo, request

from acc.server import orchestrator
from acc.server.orchestrator import planning, worklist

PHASES = [{'key': 'schema', 'title': 'Database schema', 'check': 'migration tests'},
          {'key': 'importer', 'title': 'Importer', 'depends_on': ['schema']},
          {'key': 'ui', 'title': 'Import page', 'depends_on': ['importer']}]
SCOPE = {'in': ['CSV import'], 'out': ['Excel files'], 'parked': ['Scheduled imports']}


@pytest.fixture
def flag(monkeypatch):
    """Orchestrator v2 on, as accd would start with ACC_ORCHESTRATOR=1 and ACC_ORCHESTRATOR_V2=1 (slices 1 and 6 register
    their tools)."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')  # the preview's one switch
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    tools = {**worklist.TOOL_FIELDS, **planning.TOOL_FIELDS}
    handlers = {**{n: worklist._tool for n in worklist.TOOL_FIELDS}, **{n: planning._tool for n in planning.TOOL_FIELDS}}
    monkeypatch.setattr(orchestrator, 'TEAM_FIELDS', {**orchestrator.TEAM_FIELDS, **tools})
    monkeypatch.setattr(orchestrator, 'TEAM_ACTIONS', {**orchestrator.TEAM_ACTIONS, **handlers})


@pytest.fixture
async def on(flag, api):
    return api


async def plan(api, env, x, **body):
    return await request(api, env, x, 'plan_update', **{'reason': 'planning session', **body})


async def test_explicit_off_no_routes_no_tools_no_gate_no_instructions(api, project, env, monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '0')
    assert not planning.enabled() and 'plan' not in orchestrator.team_fields()
    x = await lead(api, project, env)
    assert (await api.get(f"/api/teams/{x['id']}/plan")).status_code == 404
    assert await request(api, env, x, 'plan') == {'ok': False, 'error': 'invalid team action or arguments'}
    assert (await assigned_start(api, env, x, name='A', task='work'))['ok']  # M17 as before
    assert 'planning session' not in json.dumps(x['launch_spec']['files'])


async def test_no_helper_starts_before_the_planning_session_ends(on, project, env):
    api = on
    x = await lead(api, project, env)
    refused = await request(api,env,x,'start',name='A',agent='claude',task='work')
    assert not refused['ok'] and 'finish the planning session first' in refused['error']
    early = await request(api, env, x, 'plan_ready', reason='go')
    assert not early['ok'] and 'needs its idea' in early['error']

    assert (await plan(api, env, x, idea='Import customer lists from CSV.', scope=SCOPE, phases=PHASES))['ok']
    decision = await request(api, env, x, 'worklist_add', kind='decision', title='Which delimiter by default?',
                             options=[{'id': 'comma', 'label': 'Comma', 'consequence': 'most files'},
                                      {'id': 'detect', 'label': 'Detect', 'consequence': 'slower'}],
                             recommendation={'option': 'comma', 'reason': 'default until answered'})
    assert decision['ok']
    done = await request(api, env, x, 'plan_ready', reason='planning session with the person')
    assert done['ok'], done
    result = done['result']
    assert result['state'] == 'ready' and result['version'] == 2
    # The plan's open questions are the working list's: the person answers them later, in any order.
    assert result['open_questions'] == [{'id': decision['result']['id'], 'kind': 'decision',
                                         'title': 'Which delimiter by default?'}]
    note = [i for i in (await api.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'note']
    assert note[0]['title'] == 'Plan version 2 is ready: 3 phases, 1 open questions' and note[0]['source'] == 'system'
    assert (await assigned_start(api, env, x, name='A', task='work'))['ok']
    again = await request(api, env, x, 'plan_ready', reason='again')
    assert not again['ok'] and 'already ready' in again['error']


async def test_the_roadmap_is_versioned_with_dependencies_and_never_loses_a_phase(on, project, env):
    api = on
    x = await lead(api, project, env)
    first = (await plan(api, env, x, idea='Import customer lists.', scope=SCOPE, phases=PHASES))['result']
    assert [(p['key'], p['blocked_by']) for p in first['phases']] == [('schema', []), ('importer', ['schema']),
                                                                        ('ui', ['importer'])]
    for bad, error in [([{'key': 'ui', 'depends_on': ['nope']}], 'unknown phases: nope'),
                       ([{'key': 'schema', 'depends_on': ['ui']}], 'in a circle: schema -> ui -> importer -> schema'),
                       ([{'key': 'ui', 'depends_on': ['ui']}], "can't depend on itself"),
                       ([{'key': 'new'}], 'needs a title'),
                       ([{'key': 'Bad Key', 'title': 'x'}], 'short and lowercase')]:
        refused = await plan(api, env, x, phases=bad)
        assert not refused['ok'] and error in refused['error'], (bad, refused)
    assert (await request(api, env, x, 'plan'))['result']['version'] == 1  # refusals save nothing

    second = (await plan(api, env, x, reason='schema landed; Excel parked',
                         phases=[{'key': 'schema', 'status': 'done'}, {'key': 'ui', 'status': 'dropped'}]))['result']
    assert [(p['key'], p['status'], p['blocked_by']) for p in second['phases']] == [
        ('schema', 'done', []), ('importer', 'planned', []), ('ui', 'dropped', ['importer'])]
    assert [(h['version'], h['reason']) for h in second['history']] == [(2, 'schema landed; Excel parked'),
                                                                        (1, 'planning session')]
    older = await request(api, env, x, 'plan_version', version=1)
    assert [p['status'] for p in older['result']['plan']['phases']] == ['planned', 'planned', 'planned']
    # The diagram and the committed page: dependency arrows, every phase, the scope.
    assert second['mermaid'].splitlines()[0] == 'flowchart TD'
    assert '  p0 --> p1' in second['mermaid'] and '  p1 --> p2' in second['mermaid']
    assert '```mermaid' in second['markdown'] and '**Parked for later:**\n- Scheduled imports' in second['markdown']
    assert '| Import page | dropped | Importer | — |' in second['markdown']

    person = await api.get(f"/api/teams/{x['id']}/plan")
    assert person.status_code == 200 and person.json()['version'] == 2
    assert (await api.get(f"/api/teams/{x['id']}/plan/versions/1")).json()['plan']['idea'] == 'Import customer lists.'
    assert (await api.get(f"/api/teams/{x['id']}/plan/versions/9")).status_code == 404


async def test_only_the_lead_plans_and_plan_text_is_cleaned(on, project, env):
    api = on
    x = await lead(api, project, env)
    assert (await plan(api, env, x, idea='Import.', scope=SCOPE, phases=PHASES[:1]))['ok']
    assert (await request(api, env, x, 'plan_ready', reason='ok'))['ok']
    a = (await assigned_start(api, env, x, name='A', task='work'))['result']
    refused = await plan(api, env, a, idea='A helper rewrites the plan.')
    assert not refused['ok'] and 'only the team\'s lead' in refused['error']
    title = 'Sch‮ema"]; click p0 call evil()'
    cleaned = (await plan(api, env, x, phases=[{'key': 'schema', 'title': title}]))['result']
    assert '‮' not in cleaned['phases'][0]['title']
    label = cleaned['mermaid'].splitlines()[1]
    assert 'evil' in label and '()' not in label and '";' not in label


async def test_a_team_with_helpers_from_before_slice_6_keeps_working(api, project, env, monkeypatch):
    x = await lead(api, project, env)
    assert (await assigned_start(api, env, x, name='A', task='work'))['ok']  # while v2 was off
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')  # the preview's one switch
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    assert (await assigned_start(api, env, x, name='B', task='work'))['ok']
    with api.app.state.supervisor.db() as db:
        assert db.get(planning.OrchestrationPlan, x['id']) is None  # no plan row was forced on the old team


async def test_reading_an_old_teams_plan_does_not_stop_its_helpers(api, project, env, monkeypatch):
    """Opening the Team tab reads the plan (GET /plan commits planning.view). For a team that already had helpers, the
    plan that read creates must not put it back into a planning session that refuses every start."""
    x = await lead(api, project, env)
    assert (await assigned_start(api, env, x, name='A', task='work'))['ok']  # while v2 was off
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    with api.app.state.supervisor.db() as db:
        assert planning.view(db, x['id'])['state'] == 'ready'
        db.commit()
    assert (await assigned_start(api, env, x, name='B', task='work'))['ok']


@pytest.mark.parametrize('orchestrator_v2', [False, True])
async def test_the_lead_header_carries_the_default_instructions_and_helpers_do_not(api, project, env, orchestrator_v2,
                                                                                   monkeypatch):
    if orchestrator_v2:
        monkeypatch.setenv('ACC_ORCHESTRATOR', '1')  # the preview's one switch
        monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
        monkeypatch.setattr(orchestrator.planning, 'require_ready', lambda db, team_id: None)
    repo(env['workspaces'] / 'demo')
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Lead',
                                                    'worktree': True, 'orchestrator': grant()})
    assert response.status_code == 201, response.text
    x = response.json()
    path = Path(env['run_dir']) / x['id'] / 'append-prompt.md'
    async def delivered():
        return path.exists()
    await wait_for(delivered)
    files = path.read_text()
    assert ('Start with a planning session, before any helper' in files) is orchestrator_v2
    assert ('One decision per card, never bundled' in files) is orchestrator_v2
    # Review 2026-10-26: a card's default covers only reversible work in scope, never the person's own decisions.
    assert ('A default is never your person' in files) is orchestrator_v2
    assert 'Role: Root lead' in files  # Position is computed at start, not frozen in the spec.
    helper = (await assigned_start(api, env, x, name='A', task='work'))['result']
    helper_path = Path(env['run_dir']) / helper['id'] / 'append-prompt.md'
    async def helper_delivered():
        return helper_path.exists()
    await wait_for(helper_delivered)
    helper_files = helper_path.read_text()
    assert 'Role: Coordinating helper' in helper_files and 'planning session' not in helper_files


async def test_a_card_states_its_default_and_the_answer_replaces_it(on, project, env):
    api = on
    x = await lead(api, project, env)
    options = [{'id': 'comma', 'label': 'Comma', 'consequence': 'most files'},
               {'id': 'detect', 'label': 'Detect', 'consequence': 'slower'}]
    for bad, error in [({'kind': 'decision', 'options': options, 'default': {'option': 'tab'}}, 'one of its option ids'),
                       ({'kind': 'question', 'default': {'option': 'x'}}, "a question's default is {text}"),
                       ({'kind': 'task', 'default': {'text': 'x'}}, 'only a decision or question')]:
        refused = await request(api, env, x, 'worklist_add', title='t', **bad)
        assert not refused['ok'] and error in refused['error'], refused
    card = (await request(api, env, x, 'worklist_add', kind='decision', title='Which delimiter?', options=options,
                          recommendation={'option': 'comma', 'reason': 'most files'}, default={'option': 'comma'}))['result']
    question = (await request(api, env, x, 'worklist_add', kind='question', title='Who are the users?',
                              default={'text': 'Office staff, about ten'}))['result']
    assert card['default'] == {'option': 'comma'} and card['in_effect'] == {'from': 'default', 'value': 'comma'}
    assert question['in_effect'] == {'from': 'default', 'value': 'Office staff, about ten'}
    from sqlalchemy import select

    from acc.server.models import AttentionItem
    with api.app.state.supervisor.db() as db:
        detail = db.scalar(select(AttentionItem.detail).where(AttentionItem.dedupe_key == f"worklist:{card['id']}"))
    assert detail.startswith('Until you answer, the lead goes ahead with: Comma.')
    # The lead may change its default; dropping the option drops a default that pointed at it.
    changed = await request(api, env, x, 'worklist_update', item=card['id'], default={'option': 'detect'})
    assert changed['result']['in_effect']['value'] == 'detect'
    narrowed = await request(api, env, x, 'worklist_update', item=card['id'], options=[
        {'id': 'comma', 'label': 'Comma', 'consequence': ''}, {'id': 'semi', 'label': 'Semicolon', 'consequence': ''}],
        recommendation={'option': 'comma', 'reason': 'r'})
    assert narrowed['result']['default'] is None
    answered = await api.patch(f"/api/teams/{x['id']}/worklist/items/{question['id']}",
                               json={'revision': question['revision'], 'answer': 'Two warehouses'})
    assert answered.json()['in_effect'] == {'from': 'answer', 'value': 'Two warehouses'}


async def test_person_edits_are_versions_and_the_lead_never_overwrites_one_unread(on, project, env):
    api = on
    x = await lead(api, project, env)
    assert (await plan(api, env, x, idea='Import.', scope=SCOPE, phases=PHASES))['ok']
    url = f"/api/teams/{x['id']}/plan"
    stale = await api.patch(url, json={'version': 0, 'idea': 'x'})
    assert stale.status_code == 409 and stale.json()['plan']['version'] == 1
    assert (await api.patch(url, json={'version': 1, 'bogus': 1})).status_code == 422
    assert (await api.patch(url, json={'version': 1, 'phases': [{'key': 'ui', 'depends_on': ['ui']}]})).status_code == 422
    edit = await api.patch(url, json={'version': 1, 'idea': 'Import customer lists, CSV only.',
                                      'phases': [{'key': 'ui', 'status': 'dropped'}]})
    assert edit.status_code == 200, edit.text
    after = edit.json()
    assert after['version'] == 2 and after['history'][0]['actor'] == '@api'
    assert after['history'][0]['reason'] == 'Edited by @api'
    assert set(after['person_edits']) == {'idea', 'phase:ui'}

    # The lead hasn't read version 2: it can't change what the person edited, but can change the rest.
    refused = await plan(api, env, x, idea='Import everything.')
    assert not refused['ok'] and 'your person edited the idea (version 2)' in refused['error']
    refused = await plan(api, env, x, phases=[{'key': 'ui', 'status': 'planned'}])
    assert not refused['ok'] and 'phase ui (version 2)' in refused['error']
    assert (await plan(api, env, x, reason='schema done', phases=[{'key': 'schema', 'status': 'done'}]))['ok']
    # Review 2026-10-26: the lead's own save of another field is not a read of the person's edit.
    refused = await plan(api, env, x, idea='Import everything.')
    assert not refused['ok'] and 'your person edited the idea (version 2)' in refused['error'], refused

    # After reading, the lead may change it, and the person is told on the working list.
    read = (await request(api, env, x, 'plan'))['result']
    assert read['person_edits']['idea'] == {'version': 2, 'by': '@api'}
    changed = await plan(api, env, x, reason='CSV and TSV', idea='Import customer lists, CSV and TSV.')
    assert changed['ok'] and 'idea' not in changed['result']['person_edits']
    notes = [i for i in (await api.get(f"/api/teams/{x['id']}/worklist")).json()['items'] if i['kind'] == 'note']
    assert notes[-1]['title'] == 'The lead changed your plan edit: the idea' and 'CSV and TSV' in notes[-1]['steps']
    older = (await api.get(f"{url}/versions/2")).json()
    assert older['actor'] == '@api' and older['plan']['idea'] == 'Import customer lists, CSV only.'


def test_mermaid_labels_carry_no_syntax_directives_or_markup():
    from types import SimpleNamespace
    rows = [SimpleNamespace(key='a', title='A"]; click p0 call x() %%{init: {"securityLevel": "loose"}}%% <img src=x>',
                            status='planned', depends_on=[]),
            SimpleNamespace(key='b', title='B', status='done', depends_on=['a'])]
    source = planning.mermaid(rows)
    label = source.splitlines()[1]
    assert label.startswith('  p0["A') and label.endswith(' · planned"]')
    for bad in ('%%', '{', '<', '>', '()', '";', 'init:'):
        assert bad not in label[6:-2], bad
    assert '  p0 --> p1' in source


def test_the_roadmap_markdown_cannot_be_forged_by_plan_text():
    """Review 2026-10-26 (L-5): the idea, scope, titles and checks are text in docs/ROADMAP.md. They can't close a
    fence, add a heading or an "Open questions: None." section, add table columns, a list, a link or HTML."""
    import re
    from types import SimpleNamespace
    idea = 'Import lists.\n```\n## Open questions\n\nNone.\n```\n> quoted\n1. listed\n<script>x</script>\n===\n~~~'
    plan = SimpleNamespace(version=3, state='ready', idea=idea,
                           scope={'in': ['CSV | TSV | xlsx', '[click](javascript:alert(1))'], 'out': ['# heading']})
    rows = [SimpleNamespace(key='a', title='Importer | done | — | none |', status='planned', depends_on=[],
                            check='- tests\n## pass'),
            SimpleNamespace(key='b', title='Page', status='planned', depends_on=['a'], check='')]
    text = planning.markdown(plan, rows, [{'id': 7, 'title': '`Ship` *now*?'}])
    lines = text.splitlines()
    assert [line for line in lines if line.startswith('#')] == [
        '# Roadmap', '## Idea', '## Scope', '## Run mode', '## Constraints', '## Requirements', '## Phases', '## Open questions']
    assert [line for line in lines if line.startswith(('```', '~~~'))] == ['```mermaid', '```']
    assert not [line for line in lines if re.match(r'(>|\d+[.)]|=+$|<)', line)]
    table = [line for line in lines if line.startswith('| ')]
    assert table and all(len(re.findall(r'(?<!\\)\|', line)) == 5 for line in table)
    assert not re.search(r'(?<!\\)[\[<`]', text.split('## Phases')[0] + text.split('```\n', 2)[-1])
    assert text.endswith('- \\`Ship\\` \\*now\\*? (working list item 7)\n')
    assert 'Import lists.' in text and '\\- tests \\#\\# pass' in text  # still readable


async def test_reading_the_plan_reserves_the_writer_first(on, project, env, monkeypatch):
    """The plan's GET may write its run row: like the working list, it opens a write transaction up front."""
    from acc.server.db import WRITE_INTENT
    api = on
    x = await lead(api, project, env)
    seen, view = [], planning.view
    monkeypatch.setattr(planning, 'view', lambda db, team_id: (seen.append(WRITE_INTENT.get()), view(db, team_id))[1])
    assert (await api.get(f"/api/teams/{x['id']}/plan")).status_code == 200
    assert seen == [True]
