"""A native-only prompt is a handoff notice, never a remote decision."""
from acc.session_relay import protocol as p

from .. import attention
from ..db import write_intent
from ..models import RelayOwnership, RelayRun, RelaySession, Session
from . import store

MESSAGE = 'Claude is asking in the terminal; answer it there'
SOURCE = 'claude_terminal'


def current(db, session_id):
    row = db.get(RelaySession, session_id)
    run = db.get(RelayRun, row.current_run_id) if row and not row.sealed else None
    if run and run.identity.get('nativePrompt') == {'kind': 'overwrite', 'runId': str(run.run_id)}:
        return {'kind': 'overwrite', 'message': MESSAGE, 'remoteAllow': False,
            'terminalUrl': f'/sessions/{session_id}?tab=terminal'}
    return None


def refuse_allow(run, params):
    if params.get('choiceId') == 'accept':
        with run.ctx.db() as db:
            if current(db, run.session_id):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)


@write_intent
def retire(run):
    with run.ctx.db() as db:
        row = db.get(RelaySession, run.session_id)
        original = db.get(RelayRun, run.run_id)
        if original and original.identity.get('nativePrompt'):
            identity = dict(original.identity)
            identity.pop('nativePrompt', None)
            original.identity = identity
            if row and row.current_run_id == run.run_id and row.sealed:
                item = attention._open(db, f'needs_input:{run.session_id}')
                if item and item.source == SOURCE:
                    attention.resolve(db, item.dedupe_key)
            db.commit()


@write_intent
def retire_inactive(run):
    with run.ctx.db() as db:
        if current(db, run.session_id) is None:
            item = attention._open(db, f'needs_input:{run.session_id}')
            if item and item.source == SOURCE:
                attention.resolve(db, item.dedupe_key)
                db.commit()


@write_intent
def publish(run, params):
    if (params.keys() != {'kind','active','runId'} or params['kind'] != 'native.prompt'
            or type(params['active']) is not bool or params['runId'] != str(run.run_id) or run.provider != 'claude'):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    with run.ctx.db() as db:
        row, _ = run.check_run(db)
        owned = db.get(RelayOwnership, run.session_id)
        original = db.get(RelayRun, run.run_id)
        if (not owned or owned.view != 'terminal' or owned.phase != 'owned' or owned.source_run_id != run.run_id or not original
                or original.binding_id != run.binding_id):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        active = params['active']
        marker = {'kind': 'overwrite', 'runId': str(run.run_id)}
        if (original.identity.get('nativePrompt') == marker) == active:
            return True
        identity = dict(original.identity)
        session = db.get(Session, run.session_id)
        if active:
            identity['nativePrompt'] = marker
            session.observed_state = 'waiting_input'
            item = attention.raise_item(db, f'needs_input:{run.session_id}', kind='needs_input',
                source=SOURCE, session_id=run.session_id, title=f'{session.name} needs your input',
                detail=MESSAGE, verified=False)
            item.source, item.verified = SOURCE, False
            item.acked_at = item.snoozed_until = None
        else:
            identity.pop('nativePrompt', None)
            item = attention._open(db, f'needs_input:{run.session_id}')
            if item and item.source == SOURCE:
                attention.resolve(db, item.dedupe_key)
            if session.observed_state == 'waiting_input':
                session.observed_state = 'active'
        original.identity = identity
        store.append(db, row, 'session.stateChanged', {'state': session.observed_state,
            'nativePrompt': current(db, run.session_id)})
        db.commit()
    return True
