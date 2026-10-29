"""Per-session containers, accd's side (docs/CONTAINER-LAUNCHER.md): limits, grants and the launcher's audit lines.

accd is the control plane. For each container launch it writes a grant (grants/<session>.json, a folder only `acc`
can write) naming the session's account, generation, pinned-image policy, private checkout, run folder, command and
the account's limits. The account's broker then asks the acc-containers root helper, which launches only what a grant
says. The helper's audit lines (audit/*.jsonl) are copied into ACC's audit log here, so they are kept as long as it is.

Limits are per agent account, chosen by owners in Settings. A new member's account starts with MEMBER_DEFAULTS; an
owner's account has no limits unless an owner sets some. None means "no limit" for that value.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from ..broker.ops import GUARD_FILES
from .models import AuditLog, Setting, User

LIMIT_KEYS = ("cpus", "memory_mib", "pids", "disk_gib", "sessions")
MEMBER_DEFAULTS = {"cpus": 2, "memory_mib": 4096, "pids": 512, "disk_gib": 20, "sessions": 4}
OWNER_DEFAULTS = {key: None for key in LIMIT_KEYS}
# Inclusive bounds, the same as the root helper's (it refuses anything else).
RANGES = {"cpus": (1, 256), "memory_mib": (256, 4 << 20), "pids": (64, 4 << 20), "disk_gib": (1, 1 << 14),
          "sessions": (1, 64)}
AUDIT_FIELDS = ("action", "account", "session_id", "generation", "unit", "code", "reason", "cleanup", "operation",
                "image", "policy_id", "limits", "detail", "peer_uid", "correlation", "sandbox", "credential_adapter", "egress")
SAFE_NAME = re.compile(r"[0-9]{8}(?:-refused-[0-9]{1,10})?\.jsonl")


def _key(account: str) -> str:
    return f"container_limits.{account}"


def check(limits: dict) -> dict:
    if set(limits) != set(LIMIT_KEYS):
        raise ValueError(f"limits are {', '.join(LIMIT_KEYS)}")
    for key, (low, high) in RANGES.items():
        value = limits[key]
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise ValueError(f"{key} must be between {low} and {high}, or empty for no limit")
    return {key: limits[key] for key in LIMIT_KEYS}


def owner_account(s, account: str, default_account: str) -> bool:
    """Is this account an owner's? Owners share the default account; an owner may also have their own."""
    users = [(role, name or default_account) for role, name in s.execute(select(User.role, User.account))]
    roles = [role for role, name in users if name == account]
    if "member" in roles:
        return False
    return account == default_account or "owner" in roles


def limits_for(s, account: str, default_account: str) -> tuple[dict, str]:
    """(limits, "set" | "default"): what an owner saved for the account, else its role's defaults."""
    row = s.get(Setting, _key(account))
    if row is not None:
        return check(row.value), "set"
    return dict(OWNER_DEFAULTS if owner_account(s, account, default_account) else MEMBER_DEFAULTS), "default"


def save_limits(s, account: str, limits: dict, actor: str) -> dict:
    value = check(limits)
    s.merge(Setting(key=_key(account), value=value, updated_by=actor))
    return value


def set_defaults(s, account: str, role: str, actor: str) -> None:
    """A new account starts with its role's defaults (members limited, owners not), unless limits are already set."""
    if s.get(Setting, _key(account)) is None:
        save_limits(s, account, OWNER_DEFAULTS if role == "owner" else MEMBER_DEFAULTS, actor)
        s.add(AuditLog(actor=actor, action="containers.limits_default", target=account, detail={"role": role}))


def guard_hashes(files: dict[str, str]) -> str:
    """ACC_GUARD_FILES as the broker computes it for a native session: the root helper sets it inside the container,
    since the broker can't give a container any environment."""
    return " ".join(f"{name}={hashlib.sha256(files[name].encode()).hexdigest()}" for name in GUARD_FILES if name in files)


