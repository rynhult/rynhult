import pytest
from fastapi import HTTPException
from relay_helpers import setup

from acc.server.api.context import Principal
from acc.server.models import RelaySession, User
from acc.server.session_relay import authority
from acc.session_relay import protocol as p


@pytest.fixture
def case(tmp_path):
    case = setup(tmp_path)
    yield case
    case.engine.dispose()


def test_two_people_no_group_or_api_inferred_permission(case):
    with case.db() as db:
        _, own = authority.current(case.ctx, db, case.resolve, 'relay')
        assert own.actor.principal == 'alex'
    # A group maintainer/member is still a member on the private-content surface.
    case.state.update(who=Principal('sam', 'member', 'fixture', 2),
                      actor=p.Actor(p.ActorKind.PERSON, 'sam', p.Channel.CHAT))
    with case.db() as db, pytest.raises(HTTPException) as error:
        authority.current(case.ctx, db, case.resolve, 'relay')
    assert error.value.status_code == 404
    case.state.update(who=Principal('@api', 'owner', 'fixture'),
                      actor=p.Actor(p.ActorKind.SERVICE, '@api', p.Channel.API))
    with case.db() as db:
        authority.current(case.ctx, db, case.resolve, 'relay')
        with pytest.raises(p.RelayError) as error:
            authority.current(case.ctx, db, case.resolve, 'relay', action='control')
        assert error.value.code is p.ErrorCode.POLICY_REFUSED


def test_deleted_person_device_revocation_epoch_and_run_binding(case):
    with case.db() as db:
        _, before = authority.current(case.ctx, db, case.resolve, 'relay')
        db.get(RelaySession, 'relay').authority_epoch += 1
        db.commit()
    with case.db() as db:
        _, after = authority.current(case.ctx, db, case.resolve, 'relay')
        with pytest.raises(p.RelayError):
            authority.recheck(before, after)
        with pytest.raises(p.RelayError):
            authority.current(case.ctx, db, case.resolve, 'relay', expected_run=999)
    case.state['revoked'] = True  # resolver models the existing auth/device verifier
    with case.db() as db, pytest.raises(HTTPException):
        authority.current(case.ctx, db, case.resolve, 'relay')
    case.state['revoked'] = False
    with case.db() as db:
        db.delete(db.get(User, 1))
        db.commit()
    with case.db() as db, pytest.raises(p.RelayError):
        authority.current(case.ctx, db, case.resolve, 'relay')


def test_decision_requires_attachment_and_exact_person_scope(case):
    decision = p.Decision('decision', 'relay', '1', p.DecisionKind.QUESTION, 'digest', 'Question', (), 9999999999999, 1)
    with case.db() as db:
        with pytest.raises(p.RelayError):
            authority.current(case.ctx, db, case.resolve, 'relay', action='decide', attached=False, decision=decision)
        authority.current(case.ctx, db, case.resolve, 'relay', action='decide', attached=True, decision=decision)
        from dataclasses import replace
        with pytest.raises(p.RelayError):
            authority.current(case.ctx, db, case.resolve, 'relay', action='decide', attached=True,
                              decision=replace(decision, kind=p.DecisionKind.PRIVILEGED))


def test_changed_security_revision_refuses_mutations_but_keeps_history(case):
    from acc.server.models import SecurityRevision
    with case.db() as db:
        db.get(SecurityRevision, 1).revision += 1
        db.commit()
    with case.db() as db:
        authority.current(case.ctx, db, case.resolve, 'relay')
        with pytest.raises(p.RelayError) as error:
            authority.current(case.ctx, db, case.resolve, 'relay', action='control')
        assert error.value.code is p.ErrorCode.STALE_AUTHORITY


def test_placement_or_launch_binding_change_invalidates_queued_authority(case):
    from acc.server.models import Session
    with case.db() as db:
        db.get(Session, 'relay').run_as = 'other-account'
        db.commit()
    with case.db() as db:
        authority.current(case.ctx, db, case.resolve, 'relay')  # retained history survives placement change
        with pytest.raises(p.RelayError) as error:
            authority.current(case.ctx, db, case.resolve, 'relay', action='control')
        assert error.value.code is p.ErrorCode.STALE_AUTHORITY
