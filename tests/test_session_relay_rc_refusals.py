"""Feature enabled: excluded RC combinations refuse before native effects."""
from types import SimpleNamespace

import pytest
from test_session_relay_public_writer import SID, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import Session
from acc.server.session_relay import authority
from acc.server.supervisor_common import LaunchRefused
from acc.session_relay import protocol as p


def unsupported(error):
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    assert 'not supported in this release' in error.value.message
    assert error.value.to_wire()['data']['details']['reason'] == 'not_supported_in_release'


@pytest.mark.parametrize('combination', ['profile', 'automation'])
async def test_g3_r4_extra_profile_and_automation_refuse_before_enrollment(runtime_case, combination):  # noqa: F811
    c = runtime_case
    calls = list(c.owners)
    with c.db() as db:
        session = db.get(Session, SID)
        if combination == 'profile':
            session.launch_spec.inputs = {**session.launch_spec.inputs, 'profile': 'extra-native-profile'}
        elif combination == 'automation':
            session.automation = {'autoresume': True}
        with pytest.raises(LaunchRefused) as error:
            c.manager.new_launch(c.resolve).reserve(db, session)
        assert 'not supported in this release' in str(error.value)
    assert c.owners == calls and not c.factory.active


async def test_g3_r3_scheduled_notice_automation_refuses_enabled(public_case, monkeypatch):  # noqa: F811
    from acc.server.session_relay import delivery
    c = public_case
    monkeypatch.setattr(delivery, 'DELIVERY_QUALIFIED', True)
    monkeypatch.setattr(delivery, 'CLAUDE_SERVICE_QUALIFIED', True)
    before = list(c.owners[0].calls)
    with c.db() as db:
        for actor in ('supervisor',):
            with pytest.raises(p.RelayError) as error:
                delivery.capture(c.ctx, db, SimpleNamespace(session_id=SID, actor=actor))
            unsupported(error)
    assert c.owners[0].calls == before


async def test_g2_r2_uncaptured_fault_final_screen_refuses_without_new_owner(public_case):  # noqa: F811
    from acc.server.session_relay import final_screen
    c = public_case
    c.launch.fence_lost()
    before = list(c.owners[0].calls)
    with pytest.raises(p.RelayError) as error:
        final_screen.read(c.ctx, c.resolve, SID, str(c.launch.run_id))
    unsupported(error)
    assert c.owners[0].calls == before


async def test_g2_r3_unqualified_explicit_recovery_stays_default_off(monkeypatch):
    from acc.server.session_relay import exclusive_recovery
    monkeypatch.setattr(exclusive_recovery, 'RECOVERY_QUALIFIED', False)
    assert exclusive_recovery.ready(SimpleNamespace(), SimpleNamespace(session_id='fixture')) is False
    with pytest.raises(p.RelayError) as error:
        await exclusive_recovery.recover(None, None, {})
    assert error.value.code is p.ErrorCode.UNSUPPORTED


async def test_g3_r2_authority_expiry_after_await_refuses_before_native_write(public_case):  # noqa: F811
    import asyncio

    from acc.server.models import SessionRun, utcnow
    c = public_case
    with c.db() as db:
        authority.current(c.ctx, db, c.resolve, SID, action='control')
    await asyncio.sleep(0)
    @write_intent
    def expire():
        with c.db() as db:
            db.get(SessionRun, c.launch.run_id).ended_at = utcnow()
            db.commit()
    before = list(c.owners[0].calls)
    expire()
    with c.db() as db, pytest.raises(p.RelayError) as error:
        authority.current(c.ctx, db, c.resolve, SID, action='control')
    assert error.value.code is p.ErrorCode.STALE_AUTHORITY
    assert error.value.to_wire()['message']
    assert c.owners[0].calls == before


async def test_g7_r2_sealed_fault_run_cannot_interrupt_replacement(public_case):  # noqa: F811
    c = public_case
    c.launch.fence_lost()
    before = list(c.owners[0].calls)
    with c.db() as db, pytest.raises(p.RelayError) as error:
        authority.current(c.ctx, db, c.resolve, SID, action='turn.interrupt')
    assert error.value.code is p.ErrorCode.BUSY  # ownership phase refuses before any native cancellation
    assert error.value.to_wire()['message']
    assert c.owners[0].calls == before


async def test_g7_r3_pending_and_background_sdk_interrupt_refuses_visible(monkeypatch):
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
    for field in ('background', 'pending', 'native_permissions'):
        adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
        checked = []
        monkeypatch.setattr(adapter, '_check', lambda *_: checked.append(True))
        setattr(adapter, field, {'unsupported-work'})
        with pytest.raises(p.RelayError) as error:
            await adapter.interrupt(SimpleNamespace(method='turn.interrupt'))
        unsupported(error)
        assert checked == [True] and not adapter.seen


async def test_g7_r4_service_cancel_refuses_before_target_or_native_effects(monkeypatch):
    from acc.server.session_relay import interrupts
    checked = []
    run = SimpleNamespace(check_run=lambda *_a, **_k: (None, SimpleNamespace(
        actor=p.Actor(p.ActorKind.SERVICE, 'supervisor', p.Channel.SCHEDULE))))
    monkeypatch.setattr(interrupts, 'PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED', True)
    with pytest.raises(p.RelayError) as error:
        interrupts.check_grant(run, None, SimpleNamespace(resolve=lambda: checked.append('native')))
    unsupported(error)
    assert not checked


@pytest.mark.parametrize('placement', [dict(host='lab-node'), dict(team_host='lab-node')])
def test_rc_scope_preserves_node_session_admission(placement):
    from acc.server.session_relay import release_scope
    session = SimpleNamespace(host=None, team_host=None, team_id=None, multirun_id=None,
        automation=None, provider='codex', claude_auth=None, launch_spec=SimpleNamespace(inputs={}))
    for field, value in placement.items():
        setattr(session, field, value)
    release_scope.local(session, SimpleNamespace(kind=p.ActorKind.PERSON))


def test_rc_scope_does_not_replace_subscription_consent_policy():
    from acc.server.session_relay import release_scope
    session = SimpleNamespace(host=None, team_host=None, team_id=None, multirun_id=None,
        automation=None, provider='claude', claude_auth='subscription', launch_spec=SimpleNamespace(inputs={}))
    release_scope.local(session, SimpleNamespace(kind=p.ActorKind.PERSON))


@pytest.mark.parametrize('channel', [p.Channel.API, p.Channel.TEAM])
def test_rc_scope_preserves_scoped_api_and_team_authority(channel):
    from acc.server.session_relay import release_scope
    session = SimpleNamespace(team_id='fixture-team', multirun_id=None, automation=None,
        launch_spec=SimpleNamespace(inputs={}))
    release_scope.local(session, p.Actor(p.ActorKind.SERVICE, 'fixture-origin', channel))
