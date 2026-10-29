"""Settings that apply across sessions: guardrails (owner-chosen, instance-wide)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import budgets, guardrails, risk_labels
from ..models import AuditLog, Setting
from .context import Context


class CustomRuleIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=300)
    reason: str = Field("", max_length=200)


class BudgetRuleIn(BaseModel):
    mode: str = Field("cap", pattern=r"^(cap|pace)$")
    limit: float = Field(80, ge=1, le=100)
    action: str = Field("notify", pattern=r"^(notify|pause)$")
    extra: float | None = None
    extra_until: float | None = None


class BudgetsIn(BaseModel):
    rules: dict[str, BudgetRuleIn] = Field(default_factory=dict, max_length=20)


class ExtraIn(BaseModel):
    window: str = Field(min_length=1, max_length=40)


class GuardrailsIn(BaseModel):
    rules: dict[str, bool] = {}
    custom: list[CustomRuleIn] = Field(default_factory=list, max_length=guardrails.MAX_CUSTOM)


class RiskLabelsIn(BaseModel):
    enabled: bool = Field(strict=True)


class PhoneBannerIn(BaseModel):
    dismissed: bool = Field(strict=True)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    db = ctx.db

    @api.get("/api/preferences/risk-labels")
    def get_risk_labels(who=Depends(ctx.principal)):
        with db() as s:
            return {"enabled": risk_labels.enabled(s, who.username)}

    @api.put("/api/preferences/risk-labels")
    def put_risk_labels(body: RiskLabelsIn, who=Depends(ctx.principal)):
        with db() as s:
            value = body.model_dump()
            s.merge(Setting(key=f"risk_labels.{who.username}", value=value, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action="risk_labels.update", target=who.username, detail=value))
            s.commit()
            return value

    # The Overview's "Connect your phone" banner, until the person dismisses it: per person, so it stays dismissed on
    # every browser and device they sign in from. Not audited: it only hides a hint.
    @api.get("/api/preferences/phone-banner")
    def get_phone_banner(who=Depends(ctx.principal)):
        with db() as s:
            row = s.get(Setting, f"phone_banner.{who.username}")
            return {"dismissed": bool(row and row.value.get("dismissed"))}

    @api.put("/api/preferences/phone-banner")
    def put_phone_banner(body: PhoneBannerIn, who=Depends(ctx.principal)):
        with db() as s:
            value = body.model_dump()
            s.merge(Setting(key=f"phone_banner.{who.username}", value=value, updated_by=who.username))
            s.commit()
            return value

    def described(value: dict) -> dict:
        return {**value, "available": [{"id": key, "label": label, "help": help_}
                                       for key, (label, help_) in guardrails.RULES.items()]}

    @api.get("/api/guardrails")
    def get_guardrails(who=Depends(ctx.principal)):
        """Everyone can see the limits their agents run under; only owners change them."""
        with db() as s:
            return described(guardrails.load(s))

    @api.put("/api/guardrails")
    def put_guardrails(body: GuardrailsIn, who=Depends(ctx.owner)):
        with db() as s:
            try:
                value = guardrails.save(s, body.model_dump(), who.username)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            s.add(AuditLog(actor=who.username, action="guardrails.update", target="guardrails", detail=value))
            s.commit()
            return described(value)

    def budget_state(s, who) -> dict:
        """Your budgets and the usage windows they can apply to. Plan usage is read from the owner's agent
        account, so only owners see windows (and can set budgets) for now."""
        if not who.is_owner:  # no owner-wide usage at all, even through rules kept from before a demotion
            return {"windows": [], "rules": budgets.load(s, who.username).get("rules", {}), "status": {},
                    "extra": budgets.EXTRA}
        return {"windows": budgets.windows(s), "rules": budgets.load(s, who.username).get("rules", {}),
                "status": budgets.status(s, who.username), "extra": budgets.EXTRA}

    @api.get("/api/budgets")
    def get_budgets(who=Depends(ctx.principal)):
        with db() as s:
            return budget_state(s, who)

    @api.put("/api/budgets")
    def put_budgets(body: BudgetsIn, who=Depends(ctx.principal)):
        with db() as s:
            if body.rules and not who.is_owner:
                raise HTTPException(403, "Plan usage isn't tracked for your agent account yet, so there's nothing to budget. Only owners can set budgets for now.")
            try:
                value = budgets.save(s, who.username, body.model_dump(), who.username)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            s.add(AuditLog(actor=who.username, action="budgets.update", target=who.username, detail=value))
            budgets.evaluate(s)
            s.commit()
            return budget_state(s, who)

    @api.post("/api/budgets/extra")
    def budget_extra(body: ExtraIn, who=Depends(ctx.principal)):
        """+5% on one window until it resets."""
        if not who.is_owner:
            raise HTTPException(403, "Plan usage isn't tracked for your agent account yet, so there's nothing to budget. Only owners can set budgets for now.")
        with db() as s:
            try:
                budgets.add_extra(s, who.username, body.window, who.username)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            s.add(AuditLog(actor=who.username, action="budgets.extra", target=body.window, detail={"percent": budgets.EXTRA}))
            budgets.evaluate(s)
            s.commit()
            return budget_state(s, who)

    return api
