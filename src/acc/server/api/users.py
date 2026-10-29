"""People (owner only): who can sign in, their role and agent account, and one-time invite or reset links."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import auth, budgets, containers, group_authority, passkeys, vault
from ..broker_client import BrokerError
from ..ha import fence
from ..models import (
    AttentionItem,
    AuditLog,
    AuthSession,
    Device,
    DevicePairing,
    Invite,
    MultiRun,
    Passkey,
    PortReservation,
    Project,
    PublishRequest,
    PushSubscription,
    RetiredUser,
    Session,
    User,
    VaultKey,
)
from ..supervisor import LaunchRefused
from .context import Context
from .schemas import InviteIn

ACCOUNT = re.compile(r"[a-z][a-z0-9-]{0,23}-agent")  # what acc-accounts accepts


def account_refusal(s, role: str, account: str, default_account: str, services_account: str,
                    user_id: int | None = None) -> str | None:
    """Why `account` can't be this person's agent account, or None (prerelease review D10). A member's sessions run as
    an account of their own: never the owners' default account, the services account, or one someone else has (they
    could work in that person's checkouts and use its logins). Owners are fully trusted and may share the default
    account, but not take a member's."""
    others = [(role_, (held or default_account)) for id_, role_, held in s.execute(select(User.id, User.role, User.account))
              if id_ != user_id]
    if role == "member":
        if account in (default_account, services_account):
            return f"{account} is the owners' agent account. A member needs an agent account of their own."
        if any(held == account for _, held in others):
            return f"Someone else already runs as {account}. A member needs an agent account of their own."
    elif any(role_ == "member" and held == account for role_, held in others):
        return f"{account} is a member's agent account. Choose a different one."
    return None


class RoleIn(BaseModel):
    role: str = Field(pattern=r"^(owner|member)$")


class RemoveIn(BaseModel):
    resources: str = Field(pattern=r"^(transfer|archive)$")  # what happens to their projects, sessions and keys


