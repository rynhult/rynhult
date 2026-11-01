"""Slice 4: opt-in durable deliveries and session-authenticated structured reports.

No agent HTTP credentials, shell execution, grant mutation or automatic re-paste.
The sessions router owns this module's worker lifespan; other slices use its outbox.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, func, or_, select, update
from sqlalchemy.orm import Mapped, defer, mapped_column

from acc import providers
from acc.broker_protocol import response_from
from acc.server import attention
from acc.server.models import AuditLog, Base, Event, Session, Team, TeamRequest, User, UTCDateTime
from acc.server.orchestrator.switch import slice_enabled
from acc.server.tty import _frame, _read_frame

TERMINAL = frozenset({'acknowledged', 'failed', 'expired'})
MAX_BYTES = 65536
TTL = timedelta(minutes=15)
# A delivery's deadlines follow its progress (a key written, terminal output read), not the clock alone, so a long
# task on a busy machine isn't failed while the provider is still echoing it.
STALL_SECONDS = 15.0  # nothing moved for this long: the delivery fails (submission_timeout)
QUIET_SECONDS = 4.0   # after Enter, no acknowledgement and no terminal output for this long: acceptance unknown
LIMIT_SECONDS = 120.0  # a delivery never takes longer than this in all


class Progress:
    """When a delivery last moved, and the deadline that follows it (STALL_SECONDS after, at most LIMIT_SECONDS in all)."""

    def __init__(self, deadline=None):
        self.loop = asyncio.get_running_loop()
        self.start = self.at = self.loop.time()
        self.deadline = deadline

    def moved(self):
        self.at = self.loop.time()
        if self.deadline is not None:
            self.deadline.reschedule(min(self.at + STALL_SECONDS, self.start + LIMIT_SECONDS))

    def quiet_for(self) -> float:
        return self.loop.time() - self.at


def now():
    return datetime.now(timezone.utc)


def enabled():
    return slice_enabled('ACC_ORCHESTRATOR_V2_DELIVERY')


def refuse(message):
    from acc.server.supervisor import LaunchRefused
    raise LaunchRefused(message)


class MessageDelivery(Base):
    __tablename__ = 'message_delivery'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    session_id: Mapped[str] = mapped_column(ForeignKey('session.id'), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey('orch_attempt.id'), index=True)
    actor: Mapped[str] = mapped_column(String(80))
    preview: Mapped[str] = mapped_column(String(200))
    text_sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int]
    body: Mapped[str] = mapped_column(Text)  # vault-redacted; never returned; erased on terminal states
    state: Mapped[str] = mapped_column(String(16), default='queued', index=True)
    ack: Mapped[str | None] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    deliver_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TaskReport(Base):
    __tablename__ = 'task_report'
    __table_args__ = (UniqueConstraint('attempt_id', 'version', name='uq_task_report_attempt_version'),)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey('orch_attempt.id'), index=True)
    version: Mapped[int] = mapped_column(default=1)
    findings_md: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(ForeignKey('session.id'), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id'), index=True)
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text)
    changed: Mapped[list] = mapped_column(JSON, default=list)
    tests: Mapped[list] = mapped_column(JSON, default=list)
    branch: Mapped[str | None] = mapped_column(String(160))
    head: Mapped[str | None] = mapped_column(String(64))
    reviewed_head: Mapped[str | None] = mapped_column(String(64))
    reviewed_branch: Mapped[str | None] = mapped_column(String(160))
    handoff_path: Mapped[str | None] = mapped_column(Text)
    needs: Mapped[list] = mapped_column(JSON, default=list)
    needs_ingested: Mapped[bool] = mapped_column(default=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


# The keys that clear a provider's input line before typing, reviewed per provider (security review 2026-10-25, OV-11;
# threat review S4-3). Esc twice is "rewind or edit a previous message" in Claude Code and Codex, and no provider is
# verified to need Esc to clear a suggestion, so none gets it: a fixed C-u clears the line everywhere. Add a key here
# only with a test that pins it and a note of how it was verified.
CLEAR_KEYS = {'claude': (b'\x15',), 'codex': (b'\x15',), 'copilot': (b'\x15',), 'cursor': (b'\x15',),
              'antigravity': (b'\x15',), 'opencode': (b'\x15',)}


def clear_keys(provider):
    return CLEAR_KEYS.get(provider, (b'\x15',))


def validate_text(text):
    if (not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_BYTES
            or re.search(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', text)
            or text.lstrip()[0] in '!/#'):
        refuse('message must be bounded plain text, without controls or a command prefix (!, /, #)')
    return re.sub(r'[\t\n]', ' ', text).strip()


NOTICES = {
    'superseded_run_mode': 'Not sent: a newer run-mode change replaced this notice before the lead resumed.',
    'input_conflict': 'Not delivered: the input could not be cleared. Clear it in the terminal before sending again.',
    'terminal_in_use': 'Not delivered: another window controls the terminal. Close it, then retry.',
    'prompt_blocked': 'Not delivered yet: the agent is busy or needs an answer in its terminal. Delivery will retry automatically.',
}


def public(row):
    result = {name: getattr(row, name) for name in ('id', 'session_id', 'attempt_id', 'actor', 'preview', 'text_sha256', 'bytes',
            'state', 'ack', 'attempts', 'error', 'created_at', 'updated_at', 'deliver_after', 'expires_at')}
    result['notice'] = NOTICES.get(row.error) or (
        'Not delivered or submission unconfirmed. Check the terminal before sending again.'
        if row.state in ('failed', 'expired') else None)
    if row.state == 'queued' and result['notice'] is None:
        result['notice'] = 'Not delivered yet: waiting for the agent to accept input. Queued messages retry automatically.'
    return result


def report_public(row):
    return {name: getattr(row, name) for name in ('session_id', 'task_id', 'team_id', 'attempt_id', 'version', 'findings_md', 'status', 'summary', 'changed',
            'tests', 'branch', 'head', 'reviewed_head', 'reviewed_branch', 'handoff_path', 'needs', 'received_at')}


def event(db, session, kind, payload):
    db.add(Event(session_id=session.id, source='orchestrator', kind=kind, payload=payload))


def transition(db, row, state, *, ack=None, error=None):
    row.state, row.ack, row.error, row.updated_at = state, ack, error, now()
    if state in TERMINAL:
        row.body = ''
    event(db, db.get(Session, row.session_id), 'team.delivery', {'id': row.id, 'session': row.session_id, 'state': state})
    from . import scope_notices
    scope_notices.refresh(db, db.get(Session, row.session_id))
    if state in ('failed', 'expired'):
        db.add(AuditLog(actor=row.actor, action='delivery.failed', target=row.session_id,
                        detail={'delivery': row.id, 'error': error}))


def enqueue(supervisor, session_id, actor, text):
    if not enabled():
        refuse('delivery queue is disabled')
    validate_text(text)
    with supervisor.db() as db:
        session = supervisor._get(db, session_id)
        if session.management != 'acc' or session.desired_state != 'running':
            refuse('delivery requires a running managed session')
        from .assignments import current
        attempt = current(db, session_id)
        row = enqueue_in_transaction(db, supervisor, session, actor, text, attempt_id=attempt.id if attempt else None)
        db.commit()
        return public(row)


def enqueue_in_transaction(db, supervisor, session, actor, text, *, attempt_id=None):
    validate_text(text)
    count = len(list(db.scalars(select(MessageDelivery.id).where(
        MessageDelivery.session_id == session.id, MessageDelivery.state.not_in(TERMINAL)).limit(21))))
    if count >= 20:
        refuse('session delivery queue is full')
    if supervisor is None:
        refuse('queued text must pass the vault redactor')  # no unredacted path, even for Rynhult's own notices
    text = supervisor.vault.redactor(db, session.vault_keys).text(text)
    validate_text(text)
    row = MessageDelivery(session_id=session.id, attempt_id=attempt_id, actor=actor, preview=text[:200], body=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), bytes=len(text.encode()), expires_at=now() + TTL)
    db.add(row)
    db.flush()
    event(db, session, 'team.delivery', {'id': row.id, 'session': session.id, 'attempt': attempt_id, 'state': 'queued'})
    return row


def recover(supervisor):
    with supervisor.db() as db:
        for row in db.scalars(select(MessageDelivery).where(MessageDelivery.state.in_(('typing', 'submitted')))):
            transition(db, row, 'failed', error='interrupted_uncertain')
        db.commit()


def runnable(session):
    return (session.management == 'acc' and session.desired_state == 'running'
            and (session.observed_state == 'idle' or session.provider == 'copilot' and session.observed_state == 'waiting_input'))


def authorized(db, session, actor):
    if actor.startswith('session:'):
        caller = db.get(Session, actor[8:])
        return (caller is not None and caller.desired_state == 'running'
                and caller.team_id == session.team_id and caller.owner == session.owner
                and child(db, caller, session.id) is not None)
    if actor in ('@api', 'supervisor'):
        return True
    person = db.scalar(select(User).where(User.username == actor))
    return person is not None and (person.role == 'owner' or session.owner == actor)


async def safety(supervisor, session_id, writer_token=None, actor=None):
    with supervisor.db() as db:
        session = supervisor._get(db, session_id)
        if not runnable(session) or actor is not None and not authorized(db, session, actor):
            return False
        if session.team_id:
            team = db.get(Team, session.team_id)
            if not team or team.stopped or (team.expires_at is not None and now() >= team.expires_at):
                return False
        account, host, name, provider = session.run_as, session.host, session.tmux_name, session.provider
    writers = getattr(supervisor, '_delivery_writers', None)
    if writers and writers.by_session.get(session_id) not in (None, writer_token):
        return False  # never erase a person's partially typed input
    screen = await supervisor.broker.call('screen_prompt', account=account, host=host, tmux_name=name, provider=provider)
    return isinstance(screen, dict) and screen.get('prompt', 'unknown') is None and screen.get('working') is not True


async def ingest(supervisor, session_id):
    with supervisor.db() as db:
        session = supervisor._get(db, session_id)
        account, host = session.run_as, session.host
    if host != 'local':
        await supervisor._fetch_host_spools(host, [(session_id, account)])
    with supervisor.db() as db:
        supervisor._ingest(db, supervisor._get(db, session_id))
        db.commit()


async def hook_ack(supervisor, session_id, marker, line):
    await ingest(supervisor, session_id)
    with supervisor.db() as db:
        session = supervisor._get(db, session_id)
        provider = providers.get(session.provider)
        for item in db.scalars(select(Event).where(Event.session_id == session_id, Event.id > marker[0],
                                                   Event.source == f'{session.provider}_hook', Event.verified.is_(True))):
            hook = provider.normalize_hook(item.payload)
            prompt = hook.get('prompt')
            if (hook.get('hook_event_name') == 'UserPromptSubmit' and isinstance(prompt, str)
                    and re.sub(r'[\t\n]', ' ', prompt).strip() == line):
                return True
        # The shared Event feed deliberately trims strings to 2,000 characters.
        # Confirm a long prompt from the bounded private spool, without changing
        # another slice's ingest code or storing the untrimmed prompt in events.
        from acc.server.ingest import read_spool_file, spool_path
        chunk = read_spool_file(spool_path(supervisor.settings, session), marker[1], 1024 * 1024) or b''
        complete = chunk.rpartition(b'\n')[0]
        redact = supervisor.vault.redactor(db, session.vault_keys)
        for raw in complete.splitlines():
            try:
                record = json.loads(raw)
                if not isinstance(record, dict) or record.get('acc_session') != session_id or record.get('provider') != session.provider:
                    continue
                hook = record.get('hook')
                if not isinstance(hook, dict):
                    continue
                hook = {**hook, 'hook_event_name': hook.get('hook_event_name') or record.get('event')}
                hook = provider.normalize_hook(redact.deep(hook))
                prompt = hook.get('prompt')
                native = hook.get('session_id')
                if native is not None and native != session.native_id:
                    continue
                if (hook.get('hook_event_name') == 'UserPromptSubmit' and isinstance(prompt, str)
                        and re.sub(r'[\t\n]', ' ', prompt).strip() == line):
                    return True
            except (ValueError, UnicodeError, RecursionError):
                continue
    return False


def screen_ack(before, after, line):
    # Positive empty-input evidence only, never "no error" or echo alone. Joined
    # terminal captures retain soft-wrapped input as one row. Unknown UI fails closed.
    rows = after.rstrip().splitlines()
    return (bool(rows) and rows[-1].strip() in ('>', '❯', '›')
            and after.count(line) > before.count(line)
            and not re.search(r'\[(?:Pasted\b|Paste #\d+)', after, re.I))


class DeliveryWriter:
    def __init__(self):
        self.closed = False

    async def close(self, **kwargs):
        self.closed = True  # a person explicitly took terminal control


def defer_delivery(supervisor, identifier, reason):
    with supervisor.db() as db:
        row = db.get(MessageDelivery, identifier)
        if row.state == 'queued':
            row.error = reason
            row.deliver_after = now() + timedelta(seconds=2)
            db.commit()


def retry(db, row):
    # Only a never-typed queue entry is safe to retry automatically. Uncertain
    # submissions must be inspected, never pasted a second time.
    if row.state != 'queued':
        refuse('Check the terminal before sending a new message; this delivery may already have been submitted.')
    row.deliver_after = now()
    row.error = None
    db.commit()
    return public(row)


class InputConflict(ValueError):
    pass


def input_conflict(screen):
    # Fail closed for a visible live prompt that still holds text after Ctrl-U.
    # Do not infer emptiness from transcript echo or from a submission timeout.
    rows = [line.rstrip() for line in screen.splitlines() if line.strip()][-8:]
    for line in reversed(rows):
        if match := re.fullmatch(r'\s*(?:[│┃]\s*)?[>❯›](?:[ \t]+(.*?))?(?:\s*[│┃])?', line):
            return bool((match[1] or '').strip())
    return False


async def _deliver(supervisor, identifier, progress=None):
    from acc.server.broker_client import peer_uid
    progress = progress or Progress()
    writer, reader_task = None, None
    writers = getattr(supervisor, '_delivery_writers', None)
    writer_token = DeliveryWriter()
    session_id = None
    try:
        with supervisor.db() as db:
            row = db.get(MessageDelivery, identifier)
            session_id, text, actor = row.session_id, row.body, row.actor
        if writers:
            acquired, displaced = writers.claim(session_id, writer_token, False)
            if not acquired:
                defer_delivery(supervisor, identifier, 'terminal_in_use')
                return
        if not await safety(supervisor, session_id, writer_token, actor):
            defer_delivery(supervisor, identifier, 'prompt_blocked')
            return
        await supervisor.cancel_automation(session_id)
        await ingest(supervisor, session_id)
        with supervisor.db() as db:
            row = db.scalar(select(MessageDelivery).where(MessageDelivery.id == identifier).with_for_update())
            if row.state != 'queued' or row.actor != actor:
                return
            # A queued nudge/scope notice may have coalesced while safety awaited the broker.
            # Claim and send the same body; an old-body ACK must never cover a new version.
            text = row.body
            session = supervisor._get(db, session_id)
            account, host, name, provider_id = session.run_as, session.host, session.tmux_name, session.provider
            marker = (db.scalar(select(Event.id).where(Event.session_id == session_id).order_by(Event.id.desc()).limit(1)) or 0,
                      session.spool_offset)
            # Durable claim before the first key. A crash from here is never replayed.
            claimed = db.execute(update(MessageDelivery).where(MessageDelivery.id == identifier,
                MessageDelivery.state == 'queued').values(state='typing', error=None, updated_at=now())).rowcount
            if not claimed:
                return
            event(db, session, 'team.delivery', {'id': identifier, 'session': session_id, 'state': 'typing'})
            db.commit()
        authority = getattr(supervisor.broker, 'authority', None)
        with authority.activity() if authority else nullcontext():
            reader, writer = await asyncio.wait_for(supervisor.broker.open_stream(account, host), 5)
            if host == 'local':
                trusted = supervisor.broker.get(account, host).trusted_uids
                if trusted is not None and peer_uid(writer) not in trusted:
                    raise ValueError('untrusted broker')
            writer.write(json.dumps({'id': 1, 'method': 'attach', 'params':
                {'tmux_name': name, 'readonly': False, 'rows': 30, 'cols': 120}}).encode() + b'\n')
            await writer.drain()
            hello = response_from(await asyncio.wait_for(reader.readline(), 5), 1, attach=True)
            if not hello.get('ok'):
                raise ValueError('attach refused')
            ready = asyncio.Event()
            async def drain():
                while True:
                    kind, data = await _read_frame(reader)
                    if kind == b'x':
                        raise ConnectionError('terminal ended')
                    if kind == b'd' and data:
                        ready.set()
                    progress.moved()  # the terminal changed: the provider is still taking the text in
            reader_task = asyncio.create_task(drain())
            # The JSON handshake acknowledges spawning tmux, not that it is
            # attached. Wait for its initial redraw before sending editing keys.
            await asyncio.wait_for(ready.wait(), 5)
            async def keys(data):
                with supervisor.db() as db:
                    pending = db.get(MessageDelivery, identifier)
                    if pending.state in TERMINAL:
                        raise ValueError('delivery authority ended')
                    if pending.attempt_id:
                        from .assignments import current
                        active_attempt = current(db, session_id)
                        if active_attempt is None or active_attempt.id != pending.attempt_id:
                            raise ValueError('delivery assignment changed')
                if writer_token.closed or writers and writers.by_session.get(session_id) is not writer_token:
                    raise ValueError('person took terminal control')
                if reader_task.done():
                    reader_task.result()
                    raise ConnectionError('terminal ended')
                writer.write(_frame(b'd', data))
                await asyncio.wait_for(writer.drain(), 2)
                progress.moved()
            # Recheck immediately before each clearing key and before typing, including a newly raised prompt.
            for key in clear_keys(provider_id):
                if not await safety(supervisor, session_id, writer_token, actor):
                    raise ValueError('prompt or state changed')
                await keys(key)
                await asyncio.sleep(.1)
            if not await safety(supervisor, session_id, writer_token, actor):
                raise ValueError('prompt or state changed')
            before = await supervisor.broker.call('terminal_output', account=account, host=host, tmux_name=name)
            before = before['text']
            # A slow TUI may not have redrawn the clearing key yet.
            for _ in range(10):
                if not input_conflict(before):
                    break
                await asyncio.sleep(.1)
                before = (await supervisor.broker.call('terminal_output', account=account, host=host, tmux_name=name))['text']
            if input_conflict(before):
                raise InputConflict('input was not cleared')
            if not await safety(supervisor, session_id, writer_token, actor):
                raise ValueError('prompt or state changed')
            line = validate_text(text)
            await keys(line.encode('utf-8'))
            await asyncio.sleep(1 if provider_id == 'codex' else .3)
            for attempt in (1, 2):
                if not await safety(supervisor, session_id, writer_token, actor):
                    raise ValueError('prompt or state changed')
                with supervisor.db() as db:
                    row = db.get(MessageDelivery, identifier)
                    if row.state not in ('typing', 'submitted'):
                        raise ValueError('delivery authority ended')
                    row.attempts = attempt
                    transition(db, row, 'submitted')
                    db.commit()
                await keys(b'\r')
                ack = None
                # Wait while the terminal still moves; give up only after QUIET_SECONDS without output.
                while progress.quiet_for() < QUIET_SECONDS:
                    await asyncio.sleep(.2)
                    if await hook_ack(supervisor, session_id, marker, line):
                        ack = 'hook:UserPromptSubmit'
                        break
                    after = await supervisor.broker.call('terminal_output', account=account, host=host, tmux_name=name)
                    if screen_ack(before, after['text'], line):
                        ack = 'screen:input-cleared'
                        break
                if ack is None:
                    if attempt == 1 and await safety(supervisor, session_id, writer_token, actor):
                        last = after['text'].rstrip().splitlines()
                        if last and providers.get(provider_id).input_pending(last[-1], line):
                            continue  # one Enter-only retry, only with positive pending-input evidence
                    raise ValueError('acceptance unknown')
                with supervisor.db() as db:
                    row = db.get(MessageDelivery, identifier)
                    if row.state in TERMINAL:
                        return
                    transition(db, row, 'acknowledged', ack=ack)
                    db.add(AuditLog(actor=row.actor, action='session.message', target=session_id,
                                    detail={'delivery': row.id}))
                    db.commit()
                return
    except asyncio.CancelledError:
        with supervisor.db() as db:
            row = db.get(MessageDelivery, identifier)
            if row and row.state not in TERMINAL:
                transition(db, row, 'failed', error='interrupted_uncertain')
                db.commit()
        raise
    except Exception as error:
        with supervisor.db() as db:
            row = db.get(MessageDelivery, identifier)
            if row and row.state not in TERMINAL:
                transition(db, row, 'failed', error='input_conflict' if isinstance(error, InputConflict) else 'submission_unconfirmed')
                db.commit()
    finally:
        if writers and session_id:
            writers.release(session_id, writer_token)
        if reader_task:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        if writer:
            writer.close()
            await writer.wait_closed()


async def deliver(supervisor, identifier):
    try:
        async with asyncio.timeout(STALL_SECONDS) as deadline:
            await _deliver(supervisor, identifier, Progress(deadline))
    except TimeoutError:
        with supervisor.db() as db:
            row = db.get(MessageDelivery, identifier)
            if row and row.state not in TERMINAL:
                transition(db, row, 'failed', error='submission_timeout')
                db.commit()


async def process_once(supervisor):
    from acc.server.db import WRITE_INTENT
    authority = getattr(supervisor.broker, 'authority', None)
    lock = supervisor.__dict__.setdefault('_delivery_lock', asyncio.Lock())
    async with lock:
        with authority.activity() if authority else nullcontext():
            token = WRITE_INTENT.set(False)
            try:
                with supervisor.db() as db:
                    mode_resume = ((MessageDelivery.actor == 'supervisor')
                        & MessageDelivery.preview.startswith(RUN_MODE_ORIGIN)
                        & (Session.desired_state == 'stopped') & (Session.id == Session.team_id)
                        & Team.stopped.is_(False) & (or_(Team.expires_at.is_(None), Team.expires_at > now())))
                    stale = list(db.scalars(select(MessageDelivery).join(Session).outerjoin(Team, Team.id == Session.team_id).where(
                        MessageDelivery.state == 'queued', or_(MessageDelivery.expires_at <= now(),
                        (Session.desired_state != 'running') & ~func.coalesce(mode_resume, False))).limit(100)))
                    for row in stale:
                        expired = row.expires_at <= now()
                        transition(db, row, 'expired' if expired else 'failed',
                                   error='queue_expired' if expired else 'session_stopped')
                    # One head per session, so a busy/offline queue cannot bury another
                    # session's delivery. Later messages never jump a deferred head.
                    heads = select(MessageDelivery.id.label('id'), func.row_number().over(
                        partition_by=MessageDelivery.session_id,
                        order_by=(MessageDelivery.created_at, MessageDelivery.id)).label('position')).where(
                            MessageDelivery.state == 'queued').subquery()
                    rows = list(db.scalars(select(MessageDelivery).join(heads, heads.c.id == MessageDelivery.id)
                        .join(Session).where(heads.c.position == 1, MessageDelivery.deliver_after <= now(),
                            Session.desired_state == 'running', or_(Session.observed_state == 'idle',
                            (Session.provider == 'copilot') & (Session.observed_state == 'waiting_input')))
                        .order_by(MessageDelivery.created_at, MessageDelivery.id).limit(4)))
                    identifiers = []
                    for row in rows:
                        if not authorized(db, db.get(Session, row.session_id), row.actor):
                            transition(db, row, 'failed', error='authority_revoked')
                        else:
                            identifiers.append(row.id)
                    db.commit()
                await asyncio.gather(*(deliver(supervisor, key) for key in identifiers))
            finally:
                WRITE_INTENT.reset(token)


def start_worker(supervisor):
    task = getattr(supervisor, '_delivery_worker', None)
    if task is None or task.done():
        async def run():
            from acc.server.db import WRITE_INTENT
            token = WRITE_INTENT.set(False)
            authority = getattr(supervisor.broker, 'authority', None)
            recovered = False
            async def reports():
                while enabled():
                    try:
                        with authority.activity() if authority else nullcontext():
                            await collect_missing_reports(supervisor)
                    except Exception:
                        pass  # no raw broker or report values in logs
                    await asyncio.sleep(5)
            reporter = asyncio.create_task(reports())
            try:
                while enabled():
                    try:
                        with authority.activity() if authority else nullcontext():
                            if not recovered:
                                recover(supervisor)
                                recovered = True
                            await process_once(supervisor)
                    except Exception:
                        await asyncio.sleep(.5)
                    await asyncio.sleep(.25)
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
                WRITE_INTENT.reset(token)
        supervisor._delivery_worker = asyncio.create_task(run())


async def stop_worker(supervisor):
    task = getattr(supervisor, '_delivery_worker', None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        supervisor._delivery_worker = None


def lifespan(supervisor, writers):
    @asynccontextmanager
    async def context(app):
        supervisor._delivery_writers = writers
        if enabled():
            start_worker(supervisor)
        try:
            yield
        finally:
            await stop_worker(supervisor)
    return context


def caller_team(supervisor, db, caller_id, action=None):
    if not enabled():
        refuse('structured delivery and reports are disabled')
    caller = supervisor._get(db, caller_id)
    team = db.get(Team, caller.team_id) if caller.team_id else None
    from .verification import allowed
    if not allowed(team, caller, action):
        refuse('team is stopped, expired, or caller is not running')
    return caller, team


def child(db, caller, helper):
    from acc.server.teams import descendants
    return next((item for item in descendants(db, caller) if item.id == helper), None)


def origin_line(caller):
    """Rynhult's own line in front of every agent-originated message, so the receiving agent can tell a team message from
    its person. The sender can't remove it (security review 2026-10-25; threat review S4-5, design correction 3)."""
    role = 'team lead' if caller.parent_session_id is None else 'helper'
    name = re.sub(r'[^\w .-]', '', caller.name or '')[:64].strip() or 'unnamed'
    return f'[Message from the {role} "{name}", an agent of this team, not from your person]: '


