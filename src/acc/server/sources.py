"""Legacy-primary compatibility projection. No discovery, credential use or new target selection."""
import uuid

from sqlalchemy import and_, event, or_, select, update
from sqlalchemy.orm import Session as DBSession

from .models import AuditLog, Base, Node, Placement, Project
from .source_models import (
    ExecutionAccount,
    ExecutionHost,
    LocationRegistration,
    ProjectLaunchDefault,
    ProjectLocation,
    ProjectRepository,
    RepositoryLocator,
    SessionTarget,
    WorkingLocation,
)
from .source_schema_v1 import backfill, sanitize_locator


class Refused(ValueError):
    pass


@event.listens_for(DBSession, 'before_flush')
def _changed_registrations(db, flush_context, instances):
    if any(isinstance(row, (Project, Placement)) for row in db.new | db.dirty):
        db.info['_source_projects'] = [row for row in db.new | db.dirty if isinstance(row, (Project, Placement))]


@event.listens_for(DBSession, 'after_flush_postexec')
def _register_legacy(db, flush_context):
    changed = db.info.pop('_source_projects', [])
    ids = {row.id if isinstance(row, Project) else row.project_id for row in changed}
    if ids:
        backfill(db.connection(), ids, metadata=Base.metadata)


@event.listens_for(DBSession, 'after_soft_rollback')
def _discard_pending(db, previous_transaction):
    db.info.pop('_source_projects', None)


def inventory(db, project, *, person=None, include_origin=True):
    """Caller authorizes the project first. Non-owner group callers pass exact person."""
    memberships = list(db.scalars(select(ProjectRepository).where(ProjectRepository.project_id == project.id)))
    repositories = []
    for membership in memberships:
        locator = db.get(RepositoryLocator, membership.locator_id) if membership.locator_id else None
        repositories.append({'membership_id': membership.id, 'repository_id': membership.repository_id,
            'locator_id': membership.locator_id,
            'locator': locator.locator if locator and (project.group_id is None or person is None) else None,
            'verification': locator.verification if locator else 'verified',
            'compatibility_primary': membership.compatibility_primary, 'revision': membership.revision})
    query = select(ProjectLocation).where(ProjectLocation.project_id == project.id)
    if person is not None:
        query = query.join(LocationRegistration, ProjectLocation.registration_id == LocationRegistration.id).outerjoin(
            Placement, LocationRegistration.placement_id == Placement.id).where(or_(
                Placement.person == person,
                and_(LocationRegistration.legacy_placement_id.is_(None), include_origin)))
    default = db.get(ProjectLaunchDefault, project.id)
    locations = []
    for link in db.scalars(query):
        registration = db.get(LocationRegistration, link.registration_id)
        if not include_origin and registration.legacy_placement_id is None:
            continue
        location = db.get(WorkingLocation, link.location_id)
        host = db.get(ExecutionHost, location.host_id)
        account = db.get(ExecutionAccount, location.account_id)
        # Current Placement visibility remains authoritative, not frozen person provenance.
        placement = None
        if registration.legacy_placement_id is not None:
            placement = db.get(Placement, registration.placement_id) if registration.placement_id else None
            if placement is None or (person is not None and placement.person != person):
                continue
        node = db.get(Node, host.node_id) if host.node_id else None
        locations.append({'id': link.id, 'location_id': location.id, 'membership_id': link.membership_id,
            'registration_id': registration.id, 'placement_id': registration.placement_id,
            'person': placement.person if placement else project.owner,
            'trust': placement.trust if placement else 'legacy' if project.trusted else 'pending',
            'sync_status': placement.sync_status if placement else 'legacy',
            'import_mode': project.import_mode, 'archived': project.archived,
            'host': node.name if node else host.legacy_name, 'host_id': host.id, 'account': account.legacy_account,
            'account_id': account.id, 'path': location.recorded_path, 'verification': location.verification,
            'compatibility_default': bool(default and default.project_location_id == link.id),
            'provenance': {key: registration.provenance[key] for key in ('import_mode', 'managed_checkout')
                           if key in registration.provenance}})
    return {'repositories': repositories, 'locations': locations}


