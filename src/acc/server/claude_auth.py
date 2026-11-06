"""Session-scoped auth observations and the automation admission rule."""
import time

from acc.providers.claude_auth import ALLOWED, DOCS, MESSAGE, detail, refusal

from .models import Observation
from .supervisor_common import LaunchRefused


class AutomationRefused(LaunchRefused, ValueError):
    code = 'claude_subscription_automation'
    docs = DOCS

    def __init__(self):
        super().__init__(f'{MESSAGE} {DOCS}')


def _fresh(row):
    return row.value.get('mode', 'unknown') if row and time.time() - row.observed_at < 60 else 'unknown'


def mode(db, session):
    return _fresh(db.get(Observation, f'claude.auth:{session.id}'))


def modes(db, sessions):
    """One query for a list of sessions: {session id: mode} for Claude sessions."""
    from sqlalchemy import select
    keys = [f'claude.auth:{session.id}' for session in sessions if session.provider == 'claude']
    rows = {row.key: row for row in db.scalars(select(Observation).where(Observation.key.in_(keys)))} if keys else {}
    return {key.split(':', 1)[1]: _fresh(rows.get(key)) for key in keys}


def store(db, session, value):
    db.merge(Observation(key=f'claude.auth:{session.id}', value={'mode': value if value in ALLOWED | {'subscription'} else 'unknown'},
                         source='broker_auth_status', observed_at=time.time()))


def require(db, session):
    if refusal(session.provider, mode(db, session)):
        raise AutomationRefused()


def public(db, session, known=None):
    value = (known.get(session.id, 'unknown') if known is not None else mode(db, session)) if session.provider == 'claude' else None
    return {'mode': value, 'automation_refusal': detail(session.project_id) if refusal(session.provider, value) else None}


async def refresh(supervisor, db, session):
    if session.provider != 'claude':
        return
    routing = dict(account=session.run_as, host=session.host, tmux_name=session.tmux_name)
    # End the read snapshot before broker I/O; reconcile can commit while it runs.
    db.commit()
    try:
        answer = await supervisor.broker.call('session_auth', **routing)
        value = answer.get('auth_mode', 'unknown')
    except Exception:
        value = 'unknown'
    store(db, session, value)
    db.flush()
