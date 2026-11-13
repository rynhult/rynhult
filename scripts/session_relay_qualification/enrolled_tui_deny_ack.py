"""Actual native TUI denial, exact invocation marker and original public CAS."""
import asyncio
from pathlib import Path

from enrolled_public_decisions import Case as PublicCase
from enrolled_tui_expiry import prepare as prepare_expiry
from public_native_http import Public


class Case(PublicCase):
    denial_ack = True

    async def after_granted(self,c,state,launch,manager,owner,attached,claim,proof):
        original = Public.call
        async def prefer_decline(client,method,params):
            if method == 'decision.reply' and params.get('choiceId') == 'accept':
                await asyncio.sleep(.3)
            return await original(client,method,params)
        Public.call = prefer_decline
        try:
            await super().after_granted(c,state,launch,manager,owner,attached,claim,proof)
        finally:
            Public.call = original
        assert Path(state.native_transcript[1]).is_relative_to(self.base.root/'home')
        proof.update(productionEmptyTreeStopped=True,isolatedHomeTranscript=True,
            scope='Pinned native TUI Deny application only: per-invocation reference, typed native hook denial attachment and matching permission-rule result under protected generated HOME, original cookie/CSRF public CAS receipt. Allow and stock keys remain unqualified; no subscription account or input.')


async def prepare(root,work,c):
    from acc.broker import claude_denial_evidence
    claude_denial_evidence.CLAUDE_TUI_DENY_ACK_QUALIFIED = True
    expiry=await prepare_expiry(root,work,c)
    return Case(expiry.base)
