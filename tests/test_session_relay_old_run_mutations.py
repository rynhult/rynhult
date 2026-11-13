# ruff: noqa: F811
"""Saved old-run requests cannot mutate an ended/replaced enrolled session.

Real public/main/grant composition, synthetic execution and replacement rows.
Geometry is unsupported in this profile; refusal does not qualify resize.
"""
import pytest
from test_session_relay_public_writer import SID, call, public_case, register  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, SessionRun, utcnow


@pytest.mark.parametrize('boundary', ['ended', 'replacement'])
@pytest.mark.parametrize('method', ['terminal.input', 'writer.take', 'control.claim', 'terminal.resize'])
async def test_saved_old_run_mutation_refuses_before_host_or_owner_activity(stream_case, boundary, method):
    c = stream_case
    old_run = str(c.launch.run_id)
    grant = (await claim(c))['result']
    if method == 'terminal.input':
        params = frame(c, grant)
    elif method == 'terminal.resize':
        params = {'attachmentId': c.attachment, 'connectionSeq': 2,
            'geometryGrant': 'never-qualified-geometry', 'cols': 100, 'rows': 40}
    else:
        params = {'expectedRunId': old_run, 'attachmentId': c.attachment, 'expectedRevision': 2}
        if method == 'writer.take':
            params.update(view='chat', reason='Previously reviewed old-run take')
            registration = await register(c.client, c, params, 'old-run-take')
        else:
            from acc.session_relay import protocol as p
            registration = await call(c.client, 'operation.register', {'expectedRunId': old_run,
                'registrationKey': 'old-run-claim', 'method': method,
                'payloadDigest': p.operation_digest(method, {'sessionId': SID, **params})})
        assert 'result' in registration, registration
        params['operationId'] = registration['result']['operationId']
    native_before = list(c.owners[0].calls)
    c.launch.publication({'kind': 'exited', 'returnCode': 0,
        'finalScreen': {'text': 'Old retained screen', 'truncated': False}})
    if boundary == 'replacement':
        @write_intent
        def replace_run():
            with c.db() as db:
                db.add(SessionRun(id=900, session_id=SID, reason='manual_resume', verified_at=utcnow()))
                db.flush()
                db.add(RelayRun(run_id=900, session_id=SID, binding_id='replacement', identity={},
                    frozen_policy={}, policy_revision=0))
                db.flush()
                row = db.get(RelaySession, SID)
                row.current_run_id, row.binding_id, row.sealed = 900, 'replacement', False
                db.commit()
        replace_run()
    result = await call(c.client, method, params)
    assert 'error' in result and 'inputGrant' not in result and 'watermark' not in result, result
    assert not c.host.writes and len(c.host.claims) == 1
    assert len(c.owners) == 1 and c.owners[0].calls == native_before and not c.launch.grants
    with c.db() as db:
        row = db.get(RelaySession, SID)
        assert row.current_run_id == (900 if boundary == 'replacement' else c.launch.run_id)
