"""Wikis: each project or local workspace keeps one in docs/wiki (Azure DevOps "publish code as wiki" layout).

Agents write it (acc-workflow skill); ACC only reads it through the broker and renders it.
"""
from __future__ import annotations

import asyncio
import base64
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from ...broker import ops
from ...broker.config import BrokerConfig
from ..broker_client import BrokerError
from ..models import Project, Session
from .context import Context

FOLDER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def router(ctx: Context) -> APIRouter:
    settings, db, broker, refused = ctx.settings, ctx.db, ctx.broker, ctx.refused
    api = APIRouter()

    def location(kind: str, key: str, who) -> tuple[str, str] | None:
        """(folder, the account whose broker may read it), for your own project or workspace; None for the Guide."""
        if kind == "guide" and key == "acc":
            return None
        with db() as s:
            if kind == "project" and key.isdigit() and ctx.owns(project := s.get(Project, int(key)), who):
                return project.workspace_path, project.account
            if kind == "workspace" and FOLDER.match(key):
                session = s.scalar(ctx.scope(select(Session), Session, who)
                                   .where(Session.workdir.like(f"%/local/{key}")).limit(1))
                if session is not None and session.workdir.rsplit("/", 1)[1] == key:
                    return session.workdir, session.run_as
        raise HTTPException(404, "That project or workspace wasn't found.")

    guide = BrokerConfig(allowed_roots=(settings.guide_dir.resolve(),))

    async def call(method: str, where: tuple[str, str] | None, **params):
        """`where` None: the Guide (ACC's own wiki, read here from accd's checkout; everyone may read it)."""
        if where is None:
            try:
                return await asyncio.to_thread(getattr(ops, method), guide, str(settings.guide_dir.resolve()), *params.values())
            except ops.OpError as error:
                raise HTTPException(404, str(error))
        path, account = where
        try:
            return await broker.call(method, account=account, path=path, **params)
        except BrokerError as error:
            raise refused(error)

    @api.get("/api/wiki/{kind}/{key}")
    async def wiki_tree(kind: str, key: str, who=Depends(ctx.principal)):
        return await call("wiki_tree", location(kind, key, who))

    @api.get("/api/wiki/{kind}/{key}/page")
    async def wiki_page(kind: str, key: str, path: str, who=Depends(ctx.principal)):
        return await call("wiki_page", location(kind, key, who), rel=path)

    @api.get("/api/wiki/{kind}/{key}/attachment")
    async def wiki_attachment(kind: str, key: str, path: str, who=Depends(ctx.principal)):
        data = await call("wiki_attachment", location(kind, key, who), rel=path)
        return Response(base64.b64decode(data["data"]), media_type=data["content_type"], headers={
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",  # inert even if opened directly
        })

    return api
