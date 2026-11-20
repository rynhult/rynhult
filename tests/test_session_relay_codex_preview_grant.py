"""Every Codex preview grant predicate has a positive control before refusal."""
# ruff: noqa: F811
from dataclasses import replace

import pytest
from test_session_relay_release_qualification import OWNER, _set, case, granted  # noqa: F401

from acc.broker.relay_runtime import CLI_DIGEST
from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, User, utcnow
from acc.server.session_relay import release_qualification as rq
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

CAPABILITIES={'text':'qualified','terminal.input':'qualified'}


@pytest.fixture
def codex_case(case,monkeypatch):
    monkeypatch.setattr('acc.server.session_relay.writers.CODEX_PUBLIC_WRITER_QUALIFIED',True)
    @write_intent
    def seed():
        with case.db() as db:
            db.get(Session,'relay').provider='codex'
            run=db.get(RelayRun,1);run.identity={**run.identity,'cliDigest':CLI_DIGEST};run.frozen_policy={}
            db.commit()
    seed()
    return case


@pytest.mark.parametrize('change',[
    pytest.param(_set('row.sealed',True),id='sealed'),
    pytest.param(_set('row.current_run_id',2),id='not-current-run'),
    pytest.param(_set('row.binding_id','foreign'),id='binding'),
    pytest.param(_set('native.ended_at',utcnow()),id='ended-native-run'),
    pytest.param(lambda c:setattr(c.run,'identity',{**c.run.identity,'cliDigest':p.sha256_digest(b'foreign')}),id='digest'),
    pytest.param(lambda c:setattr(c.run,'identity',{k:v for k,v in c.run.identity.items() if k!='cliDigest'}),id='unprobed'),
    pytest.param(_set('session.automation',{'goal':'automatic'}),id='automation'),
    pytest.param(lambda c:setattr(c.session.launch_spec,'inputs',{'profile':'custom'}),id='profile'),
    pytest.param(_set('session.owner','sam'),id='another-session-owner'),
    pytest.param(_set('session.provider','opencode'),id='unknown-provider'),
])
def test_one_failed_codex_grant_condition_withdraws_every_capability(codex_case,change):
    c=codex_case;assert granted(c)==CAPABILITIES
    def fail(value):
        value.row.capabilities={'text':'qualified','decision.tool_approval':'qualified'}
        change(value)
    assert granted(c,change=fail)=={}


@pytest.mark.parametrize('kind',['member','demoted','other-owner','service','none'])
def test_codex_grant_needs_current_install_owner_and_session_owner(codex_case,kind):
    c=codex_case;assert granted(c)==CAPABILITIES
    actor=OWNER
    @write_intent
    def change():
        nonlocal actor
        with c.db() as db:
            if kind=='member':
                db.get(Session,'relay').owner='sam';actor=p.Actor(p.ActorKind.PERSON,'sam',p.Channel.CHAT)
            elif kind=='demoted':db.get(User,1).role='member'
            elif kind=='other-owner':
                db.add(User(id=3,username='other',role='owner',password_hash='unused',totp_secret='unused'))
                actor=p.Actor(p.ActorKind.PERSON,'other',p.Channel.CHAT)
            elif kind=='service':actor=p.Actor(p.ActorKind.SERVICE,'alex',p.Channel.API)
            else:actor=None
            db.commit()
    change()
    assert granted(c,actor=actor)=={}


def test_codex_grant_needs_feature_and_provider_flags(codex_case,monkeypatch):
    c=codex_case;assert granted(c)==CAPABILITIES
    c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':False})
    assert granted(c)=={}
    c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':True})
    assert granted(c)==CAPABILITIES
    for flag in ['PUBLIC_WRITER_QUALIFIED','CODEX_PUBLIC_WRITER_QUALIFIED']:
        with monkeypatch.context() as patch:
            patch.setattr('acc.server.session_relay.writers.'+flag,False)
            assert granted(c)=={}


def test_codex_needs_neither_claude_flags_nor_subscription_consent(codex_case,monkeypatch):
    c=codex_case
    c.ctx.settings=replace(c.ctx.settings,feature_overrides={'session_relay':True,'claude_subscription_chat':False})
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED',False)
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED',False)
    assert granted(c,change=lambda v:setattr(v.run,'frozen_policy',{'authMode':'subscription','personId':2}))==CAPABILITIES


def test_codex_declaration_is_frozen_and_contains_no_l1_or_approval_operations(codex_case):
    assert granted(codex_case)==CAPABILITIES
    value=rq.CODEX_DECLARATION
    assert value.provider=='codex' and value.cli_version=='0.167.1' and value.cli_digest==CLI_DIGEST
    assert {key for key in p.CAPABILITIES if value.support(key) is p.Support.QUALIFIED}==set(CAPABILITIES)
    with pytest.raises(TypeError):value.operations['interrupt']=value.operations['text']
    with pytest.raises(AttributeError):value.qualification_id='forged'
    with codex_case.db() as db:
        assert rq.grant(codex_case.ctx,db,db.get(RelaySession,'relay'),None,OWNER)=={}


@pytest.mark.parametrize('actor',[p.Actor(p.ActorKind.PERSON,'sam',p.Channel.CHAT),p.Actor(p.ActorKind.SERVICE,'alex',p.Channel.API)])
def test_codex_owner_guard_withdraws_fabricated_capabilities(codex_case,actor):
    with codex_case.db() as db:
        row=db.get(RelaySession,'relay');row.capabilities=dict(CAPABILITIES)
        with pytest.raises(p.RelayError) as error:rq.require_owner(db,db.get(Session,'relay'),actor)
        assert error.value.code is p.ErrorCode.POLICY_REFUSED and row.capabilities=={}


async def test_demoted_codex_owner_cannot_register_mutation_even_with_stale_grant(codex_case):
    c=codex_case
    @write_intent
    def demote():
        with c.db() as db:
            db.get(User,1).role='member';db.get(RelaySession,'relay').capabilities=dict(CAPABILITIES);db.commit()
    demote();c.state['who']=Principal('alex','member','fixture',1)
    with pytest.raises(p.RelayError) as error:
        Hub(c.ctx).register(c.resolve,'relay','1','demoted-mutator','turn.submit',p.sha256_digest(b'fixture'))
    assert error.value.code is p.ErrorCode.POLICY_REFUSED
