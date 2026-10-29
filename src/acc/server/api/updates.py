"""Owners' update controls. Credentials never pass through this API."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...update_helper import write_json
from ...updates import REPO
from ..models import Node
from ..updating import ConfigError, Updates
from .context import Context


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(max_length=200)
    automatic: bool = False
    time: str = Field(default="03:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class FollowIn(BaseModel):
    enabled: bool


def router(ctx: Context):
    api = APIRouter(prefix="/api/updates")
    service = Updates(ctx)
    ctx.updates = service

    @api.get("")
    def state(who=Depends(ctx.owner)):
        return service.state()

    @api.put("/config")
    async def configure(body: ConfigIn, who=Depends(ctx.owner)):
        if body.repository and (
            not REPO.fullmatch(body.repository) or any(p in (".", "..") for p in body.repository.split("/"))
        ):
            raise HTTPException(422, "Enter the repository as owner/name, or leave it empty to turn updates off.")
        async with service.lock:
            try:
                before = service.config()
            except ConfigError:
                before = service.defaults()  # a complete explicit save repairs the file
                before["repository"] = None  # discard stale release observations too
            value = {**before, **body.model_dump()}
            write_json(ctx.settings.data_dir / "updates-config.json", value)
            if before["repository"] != body.repository:
                service.store({})  # changing source requires a fresh release check
            service.audit(
                "update.configure", who.username, body.repository, {"automatic": body.automatic, "time": body.time}
            )
            return service.state()

    @api.post("/check")
    async def check_now(who=Depends(ctx.owner)):
        return await service.check(who.username)

    @api.post("/apply", status_code=202)
    async def apply(who=Depends(ctx.owner)):
        try:
            return service.request(who.username)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post("/machines/{name}/follow")
    async def follow(name: str, body: FollowIn, who=Depends(ctx.owner)):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise HTTPException(404)
        with ctx.db() as s:
            node = s.scalar(select(Node).where(Node.name == name, Node.status == "active"))
            if node is None:
                raise HTTPException(404, "That machine wasn't found. Check Host → Machines.")
            if (node.info or {}).get("os") == "darwin":
                raise HTTPException(409, "Macs don't update this way. Update this Mac with the new installer.")
        try:
            value = service.config()
        except ConfigError as error:
            raise HTTPException(409, str(error)) from None
        value["follow"] = {**value["follow"], name: body.enabled}
        write_json(ctx.settings.data_dir / "updates-config.json", value)
        service.last_follow.pop(name, None)
        service.audit("update.follow", who.username, name, {"enabled": body.enabled})
        return {"enabled": body.enabled}

    @api.post("/machines/{name}", status_code=202)
    async def update_machine(name: str, who=Depends(ctx.owner)):
        names = (
            [
                key
                for key, link in ctx.broker.hub.links.items()
                if link.info.get("os") != "darwin" and link.info.get("build") != service.build
            ]
            if name == "all"
            else [name]
        )
        results = {}
        for machine in names:
            try:
                results[machine] = service.request_node(machine, who.username)
            except (ValueError, OSError, ConnectionError) as error:
                results[machine] = {"queued": False, "error": str(error)[:200]}
        return results

    return api
