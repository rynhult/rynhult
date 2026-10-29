"""Schedules are controlled only by authenticated people, never session tools or bearer tokens."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ... import providers
from ..compiler import CompileError, compile_files
from ..models import API_PRINCIPAL, Schedule, ScheduleRun, User, utcnow
from ..schedules import ACTIVE, Scheduler, audit, enqueue, next_time
from .schemas import SessionIn


class ScheduledLaunchIn(SessionIn):
    worktree: bool = True


class ScheduleIn(BaseModel):
    launch: ScheduledLaunchIn
    timing: dict = Field(max_length=5)
    timezone: str = Field(min_length=1, max_length=100)
    missed: str = Field('once', pattern='^(once|skip)$')
    enabled: bool = False


class TimingIn(BaseModel):
    timing: dict = Field(max_length=5)
    timezone: str = Field(min_length=1, max_length=100)


def run_out(run):
    return {k: getattr(run, k) for k in ('id', 'due_at', 'trigger', 'session_id', 'status', 'reason')}


def router(ctx):
    api = APIRouter()
    scheduler = Scheduler(ctx)
    ctx.supervisor.scheduler = scheduler

    def person(who=Depends(ctx.principal)):
        if who.username == API_PRINCIPAL:
            raise HTTPException(403, 'Schedules are managed by people, not API tokens. Sign in to Rynhult to manage them.')
        return who

    def find(db, who, schedule_id):
        row = db.get(Schedule, schedule_id)
        if row is None or row.deleted or not ctx.owns(row, who):
            raise HTTPException(404, 'That schedule wasn\'t found.')
        return row

    def out(db, row):
        runs = db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == row.id)
                          .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc()).limit(20))
        return {**{k: getattr(row, k) for k in ('id', 'owner', 'launch', 'timing', 'timezone', 'missed', 'enabled', 'next_run_at')},
                'runs': [run_out(r) for r in runs]}

    def validate(db, body, who, existing=None):
        launch = body.launch
        if launch.topic or launch.workspace or launch.project_id is None or launch.budget_ok:
            raise HTTPException(422, 'A schedule needs a project and can\'t go past a budget limit. Choose a project, then save again.')
        project = ctx.project_for(db, who, launch.project_id)
        account = existing.account if existing else who.account
        if project.account != account:
            raise HTTPException(422, 'Choose a project that runs as the schedule owner’s agent account.')
        if existing and existing.owner != who.username:
            user = db.scalar(select(User).where(User.username == existing.owner))
            if user is None or (user.role != 'owner' and project.owner != user.username):
                raise HTTPException(422, 'The schedule’s owner can’t use that project. Choose another project.')
        try:
            inputs = launch.inputs(ctx.supervisor.catalog)
            # Freeze resolved profile defaults and selections: later catalog edits cannot broaden the approval.
            dangerous = providers.get(inputs.provider).dangerous(inputs)
            if dangerous and not launch.confirm_dangerous:
                raise HTTPException(409, 'This schedule lets the agent act without asking. Confirm that you allow this, then save again.')
            following = next_time(body.timing, body.timezone, utcnow())
            if following is None:
                raise ValueError('Choose a time in the future.')
            resolved = launch.model_dump()
            resolved.update(inputs.as_dict())
            resolved.update(confirm_dangerous=bool(launch.confirm_dangerous), budget_ok=False)
            # Validate provider options before accepting the saved configuration.
            compile_files(inputs, ctx.settings.hook_path, ctx.settings.statusline_path,
                          catalog=ctx.supervisor.catalog, endpoint=ctx.supervisor.endpoint_info(db, inputs.endpoint))
        except (ValueError, CompileError, KeyError) as error:
            raise HTTPException(422, str(error)) from None
        return resolved, following

    @api.post('/api/schedules/preview')
    def preview(body: TimingIn, who=Depends(person)):
        """The next few runs for a timing, as the scheduler will compute them, for the form's live "Next run" line."""
        runs, after = [], utcnow()
        try:
            while len(runs) < 3 and (following := next_time(body.timing, body.timezone, after)) is not None:
                runs.append(following)
                after = following
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {'next': runs}

    @api.get('/api/schedules')
    def listing(who=Depends(person)):
        with ctx.db() as db:
            return [out(db, r) for r in db.scalars(ctx.scope(select(Schedule), Schedule, who)
                                                   .where(Schedule.deleted.is_(False)).order_by(Schedule.created_at.desc()))]

    @api.post('/api/schedules', status_code=201)
    def create(body: ScheduleIn, who=Depends(person)):
        import uuid
        with ctx.db() as db:
            launch, following = validate(db, body, who)
            row = Schedule(id=uuid.uuid4().hex, owner=who.username, account=who.account, launch=launch,
                           timing=body.timing, timezone=body.timezone, missed=body.missed, enabled=body.enabled,
                           next_run_at=following)
            db.add(row)
            audit(db, row, 'create', {'provider': launch['provider'], 'dangerous_confirmed': launch['confirm_dangerous']})
            if row.enabled:
                audit(db, row, 'enable')
            db.commit()
            return out(db, row)

    @api.put('/api/schedules/{schedule_id}')
    def edit(schedule_id: str, body: ScheduleIn, who=Depends(person)):
        with ctx.db() as db:
            row = find(db, who, schedule_id)
            launch, following = validate(db, body, who, row)
            if row.enabled != body.enabled:
                audit(db, row, 'enable' if body.enabled else 'disable', actor=who.username)
            row.launch, row.next_run_at = launch, following
            row.timing, row.timezone, row.missed, row.enabled = body.timing, body.timezone, body.missed, body.enabled
            audit(db, row, 'update', {'provider': launch['provider'], 'dangerous_confirmed': launch['confirm_dangerous']}, actor=who.username)
            db.commit()
            return out(db, row)

    @api.post('/api/schedules/{schedule_id}/enable')
    @api.post('/api/schedules/{schedule_id}/disable')
    def toggle(schedule_id: str, request: Request, who=Depends(person)):
        with ctx.db() as db:
            row = find(db, who, schedule_id)
            enabled = request.url.path.endswith('/enable')
            if enabled:
                row.next_run_at = next_time(row.timing, row.timezone, utcnow())
                if row.next_run_at is None:
                    raise HTTPException(409, 'This schedule has no future run time. Edit it to choose a time in the future.')
            row.enabled = enabled
            audit(db, row, 'enable' if enabled else 'disable', actor=who.username)
            db.commit()
            return out(db, row)

    @api.post('/api/schedules/{schedule_id}/run', status_code=201)
    def run_now(schedule_id: str, who=Depends(person)):
        with ctx.db() as db:
            row = find(db, who, schedule_id)
            run = enqueue(db, row, utcnow(), trigger='person')
            audit(db, row, 'run', {'run_id': run.id, 'status': 'pending'}, actor=who.username)
            db.commit()
            return run_out(run)

    @api.post('/api/schedules/{schedule_id}/cancel')
    async def cancel(schedule_id: str, who=Depends(person)):
        with ctx.db() as db:
            row = find(db, who, schedule_id)
            runs = list(db.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == row.id)))
            session_ids = []
            for run in runs:
                if run.session_id:
                    from ..models import Session
                    session = db.get(Session, run.session_id)
                    if session and session.desired_state == 'running':
                        # Save stop intent with cancellation before any awaited broker effect.
                        session.desired_state = 'stopped'
                        session_ids.append(session.id)
                if run.status in ACTIVE:
                    run.status, run.reason = 'cancelled', 'Cancelled by a person'
            audit(db, row, 'cancel', actor=who.username)
            db.commit()
        for session_id in session_ids:
            await ctx.supervisor.stop(session_id, who.username)
        return {'ok': True}

    @api.delete('/api/schedules/{schedule_id}')
    def delete(schedule_id: str, who=Depends(person)):
        with ctx.db() as db:
            row = find(db, who, schedule_id)
            row.enabled, row.deleted = False, True
            audit(db, row, 'disable', actor=who.username)
            audit(db, row, 'delete', actor=who.username)
            db.commit()
        return {'ok': True}

    return api
