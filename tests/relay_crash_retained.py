"""Public retained evidence across an actual isolated broker SIGKILL/restart."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

SID = 'abc123def456'
NEXT = 'def456abc123'


class Retained:
    @classmethod
    async def start(cls, c, monkeypatch):
        self = cls()
        self.c = c
        self.run = str(c.launch.run_id)
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        @write_intent
        def qualify():
            with c.db() as db:
                row = db.get(RelaySession, SID)
                row.capabilities = {'text': 'qualified'}
                db.commit()
        qualify()
        await c.inspect('qualify')
        fields = {'sessionId': SID, 'expectedRunId': self.run,
            'text': 'Retain this completed native turn across broker crash',
            'expectedControlRevision': 1, 'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
        registered = Hub(c.ctx).register(c.resolve, SID, self.run, 'completed-before-real-crash',
            'turn.submit', p.operation_digest('turn.submit', fields))
        self.identifier = registered.operation_id
        result = await c.manager.submit(c.resolve, SID, {**fields, 'operationId': self.identifier})
        assert result.state is p.ReceiptState.ACCEPTED
        async with asyncio.timeout(20):
            while not (await c.inspect('idle'))['idle']:
                await asyncio.sleep(.02)
        return await cls.observe(c, self.run, self.identifier, capture_screen=True)

    @classmethod
    async def observe(cls, c, run, identifier, *, capture_screen=False):
        self = cls()
        self.c, self.run, self.identifier = c, str(run), identifier
        path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/public_native_http.py'
        spec = importlib.util.spec_from_file_location('crash_public_native', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.public_type = module.Public
        self.public = await module.Public.create(c, c.manager, SID)
        response = await self.public.client.get(f'/api/sessions/{SID}/relay/operations/{self.identifier}')
        assert response.status_code == 200, response.text
        self.receipt = response.json()
        assert self.receipt['state'] == 'accepted'
        self.screen = await self.read_screen(self.public) if capture_screen else None
        assert self.screen is None or self.screen['text'].strip()
        self.before_events = await self.events(self.public)
        assert self.before_events['events']
        assert any(event['kind'] == 'message.upsert' and 'Offline lifetime response.' in str(event['payload'])
            for event in self.before_events['events']), self.before_events
        return self

    async def read_screen(self, public):
        response = await public.client.get(f'/api/sessions/{SID}/relay/terminal', params={
            'expectedRunId': self.run, 'attachmentId': public.view['attachmentId']})
        assert response.status_code == 200, response.text
        return response.json()

    async def events(self, public, **params):
        response = await public.client.get(f'/api/sessions/{SID}/relay/events', params={'runId': self.run, **params})
        assert response.status_code == 200, response.text
        return response.json()

    async def after_restart(self, fresh, old_token=None):
        c = self.c
        # A new authenticated observer is required: do not borrow the old connection.
        await self.public.close()
        reader = await self.public_type.create(c, c.manager, SID)
        try:
            snap = (await reader.call('session.snapshot', {}))['result']
            assert snap['runId'] == self.run and snap['exitOutcome'] == 'unknown', snap
            response = await reader.client.get(f'/api/sessions/{SID}/relay/operations/{self.identifier}')
            assert response.status_code == 200 and response.json() == self.receipt
            # SIGKILL cannot publish a final screen or prove process exit. Do
            # not relabel an earlier live capture as the final screen. Durable
            # output/events and the original receipt must still be readable.
            response = await reader.client.get(f'/api/sessions/{SID}/relay/terminal', params={
                'expectedRunId': self.run, 'attachmentId': reader.view['attachmentId']})
            assert response.status_code != 200 and 'text' not in response.json(), response.text
            events = await self.events(reader)
            old = {event['eventSeq']: event for event in self.before_events['events']}
            now = {event['eventSeq']: event for event in events['events']}
            assert all(now.get(seq) == event for seq, event in old.items())
            for method in ('turn.submit', 'turn.interrupt', 'control.claim', 'writer.take'):
                reply = await reader.call('operation.register', {'expectedRunId': self.run,
                    'registrationKey': 'after-real-crash-' + method, 'method': method,
                    'payloadDigest': p.sha256_digest(b'Old session must remain read only')})
                assert 'error' in reply, reply
            # Cursors are bound to the authenticated observer scope, not just
            # to a run/sequence; a new login cannot borrow an old cursor.
            stale = await reader.client.get(f'/api/sessions/{SID}/relay/events',
                params={'runId': self.run, 'cursor': self.before_events['cursor']})
            assert stale.status_code != 200 and 'events' not in stale.json(), stale.text
            # Old identity cannot be retargeted to the explicitly created fresh session.
            from acc.server.session_relay import authority
            with c.db() as db:
                row, grant = authority.current(c.ctx, db, c.resolve, NEXT)
                assert str(grant.run_id) == str(fresh.run_id) != self.run
                assert row.active_operation_id is None
                assert db.get(RelaySession, SID).sealed
                assert db.get(RelayOperation, self.identifier).receipt == self.receipt
            after = await c.inspect('state')
            assert after['requests'] == 1 and after['active'] == 1
            reader.revoke()
            revoked = await reader.client.get(f'/api/sessions/{SID}/relay/operations/{self.identifier}')
            assert revoked.status_code == 401 and self.identifier not in revoked.text
            self.proof = {'scope': 'Actual broker SIGKILL/restart; pinned Codex; typed simulated node; explicit fixture-reviewed fresh session, not browser Launch or physical node/HA proof',
                'oldRun': self.run, 'newRun': str(fresh.run_id), 'retainedReceiptUnchanged': True,
                'preCrashLiveScreenSha256': hashlib.sha256(self.screen['text'].encode()).hexdigest() if self.screen else None,
                'finalScreenUnavailableWithoutCapture': True, 'retainedEventsUnchanged': len(old), 'freshAuthenticatedObserver': True, 'revokedReaderCannotRead': True,
                'oldMutationRegistrationsRefused': 4, 'modelRequests': 1,
                'foreignLoginCursorRefused': True, 'noReplayOrAutomaticEnrollment': True, 'terminalMediated': False}
        finally:
            await reader.close()