# Review 2026-10-26 (L-2): the person's answers and edits wake an idle lead. Rynhult's own words, never the person's
# text: the lead reads what changed with its tools.
NUDGE_ORIGIN = '[From Rynhult, not from your person or an agent]: '
RUN_MODE_ORIGIN = '[Run-mode change from Rynhult]: '
NUDGE_WHAT = {'answer': 'answered a card on the working list', 'plan': 'edited the plan', 'now': 'changed NOW'}


def run_mode_notice(db, supervisor, team_id, mode, version):
    """A committed mode change waits for the stopped lead's resume; ordinary stopped-session messages still fail."""
    for old in db.scalars(select(MessageDelivery).where(MessageDelivery.session_id == team_id,
        MessageDelivery.actor == 'supervisor', MessageDelivery.state == 'queued',
        MessageDelivery.preview.startswith(RUN_MODE_ORIGIN))):
        transition(db, old, 'expired', error='superseded_run_mode')
    continuation = ('Continue: plan_ready when the plan is complete, then start helpers for unblocked phases.'
        if mode == 'build' else 'Continue read-only discovery and planning. Do not start implementation helpers.')
    return enqueue_in_transaction(db, supervisor, db.get(Session, team_id), 'supervisor',
        f'{RUN_MODE_ORIGIN}Plan version {version}. Run mode is now {mode}. {continuation}')


