"""Shared authority invariants for instance-user removal and group administration."""
from fastapi import HTTPException
from sqlalchemy import select

from .models import (
    AuditLog,
    Group,
    GroupKeyGrant,
    GroupMembership,
    GroupProjectOwner,
    Project,
    Session,
    VaultGrant,
    VaultKey,
)


def revoke_key_grants(s, group, actor, *, person=None, consent=None, reason='explicit'):
    """Delete consent and persist affected sessions' stop intent in the caller's transaction."""
    query = select(GroupKeyGrant).where(GroupKeyGrant.group_id == group.id)
    if person is not None:
        query = query.join(VaultKey).where(VaultKey.owner == person)
    grants = [consent] if consent is not None else list(s.scalars(query))
    stops = set()
    for grant in grants:
        attached = set(s.scalars(select(VaultGrant.project_id).where(VaultGrant.key_id == grant.key_id)))
        # Include in-flight deliveries whose used-key IDs are not recorded yet.
        for row in s.scalars(select(Session).join(Project).where(
                Project.group_id == group.id, Session.desired_state == 'running')):
            if grant.key_id in (row.vault_keys or []) or (
                    row.project_id in attached and (grant.project_id is None or grant.project_id == row.project_id)):
                row.desired_state = 'stopped'
                row.team_stop_pending = True
                stops.add(row.id)
        s.add(AuditLog(actor=actor, action='group.key_revoke', target=group.id,
                       detail={'group_id': group.id, 'revision': group.revision, 'grant_id': grant.id,
                               'key_id': grant.key_id, 'project_id': grant.project_id, 'host': grant.host,
                               'given_by': grant.given_by, 'reason': reason}))
        s.delete(grant)
    return stops


def remove_user(s, user_id: int, actor: str) -> set[str]:
    """Called in the same write transaction as retiring the ACC user. Never transfers group ownership."""
    memberships = s.scalars(select(GroupMembership).where(GroupMembership.user_id == user_id)).all()
    stops = set()
    for membership in memberships:
        group = s.get(Group, membership.group_id)
        if membership.role == 'maintainer':
            replacement = s.scalar(select(GroupMembership.user_id).where(
                GroupMembership.group_id == group.id, GroupMembership.role == 'maintainer',
                GroupMembership.user_id != user_id))
            if replacement is None:
                raise HTTPException(409, 'This person is the only maintainer of a group. Appoint another maintainer in Groups → Manage people first.')
        owned = s.scalars(select(GroupProjectOwner).join(Project).where(
            Project.group_id == group.id, GroupProjectOwner.user_id == user_id)).all()
        for entry in owned:
            if s.scalar(select(GroupProjectOwner.user_id).where(GroupProjectOwner.project_id == entry.project_id,
                                                               GroupProjectOwner.user_id != user_id)) is None:
                raise HTTPException(409, 'This person is the only named owner of a group project. Name another project owner in Groups first.')
            s.delete(entry)
        from .models import User
        person = s.get(User, user_id)
        revoked = []
        stops.update(revoke_execution(s, group.id, person.username if person else None, revoked=revoked))
        group.revision += 1
        if person:
            stops.update(revoke_key_grants(s, group, actor, person=person.username, reason='user_removed'))
        s.add(AuditLog(actor=actor, action='group.membership', target=group.id,
                       detail={'group_id': group.id, 'user_id': user_id, 'role': 'remove', 'revision': group.revision,
                               **({'remote_placements_revoked': revoked} if revoked else {})}))
        s.delete(membership)
    s.flush()
    return stops


def revoke_execution(s, group_id, person=None, project_id=None, revoked=None):
    """Persist stop intent with revocation; broker cleanup can retry without reopening authority."""
    from sqlalchemy import delete

    from .models import GroupClaim, Session
    query = select(Session).join(Project).where(Project.group_id == group_id)
    if person:
        query = query.where(Session.owner == person)
    if project_id is not None:
        query = query.where(Session.project_id == project_id)
    from . import group_remote
    ids = list(group_remote.revoke_placements(s, person=person, group_id=group_id, project_id=project_id, revoked=revoked))
    for session in s.scalars(query):
        if session.desired_state == 'running':
            session.desired_state = 'stopped'
            session.team_stop_pending = True  # retain concurrency until broker confirms the stop
            ids.append(session.id)
        s.execute(delete(GroupClaim).where(GroupClaim.session_id == session.id))
    return ids
