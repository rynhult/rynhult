"""Read-only quota hints. Never fetch credentials, poll providers, or borrow another account's readings."""
import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ... import providers
from ..models import API_PRINCIPAL, Observation, Session, User

DEFAULTS = {'enabled': False, 'max_used_percent': 90, 'max_age_minutes': 10}


def number(value):
    try:
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def timestamp(value):
    numeric = number(value)
    if numeric is not None:
        return numeric
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return date.timestamp() if date.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def reading(kind, value, observed_at, *, now, max_age_seconds):
    unknown = {'status': 'unknown'}
    if not isinstance(value, dict) or value.get('error'):
        return unknown
    at = number(observed_at)
    if at is None or at > now:
        return unknown
    if kind == 'claude_cache':
        fetched = number(value.get('fetched_at'))
        if fetched is None or fetched > now:
            return unknown
        at = min(at, fetched)
    if now - at > max_age_seconds:
        return {'status': 'stale'}
    if kind in ('claude_statusline', 'claude_cache'):
        field = 'used_percentage' if kind == 'claude_statusline' else 'utilization'
        rows = [(value.get(key), field) for key in ('five_hour', 'seven_day')]
    elif kind == 'codex':
        rows = [(value.get(key), 'used_percent') for key in ('primary', 'secondary')]
    elif kind == 'copilot':
        credits = value.get('credits')
        if not isinstance(credits, dict) or credits.get('unlimited'):
            return unknown
        left = number(credits.get('percent_remaining'))
        rows = [({'used': 100-left, 'resets_at': value.get('resets_at')}, 'used')] if left is not None else []
    else:
        return unknown
    used = []
    for row, field in rows:
        if not isinstance(row, dict):
            continue
        percent, reset = number(row.get(field)), timestamp(row.get('resets_at'))
        if percent is not None and percent >= 0 and reset is not None and reset > now:
            used.append(min(100., percent))
    if not used:
        return unknown
    worst = max(used)
    return {'status': 'fresh', 'used_percent': worst, 'headroom_percent': 100-worst, 'observed_at': at}


def worse(old, new):
    """Of two readings for one machine and provider, the one to trust: fresh over stale over unknown, and of two fresh
    ones the higher usage. Within a window usage only grows, and a window that reset is never fresh. Statusline files
    (content and time) are written by the agent account, so a newer reading must never hide a higher one."""
    order = {'unknown': 0, 'stale': 1, 'fresh': 2}
    if order[new['status']] != order[old['status']]:
        return new if order[new['status']] > order[old['status']] else old
    return new if new['status'] == 'fresh' and new['used_percent'] > old['used_percent'] else old


def snapshot(db, account, machines, agents, default_account, policy, now, *, owner):
    rules = policy.get('quota', DEFAULTS)
    if not rules['enabled']:
        return {}
    result = {(machine, agent): {'status': 'unknown'} for machine in machines for agent in agents}
    if owner is None:
        return result
    kwargs = {'now': now, 'max_age_seconds': rules['max_age_minutes']*60}
    if 'claude' in agents:
        rows = db.execute(select(Observation, Session).join(Session, Observation.key == 'claude.statusline.'+Session.id)
                          .where(Session.owner == owner, Session.run_as == account, Session.provider == 'claude',
                                 Observation.observed_at <= now)  # a future time (a touched file) can't fill the limit
                          .options(joinedload(Session.project), joinedload(Session.placement))
                          .order_by(Observation.observed_at.desc()).limit(256))
        for obs, session in rows:
            machine = session.host
            if machine in machines:
                result[machine, 'claude'] = worse(result[machine, 'claude'], reading('claude_statusline', obs.value.get(
                    'rate_limits') if isinstance(obs.value, dict) else None, obs.observed_at, **kwargs))
    # Singleton plan usage has the same owner-only visibility as /api/providers.
    if (account == default_account and 'local' in machines
            and (owner == API_PRINCIPAL or db.scalar(select(User.id).where(
                User.username == owner, User.role == 'owner')) is not None)):
        for agent, kind in [('claude', 'claude_cache'), ('codex', 'codex'), ('copilot', 'copilot')]:
            if agent not in agents:
                continue
            provider = providers.get(agent)
            if 'usage' not in provider.supports:
                continue
            obs = db.get(Observation, provider.usage_observation)
            if obs and isinstance(obs.value, dict) and obs.value.get('_scope') == {'account': account, 'machine': 'local'}:
                result['local', agent] = worse(result['local', agent], reading(kind, obs.value, obs.observed_at, **kwargs))
    return result
