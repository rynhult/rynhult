"""Owner-only consent, exact wire previews, report decisions and erasure."""


import hashlib
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from acc.env import environment

from ..models import AuditLog, TelemetryItem
from ..telemetry import DEFAULT_URL
from .context import Context


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage: bool = Field(strict=True)
    reports: bool = Field(strict=True)
    url: str = Field(default=DEFAULT_URL, max_length=2048)
    delete_previous: bool = True


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["send", "discard", "edit"]
    # Optional deletion only: reports cannot acquire new text during review.
    expected_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    remove_message: bool = False
    remove_log: bool = False


def router(ctx: Context):
    api = APIRouter(prefix="/api/telemetry", dependencies=[Depends(ctx.owner)])
    client = ctx.supervisor.telemetry

    @api.get("")
    def state(offset: int = 0):
        with ctx.db() as s:
            items = list(
                s.scalars(select(TelemetryItem).order_by(TelemetryItem.created.desc()).offset(max(0, offset)).limit(50))
            )
            keys = {k["id"]: k for k in client.load(s, "keys").get("items", [])}
            return {
                "consent": client.consent(s),
                "url": client.url(s),
                "total": s.scalar(select(func.count()).select_from(TelemetryItem)),
                "url_locked": "ACC_TELEMETRY_URL" in environment,
                "policy": client.policy(s),
                "items": [
                    {
                        "id": i.id,
                        "kind": i.kind,
                        "state": i.state,
                        "body": i.body,
                        "digest": hashlib.sha256(i.body.encode()).hexdigest(),
                        "destination": keys.get(i.key_id, {}).get("url", ""),
                        "created": i.created,
                        "sent_at": i.sent_at,
                        "result": i.result,
                        "receipt": i.receipt,
                        "attempts": i.attempts,
                    }
                    for i in items
                ],
            }

    @api.put("")
    async def configure(body: ConsentIn, who=Depends(ctx.owner)):
        async with client.send_lock:
            with ctx.db() as s:
                old = client.consent(s)
                try:
                    client.configure(s, body.usage, body.reports, body.url, who.username)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                if body.delete_previous and (
                    (old["usage"] and not body.usage) or (old["reports"] and not body.reports)
                ):
                    client.request_delete(s, who.username)
                s.commit()
        return {"ok": True}

    @api.get("/preview")
    def preview():
        with ctx.db() as s:
            item = s.scalar(
                select(TelemetryItem).where(
                    TelemetryItem.kind == "usage", TelemetryItem.state.in_(["approved", "retry"])
                )
            )
            if item is None and client.consent(s)["usage"]:
                item = client.prepare_usage(s)
            body = item.body if item else client.usage(s)
            s.commit()  # retain preview's key, without making any network request
            return {
                "usage": body,
                "reports": [
                    i.body
                    for i in s.scalars(
                        select(TelemetryItem).where(TelemetryItem.kind == "reports", TelemetryItem.state == "queued")
                    )
                ],
            }

    @api.post("/reports/{report_id}")
    def decide(report_id: str, body: DecisionIn, who=Depends(ctx.owner)):
        with ctx.db() as s:
            item = s.get(TelemetryItem, report_id)
            if not item or item.kind != "reports" or item.state != "queued" or time.time() - item.created > 7 * 86400:
                raise HTTPException(404, "That report is no longer waiting for review. Reload the page.")
            if not client.consent(s)["reports"]:
                raise HTTPException(409, "Bug reports are turned off. Turn them on in Settings → Usage and bug reports to send this one.")
            if body.decision == "send" and (body.remove_message or body.remove_log):
                raise HTTPException(422, "Remove the text first, then review the report again and send it.")
            if body.decision == "send" and body.expected_digest != hashlib.sha256(item.body.encode()).hexdigest():
                raise HTTPException(409, "The report changed since you reviewed it. Review it again before sending.")
            if body.remove_message or body.remove_log:
                from acc.telemetry_schema import Report, encode

                report = Report.model_validate_json(item.body)
                if body.remove_message:
                    report.message = ""
                # remove_log remains a compatible review flag; report/1 has no log field.
                item.body = encode(report).decode()
            item.state = {"send": "approved", "discard": "discarded", "edit": "queued"}[body.decision]
            from ..attention import resolve

            if body.decision != "edit":
                resolve(s, "telemetry:" + item.id)
            s.add(
                AuditLog(actor=who.username, action="telemetry.report", target=item.id, detail={"outcome": item.state})
            )
            s.commit()
        return {"ok": True}

    @api.post("/delete")
    def erase(who=Depends(ctx.owner)):
        with ctx.db() as s:
            client.request_delete(s, who.username)
            s.commit()
        return {"ok": True}

    return api
