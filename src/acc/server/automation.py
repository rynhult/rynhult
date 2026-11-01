"""Durable, bounded session continuations. No model judges goal completion."""
from __future__ import annotations

import re
import time
import uuid

from sqlalchemy import text

from ..providers.limits import LIMITS, UNSUPPORTED
from ..providers.limits import capability as capability
from ..providers.limits import limit_signal as limit_signal
from . import budgets
from .models import AuditLog


def reset_time(provider, windows, fallback):
    exhausted = [w['resets_at'] for w in windows if w['provider'] == provider and w['used'] >= 100 and not w.get('stale')]
    return max(exhausted) if exhausted else fallback


def validate(data, provider):
    if data.get('autoresume') and provider not in LIMITS:
        raise ValueError(UNSUPPORTED)
    if set(data) - {'autoresume', 'goal'}:
        raise ValueError('Unknown session automation setting.')
    if 'autoresume' in data and not isinstance(data['autoresume'], bool):
        raise ValueError('Auto-resume must be true or false.')
    goal = data.get('goal')
    if goal:
        if not isinstance(goal, dict) or not isinstance(goal.get('text'), str) or not 1 <= len(goal['text'].strip()) <= 4000:
            raise ValueError('Write a goal of 1–4000 characters.')
        for key, lower, upper in [('max_continuations', 1, 100), ('max_hours', .01, 168), ('max_usage', 1, 100)]:
            value = goal.get(key)
            if key == 'max_usage' and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ValueError(f'{key} must be between {lower} and {upper}.')
        if int(goal['max_continuations']) != goal['max_continuations']:
            raise ValueError('Maximum continuations must be a whole number.')
    return data


def audit(db, session, action, detail=None, actor='supervisor'):
    db.add(AuditLog(actor=actor, action=action, target=session.id, detail=detail or {}))


def configure(db, session, data, actor):
    validate(data, session.provider)
    if session.management != 'acc' or session.launch_spec.inputs.get('endpoint'):
        raise ValueError('Automation requires a managed session using its provider plan.')
    state = dict(session.automation or {})
    if session.observed_state == 'starting' and session.launch_spec.inputs.get('initial_prompt'):
        state['expect_initial'] = True
    if 'autoresume' in data:
        state['autoresume'] = bool(data['autoresume'])
        if not state['autoresume']:
            state.pop('waiting', None)
        audit(db, session, 'session.autoresume', {'enabled': state['autoresume']}, actor)
    if 'goal' in data:
        goal = data['goal']
        old = state.get('goal') or {}
        if goal:
            # Editing caps never replenishes an established goal's counter or deadline.
            state['goal'] = {k: goal.get(k) for k in ('text', 'max_continuations', 'max_hours', 'max_usage')}
            state['goal'].update(used=old.get('used', 0), started=old.get('started', time.time()),
                                 status='running', revision=uuid.uuid4().hex, marker_after=db.execute(text("SELECT coalesce(max(seq), -1) FROM transcript_fts WHERE session_id=:id"), {'id': session.id}).scalar())
        else:
            state['goal'] = {**old, 'status': 'stopped'} if old else None
        audit(db, session, 'session.goal.edit' if goal else 'session.goal.stop', {}, actor)
    session.automation = state


def cancel(db, session, reason='person typed'):
    state = dict(session.automation or {})
    if not state:
        return
    waiting = state.pop('waiting', None)
    if waiting:
        state['last_reset'] = max(state.get('last_reset', 0), waiting['reset'])
    state['cancelled_at'] = time.time()
    if state.get('goal') and state['goal'].get('status') == 'running':
        state['goal'] = {**state['goal'], 'status': reason}
        audit(db, session, 'session.goal.stop', {'reason': reason})
    session.automation = state


def decide(db, session, signal=None, assistant='', turn=None, now=None, usage_windows=None):
    """Update progress and return one claimed command, or None. Caller commits before dispatch."""
    now = time.time() if now is None else now
    state = dict(session.automation or {})
    if not state or session.desired_state != 'running':
        return None
    windows = budgets.windows(db) if usage_windows is None else usage_windows
    goal = dict(state.get('goal') or {})
    running = goal.get('status') == 'running'
    if running:
        marker = re.search(r'^GOAL:\s*(done|blocked(?:\s*:\s*[^\n]+)?)\s*$', assistant, re.M | re.I)
        fresh = turn is None or not str(turn).isdigit() or int(turn) > goal.get('marker_after', -1)
        reason = marker[1] if marker and fresh else None
        if session.observed_state == 'waiting_input':
            reason = 'needs your input'
        elif goal['used'] >= goal['max_continuations']:
            reason = 'continuation cap reached'
        elif now >= goal['started'] + goal['max_hours'] * 3600:
            reason = 'time cap reached'
        if not reason and goal.get('max_usage') is not None:
            readings = [w for w in windows if w['provider'] == session.provider and not w['stale']]
            if not readings:
                state['notice'] = 'Waiting for usage data to enforce the goal cap'
                session.automation = state
                return None
            if any(w['used'] >= goal['max_usage'] for w in readings):
                reason = 'usage cap reached'
        if reason:
            goal['status'] = reason
            running = False
            audit(db, session, 'session.goal.stop', {'reason': reason})
        state['goal'] = goal
    if signal is None:
        state['limit_seen'] = False
    if signal is not None and state.get('autoresume') and not state.get('waiting') and not state.get('limit_seen'):
        reset = reset_time(session.provider, windows, signal.get('reset'))
        # Old screen text must not recreate a consumed/cancelled reset.
        if reset and reset > state.get('last_reset', 0) and reset > state.get('cancelled_at', 0):
            state['limit_seen'] = True
            state['waiting'] = {'reset': reset, 'id': f'{session.id}:reset:{reset}'}
    if signal is not None and state.get('autoresume') and not state.get('waiting') and not state.get('last_reset'):
        state['notice'] = 'Limit reached; waiting for a reset time from usage data or the CLI'
    else:
        state.pop('notice', None)
    command = None
    waiting = state.get('waiting')
    if budgets.paused(db, session.owner or '@api', session.provider, project_id=session.project_id, machine=session.host, account=session.run_as):
        state['notice'] = 'Your budget pauses automated continuations'
    elif session.observed_state == 'idle':
        if waiting:
            if goal and not running:
                state['notice'] = 'Goal stopped; automatic continuation is stopped'
            elif now >= waiting['reset']:
                command = {'id': waiting['id'], 'text': 'Your usage limit has reset. Continue where you left off.'}
                state['last_reset'] = waiting['reset']
                state.pop('waiting')
        elif signal is None and running and turn and state.get('last_turn') != turn:
            command = {'id': f"{session.id}:goal:{goal['revision']}:{goal['used'] + 1}",
                       'text': f"Continue toward your goal: {goal['text'][:3000]}\nReport a standalone line GOAL: done when achieved, or GOAL: blocked: <why> when you need the person. Stay within the remaining {goal['max_continuations'] - goal['used']} continuations and {max(0, (goal['started'] + goal['max_hours'] * 3600 - now) / 3600):.2f} hours."}
        if command:
            state['last_turn'] = turn
            state['dispatch'] = {**command, 'status': 'claimed', 'at': now}
            state['expected_prompt'] = re.sub(r'\s+', ' ', command['text']).strip()
            if running:
                state['goal'] = {**goal, 'used': goal['used'] + 1}
            audit(db, session, 'session.autoresume' if waiting else 'session.goal.continue', {'id': command['id']})
    session.automation = state
    return command
