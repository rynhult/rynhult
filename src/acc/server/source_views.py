"""Read-only, audience-scoped P4 projections. No provider, broker or file reads."""
from sqlalchemy import case, exists, func, or_, select

from .models import GroupMachineAccount, Node, Placement, Project, Session
from .source_models import (
    ExecutionAccount,
    ExecutionHost,
    HostingInstance,
    LocationRegistration,
    ProjectLaunchDefault,
    ProjectLocation,
    ProjectRepository,
    Repository,
    RepositoryLocator,
    SourceOwner,
    WorkingLocation,
)


def audience(project):
    return 'group:' + project.group_id if project.group_id else 'person:' + (project.owner or '@api')


def visible_memberships(project):
    domain = audience(project)
    return select(ProjectRepository).outerjoin(RepositoryLocator, RepositoryLocator.id == ProjectRepository.locator_id).where(
        ProjectRepository.project_id == project.id, ProjectRepository.visibility_domain == domain,
        or_(ProjectRepository.locator_id.is_(None), RepositoryLocator.authority == domain))


def snapshot(db, project, *, person=None, include_origin=True, locations_allowed=True, is_owner=False):
    """Caller authorizes the project first; metadata scope never grants execution."""
    member_query = visible_memberships(project)
    scoped = member_query.subquery()
    membership_count = db.scalar(select(func.count()).select_from(scoped))
    identity = case((scoped.c.repository_id.is_not(None), 'repository:' + scoped.c.repository_id),
                    else_='locator:' + scoped.c.locator_id)
    repository_count = db.scalar(select(func.count(func.distinct(identity))).select_from(scoped))
    # Batch metadata instead of one Repository/Owner/Instance lookup per row.
    details = member_query.add_columns(RepositoryLocator, Repository, SourceOwner, HostingInstance).outerjoin(
        Repository, Repository.id == ProjectRepository.repository_id).outerjoin(
        SourceOwner, SourceOwner.id == Repository.owner_id).outerjoin(
        HostingInstance, HostingInstance.id == Repository.instance_id)
    memberships = list(db.execute(details.order_by(ProjectRepository.id).limit(100)))
    repositories, by_identity = [], {}
    for member, locator, repo, owner, instance in memberships:
        key = ('repository', member.repository_id) if member.repository_id else ('locator', member.locator_id)
        if key in by_identity:
            row = by_identity[key]
            row['membership_ids'].append(member.id)
            row['compatibility_primary'] = row['compatibility_primary'] or member.compatibility_primary
            continue
        repositories.append({
            'membership_id': member.id, 'membership_ids': [member.id], 'repository_id': member.repository_id,
            'locator_id': member.locator_id, 'locator': locator.locator if locator and include_origin else None,
            'verification': locator.verification if locator else 'verified',
            'name': repo.name if repo and include_origin else None,
            'owner': {'id': owner.id, 'login': owner.login} if owner and include_origin else None,
            'instance': {'id': instance.id, 'provider': instance.provider, 'api_origin': instance.api_origin} if instance and include_origin else None,
            'compatibility_primary': member.compatibility_primary,
        })
        by_identity[key] = repositories[-1]
    location_query = select(ProjectLocation, LocationRegistration, WorkingLocation, ExecutionHost, ExecutionAccount, Placement, Node).join(
        LocationRegistration, LocationRegistration.id == ProjectLocation.registration_id).join(
        WorkingLocation, WorkingLocation.id == ProjectLocation.location_id).join(
        ExecutionHost, ExecutionHost.id == WorkingLocation.host_id).join(
        ExecutionAccount, ExecutionAccount.id == WorkingLocation.account_id).outerjoin(
        Placement, Placement.id == LocationRegistration.placement_id).outerjoin(
        Node, Node.id == ExecutionHost.node_id).where(
            ProjectLocation.project_id == project.id, WorkingLocation.authority == audience(project),
            or_(ProjectLocation.membership_id.is_(None), ProjectLocation.membership_id.in_(member_query.with_only_columns(ProjectRepository.id))),
            or_(LocationRegistration.legacy_placement_id.is_(None), Placement.id.is_not(None)))
    if not include_origin:
        location_query = location_query.where(LocationRegistration.legacy_placement_id.is_not(None))
    if person is not None:
        location_query = location_query.where(or_(LocationRegistration.legacy_placement_id.is_(None), Placement.person == person))
    scoped_locations = location_query.with_only_columns(ProjectLocation.id, ProjectLocation.location_id).subquery()
    registration_count = db.scalar(select(func.count()).select_from(scoped_locations)) if locations_allowed else 0
    location_count = db.scalar(select(func.count(func.distinct(scoped_locations.c.location_id)))) if locations_allowed else 0
    locations = []
    default = db.get(ProjectLaunchDefault, project.id)
    rows = db.execute(location_query.order_by(ProjectLocation.id).limit(100)) if locations_allowed else []
    for link, registration, location, host, account, placement, node in rows:
        # Count registrations, not path spellings or guessed physical directories.
        locations.append({
            'id': link.id, 'location_id': location.id, 'membership_id': link.membership_id,
            'registration_id': registration.id, 'placement_id': registration.placement_id,
            'host': node.name if node else host.legacy_name, 'host_id': host.id,
            'account': account.legacy_account, 'account_id': account.id, 'path': location.recorded_path,
            'verification': location.verification, 'trust': placement.trust if placement else 'legacy' if project.trusted else 'pending',
            'sync_status': placement.sync_status if placement else 'legacy',
            'compatibility_default': bool(default and default.project_location_id == link.id),
            'host_status': 'local' if host.kind == 'local' else 'not_checked',
            'last_seen_at': node.last_seen.isoformat() if is_owner and node and node.last_seen else None,
        })
    # Ownership transfer does not grant a new metadata audience or execution authority.
    # Only installation owners receive this neutral existence notice, never hidden details.
    audience_mismatch = bool(is_owner and not project.group_id and db.scalar(select(or_(
        exists(select(ProjectRepository.id).outerjoin(RepositoryLocator, RepositoryLocator.id == ProjectRepository.locator_id).where(
            ProjectRepository.project_id == project.id, or_(ProjectRepository.visibility_domain != audience(project),
                                                            RepositoryLocator.authority != audience(project)))),
        exists(select(ProjectLocation.id).join(WorkingLocation, WorkingLocation.id == ProjectLocation.location_id).where(
            ProjectLocation.project_id == project.id, WorkingLocation.authority != audience(project)))))))
    return {'project_id': project.id, 'repositories': repositories, 'locations': locations,
            'audience_mismatch': audience_mismatch,
            'counts': {'repositories': repository_count, 'locations': location_count},
            'membership_count': membership_count, 'registration_count': registration_count,
            'truncated': {'repositories': membership_count > len(memberships), 'locations': registration_count > len(locations)},
            'counts_scope': 'visible', 'read_only': True}


