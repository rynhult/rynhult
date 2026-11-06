"""Settings that apply across sessions: guardrails (owner-chosen, instance-wide)."""
from __future__ import annotations

from typing import Literal, get_args
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

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
    rules: dict[str, StrictBool] = {}
    custom: list[CustomRuleIn] = Field(default_factory=list, max_length=guardrails.MAX_CUSTOM)
    revision: int | None = Field(default=None, ge=0, strict=True)


class RiskLabelsIn(BaseModel):
    enabled: bool = Field(strict=True)


def shell_enabled(value, request):
    """Classic choices remain stored; the released layout is always new."""
    return True


class UIShellIn(BaseModel):
    enabled: bool = Field(strict=True)


HeaderModule = Literal['machine_load', 'plan_usage', 'go_to', 'conversation_search', 'file_search']


class UIHeaderIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order: list[HeaderModule] = Field(min_length=5, max_length=5)
    hidden: list[HeaderModule] = Field(max_length=5)

    @field_validator('order', 'hidden')
    @classmethod
    def unique_modules(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Each header module may appear only once.')
        return value


def default_header() -> dict:
    return {'order': list(get_args(HeaderModule)), 'hidden': []}


class SetupLevelIn(BaseModel):
    # No names, policy fields or launch options may cross this boundary.
    model_config = ConfigDict(extra="forbid")
    level: int | None = Field(ge=0, le=6, strict=True)
    dismissed: bool = Field(default=True, strict=True)


class PhoneBannerIn(BaseModel):
    dismissed: bool = Field(strict=True)


def router(ctx: Context) -> APIRouter:
    from .security_settings import audited_router
    api = audited_router(ctx, audit_paths=('/api/guardrails',))
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

    def shell_person_id(who) -> str | None:
        # SQLite can reuse User.id after removal. Usernames are immutable and
        # permanently reserved by RetiredUser; this opaque presentation ID cannot
        # be reassigned. The browser also namespaces it by installation origin.
        return str(uuid5(NAMESPACE_URL, f"urn:rynhult:ui-shell:{who.username}")) if who.user_id is not None else None

    @api.get("/api/preferences/ui-shell")
    def get_ui_shell(request: Request, who=Depends(ctx.principal)):
        with db() as s:
            row = s.get(Setting, f"ui_shell.{who.username}")
            return {"enabled": shell_enabled(row.value if row else {}, request), "person_id": shell_person_id(who)}

    @api.put("/api/preferences/ui-shell")
    def put_ui_shell(body: UIShellIn, request: Request, who=Depends(ctx.principal)):
        with db() as s:
            value = body.model_dump()
            s.merge(Setting(key=f"ui_shell.{who.username}", value=value, updated_by=who.username))
            s.commit()
            return {"enabled": shell_enabled(value, request), "person_id": shell_person_id(who)}

    # Presentation only, with exactly the same caller-scoped principal contract as ui-shell.
    # Required controls and role authorization are deliberately absent from this schema.
    @api.get('/api/preferences/ui-header')
    def get_ui_header(who=Depends(ctx.principal)):
        with db() as s:
            row = s.get(Setting, f'ui_header.{who.username}')
            if row:
                try:
                    return UIHeaderIn.model_validate(row.value).model_dump()
                except ValidationError:
                    pass  # Invalid/obsolete stored presentation must never hide required controls.
            return default_header()

    @api.put('/api/preferences/ui-header')
    def put_ui_header(body: UIHeaderIn, who=Depends(ctx.principal)):
        with db() as s:
            value = body.model_dump()
            s.merge(Setting(key=f'ui_header.{who.username}', value=value, updated_by=who.username))
            s.commit()
            return value

    def setup_person(who=Depends(ctx.principal)):
        if who.user_id is None:
            raise HTTPException(403, "Choose your layout from a signed-in person browser.")
        return who

    @api.get("/api/preferences/setup-level")
    def get_setup_level(who=Depends(setup_person)):
        with db() as s:
            row = s.get(Setting, f"setup_level.{who.username}")
            value = row.value if row else {}
            # Missing/unknown versions preserve the existing complete layout.
            level = value.get("level") if value.get("version") == 1 else None
            if type(level) is not int or not 0 <= level <= 6:
                level = None
            return {"level": level, "dismissed": value.get("dismissed") is True, "version": 1}

    @api.put("/api/preferences/setup-level")
    def put_setup_level(body: SetupLevelIn, who=Depends(setup_person)):
        with db() as s:
            value = {**body.model_dump(), "version": 1}
            s.merge(Setting(key=f"setup_level.{who.username}", value=value, updated_by=who.username))
            s.commit()
            return value

    def described(value: dict) -> dict:
        return {**value, "available": [{"id": key, "label": label, "help": help_}
                                       for key, (label, help_) in guardrails.RULES.items()]}

    @api.get("/api/guardrails")
    def get_guardrails(who=Depends(ctx.principal)):
        """Everyone can see the limits their agents run under; only owners change them."""
        from .security_settings import human
        human(who)
        with db() as s:
            value = guardrails.load(s)
            from ..security_settings import revision
            value['revision'] = revision(s)
            if not who.is_owner:
                value['custom'] = []
            return described(value)

    @api.put("/api/guardrails")
    def put_guardrails(body: GuardrailsIn, request: Request, who=Depends(ctx.owner)):
        with db() as s:
            from .. import security_settings
            from .security_settings import browser
            browser(s, request, who)
            if body.revision is None:
                raise HTTPException(409, 'Review Security settings again before saving (a policy revision is required).')
            try:
                # Compatibility route: instance-scoped, revision-checked partial update.
                security_settings.lock_revision(s)
                current = guardrails.load(s)
                submitted = body.model_dump(exclude_unset=True, exclude={"revision"})
                value = guardrails.validate({
                    'rules': {**current['rules'], **submitted.get('rules', {})},
                    'custom': submitted.get('custom', current['custom']),
                })
                scope = {'kind': 'instance'}
                _, before, _ = security_settings.planned(s, scope, {}, 'custom')
                overrides = {**before, **submitted.get('rules', {})}
                legacy = s.get(Setting, 'guardrails')
                if value['custom'] != ((legacy.value.get('custom') or []) if legacy else []) and security_settings.affected(s, {'kind': 'instance'}):
                    raise security_settings.Conflict('Stop affected sessions before changing custom Security blocks.')
                security_settings.save(s, scope, overrides, actor=who.username,
                                       expected_revision=body.revision)
                guardrails.save(s, value, who.username)
            except security_settings.Conflict as error:
                raise HTTPException(409, str(error)) from None
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
