"""Own-machine group authority. Enrollment, account identity and project consent are separate."""
from sqlalchemy import select

from .models import GroupCheckout, GroupMachineAccount, GroupWork, Node, Placement, Session


def valid(db, placement):
    binding = db.get(GroupMachineAccount, placement.machine_account_id) if placement.machine_account_id else None
    node = db.get(Node, binding.node_id) if binding else None
    from .agent_setup import valid_agent_account
    return bool(binding and valid_agent_account(binding.account) and binding.status == 'approved' and binding.proof_verified_at is not None and node and node.status == 'active'
                and node.name == placement.host and binding.person == placement.person
                and binding.account == placement.account and placement.trust == 'approved'
                and placement.sync_status == 'ready')


def source_placement(db, session):
    work = db.get(GroupWork, session.id)
    checkout = db.get(GroupCheckout, work.checkout_id) if work else None
    return db.get(Placement, checkout.placement_id) if checkout else None


def revoke_placements(db, *, person=None, binding_id=None, placement_id=None, group_id=None, project_id=None, revoked=None):
    """Revoke matching own-machine placements; returns the sessions to stop. `revoked` collects the placement ids
    (for the caller's audit entry)."""
    from sqlalchemy import delete

    from .models import Group, GroupClaim, Project
    query = select(Placement).join(Project).where(Placement.machine_account_id.is_not(None))
    if project_id is not None:
        query = query.where(Placement.project_id == project_id)
    if person is not None:
        query = query.where(Placement.person == person)
    if group_id is not None:
        query = query.where(Project.group_id == group_id)
    if binding_id is not None:
        query = query.where(Placement.machine_account_id == binding_id)
    if placement_id is not None:
        query = query.where(Placement.id == placement_id)
    rows = list(db.scalars(query))
    # Serialize revocation with exchange Git writes (which hold the group fence).
    from sqlalchemy import update
    for gid in sorted({db.get(Project, p.project_id).group_id for p in rows}):
        db.execute(update(Group).where(Group.id == gid).values(board_revision=Group.board_revision))
    ids = [p.id for p in rows]
    if revoked is not None:
        revoked.extend(p.id for p in rows if p.trust != 'revoked')
    for pid in ids:
        db.get(Placement, pid).trust = 'revoked'
    stops = []
    for session in db.scalars(select(Session).join(GroupWork, GroupWork.session_id == Session.id)
                             .join(GroupCheckout, GroupCheckout.id == GroupWork.checkout_id)
                             .where(GroupCheckout.placement_id.in_(ids))):
        if session.desired_state == 'running':
            session.desired_state = 'stopped'
            session.team_stop_pending = True
            stops.append(session.id)
        db.execute(delete(GroupClaim).where(GroupClaim.session_id == session.id))
    return stops


def connected(brokers, db, placement):
    if not valid(db, placement):
        return False
    binding = db.get(GroupMachineAccount, placement.machine_account_id)
    try:
        link = brokers.hub.get(placement.host)
    except (AttributeError, ConnectionError):
        return False
    return link.node_id == binding.node_id and placement.account in link.accounts


def reserved_for_other(db, host, account, person):
    """Revocation doesn't erase somebody's private files or free their account for another person."""
    return db.scalar(select(GroupMachineAccount.id).join(Node).where(
        Node.name == host, GroupMachineAccount.account == account, GroupMachineAccount.approved_by.is_not(None),
        GroupMachineAccount.person != person).limit(1)) is not None


def personal_conflict(db, session):
    from .models import User
    if session.host == 'local' or session.project.group_id:
        return False
    user = db.scalar(select(User).where(User.username == session.owner))
    return bool(user and user.role != 'owner' and reserved_for_other(db, session.host, session.run_as, session.owner))
