"""Attention inbox: list, acknowledge and snooze operational alerts."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from .. import attention, auth, risk_labels
from ..models import AttentionItem, AuditLog, Node, Project, PublishRequest, Session, ToolRequest
from ..tools import out
from .context import Context


class Snooze(BaseModel):
    minutes: int = Field(60, ge=0, le=43200, strict=True)


class DismissItem(BaseModel):
    id: int = Field(gt=0, strict=True)
    updated_at: datetime


class DismissItems(BaseModel):
    items: list[DismissItem] = Field(min_length=1, max_length=500)


class UndoDismiss(BaseModel):
    batch: int = Field(gt=0, strict=True)


def router(ctx: Context) -> APIRouter:
    db = ctx.db
    api = APIRouter()

    def attention_out(item: AttentionItem, names: dict[str, tuple[str, str]], publishes: dict | None = None,
                      tools: dict | None = None, show_risk: bool = True, image_check: dict | None = None) -> dict:
        publishes = publishes or {}
        name, project = names.get(item.session_id or "", (None, None))
        return {"image_check": image_check, "id": item.id, "kind": item.kind, "severity": item.severity, "source": item.source,
                "verified": item.verified, "title": item.title, "detail": item.detail,
                "session_id": item.session_id, "session_name": name, "project": project,
                "created_at": item.created_at, "updated_at": item.updated_at,
                "snoozed_until": item.snoozed_until, "acked_at": item.acked_at, "resolved_at": item.resolved_at,
                "persistent": attention.persistent_reason(item),
                "bulk_dismissible": attention.bulk_dismissible(item),
                # "Review · details on screen" says nothing: no label when the prompt carried nothing to judge (UX review R8)
                "risk": item.risk if item.kind == "needs_input" and show_risk and item.risk not in (None, risk_labels.DEFAULT) else None,
                "publish_id": item.dedupe_key.split(":", 1)[1] if item.kind == "publish_request" else None,
                "publish": publishes.get(item.dedupe_key.split(":", 1)[1]) if item.kind == "publish_request" else None,
                "tool": (tools or {}).get(item.dedupe_key.split(":", 1)[1]) if item.kind == "tool_request" else None}

    @api.get("/api/attention")
    def list_attention(scope: str = "open", limit: int = 200, session_id: str | None = None,
                       who=Depends(ctx.principal)):
        with db() as s:
            query = ctx.inbox_scope(select(AttentionItem), AttentionItem, who)
            if scope == "open":
                query = query.where(AttentionItem.resolved_at.is_(None),
                                    AttentionItem.acked_at.is_(None) | attention.persistent_condition(),
                                    (AttentionItem.snoozed_until.is_(None)) | (AttentionItem.snoozed_until <= auth.now()))
                query = query.order_by(attention.persistent_condition().desc())
            elif scope == "pending":
                query = query.where(AttentionItem.resolved_at.is_(None))
            if session_id is not None:
                query = query.where(AttentionItem.session_id == session_id)
            query = query.order_by(AttentionItem.created_at.desc()).limit(max(1, min(limit, 500)))
            items = list(s.scalars(query))
            # Only the listed items' sessions, not every session this person can see (rc performance check).
            wanted = {i.session_id for i in items if i.session_id}
            session_rows = list(s.execute(
                ctx.scope(select(Session.id, Session.name, Session.team_id, Project.slug, Session.project_id).join(Project, Session.project),
                          Session, who).where(Session.id.in_(wanted)))) if wanted else []
            names = {sid: (name + (f" · team {team}" if team else ""), slug)
                     for sid, name, team, slug, project_id in session_rows}
            risk_projects = {sid: project_id for sid, name, team, slug, project_id in session_rows}
            ids = [i.dedupe_key.split(":", 1)[1] for i in items if i.kind == "publish_request"]
            # The same facts Publishing shows under the request (UX review IN-1): an approver on a phone sees them too.
            requests = list(s.scalars(select(PublishRequest).where(PublishRequest.id.in_(ids)))) if ids else []
            slugs = dict(s.execute(select(Project.id, Project.slug).where(
                Project.id.in_({r.project_id for r in requests if r.project_id is not None}))).all()) if requests else {}
            publishes = {r.id: {"host": r.host, "port": r.port, "access": r.access, "action": r.action,
                                "requested_by": r.requested_by, "reason": r.reason, "project": slugs.get(r.project_id),
                                "replaces": (r.context or {}).get("replaces"), "port_owner": (r.context or {}).get("port_owner")}
                         for r in requests}
            tool_ids = [i.dedupe_key.split(":", 1)[1] for i in items if i.kind == "tool_request"]
            tools = {r.id: out(r) for r in s.scalars(select(ToolRequest).where(ToolRequest.id.in_(tool_ids)))}
            # Reuse the authorized session metadata and evaluate each scope once,
            # not a second session read or one identical policy read per item.
            risk_enabled = {project_id: risk_labels.enabled(s, who.username, project_id=project_id)
                            for project_id in {risk_projects.get(i.session_id) for i in items}}
            from ..image_capabilities import check_action
            return [attention_out(i, names, publishes, tools, risk_enabled[risk_projects.get(i.session_id)],
                                  check_action(s, i) if who.is_owner else None) for i in items]

    @api.post("/api/attention/dismiss")
    def dismiss_attention(body: DismissItems, who=Depends(ctx.principal)):
        ids = [item.id for item in body.items]
        if len(set(ids)) != len(ids) or any(item.updated_at.tzinfo is None for item in body.items):
            raise HTTPException(422, "Use distinct items with timezone-aware update timestamps.")
        at = auth.now()
        with db() as s:
            visible = {item.id: item for item in s.scalars(ctx.inbox_scope(
                select(AttentionItem), AttentionItem, who).where(AttentionItem.id.in_(ids)))}
            dismissed = []
            for expected in body.items:
                item = visible.get(expected.id)
                if item is None or not attention.bulk_dismissible(item):
                    continue
                changed = s.execute(update(AttentionItem).where(
                    AttentionItem.id == item.id, AttentionItem.updated_at == expected.updated_at,
                    AttentionItem.acked_at.is_(None), AttentionItem.resolved_at.is_(None),
                ).values(acked_at=at, updated_at=at).execution_options(synchronize_session=False))
                if changed.rowcount:
                    dismissed.append(item.id)
            batch = AuditLog(ts=at, actor=who.username, action="attention.dismiss",
                             detail={"items": dismissed, "skipped": len(ids) - len(dismissed)})
            s.add(batch)
            s.flush()
            batch_id = batch.id
            s.commit()
            return {"batch": batch_id, "dismissed": len(dismissed), "skipped": len(ids) - len(dismissed)}

    @api.post("/api/attention/dismiss/undo")
    def undo_dismiss_attention(body: UndoDismiss, who=Depends(ctx.principal)):
        at = auth.now()
        with db() as s:
            batch = s.get(AuditLog, body.batch)
            if batch is None or batch.actor != who.username or batch.action != "attention.dismiss":
                raise HTTPException(404, "Dismissal not found.")
            if at - batch.ts > timedelta(seconds=10):
                raise HTTPException(409, "The Undo period has ended.")
            visible = ctx.inbox_scope(select(AttentionItem.id), AttentionItem, who).where(
                AttentionItem.id.in_(batch.detail["items"]))
            restored = s.execute(update(AttentionItem).where(
                AttentionItem.id.in_(visible), AttentionItem.acked_at == batch.ts,
                AttentionItem.updated_at == batch.ts, AttentionItem.resolved_at.is_(None),
            ).values(acked_at=None, updated_at=at).execution_options(synchronize_session=False)).rowcount
            s.add(AuditLog(actor=who.username, action="attention.dismiss_undo", target=str(batch.id),
                           detail={"restored": restored}))
            s.commit()
            return {"restored": restored}

    @api.post("/api/attention/{item_id}/ack")
    def ack_attention(item_id: int, who=Depends(ctx.principal)):
        with db() as s:
            item = s.get(AttentionItem, item_id)
            if item is None or s.scalar(ctx.inbox_scope(select(AttentionItem.id), AttentionItem, who).where(AttentionItem.id == item_id)) is None:
                raise HTTPException(404, "That item is no longer in your Inbox. Reload the page.")
            if item.acked_at is None:
                item.acked_at = auth.now()
                s.add(AuditLog(actor=who.username, action="attention.ack", target=str(item_id), detail={"kind": item.kind}))
            s.commit()
            return {"ok": True}

    @api.post("/api/attention/{item_id}/snooze")
    def snooze_attention(item_id: int, body: Snooze, who=Depends(ctx.principal)):
        with db() as s:
            item = s.scalar(ctx.inbox_scope(select(AttentionItem), AttentionItem, who).where(AttentionItem.id == item_id))
            if item is None:
                raise HTTPException(404, "That item is no longer in your Inbox. Reload the page.")
            if not attention.persistent_reason(item) or item.resolved_at is not None:
                raise HTTPException(400, "Only unresolved machine and backup alerts can be snoozed.")
            until = auth.now() + timedelta(minutes=body.minutes) if body.minutes else None
            item.snoozed_until = until
            if item.kind == "machine_offline":
                node = s.scalar(select(Node).where(Node.name == item.dedupe_key.split(":", 1)[1]))
                if node:
                    node.snoozed_until = until
            s.add(AuditLog(actor=who.username, action="attention.snooze", target=str(item_id),
                           detail={"kind": item.kind, "minutes": body.minutes}))
            s.commit()
            return {"ok": True}

    return api
