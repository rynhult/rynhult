"""Pinned owned Codex stock key decoder only; no input-grant/CAS qualification."""
import asyncio
import json
import os
import termios
import time
from pathlib import Path

from installed_security import prepare as prepare_security
from websockets.asyncio.client import unix_connect

from acc.broker import ops
from acc.session_relay import native_approval_aliases
from acc.session_relay import protocol as p
from acc.session_relay.terminal_host import frame


class Case:
    def __init__(self,base,*,concurrent=False,withdraw=False):self.base=base;self.concurrent=concurrent;self.withdraw=withdraw
    def __getattr__(self,key):return getattr(self.base,key)
    async def exercise(self,c,state,launch,manager,config):
        view=state.owned_view;host=state.terminal_host;socket=state.native_socket
        assert view is not None and view.bootstrapped() and view.peer is not None
        assert host.grant is None and host.input_fenced
        assert host.writer is None and host.output_task is None
        host.consume_output=False  # this fixture explicitly owns the one read consumer before connect
        assert native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED is False
        native_approval_aliases.NATIVE_APPROVAL_DECODER_QUALIFIED=True
        def tty_mode():
            path=ops.tmux(config,'display-message','-p','-t','='+view.name+':','#{pane_tty}',check=True).stdout.strip()
            fd=os.open(path,os.O_RDONLY|os.O_NOCTTY|os.O_NONBLOCK)
            try:
                mode=termios.tcgetattr(fd)
                return {'path':path,'iflag':mode[0],'lflag':mode[3],
                    'ICRNL':bool(mode[0]&termios.ICRNL),'ICANON':bool(mode[3]&termios.ICANON)}
            finally:os.close(fd)
        offers=[];replies=[];native_responses=[];incoming=[]
        raw_reply=socket.approval_aliases.reply
        async def observe_reply(peer,route,request):
            incoming.append({'ownedRoute':route is host,'request':request})
            return await raw_reply(peer,route,request)
        socket.approval_aliases.reply=observe_reply
        respond=state.owner.respond
        async def track(*args,**kwargs):native_responses.append(args);return await respond(*args,**kwargs)
        state.owner.respond=track
        async def offer(value):offers.append(value);return True  # deliberate decoder stub, no actor or write grant
        async def decoded(value,choice):replies.append((value,choice))  # no provider dispatch or synthetic accepted receipt
        host.offer_native_approval=offer;host.native_approval_response=decoded
        output=[]
        async def observe():
            async for data in host.attach_read(host.run):output.append(data)
        reading=asyncio.create_task(observe())
        async with asyncio.timeout(5):
            while not output:await asyncio.sleep(.01)
        assert host.grant is None and host.input_fenced
        try:
            # Fixture-only local edit establishes post-startup interactivity before
            # any protected request. No Enter/query and no real person's draft.
            marker='decoder-startup-local-probe'
            host.writer.write(frame(b'd',marker.encode()))
            await host.writer.drain()
            async with asyncio.timeout(10):
                while marker not in ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout:
                    await asyncio.sleep(.025)
            host.writer.write(frame(b'd',b'\x7f'*len(marker)))
            await host.writer.drain()
            async with asyncio.timeout(10):
                while marker in ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout:
                    await asyncio.sleep(.025)
            assert not native_responses and self.base.calls==0
            results=[]
            keys=(('enter','0d','accept'),('escape','1b','cancel'))
            async def offer_key(key):
                params={'threadId':state.binding.native_conversation,'turnId':'decoder-turn-'+key,
                    'itemId':'decoder-item-'+key,'command':'echo STOCK_REVIEW_'+key.upper(),
                    'cwd':state.binding.workspace,'reason':'Offline pinned decoder qualification',
                    'startedAtMs':time.time_ns()//1_000_000}
                alias=await socket.offer_approval(host,decision_id='decoder-'+key,
                    action_digest=p.sha256_digest(p.canonical_json(params)),revision=0,params=params,
                    choices=frozenset({'accept','cancel'}))
                assert alias!=params['itemId'] and offers[-1].alias==alias
                async with unix_connect(str(socket.path),compression=None) as foreign:
                    await foreign.send(json.dumps({'id':0,'method':'initialize',
                        'params':{'clientInfo':{'name':'codex-tui','version':'0.167.1'}}}))
                    assert 'result' in json.loads(await foreign.recv())
                    await foreign.send(json.dumps({'id':alias,'result':{'decision':'accept'}}))
                    assert 'error' in json.loads(await foreign.recv())
                assert alias in socket.approval_aliases.pending and not replies[len(results):]
                return alias
            outstanding={}
            if self.concurrent:
                for key,_,_ in keys:outstanding[key]=await offer_key(key)
                assert len(socket.approval_aliases.pending)==2 and not replies
            withdrawal=None
            if self.withdraw:
                assert self.concurrent
                retired=socket.approval_aliases.pending[outstanding['enter']]
                async with asyncio.timeout(10):
                    while True:
                        pane=ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout
                        if 'STOCK_REVIEW_ENTER' in pane and 'Press enter to confirm or esc to cancel' in pane:break
                        await asyncio.sleep(.025)
                assert await socket.withdraw_approval(object(),retired.offer) is False
                assert await socket.withdraw_approval(host,retired.offer) is True
                try:
                    await socket.approval_aliases.reply(retired.peer,host,
                        {'id':retired.offer.alias,'result':{'decision':'accept'}})
                except p.RelayError as error:assert error.code is p.ErrorCode.STALE_AUTHORITY
                else:raise AssertionError('Retired owned alias accepted a late response')
                assert outstanding['escape'] in socket.approval_aliases.pending and not replies
                withdrawal={'alias':retired.offer.alias,'decisionId':retired.offer.decision_id,
                    'lateOwnedResponseRefused':True,'otherAlias':outstanding['escape'],
                    'notificationMethod':'serverRequest/resolved','applicationAcknowledgementClaimed':False}
                keys=keys[1:]
            for key,raw,expected in keys:
                alias=outstanding[key] if self.concurrent else await offer_key(key)
                async with asyncio.timeout(10):
                    while True:
                        pane=ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout
                        if ('STOCK_REVIEW_'+key.upper() in pane and 'Press enter to confirm or esc to cancel' in pane
                                and '1. Yes, proceed' in pane):break
                        await asyncio.sleep(.025)
                if self.withdraw:withdrawal['otherModalShownBeforeKey']=True
                # Exact framed terminal bytes into this generated-home fake-model Codex view only.
                # This proves its decoder, not public input authority or main CAS.
                host.writer.write(frame(b'd',bytes.fromhex(raw)))
                await host.writer.drain()
                try:
                    async with asyncio.timeout(10):
                        while len(replies)<=len(results):await asyncio.sleep(.025)
                except TimeoutError:
                    print('Stock decoder diagnostic:',key,incoming,flush=True)
                    print('Native TTY mode:',tty_mode(),file=__import__('sys').stderr,flush=True)
                    print('Native process status:',Path('/proc/'+str(view.peer[0])+'/stat').read_text(),flush=True)
                    print(ops.tmux(config,'capture-pane','-p','-J','-t','='+view.name+':',check=True).stdout[-2000:],flush=True)
                    raise
                bound,choice=replies[-1]
                assert bound.alias==alias and choice==expected
                assert alias not in socket.approval_aliases.pending
                if self.concurrent and key=='enter':
                    assert outstanding['escape'] in socket.approval_aliases.pending and len(replies)==1
                results.append({'key':key,'alias':alias,'choice':choice,'decisionId':bound.decision_id,'encodingHex':raw,'nativeResponse':next(value['request'] for value in reversed(incoming) if value['ownedRoute'])})
            assert not socket.approval_aliases.pending
            assert not native_responses and self.base.calls==0 and host.grant is None and host.input_fenced
            proof={'provider':'codex','nativeId':state.binding.native_conversation,'ownedPeerVerified':True,
                'stockDecoderKeys':results,'concurrentOffers':self.concurrent,'exactWithdrawal':self.withdraw,'withdrawal':withdrawal,'foreignPeerAliasRefused':True,'noProviderResponseCalls':True,
                'noModelCalls':True,'noInputGrantOrActorInferred':True,'decoderOnly':True,'inputPath':'owned-view framed attach','sendKeysUsed':False,'readAttachmentBeforeKeys':True,'postStartupLocalEditDecodedAndCleared':True,'nativeTTYMode':tty_mode(),
                'terminalMediated':False,'declaredCapabilities':{},
                'scope':'Pinned actual owned Codex0.167.1 post-startup basic command stock Enter/Escape decoder and private peer-bound aliases with stubbed route consent. Exact fixture-only framed bytes0d/1b under generated HOME; no public authenticated frame ticket, main Hub CAS, original actor receipt, provider permission application or file-key qualification is claimed.'}
            Path('/evidence/result.json').write_text(json.dumps(proof))
        finally:
            await host.detach(host.run)
            reading.cancel()
            await asyncio.gather(reading,return_exceptions=True)



async def prepare(root,work,c):
    return Case(await prepare_security(root,work,c))