def nudge_lead(db, team_id, what):
    """Queue person-edit notices or a separate system-origin proposal correction, in the write transaction.
    Coalesce only an unclaimed queued line: an in-flight delivery may already have caused the
    lead to read its tools before its acknowledgement arrives. Keep one follow-up for later changes, delivered at
    the next idle prompt, rather than assuming the current turn will see them."""
    if not enabled():
        return None
    lead, team = db.get(Session, team_id), db.get(Team, team_id)
    if (lead is None or team is None or team.stopped or (team.expires_at is not None and now() >= team.expires_at)
            or lead.management != 'acc' or lead.desired_state != 'running'):
        return None
    origin = NUDGE_ORIGIN + ('No difference found between current and proposed. ' if what == 'proposal' else 'your person ')
    pending = db.scalar(select(MessageDelivery).where(
        MessageDelivery.session_id == team_id, MessageDelivery.actor == 'supervisor',
        MessageDelivery.state == 'queued', MessageDelivery.preview.startswith(origin)).limit(1))
    if what == 'proposal':
        text = origin + 'Review the corrected proposal notes with worklist. No approval was requested or recorded.'
    else:
        changed = [words for key, words in NUDGE_WHAT.items() if key == what or pending is not None and words in pending.body]
        said = ' and '.join([', '.join(changed[:-1]), changed[-1]] if len(changed) > 1 else changed)
        text = NUDGE_ORIGIN + f'your person {said}. Read the working list and the plan (worklist, plan) before you go on.'
    if pending is None:
        pending = MessageDelivery(session_id=team_id, actor='supervisor', expires_at=team.expires_at or (now() + TTL))
        db.add(pending)
    pending.body, pending.preview, pending.bytes = text, text[:200], len(text.encode())
    pending.text_sha256, pending.updated_at = hashlib.sha256(text.encode()).hexdigest(), now()
    db.flush()
    event(db, lead, 'team.delivery', {'id': pending.id, 'session': team_id, 'state': 'queued'})
    return pending