def require_creation(db, settings, project, container: bool, host: str, actor: str, provider: str) -> None:
    """All creators, including helpers, must honor the project's container policy. No native fallback."""
    from .. import container_host_record
    from .supervisor_common import LaunchRefused

    problem, reason = None, None
    if project.containers and not container:
        problem, reason = "this project requires container sessions; this launch path doesn't support them", "native_fallback"
    elif container:
        if not settings.containers_enabled or not settings.containers_attach_qualified or host != "local":
            problem, reason = "container launches require the local qualified attach path and ACC_CONTAINERS", "container_unavailable"
        else:
            qualified = container_host_record.check(settings.containers_record)
            if not qualified["passed"]:
                problem, reason = qualified["reason"], "host_not_qualified"
    if problem:
        db.add(AuditLog(actor=actor, action="container.launch_refused", target=project.slug,
                        detail={"reason": reason, "provider": provider}))
        db.commit()  # callers invoke before reserving resources or making any other mutations
        raise LaunchRefused(problem)


def write_grant(settings, session, generation: int, args: list[str], files: dict[str, str], limits: dict) -> Path:
    """grants/<session>.json, atomically and readable by root only through the folder (0750 acc:acc)."""
    grant = {"version": 1, "session_id": session.id, "generation": generation, "policy_id": settings.containers_policy,
             "account": session.run_as, "provider": session.provider, "args": list(args), "checkout": session.cwd,
             "run_dir": str(settings.session_run_dir(session)), "guard_files": guard_hashes(files),
             "limits": check(limits)}
    folder = settings.containers_dir / "grants"
    target = folder / f"{session.id}.json"
    tmp = folder / f".{session.id}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w") as handle:
        json.dump(grant, handle)
    os.replace(tmp, target)
    return target


def revoke_grant(settings, session_id: str) -> None:
    """Without its grant the root helper's reconcile stops whatever is left of the session's container."""
    (settings.containers_dir / "grants" / f"{session_id}.json").unlink(missing_ok=True)


def ingest_audit(s, settings, limit: int = 1000) -> int:
    """Copy new acc-containers audit lines into ACC's audit log. Independent file offsets are kept in a Setting."""
    folder = settings.containers_dir / "audit"
    cursor = s.get(Setting, "containers.audit_cursor")
    previous = dict(cursor.value) if cursor else {}
    positions = dict(previous.get("files", {}))
    legacy_before = previous.get("legacy_before", "")
    if "file" in previous:  # migrate the original single-stream cursor without replaying older days
        positions[previous["file"]] = previous["offset"]
        legacy_before = previous["file"][:8]
    try:
        names = sorted(p.name for p in folder.iterdir() if SAFE_NAME.fullmatch(p.name))
    except OSError:
        return 0
    added = 0
    for name in names:
        if name[:8] < legacy_before or added >= limit:
            continue
        offset = positions.get(name, 0)
        try:
            with open(folder / name, "rb") as handle:
                handle.seek(offset)
                data = handle.read(1 << 20)
        except OSError:
            continue
        end = data.rfind(b"\n") + 1  # whole lines only: the helper may be writing the next one
        for line in data[:end].splitlines():
            if added >= limit:
                break
            offset += len(line) + 1
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not str(entry.get("action", "")).startswith("container."):
                continue
            detail = {k: entry[k] for k in AUDIT_FIELDS if k in entry and k != "action"}
            if len(json.dumps(detail)) > 4000:
                detail = {k: detail[k] for k in ("account", "session_id", "code", "reason") if k in detail} | {"truncated": True}
            when = None
            try:
                when = datetime.fromisoformat(str(entry.get("ts")))
            except ValueError:
                pass
            row = AuditLog(actor="acc-containers", action=str(entry["action"])[:64],
                           target=str(entry.get("session_id") or entry.get("unit") or "")[:200] or None, detail=detail)
            if when is not None:
                row.ts = when
            s.add(row)
            added += 1
        positions[name] = offset
        if added >= limit:
            break
    position = {"files": {name: offset for name, offset in positions.items() if name in names},
                "legacy_before": legacy_before}
    if position != (cursor.value if cursor else None):
        s.merge(Setting(key="containers.audit_cursor", value=position, updated_by="acc-containers"))
    return added
