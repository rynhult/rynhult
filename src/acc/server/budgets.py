"""Usage budgets: a person's own limits on the plan usage windows ACC reads. Opt-in only.

Nobody has a budget until they set one (Settings key "budget.<username>", absent by default), and nothing is ever
applied for them. A budget is per usage window (Claude's 5-hour and 7-day windows, Codex's, Copilot's monthly AI
credits):
- "cap": warn once the window is past `limit` percent;
- "pace": `limit` percent spread evenly over the window: warn when usage runs ahead of the time gone by.
At the limit it notifies (an inbox item and a push), or also pauses new launches of that agent until the window
resets (each launch can still be confirmed past it; running sessions are never touched). "+5%" raises the limit
until the window resets.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import attention
from .models import API_PRINCIPAL, AttentionItem, Observation, Setting, User

PREFIX = "budget."
MODES = ("cap", "pace")
ACTIONS = ("notify", "pause")
EXTRA = 5


def windows(db: DbSession) -> list[dict]:
    """The usage windows ACC has readings for: {id, provider, label, used (percent), resets_at, length (seconds),
    stale}. A window whose reset has passed is stale: its reading is from before the reset, so nothing is enforced
    on it (no pause, no alert) until a fresh reading arrives; the rule waits for the next window."""
    now = time.time()
    return [{**w, "stale": w["resets_at"] <= now} for w in _readings(db)]


def _readings(db: DbSession) -> list[dict]:
    out = []
    claude = db.get(Observation, "claude.rate_limits")
    if claude:
        for key, label, length in (("five_hour", "5-hour window", 5 * 3600), ("seven_day", "7-day window", 7 * 86400)):
            w = claude.value.get(key) or {}
            if w.get("used_percentage") is not None and w.get("resets_at"):
                out.append({"id": f"claude.{key}", "provider": "claude", "label": f"Claude {label}",
                            "used": float(w["used_percentage"]), "resets_at": float(w["resets_at"]), "length": length})
    codex = db.get(Observation, "codex.rate_limits")
    if codex:
        for key in ("primary", "secondary"):
            w = codex.value.get(key) or {}
            if w.get("used_percent") is not None and w.get("resets_at") and w.get("window_minutes"):
                minutes = int(w["window_minutes"])
                label = "weekly window" if minutes >= 7 * 1440 else "5-hour window" if minutes <= 300 else f"{minutes // 60}-hour window"
                out.append({"id": f"codex.{key}", "provider": "codex", "label": f"Codex {label}",
                            "used": float(w["used_percent"]), "resets_at": float(w["resets_at"]), "length": minutes * 60})
    copilot = db.get(Observation, "copilot.usage")
    credits = (copilot.value.get("credits") or {}) if copilot else {}
    if credits and not credits.get("unlimited") and copilot.value.get("resets_at"):
        resets = datetime.fromisoformat(copilot.value["resets_at"].replace("Z", "+00:00"))
        start = resets.replace(year=resets.year - (resets.month == 1), month=(resets.month - 2) % 12 + 1)
        out.append({"id": "copilot.credits", "provider": "copilot", "label": "Copilot AI credits",
                    "used": round(100 - float(credits.get("percent_remaining", 100)), 2),
                    "resets_at": resets.timestamp(), "length": (resets - start).total_seconds()})
    return out


def forget(db: DbSession, username: str) -> bool:
    """Delete a person's budget (when they stop being an owner, or are removed): rules that can't apply are not kept.
    True when there was one."""
    row = db.get(Setting, PREFIX + username)
    if row is not None:
        db.delete(row)
    for item in db.scalars(select(AttentionItem).where(AttentionItem.dedupe_key.startswith(f"budget:{username}:", autoescape=True),
                                                       AttentionItem.resolved_at.is_(None))):
        item.resolved_at = attention.now()  # their alerts end with the budget
    return row is not None


def eligible(db: DbSession, username: str) -> bool:
    """Plan usage is read from the owners' agent account, so only owners' budgets apply. A demoted owner's rules are
    deleted (forget); this check still keeps anyone who isn't an owner from seeing usage, being paused or alerted.
    The bootstrap token ("@api", no user row) is an owner."""
    if username == API_PRINCIPAL:
        return True
    return db.scalar(select(User.role).where(User.username == username)) == "owner"  # not a removed person's name


def load(db: DbSession, username: str) -> dict:
    row = db.get(Setting, PREFIX + username)
    return row.value if row else {"rules": {}}


def validate(data: dict, known: set[str]) -> dict:
    rules = {}
    for window_id, rule in (data.get("rules") or {}).items():
        if not isinstance(rule, dict):
            continue
        if window_id not in known and not window_id.split(".")[0] in ("claude", "codex", "copilot"):
            raise ValueError(f"unknown usage window {window_id!r}")
        mode, action = rule.get("mode", "cap"), rule.get("action", "notify")
        if mode not in MODES or action not in ACTIONS:
            raise ValueError("mode is cap or pace; action is notify or pause")
        limit = rule.get("limit", 80 if mode == "cap" else 100)
        if not isinstance(limit, (int, float)) or not 1 <= limit <= 100:
            raise ValueError("a limit is a percentage from 1 to 100")
        kept = {"mode": mode, "limit": round(float(limit), 1), "action": action}
        if rule.get("extra_until") and float(rule["extra_until"]) > time.time():  # an active +5% survives edits
            kept |= {"extra": float(rule.get("extra") or 0), "extra_until": float(rule["extra_until"])}
        rules[window_id] = kept
    return {"rules": rules}


def allowed(rule: dict, window: dict, now: float | None = None) -> float:
    """How much of the window this budget allows by now, in percent."""
    now = now or time.time()
    limit = rule["limit"]
    if rule["mode"] == "pace":
        start = window["resets_at"] - window["length"]
        gone = min(1.0, max(0.0, (now - start) / window["length"])) if window["length"] else 1.0
        limit = limit * gone
    if rule.get("extra_until", 0) > now:
        limit += rule.get("extra", 0)
    return round(min(100.0, limit), 1)


def status(db: DbSession, username: str) -> dict:
    if not eligible(db, username):
        return {}
    found = {w["id"]: w for w in windows(db)}
    rules = load(db, username).get("rules", {})
    out = {}
    for window_id, rule in rules.items():
        w = found.get(window_id)
        if w:
            limit = allowed(rule, w)
            out[window_id] = {"allowed": limit, "used": w["used"], "over": not w["stale"] and w["used"] > limit,
                              "action": rule["action"], "stale": w["stale"]}
    return out


def paused(db: DbSession, username: str, provider: str, *, project_id=None, machine=None, account=None) -> list[str]:
    """The windows of this agent where this person's budget asks to pause new launches (empty: go ahead)."""
    from .security_settings import resolve
    if not resolve(db, owner=username, project_id=project_id, machine=machine, account=account)['switches']['budget_checks']:
        return []
    labels = {w["id"]: w["label"] for w in windows(db)}
    return [labels.get(window_id, window_id) for window_id, s in status(db, username).items()
            if s["over"] and s["action"] == "pause" and window_id.split(".")[0] == provider]


