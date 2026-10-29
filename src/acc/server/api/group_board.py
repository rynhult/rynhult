"""Group-safe projections and own-session coordination. No private content routes."""
from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .. import group_board as board
from ..models import AuditLog, GroupClaim, GroupOperation, GroupWork, Project, Session, utcnow
from .context import Context


class LaunchIn(BaseModel):
    checkout_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    task: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=64)
    provider: Literal['claude', 'codex'] = 'claude'
    budget_ok: bool = False
    prompt: str = Field(default='', max_length=32000)
    trust_source: bool = False


class PolicyIn(BaseModel):
    revision: int
    concurrent: int = Field(ge=1, le=100)
    daily_launches: int = Field(ge=1, le=10000)
    usage_warning_usd: float | None = Field(default=None, ge=0, le=1000000, allow_inf_nan=False)


class OperationIn(BaseModel):
    revision: int = Field(ge=1)
    action: Literal['assign', 'ack', 'resolve']
    user_id: int | None = None


class ClaimIn(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    shared: bool = False


class TaskIn(BaseModel):
    task: str = Field(min_length=1, max_length=500)


def usage(s, group):
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    import math

    from ..models import GroupLaunch, Observation
    sessions = list(s.scalars(select(Session).join(Project).where(Project.group_id == group.id).order_by(Session.created_at.desc()).limit(100)))
    total = s.scalar(select(func.count(Session.id)).join(Project).where(Project.group_id == group.id))
    ledger = []
    for session in sessions:
        observed = s.get(Observation, f'claude.statusline.{session.id}')
        cost = (observed.value.get('cost') or {}).get('total_cost_usd') if observed and isinstance(observed.value.get('cost'), dict) else None
        measured = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
        ledger.append({'session_id': session.id, 'project_id': session.project_id, 'person': session.owner,
                       'provider': session.provider, 'machine': session.host, 'cost': cost if measured else None,
                       'units': 'USD' if measured else None, 'source': observed.source if measured else None,
                       'observed_at': observed.observed_at if measured else None})
    measured = [x['cost'] for x in ledger if x['cost'] is not None]
    return {'ledger': ledger, 'units': 'USD' if measured else None,
            'concurrent': s.scalar(select(func.count(Session.id)).join(Project).where(Project.group_id == group.id,
                (Session.desired_state == 'running') | Session.team_stop_pending | Session.multirun_stop_pending)),
            'daily_launches': s.scalar(select(func.count(GroupLaunch.id)).join(Session).join(Project).where(Project.group_id == group.id, GroupLaunch.created_at >= today)),
            'limits': {'concurrent': group.concurrent, 'daily_launches': group.daily_launches},
            'ceilings': {'concurrent': group.concurrent_ceiling, 'daily_launches': group.daily_ceiling},
            'usage_warning_usd': group.usage_warning_usd,
            'usage_warning_reached': bool(measured and group.usage_warning_usd is not None and sum(measured) >= group.usage_warning_usd),
            'paused': group.paused, 'override_until': group.override_until,
            'cost': sum(measured) if measured else None, 'coverage': ('complete' if len(measured) == total else 'partial') if measured else 'unknown', 'source': 'Rynhult session ledger', 'ledger_truncated': total > len(sessions),
            'warning': 'Provider spend is unknown. Launch quotas are not a monetary cap.'}


def router(ctx: Context):
    api = APIRouter(prefix='/api/groups/{group_id}')

    def own(s, group_id, session_id, who):
        group = board.access(s, group_id, who, write=True)
        session = s.get(Session, session_id)
        work = s.get(GroupWork, session_id)
        if (session is None or work is None or session.project.group_id != group_id
                or (session.owner != who.username and not who.is_owner)):
            raise HTTPException(404, 'Group session not found.')
        return group, session, work

    def audit(s, who, group, action, session_id=None, **subjects):
        s.add(AuditLog(actor=who.username, action='group.' + action, target=group.id,
                       detail={'session_id': session_id, 'board_revision': group.board_revision, **subjects}))

    @api.post('/projects/{project_id}/sessions', status_code=201)
    async def launch(group_id: str, project_id: int, body: LaunchIn, response: Response, who=Depends(ctx.principal)):
        import asyncio

        from ..compiler import CompileError, LaunchInputs
        from ..supervisor_common import LaunchRefused
        lock = ctx.supervisor.__dict__.setdefault('_group_launch_lock', asyncio.Lock())
        async with lock:
            with ctx.db() as s:
                board.access(s, group_id, who, write=True)
                project = s.get(Project, project_id)
                if project is None or project.group_id != group_id:
                    raise HTTPException(404, 'Group project not found.')
                try:
                    board.launch_binding(s, project, body.checkout_id, who.username, who.account, remote_enabled=ctx.settings.groups_remote_enabled)
                    board.safe_task(body.task)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                except LaunchRefused as error:
                    raise HTTPException(404, str(error)) from None
                if not body.trust_source:
                    raise HTTPException(409, 'Review the published source and confirm you trust it before running it with your account and keys.')
                prior = s.get(Session, body.checkout_id)
                if prior:
                    if prior.owner != who.username or prior.project_id != project_id:
                        raise HTTPException(404, 'Checkout not found. Start the launch again from the Work board.')
                    response.status_code = 200
                    return {'id': prior.id}
            try:
                from .. import budgets
                with ctx.db() as s:
                    if budgets.paused(s, who.username, body.provider) and not body.budget_ok:
                        raise HTTPException(409, 'Your personal budget has paused launches. To launch anyway, confirm the override for your own budget.')
                inputs = LaunchInputs(provider=body.provider, initial_prompt=body.prompt)
                sid = await ctx.supervisor.create_session(project_id, inputs, body.name, who.username,
                    owner=who.username, group_checkout=body.checkout_id, group_task=body.task)
            except (LaunchRefused, CompileError) as error:
                raise HTTPException(409, str(error)) from None
            return {'id': sid}

    def eligible_keys(s, group_id, project_id, who):
        from ..models import GroupKeyGrant, VaultGrant, VaultKey
        board.access(s, group_id, who)
        project = s.get(Project, project_id)
        if not project or project.group_id != group_id:
            raise HTTPException(404, 'Project not found in this group.')
        attached = set(s.scalars(select(VaultGrant.key_id).where(VaultGrant.project_id == project_id)))
        grants = s.scalars(select(GroupKeyGrant).where(GroupKeyGrant.group_id == group_id,
            (GroupKeyGrant.project_id.is_(None)) | (GroupKeyGrant.project_id == project_id),
            (GroupKeyGrant.host.is_(None)) | (GroupKeyGrant.host == 'local')))
        return [{'key_id': g.key_id, 'attached': g.key_id in attached} for g in grants
                if (key := s.get(VaultKey, g.key_id)) and key.owner == g.given_by]

    @api.get('/projects/{project_id}/keys')
    def keys(group_id: str, project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as s:
            return eligible_keys(s, group_id, project_id, who)

    @api.post('/projects/{project_id}/keys/{key_id}/attach')
    def attach(group_id: str, project_id: int, key_id: str, who=Depends(ctx.principal)):
        from ..models import VaultGrant
        with ctx.db() as s:
            group = board.access(s, group_id, who, write=True, admin=True)
            board.fence(s, group)
            project = s.get(Project, project_id)
            if project and project.group_id == group_id and project.archived:  # archive ends new key access, like launches
                raise HTTPException(409, 'This group project is archived, so keys can no longer be attached.')
            if key_id not in {x['key_id'] for x in eligible_keys(s, group_id, project_id, who)}:
                raise HTTPException(404, "That key isn't granted to this group project, or it's no longer available.")
            if s.get(VaultGrant, (key_id, project_id)) is None:
                s.add(VaultGrant(key_id=key_id, project_id=project_id, given_by=who.username))
            audit(s, who, group, 'key_attach', key_id=key_id, project_id=project_id)
            s.commit()
            return {'ok': True}

    @api.get('/board')
    def get_board(group_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            group = board.access(s, group_id, who)
            data = board.snapshot(s, group, ctx.settings.groups_remote_enabled)
            from ..models import GroupMembership
            membership = s.get(GroupMembership, (group_id, who.user_id)) if who.user_id else None
            if not who.is_owner and membership and membership.role == 'viewer':
                for row in data['rows']:
                    row['claims'] = []
                    row['touched_paths'] = []
                data['paths_available'] = False
            else:
                data['paths_available'] = True
            return {**data, 'policy_revision': group.revision, 'usage': usage(s, group)}

    @api.get('/usage')
    def get_usage(group_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            return usage(s, board.access(s, group_id, who))

    @api.post('/policy')
    def policy(group_id: str, body: PolicyIn, who=Depends(ctx.principal)):
        from sqlalchemy import update

        from ..models import Group
        with ctx.db() as s:
            group = board.access(s, group_id, who, write=True, admin=True)
            if not who.is_owner and (body.concurrent > group.concurrent_ceiling or body.daily_launches > group.daily_ceiling):
                raise HTTPException(403, "Only an instance owner can raise launch quotas above the group's limits.")
            changed = s.execute(update(Group).where(Group.id == group.id, Group.revision == body.revision).values(
                revision=Group.revision + 1, concurrent=body.concurrent, daily_launches=body.daily_launches,
                concurrent_ceiling=max(group.concurrent_ceiling, body.concurrent) if who.is_owner else group.concurrent_ceiling,
                daily_ceiling=max(group.daily_ceiling, body.daily_launches) if who.is_owner else group.daily_ceiling,
                usage_warning_usd=body.usage_warning_usd))
            if changed.rowcount != 1:
                raise HTTPException(409, 'The group changed while you were editing. Reload the page and save the launch quotas again.')
            s.refresh(group)
            audit(s, who, group, 'policy')
            s.commit()
            return usage(s, group)

    def operation_meta(s, item):
        from ..models import Observation
        key = 'group.op.' + hashlib.sha256(item.id.encode()).hexdigest()[:32]
        row = s.get(Observation, key)
        return row, key, dict(row.value) if row else {'revision': 1, 'assignee': None, 'acknowledged': [], 'affected': []}

    @api.post('/operations/{operation_id}/update')
    def update_operation(group_id: str, operation_id: str, body: OperationIn, who=Depends(ctx.principal)):
        from ..models import GroupMembership, Observation, User
        with ctx.db() as s:
            group = board.access(s, group_id, who, write=True, admin=body.action != 'ack')
            board.fence(s, group)
            item = s.get(GroupOperation, operation_id)
            if item is None or item.group_id != group_id:
                raise HTTPException(404, 'Group operation not found.')
            _, key, meta = operation_meta(s, item)
            if meta['revision'] != body.revision:
                raise HTTPException(409, 'This operation changed while you were looking at it. Reload the page and try again.')
            if body.action == 'ack':
                if who.username not in meta.get('affected', []):
                    raise HTTPException(404, "This operation doesn't affect you, so there is nothing to acknowledge.")
                meta['acknowledged'] = sorted(set(meta.get('acknowledged', [])) | {who.username})
            elif body.action == 'assign':
                person = s.get(User, body.user_id) if body.user_id else None
                member = s.get(GroupMembership, (group_id, body.user_id)) if body.user_id else None
                if body.user_id and (not person or not member or member.role != 'maintainer'):
                    raise HTTPException(422, 'Choose a current group maintainer to assign this operation to.')
                meta['assignee'] = person.username if person else None
            else:
                item.resolved = True
            meta['revision'] += 1
            s.merge(Observation(key=key, value=meta, source='group_board', observed_at=utcnow().timestamp()))
            audit(s, who, group, 'operation_' + body.action, operation_id=operation_id, user_id=body.user_id)
            s.commit()
            return meta

    @api.get('/operations')
    def operations(group_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            board.access(s, group_id, who)
            from ..models import GroupMembership
            membership = s.get(GroupMembership, (group_id, who.user_id)) if who.user_id else None
            viewer = not who.is_owner and membership and membership.role == 'viewer'
            return [{'id': x.id, 'kind': x.kind, 'summary': 'Overlapping work needs review' if viewer and x.kind == 'overlap' else x.summary, 'resolved': x.resolved, 'created_at': x.created_at, **operation_meta(s, x)[2]}
                    for x in s.scalars(select(GroupOperation).where(GroupOperation.group_id == group_id).order_by(GroupOperation.created_at.desc()).limit(100))]

    @api.post('/operations/{operation_id}/resolve')
    def resolve(group_id: str, operation_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            group = board.access(s, group_id, who, write=True, admin=True)
            item = s.get(GroupOperation, operation_id)
            if item is None or item.group_id != group_id:
                raise HTTPException(404, 'Group operation not found.')
            item.resolved = True
            audit(s, who, group, 'operation_resolve', operation_id=operation_id)
            s.commit()
            return {'ok': True}

    @api.get('/work/{session_id}/context')
    def context(group_id: str, session_id: str, who=Depends(ctx.principal)):
        with ctx.db() as s:
            group, _, _ = own(s, group_id, session_id, who)
            return {'revision': group.board_revision, 'context': board.context(s, group, session_id, ctx.settings.groups_remote_enabled)}

    @api.post('/work/{session_id}/task')
    def task(group_id: str, session_id: str, body: TaskIn, who=Depends(ctx.principal)):
        try:
            value = board.safe_task(body.task)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with ctx.db() as s:
            group, _, work = own(s, group_id, session_id, who)
            board.fence(s, group)
            value = board.shared_redactor(ctx.supervisor.vault, s, s.get(Session, session_id).vault_keys or []).text(value)
            work.task, work.updated_at = value, utcnow()
            audit(s, who, group, 'task', session_id)
            s.commit()
            return {'revision': group.board_revision}

    @api.post('/work/{session_id}/claim')
    def claim(group_id: str, session_id: str, body: ClaimIn, who=Depends(ctx.principal)):
        try:
            path = board.safe_path(body.path)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        with ctx.db() as s:
            group, session, _ = own(s, group_id, session_id, who)
            if board.shared_redactor(ctx.supervisor.vault, s, session.vault_keys or []).text(path) != path:
                raise HTTPException(422, 'Remove passwords and keys from the path. Everyone in the group can read claimed paths.')
            board.fence(s, group)  # serialize overlap check and lease insertion with membership changes
            if session.desired_state != 'running' or not board.authorized(s, session, ctx.settings.groups_remote_enabled):
                raise HTTPException(409, "This session is stopped or no longer allowed to work in the group, so it can't claim paths.")
            now = utcnow()
            existing = list(s.scalars(select(GroupClaim).join(Session).where(Session.project_id == session.project_id,
                GroupClaim.expires_at > now)))
            conflicts = [{'session_id': c.session_id, 'path': c.path} for c in existing
                         if c.session_id != session_id and board.overlap(path, c.path)]
            if conflicts and not body.shared:
                return {'status': 'conflict', 'holders': conflicts, 'revision': group.board_revision}
            mine = next((c for c in existing if c.session_id == session_id and c.path == path), None)
            if mine is None:
                s.execute(delete(GroupClaim).where(GroupClaim.session_id == session_id, GroupClaim.expires_at <= now))
                if sum(c.session_id == session_id for c in existing) >= 32:
                    raise HTTPException(429, 'A session can hold at most 32 claimed paths. Release a claim first.')
                mine = GroupClaim(session_id=session_id, path=path, expires_at=now + board.LEASE)
                s.add(mine)
            mine.expires_at = now + board.LEASE
            for other in conflicts:
                board.conflict(s, group, session, other['session_id'], path)
            audit(s, who, group, 'claim_shared' if conflicts else 'claim', session_id)
            s.commit()
            return {'status': 'shared' if conflicts else 'accepted', 'holders': conflicts, 'revision': group.board_revision}

    @api.post('/work/{session_id}/release')
    def release(group_id: str, session_id: str, body: ClaimIn, who=Depends(ctx.principal)):
        with ctx.db() as s:
            group, _, _ = own(s, group_id, session_id, who)
            board.fence(s, group)
            s.execute(delete(GroupClaim).where(GroupClaim.session_id == session_id, GroupClaim.path == body.path))
            audit(s, who, group, 'claim_release', session_id)
            s.commit()
            return {'revision': group.board_revision}

    def work_request(session_id, request):
        from ..admin_features import enabled
        if not enabled(ctx.settings, 'groups'):
            return {'status': 'refused', 'reason': 'Groups is turned off in Admin features.'}
        import re

        from pydantic import ConfigDict, ValidationError

        from ..models import Event, User
        from .context import Principal

        class WorkRequest(BaseModel):
            model_config = ConfigDict(extra='forbid')
            type: Literal['work']
            action: Literal['list', 'claim', 'release', 'task']
            request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
            path: str = Field(default='', max_length=300)
            text: str = Field(default='', max_length=500)
            shared: bool = False

        try:
            parsed = WorkRequest.model_validate(request)
        except ValidationError:
            return {'status': 'refused', 'reason': 'invalid work request'}
        if not re.fullmatch(r'[a-f0-9]{12,32}', session_id):
            return {'status': 'refused', 'reason': 'invalid originating session'}
        with ctx.db() as s:
            session = s.get(Session, session_id)
            if not session or not session.project.group_id or not board.authorized(s, session, ctx.settings.groups_remote_enabled) or session.desired_state != 'running':
                return {'status': 'refused', 'reason': 'session authority was revoked or stopped'}
            group_id = session.project.group_id
            user = s.scalar(select(User).where(User.username == session.owner))
            who = Principal(user.username, user.role, session.run_as, user.id)
            prior = s.scalar(select(Event).where(Event.session_id == session_id, Event.kind == 'group_work_receipt',
                Event.payload['request_id'].as_string() == parsed.request_id))
            if prior:
                return prior.payload['reply']
            count = len(list(s.scalars(select(Event.id).where(Event.session_id == session_id, Event.kind == 'group_work_receipt').limit(1025))))
            if count >= 1024:
                return {'status': 'refused', 'reason': 'session work request quota reached'}
        try:
            if parsed.action == 'list':
                reply = {'status': 'accepted', **context(group_id, session_id, who)}
            elif parsed.action == 'task':
                reply = {'status': 'accepted', **task(group_id, session_id, TaskIn(task=parsed.text), who)}
            elif parsed.action == 'claim':
                reply = claim(group_id, session_id, ClaimIn(path=parsed.path, shared=parsed.shared), who)
            else:
                reply = {'status': 'accepted', **release(group_id, session_id, ClaimIn(path=parsed.path), who)}
        except (HTTPException, ValidationError):
            reply = {'status': 'refused', 'reason': 'refresh context and review your task, paths and session authority'}
        with ctx.db() as s:
            s.add(Event(session_id=session_id, source='group_board', kind='group_work_receipt',
                        payload={'request_id': parsed.request_id, 'reply': reply}))
            s.commit()
        return reply

    ctx.supervisor.group_work_request = work_request
    return api
