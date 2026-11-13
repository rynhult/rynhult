# ruff: noqa: F811
"""Retained paging must fit bytes and never return an unadvanceable cursor."""
from types import SimpleNamespace

import httpx
import pytest
from test_session_relay_past_history import past_case  # noqa: F401
from test_session_relay_public_writer import SID, public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.session_relay import history, store
from acc.server.vault import Redactor
from acc.session_relay import protocol as p


@pytest.mark.parametrize('expansion', ['redaction', 'json-encoding'])
async def test_redaction_expanded_event_refuses_instead_of_spinning_same_cursor(past_case, expansion):
    c = past_case
    secret = 'token123'  # valid minimum-length artificial vault value
    text = secret * ((50 << 10) // len(secret))

    @write_intent
    def populate():
        with c.db() as db:
            history.message(db, store.lock(db, SID), item_id='expanding-message', role='assistant',
                complete_text=text, source_epoch=900, source_revision=0, mask=lambda value: value)
            db.commit()

    populate()
    # Ordinary fixed replacement can exceed the page budget for a valid stored
    # event. JSON escaping can independently exceed it with fewer display bytes.
    name = ('RETAINED_SCREEN_MARKER_' + 'X' * 80)[:80]  # valid vault key-name limit
    redactor = Redactor([(secret, name)]) if expansion == 'redaction' else SimpleNamespace(
        text=lambda value: '\x00' * len(value) if value == text else value)
    replacement = redactor.text(text)
    assert len(p.canonical_json({'text': replacement})) > p.HISTORY_MAX_BYTES
    c.ctx.supervisor.vault.redactor = lambda *_: redactor
    params = {'runId': 900, 'limit': 200}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        for _ in range(5):
            response = await client.get(f'/api/sessions/{SID}/relay/events', params=params)
            if response.status_code == 409:
                assert response.json()['error']['data']['code'] == 'LIMIT'
                assert text not in response.text and replacement not in response.text
                break
            assert response.status_code == 200
            value = response.json()
            assert value['events'], 'Successful page cannot stall forever before an oversized retained event'
            assert value['more'] is True
            assert value['cursor'] != params.get('cursor')
            params['cursor'] = value['cursor']
        else:
            pytest.fail('Oversized retained event never produced a bounded explicit refusal')
        # A refusal does not consume/drop the event or weaken current redaction.
        c.ctx.supervisor.vault.redactor = lambda *_: SimpleNamespace(text=lambda value: '[MASKED]' if value == text else value)
        resumed = await client.get(f'/api/sessions/{SID}/relay/events', params=params)
        assert resumed.status_code == 200
        assert '[MASKED]' in resumed.text and text not in resumed.text
        assert resumed.json()['more'] is False
    assert len(c.owners) == 1 and not c.launch.grants and c.launch.terminal_grants.record is None


async def test_byte_bounded_pages_make_progress_and_preserve_every_event(past_case):
    c = past_case

    @write_intent
    def populate():
        with c.db() as db:
            row = store.lock(db, SID)
            for index in range(9):
                history.message(db, row, item_id=f'capacity-{index}', role='assistant',
                    complete_text=f'Capacity message {index}: ' + 'x' * (80 << 10),
                    source_epoch=900, source_revision=0, mask=lambda text: text)
            db.commit()

    populate()
    params = {'runId': 900, 'limit': 200}
    observed, pages = [], 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url='http://fixture') as client:
        for _ in range(10):
            response = await client.get(f'/api/sessions/{SID}/relay/events', params=params)
            assert response.status_code == 200
            value = response.json()
            assert sum(len(p.encode_frame(event)) for event in value['events']) <= p.HISTORY_MAX_BYTES
            observed.extend(event['payload'].get('itemId') for event in value['events'])
            pages += 1
            if not value['more']:
                break
            assert value['events'] and value['cursor'] != params.get('cursor')
            params['cursor'] = value['cursor']
        else:
            pytest.fail('Bounded retained pages failed to terminate')
    assert pages >= 3
    assert [item for item in observed if item and item.startswith('capacity-')] == [f'capacity-{i}' for i in range(9)]
    assert len(c.owners) == 1 and not c.launch.grants