def router(ctx: Context) -> APIRouter:
    db, broker = ctx.db, ctx.broker
    api = APIRouter(prefix="/api/users")

    def owners(s) -> int:
        return s.scalar(select(func.count()).select_from(User).where(User.role == "owner"))

    def link(token: str) -> str:
        return f"/invite/{token}"

    def issue(s, who, role: str, account: str | None, user_id: int | None) -> dict:
        token = auth.new_invite_token()
        invite = Invite(token_hash=auth.digest(token), role=role, account=account, user_id=user_id,
                        created_by=who.username, expires_at=auth.now() + timedelta(hours=auth.INVITE_HOURS))
        s.add(invite)
        s.flush()
        return {"id": invite.id, "link": link(token), "expires_at": invite.expires_at}

    def verified(s, who, request, refused: str, target: str) -> None:
        """Fresh verification (passkey, or password + code where allowed; five minutes) from the person acting, so a
        copied owner cookie or the API token can't add an owner or take over someone's sign-in. Refusals are audited."""
        actor = s.get(User, who.user_id) if who.user_id is not None else None
        try:
            passkeys.recently_verified(s, getattr(request.state, "session_hash", None), actor)
        except HTTPException:
            s.add(AuditLog(actor=who.username, action=refused, target=target, detail={"reason": "verification_required"}))
            s.commit()
            raise

    @api.get("")
    def list_users(who=Depends(ctx.owner)):
        with db() as s:
            sessions = dict(s.execute(select(AuthSession.user_id, func.max(AuthSession.last_seen_at))
                                      .group_by(AuthSession.user_id)).all())
            users = [{"id": u.id, "username": u.username, "role": u.role, "account": u.account or broker.default_account,
                      "created_at": u.created_at, "last_seen_at": sessions.get(u.id), "you": u.username == who.username}
                     for u in s.scalars(select(User).order_by(User.created_at))]
            invites = [{"id": i.id, "role": i.role, "account": i.account, "reset_for": i.user_id, "created_by": i.created_by,
                        "created_at": i.created_at, "expires_at": i.expires_at}
                       for i in s.scalars(select(Invite).where(Invite.used_at.is_(None), Invite.expires_at > auth.now())
                                          .order_by(Invite.created_at.desc()))]
        return {"users": users, "invites": invites, "accounts": broker.accounts(), "default_account": broker.default_account}

    async def create_account(account: str, who, role: str) -> None:
        """Ask the acc-accounts root helper for a new agent account (Rynhult itself can't create Linux users) and wait.
        The account starts with its role's container limits (members limited, owners not; Settings changes them)."""
        if not ACCOUNT.fullmatch(account):
            raise HTTPException(422, "Name new agent accounts like name-agent, using lowercase letters, digits and dashes.")
        request_id = f"acct-{uuid.uuid4().hex[:12]}"
        requests, result = ctx.settings.accounts_dir / "requests", ctx.settings.accounts_dir / "results" / f"{request_id}.json"
        fence(ctx.db)
        tmp = requests / f".{request_id}.tmp"
        tmp.write_text(json.dumps({"id": request_id, "account": account}))
        tmp.replace(requests / f"{request_id}.json")
        with db() as s:
            s.add(AuditLog(actor=who.username, action="account.create_requested", target=account))
            s.commit()
        deadline = time.monotonic() + ctx.settings.accounts_timeout
        while not result.exists():
            if time.monotonic() > deadline:
                raise HTTPException(504, "The host didn't create the account in time. The account-creation service on the host (acc-accounts.path) may not be running.")
            await asyncio.sleep(0.5)
        outcome = json.loads(result.read_text())  # the helper clears old results (accd can't write there)
        if not outcome.get("ok"):
            raise HTTPException(409, outcome.get("detail") or "The host refused to create the account.")
        with db() as s:
            containers.set_defaults(s, account, role, who.username)
            s.commit()
        ctx.supervisor.set_catalog(ctx.supervisor.catalog)  # link catalog skills for the new account too
        while account not in broker.accounts() and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

    @api.post("/invites", status_code=201)
    async def create_invite(body: InviteIn, request: Request, who=Depends(ctx.owner)):
        """A one-time link (72 h). Members run their sessions as an agent account of their own: pick one, or have Rynhult
        create one (create_account; the acc-accounts root helper makes the Linux user)."""
        if body.role == "owner":  # checked before anything is created
            with db() as s:
                verified(s, who, request, "user.invite_refused", "owner")
        account = body.account or (broker.default_account if body.role == "owner" else None)
        if account is None:
            raise HTTPException(422, "Choose the agent account this person's sessions run as.")
        if body.create_account and account not in broker.accounts():
            await create_account(account, who, body.role)
        if account not in broker.accounts():
            raise HTTPException(422, f"There is no agent account {account!r} on this host. Choose one from the list.")
        with db() as s:
            refusal = account_refusal(s, body.role, account, broker.default_account, broker.services_account)
            if refusal:
                raise HTTPException(422, refusal)
            out = issue(s, who, body.role, account, None)
            s.add(AuditLog(actor=who.username, action="user.invite", target=body.role, detail={"account": account, "invite": out["id"]}))
            s.commit()
        return out

    @api.post("/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: int, who=Depends(ctx.owner)):
        with db() as s:
            invite = s.get(Invite, invite_id)
            if invite is None or invite.used_at is not None:
                raise HTTPException(404, "Invite not found. It may have been used or cancelled already.")
            invite.expires_at = auth.now()
            s.add(AuditLog(actor=who.username, action="user.invite_revoked", target=str(invite_id)))
            s.commit()
        return {"ok": True}

    @api.post("/{user_id}/reset", status_code=201)
    def reset_user(user_id: int, request: Request, who=Depends(ctx.owner)):
        """A link for the person to set a new password and authenticator; their sign-ins end when they use it."""
        with db() as s:
            user = s.get(User, user_id)
            if user is None:
                raise HTTPException(404, "Person not found.")
            if user.id != who.user_id:  # your own reset stays open: the sole owner's way back after recovery
                verified(s, who, request, "user.reset_refused", user.username)
            out = issue(s, who, user.role, user.account, user.id)
            s.add(AuditLog(actor=who.username, action="user.reset_link", target=user.username, detail={"invite": out["id"]}))
            s.commit()
        return out

    @api.post("/{user_id}/role")
    async def set_role(user_id: int, body: RoleIn, request: Request, who=Depends(ctx.owner)):
        with db() as s:
            user = s.get(User, user_id)
            if user is None:
                raise HTTPException(404, "Person not found.")
            if user.role == "owner" and body.role != "owner" and owners(s) <= 1:
                raise HTTPException(409, "Rynhult needs at least one owner. Make someone else an owner first.")
            refusal = account_refusal(s, body.role, user.account or broker.default_account, broker.default_account,
                                      broker.services_account, user.id)
            if refusal:  # e.g. demoting an owner who runs as the default account: give them their own first
                raise HTTPException(409, f"{refusal} To make them a member, invite them again with an agent account of their own, or keep them an owner.")
            if passkeys.policy(s)['owners' if body.role == 'owner' else 'members'] == 'require':
                counts = dict(s.execute(select(Passkey.rp_id, func.count()).where(Passkey.user_id == user.id).group_by(Passkey.rp_id)).all())
                if ((passkeys.recovery_required(user, body.role) and not user.recovery_hash)
                        or not any(n >= (2 if body.role == 'owner' else 1) for n in counts.values())):
                    raise HTTPException(409, 'Enroll the required passkeys and, for owners or members with local credentials, an offline recovery code before changing to a required role.')
                # A copied owner cookie alone must not mint a required-role account: the acting owner verifies again
                # (with a passkey when owners require one), as for changing passkeys or policy.
                verified(s, who, request, "user.role_refused", user.username)
            old, user.role = user.role, body.role
            # Budgets only apply to owners: a demoted owner's rules are deleted, not kept doing nothing.
            dropped = old == "owner" and body.role != "owner" and budgets.forget(s, user.username)
            s.add(AuditLog(actor=who.username, action="user.role", target=user.username,
                           detail={"from": old, "to": body.role, **({"budget_deleted": True} if dropped else {})}))
            s.flush()
            budgets.evaluate(s)  # and their budget alerts end now
            s.commit()
            # Preserve their sign-ins and own terminals, but revoke access to everyone else's now.
            revoked = []
            if old == "owner" and body.role != "owner":
                hashes = list(s.scalars(select(AuthSession.token_hash).where(AuthSession.user_id == user_id)))
                for session_hash in hashes:
                    for ws in list(ctx.terminals.get(session_hash, ())):
                        target = s.get(Session, ws.path_params["acc_id"])
                        if target is None or target.owner != user.username:
                            revoked.append(ws)
        for ws in revoked:
            try:
                await ws.send_text('{"t":"closed","reason":"terminal access revoked"}')
                await ws.close(code=4403)
            except RuntimeError:
                pass
        return {"ok": True}

    def dispose(s, name: str, to: str, resources: str) -> tuple[dict, list[str]]:
        """What a removed person owned. transfer: everything becomes `to`'s (an owner). archive: projects archived,
        sessions archived (the ids returned are stopped after the commit), private keys deleted, open inbox items
        resolved; shared keys (others use them) and port reservations go to `to`. Either way nothing is left for a
        future user: the name is retired. Returns counts for the audit line, and the sessions to stop."""
        stop = []
        projects = s.scalars(select(Project).where(Project.owner == name)).all()
        sessions = s.scalars(select(Session).where(Session.owner == name)).all()
        keys = s.scalars(select(VaultKey).where(VaultKey.owner == name)).all()
        items = s.scalars(select(AttentionItem).where(AttentionItem.owner == name, AttentionItem.resolved_at.is_(None))).all()
        if resources == "transfer":
            groups = s.scalars(select(MultiRun).where(MultiRun.owner == name)).all()
            for row in (*projects, *sessions, *keys, *items, *groups):
                row.owner = to
            for request in s.scalars(select(PublishRequest).where(PublishRequest.owner == name)):
                request.owner = to
        else:
            for project in projects:
                project.archived, project.compose_autostart = True, False
            stop = [x.id for x in sessions if x.desired_state != "archived"]
            used = {value for item in ctx.supervisor.catalog.mcps.values() for value in (item.meta.get("env") or {}).values()}
            for key in keys:
                if key.shared or vault.ref(key.id) in used:
                    key.owner = to  # others' launches (or an MCP server) still use it: someone must manage it
                    continue
                s.add(AuditLog(actor=to, action="vault.delete", target=key.id,
                               detail={"variable": key.variable, "as_owner": True, "user_removed": True}))
                s.delete(key)
                ctx.supervisor.vault.forget(key.id)
            for item in items:
                item.resolved_at = auth.now()
        for port in s.scalars(select(PortReservation).where(PortReservation.owner == name)):
            port.owner = to
        devices = s.scalars(select(PushSubscription).where(PushSubscription.username == name)).all()
        for device in devices:
            s.delete(device)  # their phones stop getting Rynhult's notifications
        for invite in s.scalars(select(Invite).where(Invite.created_by == name, Invite.used_at.is_(None),
                                                     Invite.expires_at > auth.now())):
            invite.expires_at = auth.now()  # links a removed owner handed out stop working
        counts = {"projects": len(projects), "sessions": len(sessions), "keys": len(keys), "devices": len(devices)}
        return counts, stop

    @api.post("/{user_id}/remove")
    async def remove_user(user_id: int, body: RemoveIn, who=Depends(ctx.owner)):
        """Sign them out everywhere and retire their username for good. Their projects, sessions and keys are given to
        you (transfer) or archived (archive): never left for someone who later signs up with the same name."""
        return await remove_person(user_id, body.resources, who)

    async def remove_person(user_id: int, resources: str, who, also=None) -> dict:
        """People > Remove, also used by company sign-in for a member it alone lets in. `also(s, user)` runs in the
        same transaction first (it may refuse with an HTTPException, and nothing is removed)."""
        with db() as s:
            user = s.get(User, user_id)
            if user is None:
                raise HTTPException(404, "Person not found.")
            if user.username == who.username:
                raise HTTPException(409, "You can't remove yourself. Ask another owner to do it.")
            if user.role == "owner" and owners(s) <= 1:
                raise HTTPException(409, "Rynhult needs at least one owner. Make someone else an owner first.")
            if also is not None:
                also(s, user)
            hashes = [r.token_hash for r in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id))]
            for record in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
                s.delete(record)
            group_stops = group_authority.remove_user(s, user.id, who.username)
            from .. import group_remote
            from ..models import GroupMachineAccount
            for binding in s.scalars(select(GroupMachineAccount).where(GroupMachineAccount.person == user.username,
                                                                       GroupMachineAccount.status != 'revoked')):
                # Retired usernames never return, but an approved binding keeps the account reserved: show it revoked.
                cancelled = binding.approved_by is None
                binding.status = 'revoked'
                group_stops.update(group_remote.revoke_placements(s, binding_id=binding.id))
                s.add(AuditLog(actor=who.username, action='group.machine_cancel' if cancelled else 'group.machine_revoke',
                               target=binding.id, detail={'node_id': binding.node_id, 'reason': 'user_removed'}))
                if cancelled:
                    s.delete(binding)  # an unapproved request never reserved anything
            for acc_id in group_stops:
                ctx.supervisor._set_state(s, s.get(Session, acc_id), 'stopped', 'api')
            name = user.username
            for device in s.scalars(select(Device).where(Device.person == name, Device.status == 'active')):
                device.status = 'revoked'
                s.add(AuditLog(actor=who.username, action='device.revoke', target=device.id,
                               detail={'person': name, 'user_removed': True, 'policy': 'pending; not applied'}))
            for pairing in s.scalars(select(DevicePairing).where(DevicePairing.person == name)):
                pairing.status = 'cancelled'
                ctx.device_imports.pop(pairing.id)
            budgets.forget(s, name)
            counts, stop = dispose(s, name, who.username, resources)
            s.add(RetiredUser(username=name, role=user.role, account=user.account, removed_by=who.username,
                              resources=resources))
            s.delete(user)
            s.add(AuditLog(actor=who.username, action="user.remove", target=name,
                           detail={"resources": resources, **counts}))
            s.commit()
        try:
            await ctx.remote_access.reconcile()
        except HTTPException:
            pass  # status remains pending; the worker retries removing public routing
        for session_hash in hashes:
            await ctx.close_terminals(session_hash)
        await ctx.edge.sweep()  # public streams on the removed person's devices
        for acc_id in group_stops.difference(stop):
            await ctx.supervisor.stop(acc_id, who.username)
        failed = []
        for acc_id in stop:
            try:
                await ctx.supervisor.stop(acc_id, who.username, archive=True)
            except (LaunchRefused, BrokerError) as error:  # archived already (the intent is recorded first); the pane may linger
                failed.append({"session": acc_id, "error": str(error)})
        return {"ok": True, "resources": resources, **counts, **({"not_stopped": failed} if failed else {})}

    ctx.remove_person = remove_person
    return api
