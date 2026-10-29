"""Opt-in authority only. Never route a group request to a person's live checkout."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from .. import auth, group_authority
from ..models import (
    AuditLog,
    Group,
    GroupKeyGrant,
    GroupMembership,
    GroupMergeApproval,
    GroupProjectOwner,
    Placement,
    Project,
    User,
    VaultKey,
)
from .context import Context


class RevisionIn(BaseModel):
    revision: int = Field(ge=1)


class CreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r'.*\S.*')
    maintainer_id: int


class MembershipIn(RevisionIn):
    role: Literal['maintainer', 'member', 'viewer', 'remove']


class ProjectIn(RevisionIn):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')
    owners: list[int] = Field(min_length=1, max_length=100)
    workspace: bool = False


class OwnersIn(RevisionIn):
    owners: list[int] = Field(min_length=1, max_length=100)


class ApprovalIn(RevisionIn):
    branch: str = Field(pattern=r'^(main|master|develop|release/[a-zA-Z0-9_-]+)$', max_length=200)
    old_sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    new_sha: str = Field(pattern=r'^[a-f0-9]{40}$')


class PlacementIn(RevisionIn):
    user_id: int
    # No caller-supplied account/path: approval records a reserved, unprovisioned same-host binding.


class KeyGrantIn(RevisionIn):
    key_id: str
    project_id: int | None = None
    host: Literal['local'] | None = None


def router(ctx: Context) -> APIRouter:
    api = APIRouter(prefix='/api/groups')
    db = ctx.db

    def role(s, group_id, who):
        return s.scalar(select(GroupMembership.role).where(GroupMembership.group_id == group_id,
                                                          GroupMembership.user_id == who.user_id))

    def access(s, group_id, who, admin=False, key_owner=False):
        group = s.get(Group, group_id)
        held = role(s, group_id, who)
        if group is None or (not who.is_owner and not held and not key_owner):
            raise HTTPException(404, "Group not found, or you don't have access to do this.")
        if admin and not who.is_owner and held != 'maintainer':
            raise HTTPException(404, "Group not found, or you don't have access to do this.")
        return group

    def fence(s, group, revision):
        if group.archived:
            raise HTTPException(409, 'This group is archived and can no longer be changed.')
        # Compare-and-swap also protects databases that do not serialize writers like SQLite.
        changed = s.execute(update(Group).where(Group.id == group.id, Group.revision == revision,
                                                Group.archived.is_(False))
                            .values(revision=Group.revision + 1).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise HTTPException(409, 'The group changed while you were looking at it. Reload the page, check the current members and try again.')
        s.refresh(group)

    def record(s, who, group, action, **detail):
        s.add(AuditLog(actor=who.username, action='group.' + action, target=group.id,
                       detail={'group_id': group.id, 'revision': group.revision, **detail}))

    def stop_intent(s, stops):
        from ..models import Session
        for session_id in stops:
            ctx.supervisor._set_state(s, s.get(Session, session_id), 'stopped', 'api')

    async def stop_sessions(stops, actor):
        for session_id in stops:
            await ctx.supervisor.stop(session_id, actor)
        for streams in list(ctx.terminals.values()):
            for stream in list(streams):
                if stream.path_params.get('acc_id') in stops:
                    try:
                        await stream.close(code=4403)
                    except RuntimeError:
                        pass

    def output(s, group, who):
        return {'id': group.id, 'name': group.name, 'archived': group.archived, 'revision': group.revision,
                'paused': group.paused, 'role': 'owner' if who.is_owner else role(s, group.id, who),
                'execution_available': True, 'remote_execution_available': ctx.settings.groups_remote_enabled}

    def project(s, group, project_id):
        row = s.get(Project, project_id)
        if row is None or row.group_id != group.id:
            raise HTTPException(404, 'Project not found in this group.')
        return row

    def project_output(s, row):
        return {'id': row.id, 'group_id': row.group_id, 'name': row.name, 'slug': row.slug,
                'workspace': row.private_workspace, 'archived': row.archived,
                'owners': list(s.scalars(select(GroupProjectOwner.user_id).where(GroupProjectOwner.project_id == row.id))),
                'execution_available': True, 'remote_execution_available': ctx.settings.groups_remote_enabled}

    @api.get('')
    def list_groups(who=Depends(ctx.principal)):
        with db() as s:
            query = select(Group).order_by(Group.name)
            if not who.is_owner:
                query = query.join(GroupMembership).where(GroupMembership.user_id == who.user_id)
            return [output(s, g, who) for g in s.scalars(query)]

    @api.get('/key-grants/owned')
    def owned_key_grants(who=Depends(ctx.principal)):
        # Only the key owner can inspect consent, independently of group administration.
        with db() as s:
            rows = s.execute(select(GroupKeyGrant, Group.revision)
                .join(VaultKey, VaultKey.id == GroupKeyGrant.key_id)
                .join(Group, Group.id == GroupKeyGrant.group_id)
                .where(VaultKey.owner == who.username).order_by(GroupKeyGrant.id))
            return [{'id': grant.id, 'key_id': grant.key_id, 'group_id': grant.group_id,
                     'project_id': grant.project_id, 'host': grant.host, 'revision': revision}
                    for grant, revision in rows]

    @api.post('', status_code=201)
    def create(body: CreateIn, who=Depends(ctx.owner)):
        with db() as s:
            if s.get(User, body.maintainer_id) is None:
                raise HTTPException(422, 'Choose someone who already has a Rynhult account.')
            group = Group(id=uuid.uuid4().hex, name=body.name.strip(), created_by=who.username)
            s.add(group)
            s.flush()
            s.add(GroupMembership(group_id=group.id, user_id=body.maintainer_id, role='maintainer'))
            record(s, who, group, 'create', user_id=body.maintainer_id)
            s.commit()
            return output(s, group, who)

    @api.get('/{group_id}')
    def get_group(group_id: str, who=Depends(ctx.principal)):
        with db() as s:
            return output(s, access(s, group_id, who), who)

    @api.get('/{group_id}/people')
    def people(group_id: str, who=Depends(ctx.principal)):
        with db() as s:
            access(s, group_id, who, admin=True)
            # Deliberately no instance role, account, login, invite or key information.
            return [{'id': u.id, 'username': u.username, 'role': m.role if m else None,
                     'provenance': m.provenance if m else None}
                    for u, m in s.execute(select(User, GroupMembership).outerjoin(GroupMembership,
                        (GroupMembership.user_id == User.id) & (GroupMembership.group_id == group_id)).order_by(User.username))]

    @api.post('/{group_id}/members/{user_id}')
    async def membership(group_id: str, user_id: int, body: MembershipIn, who=Depends(ctx.principal)):
        with db() as s:
            leaving = user_id == who.user_id and body.role == 'remove'
            group = access(s, group_id, who, admin=not leaving)
            fence(s, group, body.revision)
            if s.get(User, user_id) is None:
                raise HTTPException(422, 'Choose someone who already has a Rynhult account.')
            held = s.get(GroupMembership, (group_id, user_id))
            if held and held.provenance != 'local':
                raise HTTPException(409, 'This membership comes from your company sign-in provider. Change it there.')
            if body.role == 'maintainer' and not who.is_owner and (held is None or held.role != 'maintainer'):
                raise HTTPException(403, 'Only an instance owner can appoint maintainers.')
            if held and held.role == 'maintainer' and body.role != 'maintainer':
                maintainers = list(s.scalars(select(GroupMembership.user_id).where(
                    GroupMembership.group_id == group_id, GroupMembership.role == 'maintainer')))
                if len(maintainers) <= 1:
                    raise HTTPException(409, 'A group needs at least one maintainer. Appoint another maintainer first.')
                owned = s.scalars(select(GroupProjectOwner).join(Project).where(
                    Project.group_id == group_id, GroupProjectOwner.user_id == user_id)).all()
                for entry in owned:
                    other = s.scalar(select(GroupProjectOwner.user_id).where(
                        GroupProjectOwner.project_id == entry.project_id, GroupProjectOwner.user_id != user_id))
                    if other is None:
                        raise HTTPException(409, 'This person is the only named owner of a group project. Name another project owner first.')
                    s.delete(entry)
            if body.role == 'remove':
                if held:
                    s.delete(held)
            elif held:
                held.role = body.role
            else:
                s.add(GroupMembership(group_id=group_id, user_id=user_id, role=body.role))
            person = s.get(User, user_id).username
            revoked = []
            stops = set(group_authority.revoke_execution(s, group_id, person, revoked=revoked)) if body.role in ('remove', 'viewer') else set()
            if body.role == 'remove':
                stops.update(group_authority.revoke_key_grants(
                    s, group, who.username, person=person, reason='member_left' if leaving else 'member_removed'))
            stop_intent(s, stops)
            record(s, who, group, 'membership', user_id=user_id, role=body.role,
                   **({'remote_placements_revoked': revoked} if revoked else {}))
            s.commit()
            result = output(s, group, who)
        await stop_sessions(stops, who.username)
        return result

    @api.post('/{group_id}/archive')
    async def archive(group_id: str, body: RevisionIn, who=Depends(ctx.owner)):
        with db() as s:
            group = access(s, group_id, who)
            fence(s, group, body.revision)
            stops = group_authority.revoke_key_grants(s, group, who.username, reason='group_archived')
            revoked = []
            stops.update(group_authority.revoke_execution(s, group_id, revoked=revoked))
            stop_intent(s, stops)
            group.archived = True
            record(s, who, group, 'archive', **({'remote_placements_revoked': revoked} if revoked else {}))
            s.commit()
            result = output(s, group, who)
        await stop_sessions(stops, who.username)
        return result

    @api.post('/{group_id}/unpause')
    def unpause(group_id: str, body: RevisionIn, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who, admin=True)
            fence(s, group, body.revision)
            from datetime import timedelta

            from ..models import utcnow
            group.paused = False
            group.override_until = utcnow() + timedelta(minutes=15)
            record(s, who, group, 'pause_override')
            s.commit()
            return output(s, group, who)

    @api.get('/{group_id}/projects')
    def projects(group_id: str, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who)
            return [project_output(s, p) for p in s.scalars(select(Project).where(Project.group_id == group.id))]

    @api.post('/{group_id}/projects', status_code=201)
    def create_project(group_id: str, body: ProjectIn, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who, admin=True)
            fence(s, group, body.revision)
            for user_id in body.owners:
                if s.get(GroupMembership, (group_id, user_id)) is None or s.get(GroupMembership, (group_id, user_id)).role != 'maintainer':
                    raise HTTPException(422, "Choose project owners from the group's current maintainers.")
            if s.scalar(select(Project.id).where(Project.slug == body.slug)) is not None or body.slug == 'local' or body.slug.startswith('local-'):
                raise HTTPException(409, 'Another project already uses that short name. Choose a different one.')
            row = Project(group_id=group_id, owner=None, name=body.name, slug=body.slug,
                          workspace_path='', account='', git_backup=False, private_workspace=body.workspace)
            s.add(row)
            s.flush()
            for user_id in set(body.owners):
                s.add(GroupProjectOwner(project_id=row.id, user_id=user_id))
            record(s, who, group, 'project_create', project_id=row.id)
            s.commit()
            return project_output(s, row)

    @api.post('/{group_id}/projects/{project_id}/owners')
    def set_project_owners(group_id: str, project_id: int, body: OwnersIn, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who, admin=True)
            row = project(s, group, project_id)
            fence(s, group, body.revision)
            for user_id in body.owners:
                member = s.get(GroupMembership, (group_id, user_id))
                if member is None or member.role != 'maintainer':
                    raise HTTPException(422, "Choose project owners from the group's current maintainers.")
            for entry in s.scalars(select(GroupProjectOwner).where(GroupProjectOwner.project_id == project_id)):
                s.delete(entry)
            s.flush()
            for user_id in set(body.owners):
                s.add(GroupProjectOwner(project_id=project_id, user_id=user_id))
            record(s, who, group, 'project_owners', project_id=project_id, owners=body.owners)
            s.commit()
            return project_output(s, row)

    @api.post('/{group_id}/projects/{project_id}/archive')
    def archive_project(group_id: str, project_id: int, body: RevisionIn, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who, admin=True)
            row = project(s, group, project_id)
            fence(s, group, body.revision)
            from ..group_authority import revoke_execution
            revoked = []
            revoke_execution(s, group_id, project_id=project_id, revoked=revoked)
            row.archived = True
            record(s, who, group, 'project_archive', project_id=project_id,
                   **({'remote_placements_revoked': revoked} if revoked else {}))
            s.commit()
            return project_output(s, row)

    @api.get('/{group_id}/projects/{project_id}/placements')
    def placements(group_id: str, project_id: int, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who)
            project(s, group, project_id)
            if not who.is_owner and role(s, group_id, who) == 'viewer':
                raise HTTPException(404, "Group viewers don't have private checkouts.")
            query = select(Placement).where(Placement.project_id == project_id)
            if not who.is_owner:
                query = query.where(Placement.person == who.username)
            return [{'id': p.id, 'person': p.person, 'host': p.host, 'account': p.account, 'path': p.path,
                     'trust': p.trust, 'sync_status': p.sync_status} for p in s.scalars(query)]

    @api.post('/{group_id}/projects/{project_id}/approvals', status_code=201)
    def approve(group_id: str, project_id: int, body: ApprovalIn, who=Depends(ctx.principal)):
        with db() as s:
            group = access(s, group_id, who, admin=True)
            project(s, group, project_id)
            # Instance owners may administer the group, but merges require a named maintainer's review.
            if role(s, group_id, who) != 'maintainer' or s.get(GroupProjectOwner, (project_id, who.user_id)) is None:
                raise HTTPException(404, 'Only a maintainer named as an owner of this project can approve merges.')
            fence(s, group, body.revision)
            approval = GroupMergeApproval(project_id=project_id, approved_by=who.username, revision=group.revision,
                                           branch=body.branch, old_sha=body.old_sha, new_sha=body.new_sha)
            s.add(approval)
            record(s, who, group, 'merge_approve', project_id=project_id, branch=body.branch,
                   old_sha=body.old_sha, new_sha=body.new_sha)
            s.commit()
            return {'id': approval.id, 'revision': group.revision, 'applied': False}

    @api.post('/{group_id}/projects/{project_id}/placements', status_code=201)
    def placement(group_id: str, project_id: int, body: PlacementIn, who=Depends(ctx.owner)):
        with db() as s:
            group = access(s, group_id, who)
            row = project(s, group, project_id)
            fence(s, group, body.revision)
            member = s.get(GroupMembership, (group_id, body.user_id))
            user = s.get(User, body.user_id)
            if member is None or member.role == 'viewer' or user is None:
                raise HTTPException(422, "Choose a current member or maintainer of the group. Viewers can't have private checkouts.")
            account = user.account or ctx.settings.default_account
            # Numeric IDs can recycle after retirement; Rynhult usernames never do.
            person_path = auth.digest(user.username)[:32]
            path = str(ctx.settings.account_root(account) / 'groups' / group_id / str(row.id) / person_path)
            existing = s.scalar(select(Placement.id).where(Placement.project_id == row.id, Placement.person == user.username,
                                                         Placement.host == 'local', Placement.account == account, Placement.path == path))
            if existing:
                raise HTTPException(409, 'This person already has a private checkout for this project.')
            binding = Placement(project_id=row.id, person=user.username, host='local', account=account, path=path,
                                approved_by=who.username)
            s.add(binding)
            record(s, who, group, 'placement_approve', project_id=project_id, user_id=user.id)
            s.commit()
            return {'id': binding.id, 'trust': binding.trust, 'sync_status': binding.sync_status}

    @api.post('/{group_id}/key-grants', status_code=201)
    def grant(group_id: str, body: KeyGrantIn, who=Depends(ctx.principal)):
        with db() as s:
            key = s.get(VaultKey, body.key_id)
            if key is None or key.owner != who.username:
                raise HTTPException(404, 'Key not found.')
            group = access(s, group_id, who, key_owner=True)
            fence(s, group, body.revision)
            if body.project_id is not None:
                project(s, group, body.project_id)
            consent = GroupKeyGrant(key_id=key.id, group_id=group_id, project_id=body.project_id,
                                    host=body.host, given_by=who.username)
            s.add(consent)
            record(s, who, group, 'key_grant', key_id=key.id, project_id=body.project_id, host=body.host)
            s.commit()
            return {'id': consent.id, 'delivery_available': False}

    @api.post('/{group_id}/key-grants/{grant_id}/revoke')
    async def revoke_grant(group_id: str, grant_id: int, body: RevisionIn, who=Depends(ctx.principal)):
        with db() as s:
            consent = s.get(GroupKeyGrant, grant_id)
            key = s.get(VaultKey, consent.key_id) if consent else None
            if consent is None or consent.group_id != group_id or key is None or key.owner != who.username:
                raise HTTPException(404, 'That key permission was not found.')
            group = access(s, group_id, who, key_owner=True)
            # Revoking consent remains possible after archival. It never widens authority.
            if group.revision != body.revision:
                raise HTTPException(409, 'The group changed while you were looking at it. Reload the page and try again.')
            group.revision += 1
            stops = group_authority.revoke_key_grants(s, group, who.username, consent=consent)
            stop_intent(s, stops)
            s.commit()
        await stop_sessions(stops, who.username)
        return {'ok': True}

    @api.get('/{group_id}/audit')
    def audit(group_id: str, who=Depends(ctx.principal)):
        with db() as s:
            access(s, group_id, who, admin=True)
            # No detail JSON, key names, paths or unrelated audit records.
            return [{'id': a.id, 'ts': a.ts, 'actor': a.actor, 'action': a.action}
                    for a in s.scalars(select(AuditLog).where(AuditLog.target == group_id,
                        AuditLog.action.in_(['group.' + a for a in ('create', 'membership', 'archive', 'pause_override',
                            'project_create', 'project_owners', 'project_archive', 'merge_approve', 'placement_approve', 'placement_provision', 'publish', 'sync', 'integrate', 'tag', 'backup', 'backup_download', 'restore', 'key_grant', 'key_revoke', 'key_use', 'policy', 'quota_pause', 'task', 'claim', 'claim_shared', 'claim_release', 'source_trust', 'remote_placement_request', 'remote_placement_approve', 'remote_placement_revoke', 'operation_resolve', 'operation_assign', 'operation_ack', 'key_attach')]))
                        .order_by(AuditLog.id.desc()).limit(200))]

    return api
