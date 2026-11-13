"""Past evidence validation must not authorize a new execution generation."""
import json
from types import SimpleNamespace

import pytest
from test_session_relay_original_reconciliation import original  # noqa: F401

from acc.server import guardrails, security_settings
from acc.server.db import write_intent
from acc.server.models import Session
from acc.server.session_relay import runtime
from acc.server.session_relay.reconciliation import OriginalEvidence
from acc.session_relay import protocol as p


@pytest.fixture
def scoped(original):  # noqa: F811
    c=original
    c.ctx.broker=SimpleNamespace(runtime_managers=set(),authority=None)
    manager=runtime.RuntimeManager(c.ctx)
    @write_intent
    def frozen():
        with c.db() as db:
            session=db.get(Session,'relay')
            session.launch_spec.inputs={'provider':'claude','permission_mode':'default'}
            session.launch_spec.files={'guardrails.json':json.dumps(guardrails.policy(db,session.cwd))}
            db.commit()
    frozen();c.manager=manager;c.evidence=OriginalEvidence(manager,'relay',1)
    return c


def test_stricter_custom_guard_keeps_original_readonly_evidence_but_refuses_execution(scoped):
    c=scoped
    @write_intent
    def tighten():
        with c.db() as db:
            guardrails.save(db,{'custom':[{'pattern':'new-restriction','reason':'New stricter policy'}]},'alex');db.commit()
    tighten()
    with c.db() as db:
        with pytest.raises(security_settings.Conflict):c.manager.current_placement(db,db.get(Session,'relay'),provider='claude')
    assert c.evidence.check()['binding']['native_conversation']=='native'
    assert not c.manager.runs and not c.manager.launches


@pytest.mark.parametrize('guard',['budget','hold','source'])
def test_execution_only_guard_cannot_erase_original_evidence(scoped,monkeypatch,guard):
    c=scoped
    if guard=='budget':monkeypatch.setattr(runtime.budgets,'paused',lambda *_a,**_k:True)
    elif guard=='hold':monkeypatch.setattr('acc.backup_activation.held',lambda *_:True)
    else:monkeypatch.setattr('acc.server.sources.session_problem',lambda *_:'Execution source changed')
    with c.db() as db:
        with pytest.raises(p.RelayError):c.manager.current_placement(db,db.get(Session,'relay'),provider='claude')
    assert c.evidence.check()['runId']=='1'
    assert not c.manager.runs and not c.manager.launches


@pytest.mark.parametrize('field,value',[('run_as','foreign'),('workdir','/foreign'),('native_id','foreign'),('provider','codex')])
def test_readonly_policy_separation_still_refuses_retargeted_original(scoped,field,value):
    c=scoped
    @write_intent
    def move():
        with c.db() as db:setattr(db.get(Session,'relay'),field,value);db.commit()
    move()
    with pytest.raises(p.RelayError):c.evidence.check()


def test_team_execution_refusal_keeps_only_exact_original_team_evidence(scoped):
    from acc.server.models import RelayRun
    c = scoped
    @write_intent
    def team_identity():
        with c.db() as db:
            db.get(Session, 'relay').team_id = 'original-team'
            run = db.get(RelayRun, 1)
            run.identity = {**run.identity, 'placement': [*run.identity['placement'],
                {'team': {'id': 'original-team', 'limits': {'recorded': True}, 'expires': None}}]}
            db.commit()
    team_identity()
    with c.db() as db:
        with pytest.raises(p.RelayError):
            c.manager.current_placement(db, db.get(Session, 'relay'), provider='claude')
    assert c.evidence.check()['runId'] == '1'
    assert not c.manager.runs and not c.manager.launches
    @write_intent
    def retarget():
        with c.db() as db:
            db.get(Session, 'relay').team_id = 'another-team'
            db.commit()
    retarget()
    with pytest.raises(p.RelayError):
        c.evidence.check()
