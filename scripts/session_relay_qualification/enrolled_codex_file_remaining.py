"""Actual file IO failure or invalid Cancel refusal; no policy exceptions."""
from enrolled_codex_file_ack import prepare as prepare_ack

from acc.server.models import RelayDecision, RelayOperation
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


async def prepare_remaining(root,work,c,mode):
    case=await prepare_ack(root,work,c)
    parent=root/'file-io-failure';parent.mkdir(mode=0o700)
    case.base.file_target=parent/'target.txt'
    if mode=='cancel_refused':
        case.accepted=False;case.selected_choice='decline'
        async def invalid(c,state,launch,manager,resolve,decision):
            fields={'sessionId':launch.session_id,'expectedRunId':str(launch.run_id),
                'decisionId':decision['decisionId'],'choiceId':'cancel','expectedRevision':decision['revision'],
                'actionDigest':decision['actionDigest']}
            registered=Hub(c.ctx).register(resolve,launch.session_id,str(launch.run_id),'invalid-file-cancel',
                'decision.reply',p.operation_digest('decision.reply',fields))
            try:await manager.answer_decision(resolve,launch.session_id,{**fields,'operationId':registered.operation_id})
            except p.RelayError:pass
            else:raise AssertionError('Unavailable file Cancel reached native dispatch')
            with c.db() as db:
                assert db.get(RelayOperation,registered.operation_id).receipt['state']=='failed'
                value=db.get(RelayDecision,(launch.session_id,decision['decisionId'])).decision
                assert value['status']=='pending' and not value.get('winner')
            case.invalid_cancel_refused=True
        case.before_answer=invalid
    else:
        if mode=='failed_lost':case.loss='after'
        elif mode=='failed_before':case.loss='before';case.expect_application=False
        else:case.expect_application=True
        async def fail(*_):parent.chmod(0o500)
        case.before_answer=fail
    original=case.exercise
    async def exercise(*args,**kwargs):
        try:return await original(*args,**kwargs)
        finally:parent.chmod(0o700)
    case.exercise=exercise
    return case
