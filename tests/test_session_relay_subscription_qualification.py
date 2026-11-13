"""Subscription path uses owned fake CLIs only; never a real login/account."""
from dataclasses import replace

import pytest
from test_session_relay_claude_agent import CLI
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server import claude_subscription
from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Setting
from acc.session_relay import protocol as p


@pytest.mark.parametrize('ownership_case', ['subscription'], indirect=True)
async def test_approved_subscription_same_id_path_only_launches_fake_cli(ownership_case):  # noqa: F811
    c=ownership_case
    with c.db() as db:
        row=db.get(RelaySession,SID)
        policy=db.get(RelayRun,row.current_run_id).frozen_policy
        assert policy['authMode']=='subscription' and policy['personId']==1
    initial=c.owned_factory.current
    assert initial.spec.spec['cli_path']==str(CLI.resolve())
    await select_owner(c,'terminal','stub-subscription-terminal')
    terminal=c.owned_factory.current
    assert terminal.spec.spec['cli_path']==str(CLI.resolve())
    await select_owner(c,'chat','stub-subscription-chat')
    assert c.owned_factory.current.spec.spec['cli_path']==str(CLI.resolve())
    assert initial.process.returncode is not None and terminal.process.returncode is not None
    assert all(owner.spec.binding.native_conversation=='fixture-conversation' for owner in c.owned_factory.owners)
    # Revoke consent through the production function, then refuse before stop.
    current=c.owned_factory.current
    @write_intent
    def revoke():
        with c.db() as db:
            claude_subscription.update(db,c.resolve()[0],False,None)
            db.commit()
    revoke()
    with pytest.raises(p.RelayError):await select_owner(c,'terminal','stub-subscription-revoked')
    assert c.owned_factory.current is current and current.process.returncode is None


@pytest.mark.parametrize('ownership_case', ['subscription'], indirect=True)
@pytest.mark.parametrize('change',['owner_disabled','stale_notice','wrong_person','service'])
async def test_subscription_consent_and_person_rechecked_before_owner_stop(ownership_case,change):  # noqa: F811
    c=ownership_case
    initial=c.owned_factory.current
    if change=='owner_disabled':
        c.ctx.settings=replace(c.ctx.settings,feature_overrides={**c.ctx.settings.feature_overrides,'claude_subscription_chat':False})
    elif change=='service':
        c.state['actor']=p.Actor(p.ActorKind.SERVICE,'alex',p.Channel.API)
    else:
        @write_intent
        def invalidate():
            with c.db() as db:
                if change=='stale_notice':
                    row=db.get(Setting,claude_subscription.key(1))
                    row.value={**row.value,'version':'stale'}
                else:
                    row=db.get(RelayRun,c.launch.run_id)
                    row.frozen_policy={**row.frozen_policy,'personId':2}
                db.commit()
        invalidate()
    try:
        with pytest.raises(p.RelayError):await select_owner(c,'terminal','stub-subscription-refusal-'+change)
        assert c.owned_factory.current is initial and initial.process.returncode is None
        assert len(c.owned_factory.owners)==1
    finally:
        # Teardown Stop is a safety action; restore only the artificial actor.
        if change=='service':c.state['actor']=p.Actor(p.ActorKind.PERSON,'alex',p.Channel.CHAT)
