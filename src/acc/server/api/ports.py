"""Port registry: local ports belong to a project (or an outside service), so projects never collide and members
publish only their own projects' ports. Dev ports are 8000-8099, prod 8100-8199 (what publishing accepts)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models import AuditLog, PortReservation, Project
from .context import Context

RANGES = {"dev": range(8000, 8100), "prod": range(8100, 8200)}


class ReserveIn(BaseModel):
    range: str = Field("dev", pattern=r"^(dev|prod)$")
    port: int | None = Field(None, ge=8000, le=8199)  # a specific one; otherwise the next free in the range


class OutsideIn(BaseModel):
    port: int = Field(ge=1024, le=65535)
    label: str = Field(min_length=1, max_length=120)


def router(ctx: Context) -> APIRouter:
    db = ctx.db
    api = APIRouter()

    def out(r: PortReservation, who, projects: dict[int, Project]) -> dict:
        project = projects.get(r.project_id) if r.project_id else None
        visible = who.is_owner or r.owner == who.username
        return {"port": r.port, "range": next((k for k, v in RANGES.items() if r.port in v), "other"),
                "project_id": project.id if project and visible else None,
                "project": (project.slug if project else r.label) if visible else None, "mine": r.owner == who.username,
                "owner": r.owner if who.is_owner else None}

    @api.get("/api/ports")
    def list_ports(who=Depends(ctx.principal)):
        """Every reserved port (ports are shared, so members see which are taken, not whose)."""
        with db() as s:
            projects = {p.id: p for p in s.scalars(select(Project))}
            return [out(r, who, projects) for r in s.scalars(select(PortReservation).order_by(PortReservation.port))]

    @api.post("/api/projects/{project_id}/ports", status_code=201)
    def reserve(project_id: int, body: ReserveIn, who=Depends(ctx.principal)):
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            taken = set(s.scalars(select(PortReservation.port)))
            if body.port is not None:
                if body.port in taken:
                    raise HTTPException(409, f"Port {body.port} is already reserved. Choose another port.")
                port = body.port
            else:
                port = next((p for p in RANGES[body.range] if p not in taken), None)
                if port is None:
                    raise HTTPException(409, f"Every {body.range} port is reserved. Release one you no longer use, then try again.")
            reservation = PortReservation(port=port, project_id=project.id, owner=project.owner)
            s.add(reservation)
            s.add(AuditLog(actor=who.username, action="port.reserve", target=str(port), detail={"project": project.slug}))
            s.commit()
            return out(reservation, who, {project.id: project})

    @api.post("/api/ports", status_code=201)
    def reserve_outside(body: OutsideIn, who=Depends(ctx.owner)):
        """A port an outside service uses (not an ACC project), so ACC never hands it out."""
        with db() as s:
            if s.get(PortReservation, body.port) is not None:
                raise HTTPException(409, f"Port {body.port} is already reserved. Choose another port.")
            reservation = PortReservation(port=body.port, label=body.label.strip(), owner=who.username)
            s.add(reservation)
            s.add(AuditLog(actor=who.username, action="port.reserve", target=str(body.port), detail={"label": body.label}))
            s.commit()
            return out(reservation, who, {})

    @api.post("/api/ports/{port}/release")
    def release(port: int, who=Depends(ctx.principal)):
        with db() as s:
            reservation = s.get(PortReservation, port)
            if not ctx.owns(reservation, who):
                raise HTTPException(404, "That port isn't reserved any more. Reload the page.")
            s.delete(reservation)
            s.add(AuditLog(actor=who.username, action="port.release", target=str(port)))
            s.commit()
        return {"ok": True}

    return api
