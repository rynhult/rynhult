"""Read-only main enrollment for an original decision, independent of writers."""
import secrets
from contextlib import nullcontext
from types import SimpleNamespace

from acc.session_relay import protocol as p
from acc.session_relay.runtime_peer import RuntimePeer

from ..db import write_in_thread
from ..models import RelayRun, Session
from . import native_decisions

QUALIFIED = False


class OriginalEvidence:
    def __init__(self, manager, session_id, run_id):
        self.manager, self.ctx = manager, manager.ctx
        self.session_id, self.run_id = session_id, run_id
        self.token = secrets.token_hex(32)
        self.enrolled = False
        self.binding = self.placement = None

    def check(self, db=None):
        from ..vault import Cipher
        with self.ctx.db() if db is None else nullcontext(db) as db:
            session = db.get(Session, self.session_id)
            original = db.get(RelayRun, self.run_id)
            if not session or not original or original.session_id != self.session_id:
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            binding = p.Binding(**original.identity['binding'])
            placement = self.manager.current_placement(db, session, provider='claude', evidence_only=True)
            saved = original.identity['placement']
            if len(saved) not in (6, 7) or len(placement) < 6:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            saved_team = saved[6].get('team', {}).get('id') if len(saved) == 7 else None
            installation = p.sha256_digest(Cipher(self.ctx.settings.data_dir / 'secrets/session_relay_cursor.key').key(create=False))
            if (binding.provider != 'claude' or binding.binding_id != original.binding_id
                    or binding.installation != installation or binding.account != session.run_as
                    or binding.workspace != session.cwd or binding.native_conversation != session.native_id
                    or original.frozen_policy.get('launchDigest') != session.launch_spec.digest
                    or original.frozen_policy.get('guardDigest') != binding.launch_policy_digest
                    or original.frozen_policy.get('authMode') != session.claude_auth
                    or original.frozen_policy.get('ownershipMode') != 'exclusive_idle'
                    or placement[:2] != saved[:2] or placement[2][:2] != saved[2][:2]
                    or placement[5] != saved[5] or session.team_id != saved_team
                    or str(placement[2][0]) != binding.node_enrollment):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if self.binding is not None and (binding != self.binding or placement != self.placement):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            self.binding, self.placement = binding, placement
            return {'binding': vars(binding), 'sessionId': self.session_id, 'runId': str(self.run_id), 'runDir': session.run_dir}

    async def handle(self, method, params):
        if method == 'evidence.enroll' and params == {'enrollment': self.token} and not self.enrolled:
            result = await write_in_thread(self.check)
            self.enrolled = True
            return result
        if method == 'evidence.publish' and self.enrolled:
            await write_in_thread(self.check)
            if (params.keys() != {'kind','decisionId','operationId','choiceId','actionDigest','nativeRef','runId','originalOperation'}
                    or params.get('runId') != str(self.run_id)):
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            run = SimpleNamespace(ctx=self.ctx, session_id=self.session_id, run_id=self.run_id,
                provider='claude', binding_id=self.binding.binding_id)
            return await write_in_thread(lambda: native_decisions.applied(run,
                {key:value for key,value in params.items() if key not in ('runId','originalOperation')},
                validate=self.check,original_operation=params['originalOperation']))
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)

    async def reconcile(self):
        if not QUALIFIED:
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        await write_in_thread(self.check)
        client = self.ctx.broker.fresh(self.binding.account, self.placement[0])
        peer = None
        try:
            reader, writer = await client.session_runtime({'version': 5, 'enrollment': self.token})
            peer = RuntimePeer(reader, writer, self.handle)
            return await peer.call('reconcile', {})
        finally:
            if peer:
                await peer.close()
            await client.close()