def require_legacy_rebindable(db, project):
    """Existing single-source routes cannot overwrite verified or multi-source state."""
    with db.no_autoflush:
        members = list(db.scalars(select(ProjectRepository).where(ProjectRepository.project_id == project.id)))
        if len(members) > 1 or any(not row.compatibility_primary or row.repository_id for row in members):
            raise Refused('This project has verified or multiple repositories. Its source cannot be changed through this operation.')
        for member in members:
            locator = db.get(RepositoryLocator, member.locator_id)
            if not locator or locator.repository_id or locator.verification != 'unresolved':
                raise Refused('This project has a verified repository. Its source cannot be changed through this operation.')
        return members[0] if members else None


def rebind_legacy_source(db, project, *, actor):
    """An authorized legacy writer explicitly binds the new single source.

    Retain the previous membership/locator for frozen sessions. Never change
    registration custody or infer that an unresolved locator is a verified repo.
    Caller commits this and the ordinary legacy edit in the same transaction.
    """
    with db.no_autoflush:
        db.scalar(select(Project.id).where(Project.id == project.id).with_for_update())
        previous = require_legacy_rebindable(db, project)
        remote = project.remote_url or project.repo
        old_locator = db.get(RepositoryLocator, previous.locator_id) if previous else None
        locator_text = sanitize_locator(remote) if remote else None
        if (old_locator.locator if old_locator else None) == locator_text:
            return
        links = list(db.scalars(select(ProjectLocation).where(ProjectLocation.project_id == project.id)))
        # Project IDs on these links/default cascade to NULL when the old
        # membership is retired. Restore the same link IDs to the new binding.
        if previous:
            db.execute(update(ProjectRepository).where(ProjectRepository.id == previous.id).values(project_id=None))
        member_id = None
        if remote:
            authority = 'group:' + project.group_id if project.group_id else 'person:' + (project.owner or '@api')
            locator_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute(RepositoryLocator.__table__.insert().values(id=locator_id, authority=authority, locator=locator_text))
            db.execute(ProjectRepository.__table__.insert().values(id=member_id, project_id=project.id,
                legacy_project_id=project.id, locator_id=locator_id, visibility_domain=authority, compatibility_primary=True))
        for link in links:
            db.execute(update(ProjectLocation).where(ProjectLocation.id == link.id).values(
                project_id=project.id, membership_id=member_id))
        db.execute(update(ProjectLaunchDefault).where(ProjectLaunchDefault.legacy_project_id == project.id).values(
            project_id=project.id, revision=ProjectLaunchDefault.revision + 1))
        db.add(AuditLog(actor=actor, action='project.source_rebind', target=project.slug,
                       detail={'project_id': project.id, 'previous_membership_id': previous.id if previous else None,
                               'membership_id': member_id}))


def approve_initial_placement(db, placement, *, actor):
    """Bind a pending empty-path registration only at its existing approval step."""
    with db.no_autoflush:
        link = db.scalar(select(ProjectLocation).join(LocationRegistration,
            ProjectLocation.registration_id == LocationRegistration.id).where(
                ProjectLocation.project_id == placement.project_id,
                LocationRegistration.placement_id == placement.id))
        if link is None:
            raise Refused('This checkout has no location registration. Reload the request before approving it.')
        registration = db.get(LocationRegistration, link.registration_id)
        location = db.get(WorkingLocation, link.location_id)
        account = db.get(ExecutionAccount, location.account_id)
        if location.recorded_path == placement.path:
            return
        if (location.recorded_path or registration.provenance['path'] or not placement.path
                or placement.trust != 'approved' or placement.sync_status != 'ready'
                or account.legacy_account != placement.account or link.person != placement.person
                or account.machine_account_id != placement.machine_account_id
                or db.scalar(select(SessionTarget.session_id).where(SessionTarget.location_id == location.id).limit(1))):
            raise Refused('This checkout location changed after registration. Review the request before approving it.')
        location.recorded_path = placement.path
        location.revision += 1
        link.trust, link.sync_status = placement.trust, placement.sync_status
        db.add(AuditLog(actor=actor, action='project.location_bind', target=str(placement.project_id),
                       detail={'placement_id': placement.id, 'location_id': location.id,
                               'revision': location.revision}))


