"""Person-owned durable schedule intent and timezone-aware calendar timing."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update

from . import attention
from .machine_presence import connected
from .models import AuditLog, Project, Schedule, ScheduleRun, Session, User, utcnow


def _field(value: str, low: int, high: int) -> set[int]:
    result = set()
    for part in value.split(','):
        match = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
        if not match:
            raise ValueError('cron uses five numeric fields, with lists, ranges and steps')
        base, step = match.groups()
        step = int(step or 1)
        if step < 1 or step > high + 1:
            raise ValueError('invalid cron step')
        if base == '*':
            start, end = low, high
        elif '-' in base:
            start, end = map(int, base.split('-'))
        else:
            start = int(base)
            end = high if match[2] else start
        if not low <= start <= end <= high:
            raise ValueError('cron field out of range')
        result.update(range(start, end + 1, step))
    return result


def _instant(local: datetime, zone: ZoneInfo) -> datetime | None:
    """First fold only; a gap is not a real local wall time."""
    instant = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    return instant if instant.astimezone(zone).replace(tzinfo=None) == local else None


def next_time(timing: dict, timezone_name: str, after: datetime) -> datetime | None:
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('choose an IANA timezone, such as America/Chicago') from None
    kind = timing.get('kind')
    if kind == 'once':
        try:
            local = datetime.fromisoformat(timing['at'])
            if local.tzinfo or local.second or local.microsecond:
                raise ValueError()
            instant = _instant(local, zone)
            if instant is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError('choose a real local datetime, to the minute, without a UTC offset') from None
        return instant if instant > after else None
    if kind in ('daily', 'weekly'):
        try:
            if not re.fullmatch(r'\d{2}:\d{2}', timing['time']):
                raise ValueError()
            clock = time.fromisoformat(timing['time'])
            weekday = timing.get('weekday')
            if kind == 'weekly' and (type(weekday) is not int or not 0 <= weekday <= 6):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError('choose HH:MM and a weekday from 0 (Sunday) to 6') from None
        minutes, hours = {clock.minute}, {clock.hour}
        days, months, weekdays = set(range(1, 32)), set(range(1, 13)), {weekday} if kind == 'weekly' else set(range(7))
        restricted_days, restricted_weekdays = False, kind == 'weekly'
    elif kind == 'cron':
        fields = str(timing.get('expression', '')).split()
        if len(fields) != 5:
            raise ValueError('cron needs five fields: minute hour day month weekday')
        minutes, hours, days, months, weekdays = [_field(f, lo, hi) for f, lo, hi in
                                                zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))]
        weekdays = {v % 7 for v in weekdays}
        restricted_days, restricted_weekdays = not fields[2].startswith('*'), not fields[4].startswith('*')
    else:
        raise ValueError('timing kind must be once, daily, weekly or cron')
    day = after.astimezone(zone).date()
    for offset in range(366 * 8):
        date = day + timedelta(days=offset)
        dom, dow = date.day in days, (date.weekday() + 1) % 7 in weekdays
        matches = (dom or dow) if restricted_days and restricted_weekdays else (dom and dow)
        if date.month not in months or not matches:
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                instant = _instant(datetime.combine(date, time(hour, minute)), zone)
                if instant is not None and instant > after:
                    return instant
    raise ValueError('timing has no occurrence within eight years')

ACTIVE = ('pending', 'claimed', 'running')


def audit(db, schedule, action, detail=None, actor=None):
    db.add(AuditLog(actor=actor or schedule.owner, action=f'schedule.{action}', target=schedule.id,
                    detail=detail or {}))


def enqueue(db, schedule, due, trigger='timer', status='pending', reason=None):
    run = ScheduleRun(id=uuid.uuid4().hex, schedule_id=schedule.id, due_at=due, trigger=trigger,
                      launch=schedule.launch, reserved_session_id=uuid.uuid4().hex[:12], status=status, reason=reason)
    db.add(run)
    if status == 'skipped':
        audit(db, schedule, 'skip', {'run_id': run.id, 'reason': reason})
    return run


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.recovered = False

    def finish(self, db, run, status, reason):
        run.status, run.reason = status, reason
        schedule = db.get(Schedule, run.schedule_id)
        audit(db, schedule, 'skip' if status == 'skipped' else 'run',
              {'run_id': run.id, 'status': status, 'reason': reason, 'session_id': run.session_id})
        if status == 'failed':
            attention.raise_item(db, f'schedule:{run.id}', owner=schedule.owner, kind='schedule_failed',
                                 title=f"Schedule failed: {run.launch['name']}", detail=reason,
                                 source='schedules', session_id=run.session_id)

    async def recover(self):
        """Called once before dispatch on startup; never repeat an ambiguous effect."""
        with self.ctx.db() as db:
            for run in db.scalars(select(ScheduleRun).where(ScheduleRun.status == 'claimed')):
                if run.session_id:
                    run.status = 'running'
                else:
                    self.finish(db, run, 'failed', 'Rynhult restarted during dispatch; inspect the project before Run now. No retry was made.')
            db.commit()
        self.recovered = True

    def materialize(self, now):
        with self.ctx.db() as db:
            for schedule in db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.deleted.is_(False),
                                                             Schedule.next_run_at <= now)):
                due = schedule.next_run_at
                # CAS consumes the timestamp and inserts intent in one transaction.
                following = next_time(schedule.timing, schedule.timezone, now)
                changed = db.execute(update(Schedule).where(Schedule.id == schedule.id, Schedule.next_run_at == due,
                                                            Schedule.enabled.is_(True), Schedule.deleted.is_(False))
                                     .values(next_run_at=following)).rowcount
                if not changed:
                    continue
                missed = (now - due).total_seconds() >= 60
                skip = missed and schedule.missed == 'skip'
                enqueue(db, schedule, due, status='skipped' if skip else 'pending',
                        reason='Missed occurrence skipped by your recovery policy' if skip else None)
            db.commit()

    def reconcile(self):
        with self.ctx.db() as db:
            for run in db.scalars(select(ScheduleRun).where(ScheduleRun.status == 'running')):
                session = db.get(Session, run.session_id)
                if session is None:
                    self.finish(db, run, 'failed', 'The scheduled session is missing')
                elif session.observed_state in ('recovery_needed', 'error', 'failed'):
                    self.finish(db, run, 'failed', session.last_error or 'Session needs recovery; inspect its session page')
                elif session.desired_state != 'running' or session.observed_state in ('stopped', 'exited', 'archived'):
                    self.finish(db, run, 'completed', 'Session ended')
            db.commit()

    async def dispatch(self, run_id):
        from .admin_features import enabled
        if not enabled(self.ctx.settings, 'schedules'):
            return
        from .api.context import Principal
        from .api.schemas import SessionIn
        with self.ctx.db() as db:
            # Pending -> claimed is a persisted, conditional claim, committed before broker calls.
            changed = db.execute(update(ScheduleRun).where(ScheduleRun.id == run_id, ScheduleRun.status == 'pending')
                                 .values(status='claimed')).rowcount
            if not changed:
                return
            run = db.get(ScheduleRun, run_id)
            schedule = db.get(Schedule, run.schedule_id)
            user = db.scalar(select(User).where(User.username == schedule.owner))
            project = db.get(Project, run.launch['project_id'])
            reason = None
            if schedule.deleted or not schedule.enabled and run.trigger == 'timer':
                reason = 'Schedule is paused or deleted'
            elif user is None or (user.account or self.ctx.settings.default_account) != schedule.account:
                reason = 'The schedule owner or agent account has changed'
            elif project is None or project.archived or project.account != schedule.account or (user.role != 'owner' and project.owner != user.username):
                reason = 'Project access or agent account has changed'
            elif not connected(self.ctx.broker, project.host or 'local'):
                reason = 'Machine offline; occurrence skipped. Nodes check in when they return.'
            else:
                # A failed run may still own an active ordinary session; failure is not permission to overlap it.
                others = list(db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule.id,
                                                                    ScheduleRun.id != run.id)))
                for other in others:
                    session = db.get(Session, other.session_id) if other.session_id else None
                    if other.status == 'claimed' or (session and session.desired_state == 'running' and
                                                    session.observed_state not in ('stopped', 'exited', 'archived')):
                        reason = 'A previous run is still active'
                        break
            if reason:
                self.finish(db, run, 'skipped', reason)
            else:
                audit(db, schedule, 'run', {'run_id': run.id, 'status': 'claimed'})
            db.commit()
            if reason:
                return
            body = SessionIn.model_validate(run.launch)
            who = Principal(user.username, user.role, schedule.account, user.id)
        try:
            await self.ctx.launch_session(body, who, schedule_run_id=run_id)
        except Exception as error:
            with self.ctx.db() as db:
                run = db.get(ScheduleRun, run_id)
                if run.status == 'claimed':
                    message = str(getattr(error, 'detail', error))
                    status = 'skipped' if 'budget pauses' in message or 'machine offline' in message.lower() else 'failed'
                    self.finish(db, run, status, message)
                    db.commit()

    async def tick(self):
        from .admin_features import enabled
        if not enabled(self.ctx.settings, 'schedules'):
            return
        if not self.recovered:
            await self.recover()
        self.reconcile()
        self.materialize(utcnow())
        with self.ctx.db() as db:
            ids = list(db.scalars(select(ScheduleRun.id).where(ScheduleRun.status == 'pending')
                                  .order_by(ScheduleRun.created_at, ScheduleRun.id).limit(20)))
        for run_id in ids:
            await self.dispatch(run_id)
