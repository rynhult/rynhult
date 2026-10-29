"""Publishing (Caddy + Cloudflare): requests, approvals, unpublish."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from .. import publish_addresses
from ..dns_providers import DNSError
from ..models import PublishRequest
from ..publish import PublishRefused
from .context import Context
from .schemas import ApproveIn, PublishIn, publish_out


class AddressIn(BaseModel):
    address: str = Field(min_length=2, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    access: str = Field(pattern="^(public|internal)$")


def router(ctx: Context) -> APIRouter:
    settings, db, publisher = ctx.settings, ctx.db, ctx.publisher
    api = APIRouter()

    @api.get("/api/publish/addresses")
    def addresses(who=Depends(ctx.owner)):
        with db() as s:
            rows = publish_addresses.inventory(s, settings)
            s.commit()
            return rows

    @api.post("/api/publish/addresses/detect")
    async def detect_addresses(who=Depends(ctx.owner)):
        return await publish_addresses.detect()

    def edit_addresses(who, **kwargs):
        try:
            with db() as s:
                rows = publish_addresses.change(s, settings, who.username, **kwargs)
                s.commit()
                return rows
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post("/api/publish/addresses", status_code=201)
    def add_address(body: AddressIn, who=Depends(ctx.owner)):
        return edit_addresses(who, entry=body.model_dump())

    @api.post("/api/publish/addresses/{entry_id}/remove")
    def remove_address(entry_id: str, who=Depends(ctx.owner)):
        return edit_addresses(who, remove=entry_id)

    @api.post("/api/publish/addresses/{entry_id}/default")
    def default_address(entry_id: str, who=Depends(ctx.owner)):
        return edit_addresses(who, default=entry_id)

    @api.get("/api/publish")
    def list_publish(who=Depends(ctx.principal), view: Literal['current', 'audit'] | None = None,
                     host: str = Query('', max_length=253),
                     status: Literal['failed', 'rejected', 'removed', 'replaced'] | None = None,
                     limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=2147483647)):
        with db() as s:
            query = ctx.scope(select(PublishRequest), PublishRequest, who)
            current = or_(PublishRequest.status.in_(['pending_approval', 'approved', 'applying']),
                          and_(PublishRequest.status == 'applied', PublishRequest.action == 'publish'))
            if view == 'current':
                query = query.where(current)
                if host:
                    query = query.where(PublishRequest.host == host.lower())
            elif view == 'audit':
                query = query.where(~current)
                if host:
                    query = query.where(func.lower(PublishRequest.host).contains(host.lower(), autoescape=True))
                replaced = and_(PublishRequest.status == 'removed',
                                func.coalesce(PublishRequest.detail, '').startswith('replaced by ', autoescape=True))
                if status == 'replaced':
                    query = query.where(replaced)
                elif status:
                    query = query.where(PublishRequest.status == status)
                    if status == 'removed':
                        query = query.where(~replaced)
                total = s.scalar(select(func.count()).select_from(query.subquery()))
            query = query.order_by(PublishRequest.created_at.desc(), PublishRequest.id.desc())
            query = query.limit(limit if view else 200).offset(offset if view else 0)
            rows = [publish_out(r, is_owner=who.is_owner)
                    for r in s.scalars(query)]
            return {'items': rows, 'total': total} if view == 'audit' else rows

    @api.get("/api/publish/zones")
    async def publish_zones(who=Depends(ctx.principal)):
        """The Cloudflare zones names can be published under. Cloudflare is optional: without its token this is a
        state ("configured": false), never an error with a server path in it (UX review 2, R4)."""
        ips = {}
        if who.is_owner:
            with db() as s:
                rows = publish_addresses.inventory(s, settings)
                s.commit()
            for access in ('public', 'internal'):
                try:
                    ips[f'{access}_ip'] = publish_addresses.choose(rows, access, None)
                except ValueError:
                    ips[f'{access}_ip'] = ''
        try:
            zones = await publisher.zones()
            # Provider account IDs are owner metadata. Requesters see only publishable names.
            return {"zones": zones if who.is_owner else [{'name': z['name']} for z in zones],
                    "configured": True, **ips}
        except PublishRefused:
            return {"zones": [], "configured": False, **ips}
        except (DNSError, OSError):
            raise HTTPException(502, 'The DNS provider didn\'t answer. An owner can check its credential in Settings → DNS.') from None

    @api.post("/api/publish", status_code=201)
    async def publish(body: PublishIn, who=Depends(ctx.principal)):
        """An owner publishing from the dashboard is already approved; a member's request waits for an owner."""
        if body.target_ip and not who.is_owner:
            raise HTTPException(403, "Only owners can choose the DNS address.")
        if body.project_id is not None:
            with db() as s:
                ctx.project_for(s, who, body.project_id)
        try:
            request_id = publisher.request(action="publish", host=body.host, port=body.port, access=body.access,
                                           requested_by=who.username, project_id=body.project_id, reason=body.reason,
                                           approved_by=who.username if who.is_owner else None, owner=who.username,
                                           replace=body.replace, target_ip=body.target_ip)
        except PublishRefused as error:
            raise HTTPException(409, str(error))
        with db() as s:
            return publish_out(s.get(PublishRequest, request_id), is_owner=who.is_owner)

    async def _decide(request_id: str, verb: str, who, confirm_replace: bool = False):
        try:
            if verb == "approve":
                publisher.approve(request_id, who.username, confirm_replace=confirm_replace)
            elif verb == "reject":
                publisher.reject(request_id, who.username)
        except PublishRefused as error:
            raise HTTPException(409, str(error))
        with db() as s:
            return publish_out(s.get(PublishRequest, request_id), is_owner=who.is_owner)

    @api.post("/api/publish/{request_id}/approve")
    async def approve_publish(request_id: str, body: ApproveIn | None = None, who=Depends(ctx.owner)):
        return await _decide(request_id, "approve", who, confirm_replace=bool(body and body.confirm_replace))

    @api.post("/api/publish/{request_id}/reject")
    async def reject_publish(request_id: str, who=Depends(ctx.owner)):
        return await _decide(request_id, "reject", who)

    @api.post("/api/publish/{request_id}/unpublish")
    async def unpublish(request_id: str, who=Depends(ctx.principal)):
        with db() as s:
            original = s.get(PublishRequest, request_id)
            if not ctx.owns(original, who):
                raise HTTPException(404, "That request wasn't found.")
            host, original_owner = original.host, original.owner
        try:
            new_id = publisher.request(action="unpublish", host=host, port=None, access=None, requested_by=who.username,
                                       approved_by=who.username if who.is_owner else None, owner=original_owner)
        except PublishRefused as error:
            raise HTTPException(409, str(error))
        with db() as s:
            return publish_out(s.get(PublishRequest, new_id), is_owner=who.is_owner)

    return api
