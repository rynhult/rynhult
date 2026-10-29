"""Project-scoped, vault-only issue-source connections and read views."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .. import vault
from ..issue_sources import (
    IssueSourceError,
    allowed_address,
    check_remote,
    gitlab_repository,
    https_url,
    resolve_addresses,
    server_context,
    source_key,
)
from ..models import AuditLog, Session, Setting, VaultGrant
from .context import Context


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class GitLabConnection(Connection):
    base_url: str = Field("https://gitlab.com", max_length=500)
    server_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")


class LinearConnection(Connection):
    team_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    label_id: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    comment_pr: bool = False


class Connections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_url: str | None = Field(None, max_length=500)
    gitlab: GitLabConnection | None = None
    linear: LinearConnection | None = None
    gitea: GitLabConnection | None = None


class ServerResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=500)


class ServerIn(ServerResolve):
    addresses: list[str] = Field(min_length=1, max_length=32)
    ca_pem: str | None = Field(None, max_length=65536)


def server_view(row, owner=False):
    return {"id": row.key.removeprefix("issue_server."), "base_url": row.value["base_url"],
            "custom_ca": bool(row.value.get("ca_pem")),
            **({"addresses": row.value["addresses"]} if owner else {})}


def router(ctx: Context) -> APIRouter:
    api = APIRouter()

    @api.get("/api/issue-servers")
    def servers(who=Depends(ctx.principal)):
        with ctx.db() as db:
            return [server_view(row, who.is_owner) for row in db.scalars(
                select(Setting).where(Setting.key.startswith("issue_server.")))]

    @api.post("/api/issue-servers/resolve")
    async def resolve_server(body: ServerResolve, who=Depends(ctx.owner)):
        try:
            base = https_url(body.base_url)
            addresses = await resolve_addresses(base)
            if any(not allowed_address(ip, True) for ip in addresses):
                raise IssueSourceError("This server's address isn't allowed. Use a server on the internet or your local network.")
            return {"base_url": base, "addresses": addresses}
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None

    @api.post("/api/issue-servers", status_code=201)
    async def add_server(body: ServerIn, who=Depends(ctx.owner)):
        resolved = await resolve_server(ServerResolve(base_url=body.base_url), who)
        if sorted(set(body.addresses)) != resolved["addresses"]:
            raise HTTPException(422, "The server's addresses changed. Choose Resolve addresses and confirm them again.")
        value = {**resolved, "ca_pem": body.ca_pem}
        try:
            server_context(value)
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None
        with ctx.db() as db:
            if len(list(db.scalars(select(Setting.key).where(Setting.key.startswith("issue_server."))))) >= 100:
                raise HTTPException(422, "You have the maximum number of git servers. Remove one you don't use before adding another.")
            row = Setting(key="issue_server." + secrets.token_hex(8), value=value, updated_by=who.username)
            db.add(row)
            db.add(AuditLog(actor=who.username, action="issue_server.add", target=row.key,
                            detail={**resolved, "custom_ca": bool(body.ca_pem)}))
            db.commit()
            return server_view(row, True)

    @api.post("/api/issue-servers/{server_id}/confirm")
    async def confirm_server(server_id: str, body: ServerIn, who=Depends(ctx.owner)):
        resolved = await resolve_server(ServerResolve(base_url=body.base_url), who)
        if sorted(set(body.addresses)) != resolved["addresses"]:
            raise HTTPException(422, "The server's addresses changed. Choose Resolve addresses and confirm them again.")
        with ctx.db() as db:
            row = db.get(Setting, "issue_server." + server_id)
            if row is None:
                raise HTTPException(404, "That server wasn't found. Reload the page.")
            if row.value["base_url"] != resolved["base_url"]:
                raise HTTPException(422, "The server's URL can't be changed. Add git server with the new URL instead.")
            value = {**resolved, "ca_pem": body.ca_pem}
            try:
                server_context(value)
            except IssueSourceError as error:
                raise HTTPException(422, str(error)) from None
            row.value = value
            row.updated_by = who.username
            db.add(AuditLog(actor=who.username, action="issue_server.confirm", target=row.key,
                            detail={**resolved, "custom_ca": bool(body.ca_pem)}))
            db.commit()
            return server_view(row, True)

    @api.post("/api/issue-servers/{server_id}/remove")
    def remove_server(server_id: str, who=Depends(ctx.owner)):
        with ctx.db() as db:
            row = db.get(Setting, "issue_server." + server_id)
            if row is None:
                raise HTTPException(404, "That server wasn't found. Reload the page.")
            db.delete(row)
            db.add(AuditLog(actor=who.username, action="issue_server.remove", target=row.key))
            db.commit()
            return {"ok": True}

    @api.get("/api/projects/{project_id}/issue-sources")
    def connections(project_id: int, who=Depends(ctx.principal)):
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            # Key ids carry no credential values or private-key names.
            comments = [{"session_id": session.id, "session": session.name,
                         "status": session.issue_source.get("comment_status"), "error": session.issue_source.get("comment_error")}
                        for session in db.scalars(select(Session).where(Session.project_id == project_id,
                                                                         Session.issue_source.is_not(None)))
                        if session.issue_source.get("comment_status")]
            return {"remote_url": project.remote_url, **(project.issue_sources or {}), "comments": comments}

    @api.post("/api/projects/{project_id}/issue-sources")
    async def save_connections(project_id: int, body: dict, who=Depends(ctx.principal)):
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            try:
                config = Connections.model_validate(body)
            except ValidationError:
                # Pydantic's default error includes rejected input, which might be a pasted raw token.
                raise HTTPException(422, "Some connection details aren't valid. Check each source's key, team IDs and HTTPS base URL.") from None
            try:
                check_remote(config.remote_url)
                for connection in (config.gitlab, config.gitea):
                    if connection:
                        if connection.server_id:
                            row = db.get(Setting, "issue_server." + connection.server_id)
                            if row is None:
                                raise IssueSourceError("choose an allowed server from Settings → Issue sources")
                            connection.base_url = row.value["base_url"]
                        else:
                            # Public hostnames are still accepted; transport enforces public DNS at use time.
                            https_url(connection.base_url)
                            if not who.is_owner:
                                addresses = await resolve_addresses(connection.base_url)
                                if any(not allowed_address(ip) for ip in addresses):
                                    raise IssueSourceError("choose an owner-allowed server for private addresses")
                            import ipaddress
                            from urllib.parse import urlsplit
                            try:
                                address = ipaddress.ip_address(urlsplit(connection.base_url).hostname)
                            except ValueError:
                                address = None
                            if address is not None and not allowed_address(str(address)):
                                raise IssueSourceError("choose an owner-allowed server for private addresses")
                        gitlab_repository(connection.base_url, config.remote_url)
            except IssueSourceError as error:
                raise HTTPException(422, str(error)) from None
            project.remote_url = config.remote_url
            project.issue_sources = config.model_dump(exclude={"remote_url"}, exclude_none=True)
            try:
                for source in project.issue_sources:
                    source_key(db, project, source, who.username, ctx.supervisor.vault)
                    db.get(VaultGrant, (project.issue_sources[source]["key_id"], project.id)).server_only = True
            except (IssueSourceError, vault.VaultError) as error:
                raise HTTPException(422, str(error)) from None
            db.add(AuditLog(actor=who.username, action="project.issue_sources", target=project.slug,
                            detail={"sources": sorted(project.issue_sources),
                                    "linear_comment_pr": bool(config.linear and config.linear.comment_pr)}))
            db.commit()
            return {"remote_url": project.remote_url, **project.issue_sources}

    @api.get("/api/projects/{project_id}/issue-sources/{source}")
    async def list_items(project_id: int, source: str, kind: str = "issue", state: str = "open",
                         label: str = Query("", max_length=100), assignee: str = Query("", max_length=100),
                         cursor: str | None = Query(None, max_length=500), who=Depends(ctx.principal)):
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            try:
                config, token = source_key(db, project, source, who.username, ctx.supervisor.vault)
            except (IssueSourceError, vault.VaultError) as error:
                raise HTTPException(422, str(error)) from None
            remote = project.remote_url
        try:
            result = await ctx.supervisor.issue_sources.list(source, config, remote, token, kind=kind, state=state,
                                                             label=label, assignee=assignee, cursor=cursor)
            if source == "gitlab" and kind == "merge_request":
                await ctx.supervisor.comment_linear_pulls(project_id, [
                    {"head": item["branch"], "url": item["url"]} for item in result["items"]])
            return result
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None

    @api.get("/api/projects/{project_id}/issue-sources/{source}/{kind}/{item_id}")
    async def get_item(project_id: int, source: str, kind: str, item_id: str, who=Depends(ctx.principal)):
        with ctx.db() as db:
            project = ctx.project_for(db, who, project_id)
            try:
                config, token = source_key(db, project, source, who.username, ctx.supervisor.vault)
            except (IssueSourceError, vault.VaultError) as error:
                raise HTTPException(422, str(error)) from None
            remote = project.remote_url
        try:
            return await ctx.supervisor.issue_sources.detail(source, config, remote, token, kind, item_id)
        except IssueSourceError as error:
            raise HTTPException(422, str(error)) from None

    return api
