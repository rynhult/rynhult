"""Read-only, caller/run-bound native Chat pages and bounded SSE follows."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from .. import auth
from ..broker_client import BrokerBusy, BrokerError, BrokerUnavailable, MachineOffline
from ..db import write_in_thread
from ..models import AuditLog, SessionRun, utcnow


def _text_size(value):
    try:
        return len(value.encode('utf-8')) if isinstance(value, str) else -1
    except UnicodeError:
        return -1


class Reads:
    """Share concurrent broker reads; never retain raw results after the readers leave."""
    def __init__(self):
        self.active = {}
        self.last = OrderedDict()
        self.locks = {}

    async def call(self, key, interval, fetch):
        if key not in self.active:
            if len(self.active) >= 32:
                raise HTTPException(429, 'Too many Chat reads. Try again shortly.')
            lock = self.locks.setdefault(key[0], asyncio.Lock())
            async def work():
                async with lock:
                    return await limited()

            async def limited():
                delay = interval - (time.monotonic() - self.last.get(key[0], 0))
                if delay > 0:
                    await asyncio.sleep(delay)
                self.last[key[0]] = time.monotonic()
                self.last.move_to_end(key[0])
                while len(self.last) > 128:
                    self.last.popitem(last=False)
                return await fetch()
            self.active[key] = [asyncio.create_task(work()), 0]
        entry = self.active[key]
        entry[1] += 1
        try:
            return await asyncio.shield(entry[0])
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.active.pop(key, None)
                if not entry[0].done():
                    entry[0].cancel()
                await asyncio.gather(entry[0], return_exceptions=True)
                if not any(k[0] == key[0] for k in self.active):
                    self.locks.pop(key[0], None)


class FollowSlots:
    """Bounded follows with reclaimable excess shares when another person arrives."""
    def __init__(self):
        self.active = OrderedDict()

    def acquire(self, caller):
        counts = {}
        for owner, _ in self.active.values():
            counts[owner] = counts.get(owner, 0) + 1
        count = counts.get(caller, 0)
        if count >= 4:
            raise HTTPException(429, 'Too many Chat views. Close a view and retry.')
        if len(self.active) >= 32:
            candidates = [(counts[owner], ticket) for ticket, (owner, _) in self.active.items()
                          if counts[owner] > count + 1]
            if not candidates:
                raise HTTPException(429, 'Too many Chat views. Close a view and retry.')
            largest = max(n for n, _ in candidates)
            ticket = next(ticket for n, ticket in candidates if n == largest)
            _, stopped = self.active.pop(ticket)
            stopped.set()
        ticket, stopped = object(), asyncio.Event()
        self.active[ticket] = (caller, stopped)
        return ticket, stopped

    def release(self, ticket):
        entry = self.active.pop(ticket, None)
        if entry:
            entry[1].set()


def router(ctx):
    routes = APIRouter()
    secret = secrets.token_bytes(32)  # restart invalidates tokens instead of trusting stale source state
    reads = Reads()
    waiting = FollowSlots()
    throttle = auth.LoginThrottle(limit=60, window=60)

    def admit(caller):
        if throttle.blocked(caller):
            raise HTTPException(429, 'Too many Chat requests. Try again shortly.')
        throttle.fail(caller)

    def binding(request, identifier):
        if not ctx.origin_ok(request.headers.get('origin')):
            raise HTTPException(403, 'Open Chat at Rynhult’s own address.')
        who = ctx.principal(request)  # revalidates browser/device admission on each emission too
        with ctx.db() as db:
            session = ctx.session_for(db, who, identifier)
            from ..admin_features import require
            require(ctx.settings, 'chat')
            run = db.scalar(select(SessionRun).where(SessionRun.session_id == identifier)
                            .order_by(SessionRun.id.desc()).limit(1))
            base = [who.username, who.user_id, identifier, session.owner, session.provider,
                    session.native_id, session.host, session.run_as, session.placement_id, session.launch_spec_id, session.cwd,
                    bool(run and run.verified_at), session.management]
            digest = hashlib.sha256(json.dumps(base).encode()).hexdigest()
            return {'key': digest, 'caller': who.username, 'run_id': run.id if run else None,
                    'verified': bool(run and run.verified_at), 'provider': session.provider,
                    'native_id': session.native_id, 'host': session.host, 'account': session.run_as,
                    'workspace': session.cwd, 'managed': session.management == 'acc', 'keys': session.vault_keys or []}

    def encode(value):
        raw = json.dumps(value, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(secret, raw, 'sha256').hexdigest()

    def decode(token, current):
        try:
            if len(token) > 4096:
                raise ValueError()
            body, signature = token.split('.')
            raw = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
            if not hmac.compare_digest(signature, hmac.new(secret, raw, 'sha256').hexdigest()):
                raise ValueError()
            value = json.loads(raw)
            if value['v'] != 1 or value['key'] != current['key'] or value['expires'] < time.time():
                raise ValueError()
        except (ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(400, 'Chat cursor is invalid or expired. Reload Chat.') from None
        if value['run'] != current['run_id']:
            raise HTTPException(409, 'The session run changed. Reload Chat.')
        return value

    def audit(actor, action, identifier, run):
        with ctx.db() as db:
            db.add(AuditLog(actor=actor, action=action, target=identifier, detail={'run_id': run}))
            db.commit()

    async def page(request, identifier, limit, token=None, follow=False):
        current = binding(request, identifier)
        saved = decode(token, current) if token else None
        before = saved.get('before') if saved and not follow else None
        after = saved.get('after') if saved and follow and saved.get('source') is not None else None
        params = {'workspace': current['workspace'], 'provider': current['provider'], 'native_id': current['native_id'], 'limit': limit,
                  'before': before, 'after': after, 'checkpoint': saved.get('source') if saved else None}
        raw = {'state': 'unknown', 'items': [], 'source': None, 'before': None, 'omitted': 0, 'reset': False}
        if not current['managed'] or current['provider'] not in ('claude', 'codex'):
            raw['state'] = 'unsupported'
        elif current['verified'] and current['native_id']:
            async def fetch():
                # Recheck after read throttling, before contacting the account/node.
                fresh = binding(request, identifier)
                if (fresh['key'], fresh['run_id']) != (current['key'], current['run_id']):
                    raise HTTPException(409, 'The session changed. Reload Chat.')
                client = ctx.broker.fresh(current['account'], host=current['host'], timeout=3)
                try:
                    return await client.call('chat_read', **params)
                finally:
                    await client.close()
            key = (current['key'], current['run_id'], json.dumps(params, sort_keys=True))
            try:
                raw = await reads.call(key, .5 if current['host'] == 'local' else 2, fetch)
            except BrokerBusy:
                raw['state'], raw['reason'] = 'busy', 'account_busy'
            except MachineOffline:
                raw['state'] = 'offline'
            except (BrokerUnavailable, BrokerError):
                raw['state'] = 'error'
        # This is the authority barrier: no await between refreshed authority/redaction and return/yield.
        fresh = binding(request, identifier)
        if (fresh['key'], fresh['run_id']) != (current['key'], current['run_id']):
            raise HTTPException(409, 'The session changed. Reload Chat.')
        if not isinstance(raw, dict) or raw.get('state') not in (
                'unknown', 'empty', 'ready', 'unsupported', 'offline', 'error', 'gap', 'busy'):
            raise HTTPException(502, 'The account sent an unreadable Chat result.')
        source = raw.get('source')
        valid_source = source is None or (isinstance(source, dict) and set(source) == {'identity', 'end', 'digest'}
            and type(source['end']) is int and 0 <= source['end'] <= (1 << 63) - 1
            and all(isinstance(source[k], str) and len(source[k]) == 64
                    and all(c in '0123456789abcdef' for c in source[k]) for k in ('identity', 'digest')))
        rows = raw.get('items')
        if (not valid_source or not isinstance(rows, list) or len(rows) > limit
                or type(raw.get('omitted')) is not int or not 0 <= raw['omitted'] <= 1_000_000
                or type(raw.get('reset')) is not bool):
            raise HTTPException(502, 'The account sent an unreadable Chat result.')
        notices = raw.get('notices', {'tools': 0})
        if (type(raw.get('more', False)) is not bool or raw.get('reason') not in (
                None, 'account_busy', 'discovery_budget', 'source_changed', 'prefix_verification', 'time_budget',
                'byte_budget', 'partial_record', 'page_limit', 'record_budget')
                or not isinstance(notices, dict) or set(notices) != {'tools'}
                or type(notices['tools']) is not int or not 0 <= notices['tools'] <= 1_000_000):
            raise HTTPException(502, 'The account sent an unreadable Chat result.')
        for item in rows:
            if (not isinstance(item, dict) or set(item) != {'id', 'revision', 'text', 'role', 'position', 'end'}
                    or not 0 <= _text_size(item['text']) <= 128 << 10
                    or item['role'] not in ('user', 'assistant') or source is None
                    or type(item['position']) is not int or type(item['end']) is not int
                    or not 0 <= item['position'] < item['end'] <= source['end']
                    or any(not isinstance(item[k], str) or len(item[k]) != 64
                           or any(c not in '0123456789abcdef' for c in item[k]) for k in ('id', 'revision'))):
                raise HTTPException(502, 'The account sent an unreadable Chat result.')
        if raw.get('before') is not None and (type(raw['before']) is not int or source is None
                                              or not 0 <= raw['before'] <= source['end']):
            raise HTTPException(502, 'The account sent an unreadable Chat result.')
        with ctx.db() as db:
            mask = ctx.supervisor.vault.redactor(db, fresh['keys'])
            safe_items = []
            for item in rows:
                text = mask.text(item['text'])
                safe_items.append({**item, 'text': text,
                    'id': hashlib.sha256((current['key'] + item['id']).encode()).hexdigest(),
                    'revision': hashlib.sha256(text.encode()).hexdigest()})
            result = {**raw, 'items': safe_items}
        # Project broker data; no arbitrary metadata or path is a browser escape hatch.
        items = []
        for item in result.get('items', [])[:limit]:
            if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant') or not isinstance(item.get('text'), str):
                continue
            items.append({k: item[k] for k in ('id', 'revision', 'role', 'text') if k in item})
        if len(json.dumps(items).encode()) > 256 << 10:
            raise HTTPException(502, 'Chat page exceeded its safe size. Open Terminal.')
        failed = result['state'] in ('error', 'offline', 'busy')
        source = None if failed else result.get('source')
        if failed:
            items = []
        offset = source['end'] if source else 0
        # Broker end is the committed high-water mark, including explicitly omitted
        # records; it never crosses a deferred page-limit record.
        if saved and saved.get('source') is not None and not follow and not result.get('reset') and source is not None:
            source = saved['source']
            offset = saved['after']
        # Failure is not a new source observation. Keep BOTH independent offsets
        # and the exact caller token (including expiry) until continuity is proven.
        no_observation = source is None or result.get('reason') == 'prefix_verification'
        retained = token if saved and no_observation and (failed or not result.get('reset')) else None
        has_position = source is not None or bool(retained and saved.get('source') is not None)
        reset = (bool(result.get('reset')) and not failed) or not has_position
        value = {'v': 1, 'key': current['key'], 'run': current['run_id'], 'source': source,
                 'after': offset, 'before': None if failed else result.get('before'), 'expires': int(time.time()) + 3600}
        return {'session_id': identifier, 'run_id': current['run_id'], 'state': result['state'],
                'items': items, 'historical_run_attribution': 'unknown', 'observed_at': utcnow().isoformat() if source is not None and result.get('reason') != 'prefix_verification' else None,
                'omitted': result.get('omitted', 0), 'reset': reset,
                'more': bool(result.get('more')), 'notices': result.get('notices', {'tools': 0}),
                'reason': result.get('reason'),
                'cursor': retained or encode(value),
                'before_cursor': (retained if saved.get('before') is not None else None) if retained else (
                    encode(value) if result.get('before') is not None else None),
                'capabilities': {'read_history': result['state'] not in ('unsupported', 'unknown', 'error', 'offline', 'busy'),
                                 'follow_messages': result['state'] not in ('unsupported', 'unknown', 'error', 'offline', 'busy'),
                                 'rich_tools': False, 'send_text': False, 'send_multiline': False,
                                 'attach_image': False, 'attach_document': False, 'model_effort': False}}

    @routes.get('/api/sessions/{acc_id}/chat')
    async def chat(request: Request, acc_id: str, limit: int = Query(100, ge=1, le=200),
                   before: str | None = Query(None, max_length=4096), who=Depends(ctx.principal)):
        current = binding(request, acc_id)
        admit(who.username)
        await write_in_thread(audit, who.username, 'chat.read', acc_id, current['run_id'])
        return JSONResponse(await page(request, acc_id, limit, before), headers={'Cache-Control': 'no-store'})

    @routes.get('/api/sessions/{acc_id}/chat/events')
    async def events(request: Request, acc_id: str, after: str | None = Query(None, max_length=4096),
                     wait: float = Query(25, ge=0, le=25), who=Depends(ctx.principal)):
        current = binding(request, acc_id)
        admit(who.username)
        after = request.headers.get('last-event-id') or after
        if after:
            decode(after, current)
        # Reserve before the first await; reclaim excess shares for other people.
        ticket, stopped = waiting.acquire(who.username)
        closed = False

        async def close():
            nonlocal closed
            if closed:
                return
            closed = True
            waiting.release(ticket)
            await write_in_thread(audit, who.username, 'chat.close', acc_id, current['run_id'])

        try:
            await write_in_thread(audit, who.username, 'chat.open', acc_id, current['run_id'])
            first = await page(request, acc_id, 100, after, follow=True)
        except BaseException:
            await close()
            raise

        async def stream():
            started = time.monotonic()
            result = first
            try:
                while not stopped.is_set():
                    # Response construction is not stream authorization. Recheck before the first chunk too.
                    fresh = binding(request, acc_id)
                    if (fresh['key'], fresh['run_id']) != (current['key'], current['run_id']):
                        return
                    yield 'id: ' + result['cursor'] + '\nevent: chat\ndata: ' + json.dumps(result, ensure_ascii=True) + '\n\n'
                    if time.monotonic() - started >= wait or result['reset']:
                        return
                    await asyncio.sleep(.5 if current['host'] == 'local' else 2)
                    result = await page(request, acc_id, 100, result['cursor'], follow=True)
            except HTTPException:
                # Never emit a cached chunk after access ends or the run changes.
                return
            finally:
                await close()

        return StreamingResponse(stream(), media_type='text/event-stream', background=BackgroundTask(close),
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    return routes