HELPER_NOTICE = NUDGE_ORIGIN + 'Helper updates: '


def nudge_parent(db, *, team_id, parent_id, name, status, assignment=None, attempt=None, version=None):
    """Transactional outbox notice, with a fixed two-second batching window.

    Only unclaimed rows can change. Report bodies and failure reasons never enter
    the notice; bounded metadata tells the parent which evidence to read.
    """
    if not enabled() or not parent_id:
        return None
    parent, team = db.get(Session, parent_id), db.get(Team, team_id)
    if (parent is None or team is None or parent.team_id != team_id or team.stopped
            or (team.expires_at is not None and now() >= team.expires_at) or parent.management != 'acc' or parent.desired_state != 'running'):
        return None
    name = re.sub(r'[^\w .-]', '', name or '')[:64].strip() or 'unnamed'
    line = f'helper {name} {status} for assignment {assignment or "unassigned"} (attempt {attempt or "none"}, version {version or 0})'
    pending = db.scalar(select(MessageDelivery).where(
        MessageDelivery.session_id == parent_id, MessageDelivery.actor == 'supervisor',
        MessageDelivery.state == 'queued', MessageDelivery.preview.startswith(HELPER_NOTICE)).limit(1))
    suffix = '. Read it with the team tools and continue.'
    lines = pending.body[len(HELPER_NOTICE):-len(suffix)].split('; ') if pending else []
    if line not in lines:
        if len(lines) < 16:
            lines.append(line)
        elif 'more helper updates available' not in lines:
            lines.append('more helper updates available')
    text = HELPER_NOTICE + '; '.join(lines) + suffix
    if pending is None:
        pending = MessageDelivery(session_id=parent_id, actor='supervisor', expires_at=team.expires_at or (now() + TTL),
                                  deliver_after=now() + timedelta(seconds=2))
        db.add(pending)
    pending.body, pending.preview, pending.bytes = text, text[:200], len(text.encode())
    pending.text_sha256, pending.updated_at = hashlib.sha256(text.encode()).hexdigest(), now()
    db.flush()
    event(db, parent, 'team.delivery', {'id': pending.id, 'session': parent_id, 'state': 'queued'})
    return pending


