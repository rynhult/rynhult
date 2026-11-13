"""Exact native SDK interrupt cannot cross an explicit fresh-session boundary."""
import asyncio
import importlib.util
from pathlib import Path
from uuid import uuid4

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, Session
from acc.session_relay import protocol as p

NEXT = 'def456abc123'


async def fresh_session_after_interrupt(c, inspect, old_request, old_receipt):
    old = c.launch
    before = await inspect('state')
    await c.manager.close()
    async with asyncio.timeout(20):
        while (await inspect('state'))['active']:
            await asyncio.sleep(.02)
    fresh = c.manager.new_exclusive_launch(c.resolve)
    native = str(uuid4())
    @write_intent
    def review_new_session():
        with c.db() as db:
            previous = db.get(Session, old.session_id)
            db.add(Session(id=NEXT, provider='claude', name='Explicit fresh-session review after SDK interrupt',
                native_id=native, claude_auth='api_key', tmux_name='acc-' + NEXT,
                launch_spec_id=previous.launch_spec_id, run_as=previous.run_as, owner=previous.owner,
                workdir=previous.workdir, run_dir=str(Path(previous.run_dir).parent / NEXT)))
            db.flush()
            fresh.reserve(db, db.get(Session, NEXT))
            db.commit()
    review_new_session()
    await fresh.launch(NEXT)
    assert fresh.run_id != old.run_id and fresh.binding.native_conversation != old.binding.native_conversation
    @write_intent
    def qualify():
        with c.db() as db:
            row = db.get(RelaySession, NEXT)
            row.capabilities = {'text': 'qualified', 'interrupt': 'qualified',
                'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified'}
            revision = row.control_revision
            db.commit()
            return revision
    revision = qualify()
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/public_native_http.py'
    spec = importlib.util.spec_from_file_location('sdk_recovery_public', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    public = await module.Public.create(c, c.manager, NEXT, features=['queue', 'writer'])
    old_reader = await module.Public.create(c, c.manager, old.session_id, features=['queue'])
    try:
        idle = await inspect('state')
        assert idle['sdk_queries'] == 0 and not idle['sdk_interrupts']
        assert idle['owner_process'] != before['owner_process']
        await inspect('hold_long_public_model')
        fields = {'sessionId': NEXT, 'expectedRunId': str(fresh.run_id),
            'text': 'Fresh reviewed session must survive an old interrupt ticket',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': revision, 'ifBusy': 'reject'}
        registered = await public.call('operation.register', {'expectedRunId': str(fresh.run_id),
            'registrationKey': 'fresh-session-work', 'method': 'turn.submit',
            'payloadDigest': p.operation_digest('turn.submit', fields)})
        assert 'result' in registered, registered
        active = registered['result']['operationId']
        submitted = await public.call('turn.submit', {**fields, 'operationId': active})
        assert submitted['result']['state'] == 'uncertain', submitted
        async with asyncio.timeout(10):
            while not (await inspect('state'))['model_held']:
                await asyncio.sleep(.02)
        # Observing/replaying an old immutable result cannot dispatch to the new SDK.
        observed = await old_reader.client.get(f'/api/sessions/{old.session_id}/relay/operations/{old_request["operationId"]}')
        assert observed.status_code == 200 and observed.json() == old_receipt, observed.text
        old_reply = await old_reader.call('turn.interrupt', old_request)
        assert ('error' in old_reply or old_reply['result'] == old_receipt), old_reply
        # Explicitly retargeting the old operation/run or old turn is also refused.
        retargeted = {**old_request, 'sessionId': NEXT, 'expectedRunId': str(fresh.run_id)}
        reply = await public.call('turn.interrupt', retargeted)
        assert 'error' in reply, reply
        fields = {key: value for key, value in retargeted.items() if key != 'operationId'}
        fields['expectedControlRevision'] = revision
        registered = await public.call('operation.register', {'expectedRunId': str(fresh.run_id),
            'registrationKey': 'fresh-registration-stale-native-target', 'method': 'turn.interrupt',
            'payloadDigest': p.operation_digest('turn.interrupt', fields)})
        assert 'result' in registered, registered
        reply = await public.call('turn.interrupt', {**fields, 'operationId': registered['result']['operationId']})
        assert 'error' in reply or reply['result']['state'] == 'failed', reply
        after = await inspect('state')
        assert after['model_held'] and after['sdk_queries'] == 1 and not after['sdk_interrupts']
        assert after['owner_process'] == idle['owner_process'] and after['enclosures'] == 1
        with c.db() as db:
            assert db.get(RelaySession, NEXT).active_operation_id == active
            assert db.get(RelaySession, old.session_id).sealed
            assert db.get(RelayOperation, old_request['operationId']).receipt == old_receipt
        return {'explicitFreshSession': True, 'newNativeConversation': True, 'newRun': str(fresh.run_id),
            'oldRun': str(old.run_id), 'oldNativeProcessExitedBeforeFreshLaunch': True,
            'newSdkQueryHeldUnaffected': True, 'newSdkInterruptWrites': 0,
            'oldReceiptUnchanged': True, 'staleInterruptVariants': 3,
            'noAutomaticQueryOrReplay': True,
            'scope': 'API-key installed native SDK; explicit fixture-reviewed new session, not browser Launch or automatic same-session recovery; request acceptance is not confirmed cancellation settlement'}
    finally:
        await inspect('release_model')
        await public.close()
        await old_reader.close()