def evaluate(db: DbSession) -> None:
    """Raise (or clear) each person's budget items. Only people who set a budget have any."""
    found = {w["id"]: w for w in windows(db)}
    for row in db.query(Setting).filter(Setting.key.like(PREFIX + "%")).all():
        username = row.key[len(PREFIX):]
        from .security_settings import resolve
        allowed_here = eligible(db, username) and resolve(db, owner=username)['switches']['budget_checks']
        for window_id, rule in (row.value.get("rules") or {}).items():
            key = f"budget:{username}:{window_id}"
            w = found.get(window_id)
            if not allowed_here:
                attention.resolve(db, key)
                continue
            if not w:
                continue
            limit = allowed(rule, w)
            if not w["stale"] and w["used"] > limit:
                what = "of the time gone by" if rule["mode"] == "pace" else "budget"
                attention.raise_item(
                    db, key, kind="budget", severity="warn", source="budgets", owner=username,
                    title=f"{w['label']}: {w['used']:.0f}% used, past your {limit:.0f}% {what}",
                    detail=("New launches of this agent are paused until it resets; you can still confirm a launch. "
                            if rule["action"] == "pause" else "") + "Settings → Budgets has +5% for this window.")
            else:
                attention.resolve(db, key)


def add_extra(db: DbSession, username: str, window_id: str, actor: str) -> dict:
    if not eligible(db, username):
        raise ValueError("plan usage isn't tracked for your agent account yet, so there's nothing to budget")
    found = {w["id"]: w for w in windows(db)}
    value = load(db, username)
    rule = (value.get("rules") or {}).get(window_id)
    if rule is None or window_id not in found:
        raise ValueError("no budget for that window")
    if found[window_id]["stale"]:
        raise ValueError("that window has reset; its budget applies again once there's a new reading")
    now = time.time()
    current = rule.get("extra", 0) if rule.get("extra_until", 0) > now else 0
    # a new dict: the loaded JSON must not change in place, or SQLAlchemy sees no change to save
    rule = {**rule, "extra": current + EXTRA, "extra_until": found[window_id]["resets_at"]}
    db.merge(Setting(key=PREFIX + username, value={"rules": {**value["rules"], window_id: rule}}, updated_by=actor))
    return rule


def save(db: DbSession, username: str, data: dict, actor: str) -> dict:
    value = validate(data, {w["id"] for w in windows(db)})
    if value["rules"]:
        db.merge(Setting(key=PREFIX + username, value=value, updated_by=actor))
    else:  # no budgets: back to having none at all
        row = db.get(Setting, PREFIX + username)
        if row:
            db.delete(row)
    for window_id in {w["id"] for w in windows(db)} - set(value["rules"]):
        attention.resolve(db, f"budget:{username}:{window_id}")
    return value