def wake(supervisor):
    """Start the delivery worker from a route's worker thread (it runs from startup; this covers a stopped one)."""
    import anyio.from_thread
    try:
        anyio.from_thread.run_sync(start_worker, supervisor)
    except RuntimeError:
        pass  # not in a worker thread of the event loop


async def team_message(supervisor, caller_id, payload):
    from acc.server.nodes import node_lines
    identifier = payload.get('delivery')
    if not identifier:
        validate_text(payload.get('text'))
        text = node_lines(payload['text'], MAX_BYTES)
        validate_text(text)
    with supervisor.db() as db:
        caller, team = caller_team(supervisor, db, caller_id)
        helper = child(db, caller, payload.get('helper'))
        if helper is None:
            refuse('helper is outside caller subtree')
        helper_id, origin = helper.id, origin_line(caller)
        if identifier:
            row = db.get(MessageDelivery, identifier)
            if row is None or row.session_id != helper.id or row.actor != f'session:{caller_id}':
                refuse('no such delivery from this sender to that helper')
            result = retry(db, row)
            start_worker(supervisor)
            return {'helper': helper_id, 'delivery': result}
    row = enqueue(supervisor, helper_id, f'session:{caller_id}', origin + text)
    start_worker(supervisor)
    return {'helper': helper_id, 'delivery': row}


