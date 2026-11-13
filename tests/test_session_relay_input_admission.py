# ruff: noqa: F811
"""Live frame dispatch retains auth-first and negotiated capability boundaries."""
import time

import pytest
from test_session_relay_public_writer import SID, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_terminal_grants import claim, frame, stream_case  # noqa: F401


async def input_reply(c, params):
    return await c.client.post(f'/api/sessions/{SID}/relay', json={'jsonrpc': '2.0', 'id': 'frame',
        'method': 'ry/terminal.input', 'params': {'sessionId': SID, **params}})


@pytest.mark.parametrize('grant_state', ['valid', 'invalid', 'expired'])
async def test_live_frame_authenticates_revoked_caller_before_private_grant_refusal(stream_case, grant_state):
    c = stream_case
    grant = (await claim(c))['result']
    params = frame(c, grant)
    if grant_state == 'invalid':
        params['inputGrant'] = '0' * 64
    elif grant_state == 'expired':
        c.launch.terminal_grants.record.expires = time.monotonic() - 1
    c.state['revoked'] = True
    response = await input_reply(c, params)
    assert response.status_code == 401
    assert not c.host.writes and len(c.host.claims) == 1


@pytest.mark.parametrize('change', ['connection', 'attachment', 'session'])
async def test_live_frame_cannot_use_foreign_http_binding(stream_case, change):
    c = stream_case
    grant = (await claim(c))['result']
    params = frame(c, grant)
    if change == 'connection':
        c.client.headers['X-ACC-Relay-Connection'] = '0' * 64
    elif change == 'attachment':
        params['attachmentId'] = 'foreign-attachment'
    else:
        params['sessionId'] = 'foreign-session'
    response = await input_reply(c, params)
    assert response.status_code == 200 and 'error' in response.json()
    assert not c.host.writes and len(c.host.claims) == 1


@pytest.mark.parametrize('change', ['epoch', 'binding'])
async def test_next_frame_reads_new_root_fence_instead_of_prior_locked_snapshot(stream_case, change):
    from acc.server.db import write_intent
    from acc.server.models import RelaySession

    c = stream_case
    grant = (await claim(c))['result']
    first = await input_reply(c, frame(c, grant))
    assert first.status_code == 200 and 'result' in first.json()
    assert len(c.host.writes) == 1

    @write_intent
    def replace_fence():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            if change == 'epoch':
                row.authority_epoch += 1
            else:
                row.binding_id = 'replacement-binding'
            db.commit()

    replace_fence()
    second = await input_reply(c, frame(c, grant, sequence=2))
    assert second.status_code == 200 and 'error' in second.json()
    assert len(c.host.writes) == 1 and len(c.host.claims) == 1


@pytest.mark.parametrize('state', ['pending', 'reserved', 'outcome_unknown'])
async def test_next_frame_reads_new_decision_fence_after_previous_frame(stream_case, state):
    from acc.server.db import write_intent
    from acc.server.models import RelayDecision
    from acc.session_relay import protocol as p

    c = stream_case
    grant = (await claim(c))['result']
    first = await input_reply(c, frame(c, grant))
    assert first.status_code == 200 and 'result' in first.json()
    assert len(c.host.writes) == 1
    c.launch.publication({'kind': 'decision', 'decisionId': 'later-review',
        'actionDigest': p.sha256_digest(b'artificial later decision'),
        'expiresAtMs': int(time.time() * 1000) + 60_000,
        'choices': ['accept', 'cancel'], 'description': 'Artificial review after frame one'})
    if state != 'pending':
        @write_intent
        def advance():
            with c.db() as db:
                row = db.get(RelayDecision, (SID, 'later-review'))
                row.decision = {**row.decision, 'status': state}
                db.commit()
        advance()
    refused = await input_reply(c, frame(c, grant, sequence=2))
    assert refused.status_code == 200
    error = refused.json()['error']['data']
    assert error['code'] == ('UNSUPPORTED' if state == 'pending' else 'BUSY'), error
    assert len(c.host.writes) == 1
