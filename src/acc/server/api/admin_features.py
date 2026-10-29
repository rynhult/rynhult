"""Owner-only availability policy; stored choices never replace qualification or consent."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import admin_features as policy
from .. import passkeys
from ..models import API_PRINCIPAL, AuditLog, Project, Session, Setting, Team, User


class ChangeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    revision: int = Field(ge=0, strict=True)


def router(ctx):
    api = APIRouter()
    lock = asyncio.Lock()
    features = ctx.settings.features

    def described():
        rows = features.rows()
        from ...container_host_record import check
        for row in rows:
            if row['id'] == 'containers':
                row['qualified'] = bool(check(ctx.settings.containers_record)['passed'] and ctx.settings.containers_attach_qualified)
        return {'revision': features.revision, 'features': rows}

    @api.get('/api/admin-features')
    def get_features(who=Depends(ctx.owner)):
        return described()

    @api.put('/api/admin-features/{key}')
    async def put_feature(key: str, body: ChangeIn, request: Request, who=Depends(ctx.owner)):
        if key not in policy.FEATURES:
            raise HTTPException(404, 'No optional feature with that name.')
        if who.username == API_PRINCIPAL:
            raise HTTPException(403, 'Change Admin features from an owner’s signed-in browser, not the installation API token.')
        async with lock:
            # Resolve from durable storage, not the tab or an earlier request's snapshot.
            with ctx.db() as db:
                row = db.scalar(select(Setting).where(Setting.key == policy.KEY).with_for_update())
                value = row.value if row else {'choices': {}, 'revision': 0}
                if body.revision != value.get('revision', 0):
                    raise HTTPException(409, 'Admin features changed in another tab. Refresh and try again.')
                if features.locked(key):
                    raise HTTPException(409, "Set by the server's configuration")
                before = features.enabled(key)
                if body.enabled and not before and policy.FEATURES[key].sensitive:
                    passkeys.recently_verified(db, getattr(request.state, 'session_hash', None), db.get(User, who.user_id) if who.user_id is not None else None)
                choices = {**value.get('choices', {}), key: body.enabled}
                revision = value.get('revision', 0) + 1
                leads = []
                if not body.enabled and key in ('teams', 'orchestrator', 'groups'):
                    query = select(Team).where(Team.stopped.is_(False))
                    if key == 'groups':
                        query = query.join(Session, Session.id == Team.id).join(Project, Project.id == Session.project_id).where(Project.group_id.is_not(None))
                    for team in db.scalars(query):
                        leads.append(team.id)
                        team.stopped = True
                        from ..orchestrator import worklist
                        worklist.team_stopped(db, team.id)
                        for member in db.scalars(select(Session).where(Session.team_id == team.id, Session.desired_state != 'archived')):
                            member.desired_state, member.team_stop_pending = 'stopped', True
                db.merge(Setting(key=policy.KEY, value={'choices': choices, 'revision': revision}, updated_by=who.username))
                db.add(AuditLog(actor=who.username, action='admin_features.update', target=key,
                                detail={'before': before, 'enabled': body.enabled, 'revision': revision}))
                db.commit()
            features.choices, features.revision = choices, revision
            pending = []
            # Publish Off before awaiting cleanup: new work is refused throughout drain.
            if not body.enabled:
                if key in ('teams', 'orchestrator', 'groups'):
                    for lead in leads:
                        try:
                            result = await ctx.supervisor.stop_team(lead, who.username)
                            if result.get('pending'):
                                pending.append('Team stop pending on an unavailable machine.')
                        except Exception:
                            pending.append('Team stop is durable; machine confirmation is pending.')
                    if key == 'orchestrator':
                        from ..orchestrator import reconcile_availability
                        await reconcile_availability(ctx.supervisor)
                if key == 'remote_access':
                    try:
                        await ctx.remote_access.change(False, who.username)
                    except (OSError, ValueError, RuntimeError, HTTPException):
                        # Availability already denies every public stream; operator cleanup is separately visible.
                        pending.append('Public routing removal is pending; public streams are refused. Use the private recovery address.')
                if key in ('remote_access', 'mobile'):
                    await ctx.edge.sweep()
            return {**described(), 'cleanup_pending': pending}

    return api
