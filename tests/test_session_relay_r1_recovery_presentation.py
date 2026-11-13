"""R1 presentation changes never create a provider or mutate ownership."""
import pytest
from relay_helpers import setup

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession, Session, utcnow
from acc.server.session_relay.exclusive_public import presentation
from acc.session_relay import protocol as p

SID='relay'


@pytest.fixture
def presentation_case(tmp_path):
    c=setup(tmp_path)
    @write_intent
    def owner():
        with c.db() as db:
            db.add(RelayOwnership(session_id=SID,mode='exclusive_idle',view='chat',phase='owned',
                source_run_id=1,authority={}))
            db.commit()
    owner()
    try:yield c
    finally:c.engine.dispose()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('mode', ['exclusive_idle', 'exclusive_writer'])
@pytest.mark.parametrize('state', [None, 'queued', 'failed', 'uncertain', 'accepted'])
async def test_terminal_waits_for_real_accepted_chat_and_snapshot_does_not_switch(presentation_case,state,mode,provider):
    c=presentation_case
    @write_intent
    def check():
        with c.db() as db:
            row=db.get(RelaySession,SID);owner=db.get(RelayOwnership,SID)
            db.get(Session,SID).provider=provider
            owner.mode=mode
            owner.view='chat';owner.phase='owned'
            if state:
                db.add(RelayOperation(id='first-chat',session_id=SID,run_id=row.current_run_id,
                    actor_key='test-person',registration_key='first-chat',
                    receipt={'method':'turn.submit','state':state},authority={},deadline=utcnow(),configuration_revision=1))
                db.flush()
            before=(row.current_run_id,row.revision,owner.view,owner.phase)
            value=presentation(c.ctx,db,row,p.Actor(p.ActorKind.PERSON,'josh','web'))
            if mode=='exclusive_idle':
                assert bool(value.get('terminalWaitingReason')) is (provider=='claude' and state!='accepted')
            else:
                assert value is None
            assert before==(row.current_run_id,row.revision,owner.view,owner.phase)
            owner.phase='recovery_required';row.sealed=True
            recovered=presentation(c.ctx,db,row,p.Actor(p.ActorKind.PERSON,'josh','web'))
            assert recovered['recoveryAvailable'] and recovered['recoveryMode']=='new_session'
            assert not recovered['available']
            assert recovered['terminalWaitingForChat'] is False
            assert 'terminalWaitingReason' not in recovered
            assert before[:2] == (row.current_run_id, row.revision)
            assert owner.view == 'chat' and owner.phase == 'recovery_required'
            assert row.sealed

    check()