async def team_status(supervisor, caller_id, payload):
    with supervisor.db() as db:
        caller, team = caller_team(supervisor, db, caller_id)
        helper = child(db, caller, payload.get('helper'))
        if helper is None:
            refuse('helper is outside caller subtree')
        result = await supervisor.team_summary(helper)
        result['deliveries'] = [public(row) for row in db.scalars(select(MessageDelivery).where(
            MessageDelivery.session_id == helper.id).order_by(MessageDelivery.created_at.desc()).limit(20))]
        report = latest_report(db, helper.id)
        result['report'] = report_public(report) if report else None
        return supervisor.vault.redactor(db, helper.vault_keys).deep(result)


def latest_report(db, session_id):
    from . import assignments
    attempt = assignments.current(db, session_id)
    query = select(TaskReport).where(TaskReport.session_id == session_id)
    if attempt:
        query = query.where(TaskReport.attempt_id == attempt.id)
    return db.scalar(query.order_by(TaskReport.version.desc(), TaskReport.received_at.desc()).limit(1))


def bounded(value, limit, name):
    if not isinstance(value, str) or len(value.encode()) > limit or '\x00' in value:
        refuse(f'invalid report {name}')
    return value


def path(value):
    bounded(value, 1000, 'path')
    parts = PurePosixPath(value).parts
    # Any control, bidi or zero-width character refuses the path: rewriting it would name another file (review OV-9).
    if (not value or value.startswith('/') or '\\' in value or '..' in parts
            or any(unicodedata.category(c).startswith('C') for c in value)):
        refuse('report paths must be relative')
    return value