def compatibility_target(db, project, placement=None):
    """Pin existing consumers to one explicit legacy target; never choose list[0]."""
    memberships = list(db.scalars(select(ProjectRepository).where(ProjectRepository.project_id == project.id)))
    if len(memberships) > 1 or (memberships and not memberships[0].compatibility_primary):
        raise Refused('This project has multiple or unreviewed repositories. Review its launch target before starting a session.')
    membership = memberships[0] if memberships else None
    if bool(project.remote_url or project.repo) != bool(membership):
        raise Refused('The project source changed. Review its launch target before starting a session.')
    if membership and membership.locator_id:
        locator = db.get(RepositoryLocator, membership.locator_id)
        if locator.locator != sanitize_locator(project.remote_url or project.repo):
            raise Refused('The project source changed. Review its launch target before starting a session.')
    default = None
    if placement is None:
        default = db.get(ProjectLaunchDefault, project.id)
        link = db.get(ProjectLocation, default.project_location_id) if default else None
    else:
        link = db.scalar(select(ProjectLocation).join(LocationRegistration,
            ProjectLocation.registration_id == LocationRegistration.id).where(
                ProjectLocation.project_id == project.id, LocationRegistration.placement_id == placement.id))
    if link is None or link.project_id != project.id or link.membership_id != (membership.id if membership else None):
        raise Refused('This project has no reviewed launch target. Review its location before starting a session.')
    location = db.get(WorkingLocation, link.location_id)
    registration = db.get(LocationRegistration, link.registration_id)
    host = db.get(ExecutionHost, location.host_id)
    account = db.get(ExecutionAccount, location.account_id)
    expected = (placement.host, placement.account, placement.path) if placement else (project.host, project.account, project.workspace_path)
    # Enrolled host/account generation binding is metadata-only until an explicit
    # owner rebind exists. Existing enrollment, broker and group-machine checks
    # remain authoritative; this sidecar must not strand a legitimate reinstall.
    if (account.legacy_account, location.recorded_path) != expected[1:] or (
            host.kind != 'enrolled' and host.legacy_name != expected[0]):
        raise Refused('The project location changed. Review its launch target before starting a session.')
    if placement and (account.machine_account_id != placement.machine_account_id or link.person != placement.person):
        raise Refused('The project account changed. Review its launch target before starting a session.')
    # New selectors stay disabled for unverified account generations. This frozen
    # binding only accompanies the existing authorization and routing checks.
    return {'membership_id': link.membership_id, 'membership_revision': membership.revision if membership else None,
        'repository_id': membership.repository_id if membership else None,
        'location_id': location.id, 'location_revision': location.revision,
        'registration_id': registration.id, 'default_revision': default.revision if default else None, 'host_id': host.id, 'account_id': account.id,
        'host': expected[0], 'account': expected[1], 'path': expected[2]}


def freeze_session(db, session, target):
    if db.get(SessionTarget, session.id) is None:
        # The compatibility default identifies the source, not necessarily the
        # session's execution directory (a worktree/helper/group sub-checkout).
        actual = compatibility_target(db, session.project, session.placement)
        location = db.get(WorkingLocation, actual['location_id'])
        if session.worktree and session.worktree == session.cwd:
            location.kind = 'worktree'
        db.add(SessionTarget(session_id=session.id, membership_id=actual['membership_id'],
            location_id=actual['location_id'], registration_id=actual['registration_id'],
            snapshot={**actual, 'compatibility_origin': target, 'workdir': session.workdir,
                      'worktree': session.worktree, 'branch': session.worktree_branch,
                      'run_as': session.run_as, 'execution_host': session.host}))


def session_problem(db, session):
    """Never retarget a frozen session on resume/retry. Old unresolved history stays old."""
    frozen = db.get(SessionTarget, session.id)
    if frozen is None:
        return None
    saved = frozen.snapshot
    location = db.get(WorkingLocation, frozen.location_id)
    host = db.get(ExecutionHost, location.host_id)
    # Defer the new enrollment-generation refusal until owner rebind is shipped.
    # Current node authentication/revocation and group authorization still run.
    host_changed = host.kind != 'enrolled' and session.host != saved['execution_host']
    if (host_changed or session.cwd != saved['path']
            or session.run_as != saved['run_as'] or session.worktree != saved['worktree']
            or session.workdir != saved['workdir'] or session.worktree_branch != saved['branch']):
        return "This session's recorded location changed. Ask an owner to review it before resuming."
    return None
