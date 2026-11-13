"""Live owner loss exposes only explicit new-session recovery; stale EOF is inert."""
import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_in_thread, write_intent
from acc.server.models import RelayEvent, RelayOwnership, RelaySession, Session
from acc.server.session_relay.exclusive_public import presentation
from acc.session_relay import protocol as p

SID='abc123def456'


@pytest.mark.parametrize('already_sealed',[False,True])
@pytest.mark.parametrize('replacement',[False,True])
async def test_current_lost_owner_offers_fresh_recovery_without_touching_replacement(runtime_case,already_sealed,replacement):  # noqa: F811
    c=runtime_case
    await c.launch.launch(SID)
    @write_intent
    def prepare():
        with c.db() as db:
            row=db.get(RelaySession,SID)
            if replacement:row.binding_id='replacement-binding'
            if already_sealed:
                from acc.server.session_relay import store
                store.seal(db,row)
            owner=db.get(RelayOwnership,SID)
            db.get(Session,SID).observed_state = 'stopped' if already_sealed else 'running'
            before=(row.sealed,row.revision,row.authority_epoch,owner.phase,row.event_seq,db.get(Session,SID).observed_state)
            db.commit();return before
    before=prepare()
    await write_in_thread(c.launch.fence_lost)
    @write_intent
    def inspect():
        with c.db() as db:
            row=db.get(RelaySession,SID);owner=db.get(RelayOwnership,SID)
            result=(row.sealed,row.revision,row.authority_epoch,owner.phase,row.event_seq,db.get(Session,SID).observed_state)
            view=presentation(c.ctx,db,row,p.Actor(p.ActorKind.PERSON,'alex',p.Channel.CHAT))
            if replacement:assert result==before and view is None
            else:
                assert row.sealed and row.capabilities=={} and owner.phase=='recovery_required'
                assert db.get(Session,SID).observed_state == ('stopped' if already_sealed else 'recovery_needed')
                if already_sealed:assert row.event_seq == before[4] + 1
                events=[v.event for v in db.query(RelayEvent).filter_by(session_id=SID) if v.event['kind']=='switch.changed']
                assert len(events)==1 and events[0]['payload']['state']=='recovery_required'
                assert view['recoveryAvailable'] and view['recoveryMode']=='new_session' and not view['available']
            return result
    after=inspect()
    await write_in_thread(c.launch.fence_lost)
    assert inspect()==after
    assert len(c.owners)==1 and not c.owners[0].calls[2:]
