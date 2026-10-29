"""Provider accounts: login health and the CLI re-login flows.

Every route takes `machine` (default: this one). On another machine the account must be one its node reported, and
the broker calls go to that machine's broker through its link, so the CLI's own login runs there."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ... import providers
from ..broker_client import BrokerError
from ..models import AuditLog, Observation
from .context import Context, refresh_read
from .schemas import LoginCodeIn


def router(ctx: Context) -> APIRouter:
    db, supervisor, broker, refused = ctx.db, ctx.supervisor, ctx.broker, ctx.refused
    api = APIRouter()

    def account_or_default(account: str | None, who) -> str:
        """Owners may look at any agent account (default: the default one); members only at their own."""
        name = account or (broker.default_account if who.is_owner else who.account)
        if name not in broker.accounts() or (not who.is_owner and name != who.account):
            raise HTTPException(404, f"Agent account {name!r} not found.")
        return name

    def where(account: str | None, machine: str | None, who) -> tuple[str, str | None, list[str]]:
        """(account, host, the accounts to list): this machine's, or one a connected machine reported. On another
        machine the default is the same account name if it has one, else its first."""
        link = ctx.machine(machine, who)
        if link is None:
            name = account_or_default(account, who)
            return name, None, broker.accounts() if who.is_owner else [name]
        default = broker.default_account if broker.default_account in link.accounts else next(iter(link.accounts), None)
        name = account or (default if who.is_owner else who.account)
        if name not in link.accounts or (not who.is_owner and name != who.account):
            raise HTTPException(404, f"Agent account {name!r} not found on {link.name}.")
        return name, link.name, list(link.accounts) if who.is_owner else [name]

    # Login flows run in one account's broker; the id handed to the browser carries the account ("account.flow").
    def split_flow(flow_id: str, machine: str | None, who) -> tuple[str, str | None, str]:
        account, _, flow = flow_id.partition(".")
        name, host, _ = where(account, machine, who)
        return name, host, flow

    def wrap(account: str, flow: dict) -> dict:
        return {**flow, "flow_id": f"{account}.{flow['flow_id']}"} if flow.get("flow_id") else flow

    @api.get("/api/accounts")
    async def accounts(refresh: int = 0, account: str | None = None, machine: str | None = None,
                       who=Depends(ctx.principal)):
        """One agent account's CLI logins (default: the default account), plus the list of accounts."""
        name, host, names = where(account, machine, who)
        default = name if not who.is_owner else (broker.default_account if host is None else
                                                 (broker.default_account if broker.default_account in names else names[0]))
        listing = {"account": name, "accounts": names, "default_account": default, "machine": host or "local"}
        if refresh:
            try:
                with refresh_read():
                    return {**await supervisor.refresh_auth(name, host), **listing}
            except BrokerError as error:
                raise refused(error)
        with db() as s:
            if host is not None:
                health = s.get(Observation, f"auth.health.host.{host}.{name}")
            else:
                health = s.get(Observation, f"auth.health.{name}") or (s.get(Observation, "auth.health")
                                                                       if name == broker.default_account else None)
            return {**(health.value if health else {}), **listing}

    @api.post("/api/accounts/{provider}/login")
    async def login_start(provider: str, account: str | None = None, machine: str | None = None,
                          who=Depends(ctx.principal)):
        if provider not in (*providers.ids(), "github"):
            raise HTTPException(404, "Unknown provider. Choose one from Providers.")
        name, host, _ = where(account, machine, who)
        try:
            flow = await broker.call("login_start", account=name, host=host, provider=provider)
        except BrokerError as error:
            raise refused(error)
        with db() as s:
            s.add(AuditLog(actor=who.username, action="account.login_start", target=provider,
                           detail={"account": name, "machine": host or "local"}))
            s.commit()
        return wrap(name, flow)

    async def _login_result(account: str, host: str | None, flow: dict, who) -> dict:
        if flow.get("done"):
            with db() as s:
                s.add(AuditLog(actor=who.username, action="account.login_finished", target=flow.get("provider"),
                               detail={"ok": flow.get("ok"), "account": account, "machine": host or "local"}))
                s.commit()
            if flow.get("ok"):
                try:
                    await supervisor.refresh_auth(account, host)
                except (BrokerError, ConnectionError):
                    pass
        return wrap(account, flow)

    @api.post("/api/accounts/login/{flow_id}/code")
    async def login_code(flow_id: str, body: LoginCodeIn, machine: str | None = None, who=Depends(ctx.principal)):
        account, host, flow = split_flow(flow_id, machine, who)
        try:
            result = await broker.call("login_submit", account=account, host=host, flow_id=flow, code=body.code)
            return await _login_result(account, host, result, who)
        except BrokerError as error:
            raise refused(error)

    @api.get("/api/accounts/login/{flow_id}")
    async def login_poll(flow_id: str, machine: str | None = None, who=Depends(ctx.principal)):
        account, host, flow = split_flow(flow_id, machine, who)
        try:
            return await _login_result(account, host, await broker.call("login_poll", account=account, host=host,
                                                                        flow_id=flow), who)
        except BrokerError as error:
            raise refused(error)

    @api.post("/api/accounts/login/{flow_id}/cancel")
    async def login_cancel(flow_id: str, machine: str | None = None, who=Depends(ctx.principal)):
        account, host, flow = split_flow(flow_id, machine, who)
        try:
            return await broker.call("login_cancel", account=account, host=host, flow_id=flow)
        except BrokerError as error:
            raise refused(error)

    return api