def report_fields(payload):
    if payload.get('status') not in ('done', 'blocked', 'failed', 'partial'):
        refuse('invalid report status')
    summary = bounded(payload.get('summary'), 4096, 'summary')
    findings = payload.get('findings_md')
    if findings is not None:
        bounded(findings, 65536, 'findings Markdown')
    changed, tests, needs = (payload.get(key, []) for key in ('changed', 'tests', 'needs'))
    if (not isinstance(changed, list) or len(changed) > 200 or not isinstance(tests, list) or len(tests) > 50
            or not isinstance(needs, list) or len(needs) > 20):
        refuse('report arrays exceed their bounds')
    for item in changed:
        if not isinstance(item, dict) or set(item) != {'path', 'kind'} or item['kind'] not in ('added', 'modified', 'deleted', 'renamed'):
            refuse('invalid report change')
        path(item['path'])
    for item in tests:
        if not isinstance(item, dict) or set(item) != {'command', 'passed', 'failed', 'skipped'}:
            refuse('invalid report test')
        bounded(item['command'], 1024, 'test command')
        if any(type(item[key]) is not int or not 0 <= item[key] <= 1000000 for key in ('passed', 'failed', 'skipped')):
            refuse('invalid report test counts')
    for item in needs:
        if not isinstance(item, dict) or set(item) != {'title', 'steps'}:
            refuse('needs require title and steps')
        bounded(item['title'], 200, 'need title')
        bounded(item['steps'], 16384, 'need steps')
    if len(json.dumps([summary, changed, tests, needs]).encode()) > MAX_BYTES:
        refuse('report exceeds total bound')
    # Shown to the person and relayed to the lead: no control, bidi or zero-width characters (review OV-9).
    from acc.server.nodes import node_lines, node_text
    return {'findings_md': None if findings is None else node_lines(findings, 65536), 'status': payload['status'], 'summary': node_lines(summary, 4096), 'changed': changed,
            'tests': [{**item, 'command': node_text(item['command'], 1024)} for item in tests],
            'needs': [{'title': node_text(item['title'], 200), 'steps': node_lines(item['steps'], 16384)} for item in needs]}


def task_id(db, session):
    if session.parent_session_id is None:
        return session.id
    for request in db.scalars(select(TeamRequest).where(TeamRequest.session_id == session.parent_session_id)):
        result = (request.result or {}).get('result')
        if request.payload.get('action') == 'start' and isinstance(result, dict) and result.get('id') == session.id:
            return request.id
    refuse('no authenticated start request for this task')


async def report_metadata(supervisor, session_id):
    with supervisor.db() as db:
        session = supervisor._get(db, session_id)
        from .assignments import current
        attempt = current(db, session_id)
        handoff_id = attempt.id if attempt else session_id
        metadata = {'branch': session.worktree_branch, 'head': None, 'handoff_path': None}
        params = {'account': session.run_as, 'host': session.host,
                  'path': session.cwd if session.container and session.desired_state != 'archived' else session.team_repo or session.project.workspace_path, 'branch': session.worktree_branch}
    if metadata['branch']:
        result = await supervisor.broker.call('team_inspect', **params)
        commits = result.get('commits', [])
        if commits and re.fullmatch(r'[0-9a-f]{40,64}', commits[0].get('sha', '')):
            metadata['head'] = commits[0]['sha']
        notes = await supervisor.broker.call('team_handoffs', helper=handoff_id, **params)
        if notes:
            candidate = path(notes[0]['path'])
            if candidate.startswith('docs/handoffs/') and handoff_id[:8] in candidate:
                metadata['handoff_path'] = candidate
    return metadata


async def team_report(supervisor, caller_id, payload):
    from . import assignments
    if 'attempt' in payload:
        assignments.identifier(payload['attempt'], 'attempt')
    fields = report_fields(payload)
    from .verification import review_fields
    try:
        fields.update(review_fields(payload))
    except ValueError as error:
        refuse(str(error))
    with supervisor.db() as db:
        caller, team = caller_team(supervisor, db, caller_id, 'report')
        attempt = assignments.current(db, caller.id)
        attempt_id = attempt.id if attempt else None
        if attempt and fields['status'] == 'failed':
            refuse('assignment reports use partial, blocked or done; process failures are recorded separately')
        if payload.get('attempt') and payload['attempt'] != attempt_id:
            refuse('report attempt is not the caller current attempt')
        if attempt and (closed := assignments.replaced(db, attempt)):
            refuse(f'{closed}; no report stored')
        if attempt and not payload.get('attempt') and attempt.brief.get('delivery_id'):
            brief = db.get(MessageDelivery, attempt.brief['delivery_id'])
            if brief is None or brief.state != 'acknowledged':
                refuse('new assignment brief not acknowledged; name the received attempt explicitly')
        from .selection import Selection
        selection = (db.get(Selection, attempt.selection_id) if attempt else
                     db.scalar(select(Selection).where(Selection.session_id == caller.id).order_by(Selection.created_at.desc()).limit(1)))
        if selection and selection.need.get('task_type') in ('review', 'security_review') and not fields['reviewed_head']:
            refuse('review reports must name the reviewed branch and head SHA')
        if fields['reviewed_branch'] == caller.worktree_branch and fields['reviewed_branch']:
            refuse('a review must come from a separate reviewer session')
        if fields['reviewed_branch'] and not db.scalar(select(Session.id).where(
                Session.team_id == team.id, Session.worktree_branch == fields['reviewed_branch'])):
            refuse('reviewed branch is outside this team')
        if fields['reviewed_branch'] or fields['reviewed_head']:
            # Only a review task can certify a revision, and only its named author's branch (review L-B1):
            # a lead or implementer can't mark a sibling "reviewed at this revision".
            need = selection.need if selection else {}
            if need.get('task_type') not in ('review', 'security_review'):
                refuse('only a review task can name a reviewed branch and head; report your own work without them')
            author = db.get(Session, need.get('author_session') or '')
            if author is None or author.team_id != team.id or author.worktree_branch != fields['reviewed_branch']:
                refuse("the reviewed branch must be the author's branch named in your review task")
        task = None if attempt else task_id(db, caller)
        if not attempt and db.get(TaskReport, (caller_id, task)):
            refuse('a report already exists for this task')
    try:
        async with asyncio.timeout(8):
            metadata = await report_metadata(supervisor, caller_id)
    except Exception:
        refuse('report metadata unavailable; no report stored')
    with supervisor.db() as db:
        caller, team = caller_team(supervisor, db, caller_id, 'report')
        attempt = assignments.current(db, caller.id)
        if (attempt.id if attempt else None) != attempt_id:
            refuse('assignment changed while report metadata was collected')
        version = 1
        if attempt:
            attempt = db.scalar(select(assignments.Attempt).where(assignments.Attempt.id == attempt.id).with_for_update())
            if closed := assignments.replaced(db, attempt):
                refuse(f'{closed}; no report stored')
            version = (db.scalar(select(func.max(TaskReport.version)).where(TaskReport.attempt_id == attempt.id)) or 0) + 1
            if version > 100:
                refuse('attempt report version limit reached')
            task = f'{attempt.id}:{version}'
        elif db.get(TaskReport, (caller_id, task)):
            refuse('a report already exists for this task')
        values = supervisor.vault.redactor(db, caller.vault_keys).deep({**fields, **metadata})
        row = TaskReport(session_id=caller.id, team_id=team.id, task_id=task,
                         attempt_id=attempt_id, version=version, **values)
        db.add(row)
        if attempt:
            attempt.branch, attempt.head, attempt.handoff_path = row.branch, row.head, row.handoff_path
            attempt.state = {'done': 'reported', 'partial': 'running', 'blocked': 'blocked', 'failed': 'failed'}[row.status]
            assignments.report_state(db.get(assignments.Assignment, attempt.assignment_id), row.status)
        event(db, caller, 'team.member.report', {'id': caller.id, 'status': row.status, 'attempt': attempt_id, 'version': version})
        db.add(AuditLog(actor=f'session:{caller.id}', action='report.received', target=team.id,
                        detail={'session': caller.id, 'task': task, 'attempt': attempt_id, 'version': version, 'status': row.status}))
        nudge_parent(db, team_id=team.id, parent_id=caller.parent_session_id, name=caller.name,
                     status=f'reported {row.status}', assignment=attempt.assignment_id if attempt else None,
                     attempt=attempt_id, version=version)
        attention.resolve(db, f'team_report_missing:{caller.id}')
        db.commit()
        return report_public(row)


