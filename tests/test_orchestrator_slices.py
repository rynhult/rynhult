"""Orchestrator v2 foundation seams, including slice 3's registered selection tools."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from acc.server import orchestrator, teams
from acc.server.config import Settings
from acc.server.models import TeamRequest
from acc.server.supervisor import Supervisor

REPO = Path(__file__).resolve().parents[1]
M17_TOOLS = ['start', 'list', 'status', 'read', 'message', 'stop', 'finish', 'merge']
WORKLIST_TOOLS = ['worklist', 'worklist_add', 'worklist_update', 'worklist_ingest', 'worklist_done', 'now']  # slice 1
SELECTION_TOOLS = ['assign', 'select', 'selection_status', 'give']  # slice 3
DELIVERY_TOOLS = ['report']  # slice 4
GATE_TOOLS = ['reserve', 'release', 'gate', 'gate_status', 'restack']  # slice 5
PLAN_TOOLS = ['plan', 'plan_update', 'plan_ready', 'plan_version']  # slice 6
BRIEF_TOOLS = ['brief_documents', 'brief_propose_documents', 'brief', 'brief_interpret', 'brief_draft', 'brief_question', 'brief_share', 'brief_reference']  # lane E


def test_registry_matches_the_slices_without_cross_slice_edits(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    assert orchestrator.team_fields() == orchestrator._merged('TEAM_FIELDS')
    assert orchestrator.team_actions() == orchestrator._merged('TEAM_ACTIONS')
    assert 'select' in orchestrator.team_fields() and set(GATE_TOOLS) <= set(orchestrator.team_actions())
    assert [module.__name__.rsplit('.', 1)[1] for module in orchestrator.SLICES] == [
        'worklist', 'live', 'selection', 'delivery', 'gates', 'reservations', 'planning', 'project_briefs', 'assignments']


def test_two_slices_cannot_claim_the_same_team_action(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setattr(orchestrator.worklist, 'enabled', lambda: True)
    monkeypatch.setattr(orchestrator.worklist, 'TEAM_FIELDS', {'note': set()}, raising=False)
    monkeypatch.setattr(orchestrator.selection, 'TEAM_FIELDS', {'note': set()}, raising=False)
    with pytest.raises(RuntimeError, match='same team action'):
        orchestrator._merged('TEAM_FIELDS')


def test_every_state_transition_reaches_the_slices_listeners(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    seen = []
    monkeypatch.setattr(orchestrator, 'SLICES', (SimpleNamespace(ON_STATE=[lambda db, session, previous, state: seen.append((previous, state))]),))
    added = []
    session = SimpleNamespace(id='a' * 32, observed_state='active', idle_since=None, project_id=None,
                              management='external', status_source=None, status_at=None)
    supervisor = SimpleNamespace(_attention_on_transition=lambda *args: None)
    Supervisor._set_state(supervisor, SimpleNamespace(add=added.append), session, 'idle', 'test')
    Supervisor._set_state(supervisor, SimpleNamespace(add=added.append), session, 'idle', 'test')  # no transition
    assert seen == [('active', 'idle')]
    assert [event.kind for event in added] == ['state']  # the existing event is unchanged


def queued(monkeypatch, action, **arguments):
    rows = []
    session = SimpleNamespace(id='a' * 32, team_id='a' * 32)
    db = SimpleNamespace(get=lambda *args: None, add=rows.append)
    payload = {'id': uuid.uuid4().hex, 'action': action, **arguments}
    teams.TeamMixin.queue_team_request(SimpleNamespace(settings=Settings()), db, session, {'acc_session': session.id}, {'request': payload})
    [row] = rows
    assert isinstance(row, TeamRequest)
    return row.result


def test_a_slice_registers_its_tools_without_editing_the_team_core(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    assert queued(monkeypatch, 'note', text='x') == {'ok': False, 'error': 'invalid team action or arguments'}
    monkeypatch.setattr(orchestrator, 'SLICES', (SimpleNamespace(TEAM_FIELDS={'note': {'text'}}, TEAM_ACTIONS={}),))
    assert queued(monkeypatch, 'note', text='x') is None  # accepted for processing
    assert queued(monkeypatch, 'note', other='x') == {'ok': False, 'error': 'invalid team action or arguments'}

    async def handler(supervisor, caller_id, payload):
        return {'handled': payload['action'], 'caller': caller_id}
    monkeypatch.setattr(orchestrator.SLICES[0], 'TEAM_ACTIONS', {'note': handler})
    # Dispatch-only unit seam: authority and planning behavior have their own real-DB tests.
    from acc.server.orchestrator import planning, verification
    monkeypatch.setattr(verification, 'require_action', lambda *args: None)
    monkeypatch.setattr(planning, 'require_action', lambda *args: None)
    monkeypatch.setattr(orchestrator, 'admit_write', lambda *args: None)
    db = SimpleNamespace(get=lambda *args: SimpleNamespace(team_id='b' * 32), commit=lambda: None)
    supervisor = SimpleNamespace(db=lambda: nullcontext(db), _get=lambda db, identifier: db.get(None, identifier))
    result = asyncio.run(teams.TeamMixin.team_action(supervisor, 'b' * 32, {'action': 'note', 'text': 'x'}))
    assert result == {'handled': 'note', 'caller': 'b' * 32}


def listed(*args, **env):
    reply = subprocess.run([sys.executable, '-I', str(REPO / 'bin/acc-mcp-team'), *args],
                           input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n',
                           capture_output=True, text=True, timeout=30,
                           env={**{k: v for k, v in os.environ.items() if '_ORCHESTRATOR' not in k},
                                'ACC_RUN_DIR': '/nonexistent', 'ACC_SESSION_ID': 'x', **env})
    return [tool['name'] for tool in json.loads(reply.stdout)['result']['tools']]


def test_the_team_mcp_lists_the_m17_tools_then_each_slices():
    # Without the preview, M17's tools only, whatever else is set. With it, slices 3-5 (accd gates each further), and
    # slices 1 and 6 (working list, plan) only with their own switch too.
    preview = SELECTION_TOOLS + DELIVERY_TOOLS + GATE_TOOLS
    assert listed() == listed(ACC_ORCHESTRATOR_V2='1') == listed('--orchestrator-v2') == M17_TOOLS
    assert listed('--orchestrator') == M17_TOOLS + preview
    # The agent's own environment never lists v2 tools: only accd's launch arguments do (review 2026-10-26, L-6).
    assert listed(ACC_ORCHESTRATOR='1') == listed(RYNHULT_ORCHESTRATOR='1', ACC_ORCHESTRATOR_V2='1') == M17_TOOLS
    assert listed('--orchestrator', ACC_ORCHESTRATOR_V2='1') == M17_TOOLS + preview
    assert listed('--orchestrator', '--orchestrator-v2') == M17_TOOLS + WORKLIST_TOOLS + preview + PLAN_TOOLS + BRIEF_TOOLS  # team_files
    text = (REPO / 'bin/acc-mcp-team').read_text()
    for section in ('M17_TOOLS', 'WORKLIST_TOOLS', 'SELECTION_TOOLS', 'DELIVERY_TOOLS', 'GATE_TOOLS', 'PLAN_TOOLS', 'BRIEF_TOOLS'):
        assert f'\n{section} = {{' in text, section


def test_each_slice_router_adds_only_its_own_routes(monkeypatch):
    # Slice 2's router installs its event listeners on a real context (covered by test_team_live and test_ownership).
    from acc.server.api import gates, plan, selection, team_live, worklist
    context = SimpleNamespace(principal=lambda: None, owner=lambda: None, db=lambda: None)
    monkeypatch.setattr(team_live.live, 'install', lambda ctx: None)
    # Routes register once for hot owner enablement; FeatureBoundary supplies the Off refusal.
    assert all(m.router(context).routes for m in (worklist, selection, gates, team_live, plan))
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    assert not worklist.worklist.enabled()  # the test fixture explicitly pins the legacy slice Off
    assert not plan.planning.enabled()
    routes = selection.router(SimpleNamespace(principal=lambda: None)).routes
    assert len(routes) == 5  # selections, attempt fail (C-L3), override, preferences get/put
    assert {route.path for route in gates.router(SimpleNamespace(principal=lambda: None, owner=lambda: None)).routes} == {
        '/api/gates', '/api/gates/{job_id}/cancel', '/api/gates/budget', '/api/gates/projects/{project_id}/commands',
        '/api/gates/{job_id}/accept-retry', '/api/gates/wrapup', '/api/gates/projects/{project_id}/flakes'}
