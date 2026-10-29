"""Explicit owner account approval and maintainer project approval for own machines."""
import uuid
from datetime import timedelta
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from .. import auth, group_board, group_remote
from ..agent_setup import valid_agent_account
from ..broker_client import BrokerError
from ..db import write_in_thread
from ..models import (
    AuditLog,
    Group,
    GroupMachineAccount,
    GroupMembership,
    Node,
    Placement,
    Project,
    Session,
    User,
    utcnow,
)

# Each pending request is an audit row and an entry in the owners' review list (security review 8, SR8-7).
MAX_PENDING_PER_PERSON = 5
MAX_PENDING = 100


class MachineIn(BaseModel):
    node_id: str = Field(pattern=r'^[a-f0-9]{16,32}$')
    account: str = Field(pattern=r'^[a-z_][a-z0-9_-]{0,31}$')


class ProofIn(BaseModel):
    code: str = Field(pattern=r'^[A-Z0-9]{10}$')


class PlacementIn(BaseModel):
    binding_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    revision: int = Field(ge=1)


class RevisionIn(BaseModel):
    revision: int = Field(ge=1)


def router(ctx):
    api = APIRouter()

    def enabled():
        if not ctx.settings.groups_remote_enabled:
            raise HTTPException(404, 'Own-machine group work is turned off on this Rynhult.')

    def output(row, node):
        return {'id': row.id, 'node_id': row.node_id, 'host': node.name, 'person': row.person,
                'account': row.account, 'status': row.status, 'approved_by': row.approved_by,
                'proof_verified_at': row.proof_verified_at.isoformat() if row.proof_verified_at else None}

    def live(node, account):
        if node is None or node.status != 'active':
            raise HTTPException(404, "Machine not found. It isn't enrolled, or it was removed.")
        try:
            link = ctx.broker.hub.get(node.name)
        except (ConnectionError, AttributeError):
            raise HTTPException(409, 'This machine is offline. Reconnect it, then try again.') from None
        if link.node_id != node.id or account not in link.accounts:
            raise HTTPException(409, "That machine doesn't have an account with that name. Check the account name on the machine.")

    def project_access(s, group_id, project_id, who, admin=False):
        group = group_board.access(s, group_id, who, write=True, admin=admin)
        row = s.get(Project, project_id)
        if row is None or row.group_id != group_id or row.archived:
            raise HTTPException(404, 'Group project not found.')
        return group, row

    def fence(s, group, revision):
        changed = s.execute(update(Group).where(Group.id == group.id, Group.revision == revision)
                            .values(revision=Group.revision + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, 'The group changed while you were looking at it. Reload the page and try again.')
        s.flush()

    def record(s, who, action, target, **detail):
        s.add(AuditLog(actor=who.username, action='group.' + action, target=target, detail=detail))

    def stop_intent(s, ids):
        for sid in ids:
            ctx.supervisor._set_state(s, s.get(Session, sid), 'stopped', 'api')

    async def finish_stops(ids, actor):
        for streams in list(ctx.terminals.values()):
            for stream in list(streams):
                if stream.path_params.get('acc_id') in ids:
                    try:
                        await stream.close(code=4403)
                    except RuntimeError:
                        pass
        for sid in ids:
            await ctx.supervisor.stop(sid, actor)

    @api.get('/api/group-machines')
    def bindings(who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            query = select(GroupMachineAccount, Node).join(Node)
            if not who.is_owner:
                query = query.where(GroupMachineAccount.person == who.username)
            return [output(row, node) for row, node in s.execute(query)]

    @api.post('/api/group-machines', status_code=201)
    def request_binding(body: MachineIn, who=Depends(ctx.principal)):
        enabled()
        if who.user_id is None:
            raise HTTPException(403, "Sign in as yourself to request a machine account. The API token can't do this.")
        if not valid_agent_account(body.account):
            raise HTTPException(422, 'Choose an ordinary agent account on the machine, not an administrator or service account.')
        with ctx.db() as s:
            node = s.get(Node, body.node_id)
            if node is None or node.status != 'active':
                raise HTTPException(404, "Machine not found. It isn't enrolled, or it was removed.")
            # Serialize competing claims on the node/account, including on PostgreSQL.
            s.execute(update(Node).where(Node.id == node.id).values(status=Node.status))
            if s.scalar(select(GroupMachineAccount.id).where(GroupMachineAccount.node_id == node.id,
                    GroupMachineAccount.account == body.account, GroupMachineAccount.approved_by.is_not(None))):
                raise HTTPException(409, 'That machine account is already taken. Ask an instance owner.')
            if s.scalar(select(GroupMachineAccount.id).where(GroupMachineAccount.node_id == node.id,
                    GroupMachineAccount.account == body.account, GroupMachineAccount.person == who.username)):
                raise HTTPException(409, 'You already requested this machine account.')
            pending = select(func.count()).select_from(GroupMachineAccount).where(GroupMachineAccount.status == 'pending')
            if s.scalar(pending.where(GroupMachineAccount.person == who.username)) >= MAX_PENDING_PER_PERSON:
                raise HTTPException(429, f'You have {MAX_PENDING_PER_PERSON} machine account requests waiting. '
                                         'Withdraw one, or wait for an instance owner to review them.')
            if s.scalar(pending) >= MAX_PENDING:
                raise HTTPException(429, 'Too many machine account requests are waiting. Ask an instance owner to '
                                         'review them first.')
            row = GroupMachineAccount(id=uuid.uuid4().hex, node_id=node.id, person=who.username,
                                      account=body.account, status='pending')
            s.add(row)
            record(s, who, 'machine_request', row.id, node_id=node.id, account=body.account)
            s.commit()
            return output(row, node)

    @api.post('/api/group-machines/{binding_id}/verify')
    async def verify_binding(binding_id: str, body: ProofIn, who=Depends(ctx.principal)):
        enabled()

        def prepare():
            with ctx.db() as s:
                row = s.get(GroupMachineAccount, binding_id)
                if who.user_id is None or row is None or row.person != who.username or row.status not in ('pending', 'approved'):
                    raise HTTPException(404, 'Request not found. Request the machine account again.')
                # Serialize per-person counters, including requests for different bindings on PostgreSQL.
                present = s.execute(update(User).where(User.id == who.user_id).values(username=User.username))
                if present.rowcount != 1:
                    raise HTTPException(404, 'Person not found.')
                if row.proof_verified_at is not None:
                    raise HTTPException(409, 'This request is already verified. An instance owner still needs to approve it.')
                attempts = s.scalar(select(func.count()).select_from(AuditLog).where(
                    AuditLog.actor == who.username, AuditLog.action == 'group.machine_proof_attempt',
                    AuditLog.ts > utcnow() - timedelta(minutes=1)))
                if attempts >= 10:
                    raise HTTPException(429, 'Too many tries. Wait a minute, then try again.')
                node = s.get(Node, row.node_id)
                live(node, row.account)
                identity = (node.id, node.name, row.account, row.status)
                record(s, who, 'machine_proof_attempt', row.id, node_id=node.id, account=row.account)
                s.commit()
                return identity

        node_id, host, account, requested_status = await write_in_thread(prepare)
        try:
            result = await ctx.broker.call('group_machine_verify', host=host, account=account,
                node_id=node_id, proof_account=account, request_id=binding_id, code=body.code)
            verified = result.get('verified') is True
        except (BrokerError, ConnectionError, TimeoutError):
            verified = False

        def finish():
            with ctx.db() as s:
                s.execute(update(Node).where(Node.id == node_id).values(status=Node.status))
                s.execute(update(GroupMachineAccount).where(GroupMachineAccount.id == binding_id)
                          .values(status=GroupMachineAccount.status))
                row = s.get(GroupMachineAccount, binding_id)
                node = s.get(Node, node_id)
                if (row is None or row.status != requested_status or row.person != who.username
                        or row.node_id != node_id or row.account != account or row.proof_verified_at is not None
                        or s.get(User, who.user_id) is None):
                    raise HTTPException(409, 'This request changed meanwhile. Generate a new code on your machine and try again.')
                live(node, account)
                if verified:
                    row.proof_verified_at = utcnow()
                record(s, who, 'machine_verified' if verified else 'machine_proof_refused', row.id,
                       node_id=node_id, account=account)
                response = output(row, node)
                s.commit()
                return response

        response = await write_in_thread(finish)
        if not verified:
            raise HTTPException(409, "That code didn't work: it is wrong, expired or was tried too often. Generate a new code on your machine.")
        return response

    @api.post('/api/group-machines/{binding_id}/approve')
    def approve_binding(binding_id: str, who=Depends(ctx.owner)):
        enabled()
        if who.user_id is None:  # an owner who verified the machine, not automation with the installation token
            raise HTTPException(403, 'Only an instance owner can approve machine accounts. Sign in as one to approve it.')
        with ctx.db() as s:
            row = s.get(GroupMachineAccount, binding_id)
            if row is None or row.status != 'pending':
                raise HTTPException(404, 'Machine account request not found, or it is no longer waiting for approval.')
            if s.scalar(select(User.id).where(User.username == row.person)) is None:
                raise HTTPException(404, 'Person not found.')
            node = s.get(Node, row.node_id)
            s.execute(update(Node).where(Node.id == node.id).values(status=Node.status))
            s.refresh(node)
            s.execute(update(GroupMachineAccount).where(GroupMachineAccount.id == binding_id)
                      .values(status=GroupMachineAccount.status))
            s.refresh(row)
            if row.status != 'pending':
                raise HTTPException(404, 'Machine account request not found, or it is no longer waiting for approval.')
            if s.scalar(select(GroupMachineAccount.id).where(GroupMachineAccount.node_id == node.id,
                    GroupMachineAccount.account == row.account, GroupMachineAccount.approved_by.is_not(None))):
                raise HTTPException(409, 'That machine account is already approved for someone else. This request can only be cancelled.')
            if row.proof_verified_at is None:
                raise HTTPException(409, 'The person must first confirm the request on their machine with Verify on the machine. Then you can approve it.')
            if s.scalar(select(Placement.id).where(Placement.host == node.name,
                    Placement.account == row.account,
                    (Placement.person.is_(None)) | (Placement.person != row.person)).limit(1)):
                raise HTTPException(409, "That account already holds another person's private checkouts. Use a different account.")
            if s.scalar(select(Project.id).where(Project.host == node.name, Project.account == row.account,
                    Project.group_id.is_(None), (Project.owner.is_(None)) | (Project.owner != row.person)).limit(1)):
                raise HTTPException(409, "That account already holds another person's private projects. Use a different account.")
            live(node, row.account)
            row.status, row.approved_by = 'approved', who.username
            record(s, who, 'machine_approve', row.id, node_id=node.id, person=row.person, account=row.account)
            s.commit()
            return output(row, node)

    @api.post('/api/group-machines/{binding_id}/revoke')
    async def revoke_binding(binding_id: str, who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            s.execute(update(GroupMachineAccount).where(GroupMachineAccount.id == binding_id)
                      .values(status=GroupMachineAccount.status))
            row = s.get(GroupMachineAccount, binding_id)
            if row is None or (not who.is_owner and row.person != who.username):
                raise HTTPException(404, 'Machine account not found.')
            was_pending = row.status == 'pending'
            row.status = 'revoked'
            revoked = []
            ids = group_remote.revoke_placements(s, binding_id=row.id, revoked=revoked)
            stop_intent(s, ids)
            record(s, who, 'machine_cancel' if was_pending else 'machine_revoke', row.id, node_id=row.node_id,
                   **({'remote_placements_revoked': revoked} if revoked else {}))
            if was_pending:
                s.delete(row)  # no authority or placement ever existed; release an unapproved claim
            s.commit()
        await finish_stops(ids, who.username)
        return {'status': 'revoked', 'cleanup_pending': ids}

    @api.get('/api/groups/{group_id}/projects/{project_id}/remote-placements')
    def placement_reviews(group_id: str, project_id: int, who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            project_access(s, group_id, project_id, who, admin=True)
            return [{'id': p.id, 'person': p.person, 'host': p.host, 'account': p.account,
                     'trust': p.trust, 'sync_status': p.sync_status}
                    for p in s.scalars(select(Placement).where(Placement.project_id == project_id,
                                                               Placement.machine_account_id.is_not(None)))]

    @api.post('/api/groups/{group_id}/projects/{project_id}/remote-placements', status_code=201)
    def request_placement(group_id: str, project_id: int, body: PlacementIn, who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            group, project = project_access(s, group_id, project_id, who)
            fence(s, group, body.revision)
            member = s.get(GroupMembership, (group_id, who.user_id))
            binding = s.get(GroupMachineAccount, body.binding_id)
            if (not member or member.role == 'viewer' or binding is None
                    or binding.person != who.username or binding.status != 'approved' or binding.proof_verified_at is None):
                raise HTTPException(404, 'Approved machine account not found. Request a machine account and wait for an instance owner to approve it.')
            node = s.get(Node, binding.node_id)
            live(node, binding.account)
            if s.scalar(select(Placement.id).where(Placement.project_id == project_id,
                                                  Placement.machine_account_id == binding.id)):
                raise HTTPException(409, 'You already requested a private checkout for this project on that machine.')
            # Path is derived during approval from the broker's deployment-owned roots.
            row = Placement(project_id=project.id, person=who.username, host=node.name, account=binding.account,
                            path='', machine_account_id=binding.id, trust='pending', sync_status='unprovisioned')
            s.add(row)
            s.flush()
            record(s, who, 'remote_placement_request', group_id, project_id=project_id, placement_id=row.id)
            s.commit()
            return {'id': row.id, 'trust': row.trust}

    @api.post('/api/groups/{group_id}/projects/{project_id}/remote-placements/{placement_id}/approve')
    async def approve_placement(group_id: str, project_id: int, placement_id: int, body: RevisionIn, who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            group, _ = project_access(s, group_id, project_id, who, admin=True)
            row = s.get(Placement, placement_id)
            if row is None or row.project_id != project_id or not row.machine_account_id or row.trust != 'pending':
                raise HTTPException(404, 'Private checkout request not found, or it was already handled.')
            binding = s.get(GroupMachineAccount, row.machine_account_id)
            node = s.get(Node, binding.node_id)
            user = s.scalar(select(User).where(User.username == row.person))
            member = s.get(GroupMembership, (group_id, user.id)) if user else None
            if binding.status != 'approved' or binding.proof_verified_at is None or not member or member.role == 'viewer':
                raise HTTPException(404, "This request can't be approved: the person's machine account isn't approved and verified, or they're no longer a member who can work in the group.")
            live(node, binding.account)
            if group.revision != body.revision:
                raise HTTPException(409, 'The group changed while you were looking at it. Reload the page and try again.')
            account, host, person = row.account, row.host, row.person
        try:
            info = await ctx.broker.call('ping', host=host, account=account)
            roots = info.get('roots', [])
            if info.get('role') != 'agents':
                raise HTTPException(409, "That account on the machine isn't an agent account, so it can't be used for group work.")
            if not roots or not isinstance(roots[0], str) or not roots[0].startswith('/') or '..' in PurePosixPath(roots[0]).parts:
                raise HTTPException(409, "The machine didn't report a safe folder for group work. Update Rynhult on the machine and try again.")
            path = str(PurePosixPath(roots[0]) / 'groups' / group_id / str(project_id) / auth.digest(person)[:32])
            await ctx.broker.call('group_provision', host=host, account=account, path=path)
        except BrokerError as error:
            raise HTTPException(409, str(error)) from None
        with ctx.db() as s:
            group, _ = project_access(s, group_id, project_id, who, admin=True)
            fence(s, group, body.revision)
            row = s.get(Placement, placement_id)
            binding = s.get(GroupMachineAccount, row.machine_account_id)
            node = s.get(Node, binding.node_id)
            user = s.scalar(select(User).where(User.username == person))
            member = s.get(GroupMembership, (group_id, user.id)) if user else None
            if row.trust != 'pending' or binding.status != 'approved' or binding.proof_verified_at is None or not member or member.role == 'viewer':
                raise HTTPException(409, 'The request changed while the checkout was being set up. Reload the page and try again.')
            live(node, account)
            row.path, row.trust, row.sync_status, row.approved_by = path, 'approved', 'ready', who.username
            record(s, who, 'remote_placement_approve', group_id, project_id=project_id, placement_id=row.id)
            s.commit()
        return {'id': placement_id, 'trust': 'approved', 'sync_status': 'ready'}

    @api.post('/api/groups/{group_id}/projects/{project_id}/remote-placements/{placement_id}/revoke')
    async def revoke_placement(group_id: str, project_id: int, placement_id: int, body: RevisionIn, who=Depends(ctx.principal)):
        enabled()
        with ctx.db() as s:
            group, _ = project_access(s, group_id, project_id, who, admin=True)
            fence(s, group, body.revision)
            row = s.get(Placement, placement_id)
            if row is None or row.project_id != project_id or not row.machine_account_id:
                raise HTTPException(404, 'Private checkout not found.')
            ids = group_remote.revoke_placements(s, placement_id=row.id)
            stop_intent(s, ids)
            record(s, who, 'remote_placement_revoke', group_id, project_id=project_id, placement_id=row.id)
            s.commit()
        await finish_stops(ids, who.username)
        return {'status': 'revoked', 'cleanup_pending': ids}

    return api