def pending_needs(db, team_id):
    """Slice 1 transactional outbox: consume needs and mark in the SAME transaction.

    The worklist owns conversion/triage; this module never edits worklist tables.
    """
    return list(db.scalars(select(TaskReport).where(TaskReport.team_id == team_id,
                         TaskReport.needs_ingested.is_(False)).options(defer(TaskReport.findings_md), defer(TaskReport.changed), defer(TaskReport.tests)).order_by(TaskReport.received_at).limit(100)))


def mark_needs_ingested(row):
    row.needs_ingested = True


async def collect_missing_reports(supervisor):
    from .assignments import current
    checked = supervisor.__dict__.setdefault('_delivery_report_checked', set())
    retries = supervisor.__dict__.setdefault('_delivery_report_retries', {})
    with supervisor.db() as db:
        ids = list(db.scalars(select(Session.id).where(Session.team_id.is_not(None),
            Session.parent_session_id.is_not(None), Session.observed_state.in_(('stopped', 'exited', 'archived', 'failed')))))
    visited = 0
    for identifier in ids:
        with supervisor.db() as db:
            attempt = current(db, identifier)
            attempt_id = attempt.id if attempt else None
            key = (identifier, attempt_id)
            if key in checked or retries.get(key, now()) > now():
                continue
            predicate = TaskReport.attempt_id == attempt_id if attempt else TaskReport.session_id == identifier
            if db.scalar(select(TaskReport).where(predicate)):
                checked.add(key)
                continue
        visited += 1
        if visited > 20:
            break
        try:
            async with asyncio.timeout(5):
                metadata = await report_metadata(supervisor, identifier)
        except Exception:
            retries[key] = now() + timedelta(seconds=30)
            continue
        with supervisor.db() as db:
            session = db.get(Session, identifier)
            attempt = current(db, identifier)
            if (attempt.id if attempt else None) != attempt_id or db.scalar(select(TaskReport).where(predicate)):
                continue
            if metadata['handoff_path']:
                task = f'{attempt_id}:1' if attempt else task_id(db, session)
                metadata = supervisor.vault.redactor(db, session.vault_keys).deep(metadata)
                db.add(TaskReport(session_id=session.id, team_id=session.team_id, task_id=task, attempt_id=attempt_id,
                    version=1, status='partial', summary='Committed handoff found; no structured completion claim was supplied.', **metadata))
                event(db, session, 'team.member.report', {'id': session.id, 'attempt': attempt_id, 'version': 1, 'status': 'partial'})
                db.add(AuditLog(actor='supervisor', action='report.received', target=session.team_id,
                    detail={'session': session.id, 'task': task, 'attempt': attempt_id, 'status': 'partial', 'source': 'handoff'}))
                attention.resolve(db, f'team_report_missing:{session.id}')
                checked.add(key)
            else:
                attention.raise_item(db, f'team_report_missing:{session.id}', kind='needs_input', source='orchestrator',
                    title=f'{session.name[:80]} finished without a report', session_id=session.team_id)
                retries[key] = now() + timedelta(seconds=30)
            db.commit()


def on_state(db, session, previous, state):
    if not enabled() or state not in ('stopped', 'exited', 'archived', 'failed', 'recovery_needed'):
        return
    if previous != state and session.parent_session_id:
        from . import assignments
        attempt = assignments.current(db, session.id)
        if attempt is None or attempt.state != 'reported':
            nudge_parent(db, team_id=session.team_id, parent_id=session.parent_session_id, name=session.name,
                         status='failed' if state in ('failed', 'recovery_needed') else 'stopped unexpectedly',
                         assignment=attempt.assignment_id if attempt else None, attempt=attempt.id if attempt else None)
    for row in db.scalars(select(MessageDelivery).where(MessageDelivery.state.not_in(TERMINAL),
        or_(MessageDelivery.session_id == session.id, MessageDelivery.actor == f'session:{session.id}'))):
        transition(db, row, 'failed', error='session_stopped' if row.session_id == session.id else 'authority_revoked')


# Deployment switch is read at import/startup; off preserves all M17 seams.
TEAM_FIELDS = {'report': {'attempt', 'status', 'summary', 'changed', 'tests', 'needs', 'findings_md', 'reviewed_head', 'reviewed_branch'},
                   'message': {'helper', 'text', 'delivery'}, 'status': {'helper'}}
TEAM_ACTIONS = {'report': team_report, 'message': team_message, 'status': team_status}
ON_STATE = [on_state]
