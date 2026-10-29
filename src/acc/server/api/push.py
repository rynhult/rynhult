"""Push notifications: this device's subscription, the device list, a test message."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models import AuditLog, PushSubscription
from ..push import PushError, check_endpoint
from .context import Context


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=64)


class SubscribeIn(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: Keys
    label: str = Field("This device", max_length=120)


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=2000)


def router(ctx: Context) -> APIRouter:
    db, supervisor = ctx.db, ctx.supervisor
    api = APIRouter()

    def device_out(d: PushSubscription) -> dict:
        return {"id": d.id, "label": d.label, "endpoint": d.endpoint, "created_at": d.created_at,
                "last_ok_at": d.last_ok_at, "failures": d.failures}

    @api.get("/api/push")
    def push_state(who=Depends(ctx.principal)):
        with db() as s:
            devices = [device_out(d) for d in s.scalars(select(PushSubscription).where(PushSubscription.username == who.username)
                                                        .order_by(PushSubscription.created_at))]
        return {"public_key": supervisor.push.public_key, "devices": devices}

    @api.post("/api/push/subscribe", status_code=201)
    def subscribe(body: SubscribeIn, request: Request, who=Depends(ctx.principal)):
        try:
            check_endpoint(body.endpoint)
        except PushError as error:
            raise HTTPException(422, str(error))
        with db() as s:
            device = s.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
            if device is None:
                device = PushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
                                          username=who.username, label=body.label.strip() or "This device")
                s.add(device)
                s.flush()  # audit the stable device id, never its endpoint, label or keys
                s.add(AuditLog(actor=who.username, action="push.subscribe", target=str(device.id)))
            else:  # keys can rotate; keep one row per endpoint (a browser that signs in as someone else follows them)
                ownership_changed = device.username != who.username
                keys_changed = (device.p256dh, device.auth) != (body.keys.p256dh, body.keys.auth)
                if ownership_changed or keys_changed or device.failures:
                    s.add(AuditLog(actor=who.username, action="push.update", target=str(device.id),
                                   detail={"ownership_changed": ownership_changed, "keys_changed": keys_changed}))
                device.p256dh, device.auth, device.failures = body.keys.p256dh, body.keys.auth, 0
                device.username = who.username
            s.commit()
            return device_out(device)

    @api.post("/api/push/unsubscribe")
    def unsubscribe(body: EndpointIn, who=Depends(ctx.principal)):
        with db() as s:
            device = s.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                             PushSubscription.username == who.username))
            if device is not None:
                s.delete(device)
                s.add(AuditLog(actor=who.username, action="push.unsubscribe", target=str(device.id)))
                s.commit()
        return {"ok": True}

    @api.post("/api/push/devices/{device_id}/remove")
    def remove_device(device_id: int, who=Depends(ctx.principal)):
        with db() as s:
            device = s.get(PushSubscription, device_id)
            if device is None or device.username != who.username:
                raise HTTPException(404, "That device isn't registered any more. Reload the page.")
            s.delete(device)
            s.add(AuditLog(actor=who.username, action="push.remove", target=str(device.id)))
            s.commit()
        return {"ok": True}

    @api.post("/api/push/test")
    async def test(body: EndpointIn | None = None, who=Depends(ctx.principal)):
        sent = await supervisor.notify({"title": "Rynhult test notification", "body": "Notifications reach this device.",
                                        "url": "/settings#notification-devices", "tag": "acc-test"},
                                       only=body.endpoint if body else None, username=who.username)
        if not sent:
            raise HTTPException(409, "No device accepted the test notification. Turn on notifications on this device in "
                                "Settings → Notification devices first.")
        return {"delivered": sent}

    return api