def directory(db, who, *, q='', source_owner=None, host_id=None, offset=0, limit=30,
              projects_only=False, include_archived=True, sort=None, direction="asc"):
    """Personal directory only, like /api/projects. Filter whole edges before pagination.

    Facets/counts describe this authorized page, never a global repository census.
    Group views have their separate current-membership-authorized endpoint.
    """
    query = select(Project).where(Project.group_id.is_(None))
    if projects_only:
        query = query.where(Project.private_workspace.is_(False), Project.slug != 'local',
                            ~Project.slug.startswith('local-', autoescape=True))
    if not include_archived:
        query = query.where(Project.archived.is_(False))
    if not who.is_owner:
        reserved = exists(select(GroupMachineAccount.id).join(Node).where(
            Node.name == Project.host, GroupMachineAccount.account == Project.account,
            GroupMachineAccount.approved_by.is_not(None), GroupMachineAccount.person != who.username))
        query = query.where(Project.owner == who.username, or_(Project.host == 'local', ~reserved))
    domain = 'person:' + func.coalesce(Project.owner, '@api')
    membership = select(ProjectRepository.id).outerjoin(RepositoryLocator, RepositoryLocator.id == ProjectRepository.locator_id).where(
        ProjectRepository.project_id == Project.id, ProjectRepository.visibility_domain == domain,
        or_(ProjectRepository.locator_id.is_(None), RepositoryLocator.authority == domain))
    if q:
        source_text = membership.outerjoin(Repository, Repository.id == ProjectRepository.repository_id).outerjoin(
            SourceOwner, SourceOwner.id == Repository.owner_id).where(or_(
                Repository.name.icontains(q, autoescape=True), SourceOwner.login.icontains(q, autoescape=True),
                RepositoryLocator.locator.icontains(q, autoescape=True)))
        query = query.where(or_(Project.name.icontains(q, autoescape=True), Project.slug.icontains(q, autoescape=True),
                                Project.notes.icontains(q, autoescape=True), exists(source_text.correlate(Project))))
    if source_owner:
        membership = membership.join(Repository, Repository.id == ProjectRepository.repository_id).where(Repository.owner_id == source_owner)
    def location_query():
        query = select(ProjectLocation.id).join(WorkingLocation, WorkingLocation.id == ProjectLocation.location_id).join(
            LocationRegistration, LocationRegistration.id == ProjectLocation.registration_id).outerjoin(
            Placement, Placement.id == LocationRegistration.placement_id).where(
                ProjectLocation.project_id == Project.id, WorkingLocation.authority == domain,
                or_(LocationRegistration.legacy_placement_id.is_(None), Placement.id.is_not(None)))
        if not who.is_owner:
            query = query.where(or_(LocationRegistration.legacy_placement_id.is_(None), Placement.person == who.username))
        return query

    if host_id:
        # Bind the location to this membership; never repo A's owner + repo B's host.
        location = exists(location_query().where(ProjectLocation.membership_id == ProjectRepository.id,
                                                  WorkingLocation.host_id == host_id)).correlate(Project, ProjectRepository)
        membership = membership.where(location)
    if source_owner or host_id:
        matches = exists(membership.correlate(Project))
        if host_id and not source_owner:
            local = exists(location_query().where(ProjectLocation.membership_id.is_(None), WorkingLocation.host_id == host_id)).correlate(Project)
            matches = or_(matches, local)
        query = query.where(matches)
    # Aggregate only sessions the directory viewer may see. Sort before pagination.
    latest = Session.status_at
    for timestamp in (Session.activity_at, Session.last_output_at):
        latest = case((latest.is_(None), timestamp), (timestamp > latest, timestamp), else_=latest)
    activity_query = select(func.max(latest)).where(Session.project_id == Project.id)
    if not who.is_owner:
        activity_query = activity_query.where(Session.owner == who.username)
    activity = activity_query.correlate(Project).scalar_subquery()
    status = case((Project.archived, 'Archived'), (Project.import_mode == 'readonly', 'Read-only'),
                  (~Project.trusted, 'Not trusted'), else_='Ready')
    runtime = case((Project.compose_autostart, 'Autostart on'), else_='Autostart off')
    columns = {'name': func.lower(Project.name), 'status': status, 'activity': activity, 'runtime': runtime}
    column = columns.get(sort, Project.slug)
    order = column.desc() if direction == 'desc' else column.asc()
    tie_break = [func.lower(Project.name), Project.id] if sort in ('status', 'runtime') else [Project.id]
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.add_columns(activity.label("last_activity"), status.label("status"), runtime.label("runtime")).order_by(order.nulls_last(), *tie_break).offset(offset).limit(limit))
    items, owners, hosts = [], {}, {}
    for project, last_activity, project_status, project_runtime in rows:
        view = snapshot(db, project, person=None if who.is_owner else who.username, is_owner=who.is_owner)
        items.append({'id': project.id, 'name': project.name, 'slug': project.slug, 'last_activity': last_activity, 'status': project_status, 'runtime': project_runtime, **view})
        for repo in view['repositories']:
            if repo['owner']:
                owners[repo['owner']['id']] = {**repo['owner'], 'instance': repo['instance']}
        for location in view['locations']:
            hosts[location['host_id']] = {'id': location['host_id'], 'name': location['host']}
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit,
            'next_offset': offset + limit if offset + limit < total else None,
            'facets': {'source_owners': list(owners.values()), 'hosts': list(hosts.values()), 'scope': 'visible_page'}}
