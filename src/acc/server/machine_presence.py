"""Durable machine presence policy. Offline links are never probed by this policy."""
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from acc.remote_access_helper import revision

from . import attention
from .models import AuditLog

log = logging.getLogger(__name__)


def policy_revision(node) -> str:
    return revision({
        'node_id': node.id, 'role': node.role, 'offline_threshold_seconds': node.offline_threshold_seconds,
        'idle_sleep_minutes': node.idle_sleep_minutes, 'downtime_policy': node.downtime_policy,
        'downtime_until_reconnect': node.downtime_until_reconnect,
        'public_git_backup': bool(getattr(node, 'public_git_backup', False)),
        'snoozed_until': node.snoozed_until.isoformat() if node.snoozed_until else None,
    })


def planned_downtime(node, at: datetime | None = None) -> dict:
    """Half-open local weekly intervals; ambiguous ends use the second occurrence.

    Nonexistent DST times shift forward by the gap. Overlapping/adjacent windows
    form one continuous interval. An indefinite policy deliberately has no ETA.
    """
    empty = {'active': False, 'until_reconnect': False, 'ends_at': None}
    if node is None:
        return empty
    if node.downtime_until_reconnect:
        return {'active': True, 'until_reconnect': True, 'ends_at': None}
    policy = node.downtime_policy
    if not policy or not policy.get('windows'):
        return empty
    at = at or datetime.now(timezone.utc)
    zone = ZoneInfo(policy['timezone'])
    today = at.astimezone(zone).date()
    # Heartbeats must not consume a window. Last-seen is only a fallback for a
    # machine with no recorded offline-to-online transition yet.
    seen = node.last_reconnected_at or node.last_seen
    intervals = []
    for window in policy['windows']:
        if not window.get('until_reconnect'):
            continue
        # One week back includes the previous occurrence when today's start is
        # still ahead. Compare UTC instants so a repeated start never rearms it.
        starts = [datetime.combine(today - timedelta(days=offset), time.fromisoformat(window['start']), zone)
                  .astimezone(timezone.utc) for offset in range(8)
                  if (today - timedelta(days=offset)).weekday() in window['days']]
        latest = max((start for start in starts if start <= at), default=None)
        if latest is not None and (seen is None or seen < latest):
            return {'active': True, 'until_reconnect': True, 'ends_at': None}
    for offset in range(-1, 9):
        day = today + timedelta(days=offset)
        for window in policy['windows']:
            if window.get('until_reconnect') or day.weekday() not in window['days']:
                continue
            start, end = time.fromisoformat(window['start']), time.fromisoformat(window['end'])
            end_day = day + timedelta(days=int(end <= start))
            a = datetime.combine(day, start, zone).astimezone(timezone.utc)
            # fold=1 chooses the later end when clocks repeat. For a gap, fold=0
            # shifts the nonexistent boundary forward rather than backwards.
            wall_end = datetime.combine(end_day, end, zone)
            b0, b1 = wall_end.astimezone(timezone.utc), wall_end.replace(fold=1).astimezone(timezone.utc)
            b = max(b0, b1)
            intervals.append((a, b))
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    for a, b in merged:
        if a <= at < b:
            # A week with no gap has no scheduled return; the search horizon
            # must never be presented as a real window boundary.
            end = None if b > at + timedelta(days=8) else b
            return {'active': True, 'until_reconnect': False, 'ends_at': end}
    return empty


def sleep_due(session, node, at: datetime) -> bool:
    if (node is None or node.status != 'active' or node.role != 'workstation' or not node.idle_sleep_minutes
            or session.management != 'acc' or session.desired_state != 'running'
            or session.observed_state != 'idle' or not session.native_id or session.idle_since is None):
        return False
    since = max(t for t in (session.idle_since, session.last_output_at, session.activity_at) if t is not None)
    return (at - since).total_seconds() >= node.idle_sleep_minutes * 60


def default_role(info: dict) -> str:
    text = ' '.join(str(info.get(k, '')) for k in ('os', 'release', 'platform')).lower()
    return 'workstation' if str(info.get('wsl', '')).lower() in ('true', '1', '2') or any(s in text for s in ('darwin', 'mac', 'windows', 'wsl', 'microsoft')) else 'always_on'


def connected(broker, host: str) -> bool:
    if host == 'local':
        return not bool(getattr(broker, 'authority', None))
    hub = getattr(broker, 'hub', None)
    link = hub.links.get(host) if hub else None
    return bool(link and not link.closed)


def connected_hosts(broker) -> list[str]:
    hub = getattr(broker, 'hub', None)
    return [name for name in ['local', *(hub.links if hub else [])] if connected(broker, name)]


def transition(db, node, online: bool, at: datetime | None = None, *, connection_open: bool = False):
    at = at or datetime.now(timezone.utc)
    key = f'machine_offline:{node.name}'
    if online:
        if connection_open:
            # An authenticated new link is reconnect evidence even if a controller
            # restart prevented recording the outage. Ordinary heartbeats are not.
            node.last_reconnected_at = at
            node.downtime_until_reconnect = False
            if node.offline_since is None:
                node.last_downtime_seconds = None
                db.add(AuditLog(actor='supervisor', action='node.reconnect', target=node.name,
                                detail={'downtime_seconds': None, 'offline_observed': False}))
        if node.offline_since is not None:
            downtime = max(0, int((at - node.offline_since).total_seconds()))
            node.last_downtime_seconds = downtime
            node.last_reconnected_at = at
            node.downtime_until_reconnect = False
            node.offline_since = None
            node.snoozed_until = None
            log.info('machine %s reconnected after %s seconds offline', node.name, downtime)
            db.add(AuditLog(actor='supervisor', action='node.reconnect', target=node.name,
                            detail={'downtime_seconds': downtime}))
            item = attention._open(db, key)
            if item:
                item.detail = f'Machine reconnected after {downtime} seconds offline.'
                attention.resolve(db, key)
        node.last_seen = at
    elif node.offline_since is None:
        node.offline_since = at
        log.info('machine %s disconnected', node.name)
        db.add(AuditLog(actor='supervisor', action='node.offline', target=node.name))


def evaluate(db, node, at: datetime | None = None):
    at = at or datetime.now(timezone.utc)
    key = f'machine_offline:{node.name}'
    if node.status != 'active' or node.role != 'always_on' or node.offline_since is None or planned_downtime(node, at)['active']:
        attention.resolve(db, key)
        return
    item = attention._open(db, key)
    if item:
        item.snoozed_until = node.snoozed_until
    if node.snoozed_until and node.snoozed_until > at:
        return
    seconds = max(0, int((at - node.offline_since).total_seconds()))
    if seconds < node.offline_threshold_seconds:
        attention.resolve(db, key)
        return
    if item:
        return
    attention.raise_item(db, key, kind='machine_offline', severity='warn', source='machine link',
                         title=f'{node.name} is offline (always-on machine)',
                         detail=f'Offline since {node.offline_since:%Y-%m-%d %H:%M} UTC. Check that it is powered on and connected to '
                                'the network; this alert clears when it reconnects. For planned maintenance, snooze it in Host → Machines.')
    db.add(AuditLog(actor='supervisor', action='node.offline_alert', target=node.name,
                    detail={'offline_seconds': seconds}))
