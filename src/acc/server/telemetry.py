"""Opt-in telemetry. All network writes pass through consent and exact-byte validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import time
import traceback
import uuid
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args
from urllib.parse import unquote, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from acc.env import environment
from acc.maintenance import lease, secrets_lock
from acc.telemetry_schema import RELEASE_VERSION, Event, Provider, Report, Usage, encode, key_id

from .models import AuditLog, Project, Session, Setting, TelemetryCounter, TelemetryItem, User, VaultKey, VaultRetired
from .pulls import ANSI, CONTROL, SECRETS
from .vault import MIN_REDACT, Redactor, encoded_forms

DEFAULT_URL = "https://telemetry.rynhult.dev"
DAY = 86400
AUDIT_COUNTERS = {
    "session.create": "session.launch",
    "session.resume": "session.resume",
    "multirun.start": "session.multirun",
    "session.explain": "explain.run",
    "files.commit": "files.edit",
    "files.delete": "files.edit",
    "publish.request_publish": "publish.request",
    "vault.give": "vault.give",
    "catalog.add_agent": "catalog.import",
    "catalog.add_skill": "catalog.import",
    "catalog.add_mcp": "catalog.import",
    "backup.request": "backup.run",
    "update.request": "update.install",
    "node.enroll": "node.enroll",
    "attention.ack": "inbox.answer",
    "pr.merge": "pr.merge",
    "pr.send_to_agent": "pr.feedback",
}
# Only successful calls of these fixed route templates count. No paths or request bodies are retained.
ROUTE_COUNTERS = {
    ("GET", "/api/search"): "search.query",
    ("GET", "/api/sessions/{acc_id}/changes"): "changes.view",
    ("GET", "/api/sessions/{acc_id}/export"): "session.export",
    ("POST", "/api/projects/{project_id}/preview"): "preview.open",
}


def collector_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Use an HTTPS collector URL without credentials, query or fragment")
    return value.rstrip("/")


class Scrubber:
    def __init__(self, secrets=(), identifiers=()):
        secrets = {value for value in secrets if value}
        self.redactor = Redactor([(value, "secret") for value in secrets if value])
        self.short_secrets = sorted(
            {
                form
                for value in secrets
                if len(value) < MIN_REDACT
                for form in {
                    value,
                    *encoded_forms(value),
                    base64.b64encode(value.encode()).decode().rstrip("="),
                    base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
                }
            },
            key=len,
            reverse=True,
        )
        forms = {
            form
            for value in identifiers
            if value
            for form in {
                value,
                *encoded_forms(value),
                base64.b64encode(value.encode()).decode().rstrip("="),
                base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
            }
        }
        self.identifiers = [
            re.compile(re.escape(value) if len(value) > 1 else rf"(?<!\w){re.escape(value)}(?!\w)", re.I)
            for value in sorted(forms, key=len, reverse=True)
        ]

    def _scrub(self, text):
        if any(value in text for value in self.short_secrets):
            return "«secret»"
        text = self.redactor.text(text)
        text = re.sub(r"«key: [^»]*»", "«secret»", text)
        for pattern, _ in SECRETS:
            text = pattern.sub("«secret»", text)
        text = re.sub(r"\bASIA[A-Z0-9]{16}\b", "«secret»", text)
        text = re.sub(r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{8,}\b", "«secret»", text)
        text = re.sub(r"[A-Za-z0-9_+/=-]{32,}", "«secret»", text)

        def encoded_text(match):
            value = match[0]
            try:
                decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()
            except ValueError:
                return value
            return "«secret»" if decoded and all(c.isprintable() or c.isspace() for c in decoded) else value

        text = re.sub(r"[A-Za-z0-9_+/-]{8,}={0,2}", encoded_text, text)
        text = re.sub(r"[^\s<>@]+@[^\s<>@]+\.[A-Za-z]+", "<email>", text)
        text = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\r\n]+", "<url>", text)
        # Paths can contain spaces, including home/account names. Prefer losing the
        # rest of a diagnostic over leaking a suffix after a whitespace boundary.
        text = re.sub(r"(?:[A-Za-z]:[\\/]|(?<![\w<])/(?!/)|\\\\|~)[^\r\n]*", "<path>", text)
        if re.search(r"[\w.-]+[\\/]", text):
            return "«secret»"

        def address(match):
            try:
                ipaddress.ip_address(match[0].rstrip(".").strip("[]").split("%", 1)[0])
                return "<ip>"
            except ValueError:
                return match[0]

        text = re.sub(
            r"\[?[0-9a-fA-F]*:[0-9a-fA-F:.]+(?:%[\w.-]+)?\]?|\b(?:\d{1,3}\.){3}\d{1,3}\b",
            address,
            text,
        )
        text = re.sub(r"\b[\w-]+(?:\.[\w-]+)+\b", "<host>", text)
        for pattern in self.identifiers:
            text = pattern.sub("<identifier>", text)
        return text

    def text(self, text: str, limit=300) -> str:
        for value, _ in self.redactor.forms:
            text = text.replace(value, "«secret»")
        if any(value in text for value in self.short_secrets):
            text = "«secret»"
        for _ in range(5):
            decoded = unquote(text)
            if decoded == text:
                break
            text = decoded
        text = re.sub(r"\S*%[0-9a-fA-F]{2}\S*", "«secret»", text)
        # Joining wrapped text cannot distinguish a split token from a secret
        # whose original value contains spaces. Retain metadata, not ambiguous text.
        if "\r" in text or "\n" in text:
            return "«secret»"[:limit]
        text = CONTROL.sub("", ANSI.sub("", text))
        text = re.sub(
            r"""(?i)\b(?:authorization|proxy-authorization|set-cookie|cookie|password|passwd|secret|token"""
            r"""|api[_-]?key|access[_-]?key)\b['"]?\s*[:=].*""",
            "«secret»",
            text,
        )
        text = self._scrub(text)
        text = "\n".join(line[:200] for line in text.splitlines())[:limit]
        return self._scrub(text)[:limit]


def bucket(n, events=False):
    for limit, label in (
        ((1, "1"), (5, "2-5"), (20, "6-20"), (100, "21-100"))
        if events
        else ((0, "0"), (1, "1"), (3, "2-3"), (10, "4-10"), (50, "11-50"))
    ):
        if n <= limit:
            return label
    return "101+" if events else "51+"


def os_family():
    name = platform.system().lower()
    if name == "linux" and "microsoft" in platform.release().lower():
        return "wsl"
    return {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(name, "other")


class Telemetry:
    def __init__(self, settings, db, vault=None, transport=None):
        self.settings, self.db, self.vault, self.transport = settings, db, vault, transport
        self.directory = settings.data_dir / "secrets" / "telemetry"
        self.send_lock = asyncio.Lock()

        # SQLAlchemy retains listeners; do not keep this owner and its SQLite pool alive.
        import weakref

        owner = weakref.ref(self)

        @sqlalchemy_event.listens_for(db, "before_flush")
        def counters(s, flush_context, instances):
            telemetry = owner()
            if telemetry is None:
                return
            from .models import Event as SessionEvent
            from .models import Team

            for row in list(s.new):
                name, provider = None, None
                if isinstance(row, AuditLog):
                    name = AUDIT_COUNTERS.get(row.action)
                    if name == "session.launch":
                        provider = (row.detail or {}).get("provider")
                elif isinstance(row, Team):
                    name = "orchestrator.team"
                elif isinstance(row, SessionEvent) and row.kind == "guardrail_blocked":
                    name = "guardrail.block"
                if name and (not provider or provider in get_args(Provider)):
                    telemetry.count(s, name, provider)

    @staticmethod
    def load(s, name, default=None):
        row = s.get(Setting, "telemetry." + name)
        return row.value if row else (default or {})

    def identity(self):
        from acc import __version__

        source = getattr(self.settings, "update_source", Path(__file__).resolve().parents[3])
        if (source / ".acc-build").is_file() and re.fullmatch(RELEASE_VERSION, __version__):
            return __version__, "release"
        return "dev", "git-checkout"

    def consent(self, s):
        from .admin_features import enabled
        if not enabled(self.settings, 'telemetry'):
            return {'usage': False, 'reports': False}
        c = self.load(s, "consent")
        return {
            **c,
            "usage": c.get("usage") is True and c.get("usage_schema") == "usage/1",
            "reports": c.get("reports") is True and c.get("report_schema") == "report/1",
        }

    def url(self, s):
        return environment.get("ACC_TELEMETRY_URL", self.load(s, "collector").get("url", DEFAULT_URL))

    def policy(self, s):
        value = dict(self.load(s, "policy"))
        for kind in ("usage", "reports"):
            if environment.get("ACC_TELEMETRY_" + kind.upper()) == "off":
                value[kind] = "off"
        return value

    def allowed(self, s, kind):
        # Presence (even an empty value) disables outbound telemetry in test/CI processes.
        return (
            not any(k in environment for k in ("ACC_TEST", "CI"))
            and bool(self.url(s))
            and self.consent(s).get(kind, False)
            and self.policy(s).get(kind) != "off"
        )

    def key(self, s, kind, now=None):
        now = now or time.time()
        keys = self.load(s, "keys").get("items", [])
        for key in keys:
            if key["kind"] == kind and key.get("url", self.url(s)) == self.url(s) and now - key["created"] < 90 * DAY:
                return key
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes_raw()
        key = {
            "kind": kind,
            "created": now,
            "id": key_id(public),
            "url": self.url(s),
            "public": base64.b64encode(public).decode(),
        }
        path = self.directory / (key["id"] + ".key")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(private.private_bytes_raw())
        keys.append(key)
        s.merge(Setting(key="telemetry.keys", value={"items": keys}))
        s.flush()
        return key

    def configure(self, s, usage, reports, url, actor):
        if url:
            url = collector_url(url)
        if (usage or reports) and not url:
            raise ValueError("Set an HTTPS collector URL before enabling sharing")
        if any(
            enabled and self.policy(s).get(kind) == "off" for kind, enabled in [("usage", usage), ("reports", reports)]
        ):
            raise ValueError("Sharing is disabled by installation policy")
        old = self.consent(s)
        old_url = self.url(s)
        stored_url = self.load(s, "collector").get("url", DEFAULT_URL)
        if stored_url != url:
            s.add(AuditLog(actor=actor, action="telemetry.collector", detail={"old": stored_url, "new": url}))
        if old_url != url:
            # Queued data is never silently redirected to a new controller.
            s.execute(delete(TelemetryItem).where(TelemetryItem.state.in_(["queued", "approved", "retry"])))
        value = dict(
            usage=usage,
            reports=reports,
            usage_schema="usage/1",
            report_schema="report/1",
            by=actor,
            at=datetime.now(timezone.utc).isoformat(),
        )
        s.merge(Setting(key="telemetry.consent", value=value, updated_by=actor))
        s.merge(Setting(key="telemetry.collector", value={"url": url}, updated_by=actor))
        if "ACC_TELEMETRY_URL" not in environment:
            s.merge(Setting(key="telemetry.effective_collector", value={"url": url}, updated_by=actor))
        s.flush()
        for kind, enabled in [("usage", usage), ("reports", reports)]:
            if not enabled:
                from .attention import resolve

                for pending in s.scalars(
                    select(TelemetryItem).where(TelemetryItem.kind == kind, TelemetryItem.state != "sent")
                ):
                    resolve(s, "telemetry:" + pending.id)
                s.execute(delete(TelemetryItem).where(TelemetryItem.kind == kind, TelemetryItem.state != "sent"))
                if kind == "usage":
                    s.execute(delete(TelemetryCounter))
            else:
                self.key(s, kind)
        s.add(AuditLog(actor=actor, action="telemetry.consent", detail={"old": old, "new": value}))

    def count(self, s, event, provider=None):
        if event not in get_args(Event) or (provider and provider not in get_args(Provider)):
            raise ValueError("Unknown telemetry counter")
        if not self.consent(s)["usage"] or self.policy(s).get("usage") == "off":
            return
        day = datetime.now(timezone.utc).date().isoformat()
        key = day + ":" + event + ":" + (provider or "")
        row = next((r for r in s.new if isinstance(r, TelemetryCounter) and r.id == key), None) or s.get(
            TelemetryCounter, key
        )
        if row:
            row.count += 1
        else:
            s.add(TelemetryCounter(id=key, day=day, event=event, provider=provider or "", count=1))

    def usage(self, s, day=None):
        day = day or (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        key = self.key(s, "usage")
        used, providers = {}, {}
        for row in s.scalars(select(TelemetryCounter).where(TelemetryCounter.day == day)):
            used[row.event] = used.get(row.event, 0) + row.count
            if row.provider and row.event == "session.launch":
                providers[row.provider] = providers.get(row.provider, 0) + row.count

        def total(model):
            return bucket(s.scalar(select(func.count()).select_from(model)))

        from .models import (
            DNSBinding,
            Group,
            InfrastructureCredential,
            Node,
            Observation,
            PublishRequest,
            PushSubscription,
            Schedule,
            Team,
        )

        nodes = list(s.scalars(select(Node).where(Node.status == "active")))
        systems = {os_family()}
        for node in nodes:
            name = str(node.info.get("os", "")).lower()
            if name == "linux" and str(node.info.get("wsl", "")) in {"1", "2"}:
                name = "wsl"
            systems.add(
                {"linux": "linux", "darwin": "macos", "macos": "macos", "windows": "windows", "wsl": "wsl"}.get(
                    name, "other"
                )
            )
        enabled = set()
        for observation in s.scalars(select(Observation).where(Observation.key.like("auth.seen.%"))):
            enabled.update(p for p in observation.value.get("providers", []) if p in get_args(Provider))
        features = []
        for name, model in [
            ("groups", Group),
            ("push", PushSubscription),
            ("dns_provider", DNSBinding),
        ]:
            if s.scalar(select(func.count()).select_from(model)):
                features.append(name)
        if getattr(self.settings, "containers_enabled", False):
            features.append("containers")

        def local_config(path):
            try:
                if path.stat().st_size > 65536:
                    return {}
                value = json.loads(path.read_text())
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError):
                return {}

        updates = local_config(self.settings.data_dir / "updates-config.json")
        from acc.backup_server import Service

        try:
            backup_server = bool(Service(self.settings).targets())
        except (OSError, ValueError):
            backup_server = False
        version, install_kind = self.identity()
        for name, condition in [
            ("preview", bool(getattr(self.settings, "preview_domain", ""))),
            ("auto_updates", updates.get("automatic") is True and bool(updates.get("repository"))),
            ("backup_server", backup_server),
            (
                "dns_provider",
                bool(
                    s.scalar(
                        select(func.count())
                        .select_from(InfrastructureCredential)
                        .where(InfrastructureCredential.disabled.is_(False))
                    )
                )
                or (self.settings.data_dir / "secrets" / "cloudflare_token").is_file(),
            ),
            (
                "schedules",
                bool(
                    s.scalar(
                        select(func.count())
                        .select_from(Schedule)
                        .where(Schedule.enabled.is_(True), Schedule.deleted.is_(False))
                    )
                ),
            ),
            (
                "orchestrator",
                bool(
                    s.scalar(
                        select(func.count())
                        .select_from(Team)
                        .where(Team.stopped.is_(False), Team.expires_at > datetime.now(timezone.utc))
                    )
                ),
            ),
            (
                "backups",
                bool(getattr(self.settings, "backup_base", ""))
                and s.scalar(select(func.count()).select_from(Project).where(Project.git_backup.is_(True))) > 0,
            ),
            (
                "publishing",
                s.scalar(
                    select(func.count())
                    .select_from(PublishRequest)
                    .where(
                        PublishRequest.action == "publish",
                        PublishRequest.status.in_(["approved", "applying", "applied"]),
                    )
                )
                > 0,
            ),
            (
                "issue_sources",
                s.scalar(select(func.count()).select_from(Setting).where(Setting.key.like("issue_server.%"))) > 0,
            ),
            (
                "custom_guardrails",
                bool((s.get(Setting, "guardrails").value if s.get(Setting, "guardrails") else {}).get("custom")),
            ),
        ]:
            if condition:
                features.append(name)
        payload = Usage(
            schema="usage/1",
            install=key["id"],
            day=day,
            acc=version,
            os=sorted(systems),
            install_kind=install_kind,
            machines=bucket(1 + len(nodes)),
            people=total(User),
            projects=total(Project),
            sessions_running=bucket(
                s.scalar(
                    select(func.count())
                    .select_from(Session)
                    .where(Session.observed_state.in_(["active", "starting", "waiting_input", "idle"]))
                )
            ),
            vault_keys=total(VaultKey),
            providers_enabled=sorted(enabled),
            features_enabled=sorted(set(features)),
            used={e: bucket(n, True) for e, n in used.items()},
            by_provider={p: bucket(n, True) for p, n in providers.items()},
        )
        return encode(payload).decode()

    def scrubber(self, s):
        secrets, identifiers = (
            [],
            [getattr(self.settings, "default_account", ""), getattr(self.settings, "host_label", ""), platform.node()],
        )
        if self.vault:
            for model, decrypt in ((VaultKey, self.vault.value), (VaultRetired, self.vault.retired_value)):
                for row in s.scalars(select(model)):
                    secrets.append(decrypt(row))  # failure aborts report creation: never partial redaction
        secret_root = self.settings.data_dir / "secrets"

        def collect(value):
            if isinstance(value, str):
                secrets.append(value)
            elif isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        for path in secret_root.rglob("*"):
            if path.is_file() and path.relative_to(secret_root).parts[0] != "telemetry":
                if path.stat().st_size > 65536:
                    raise ValueError("Secret inventory exceeds its safe read limit")
                value = path.read_text(errors="ignore").strip()
                secrets.extend([value, *value.splitlines()])
                try:
                    collect(json.loads(value))
                except ValueError:
                    pass
        for user in s.scalars(select(User)):
            identifiers.extend([user.username, user.account])
            secrets.append(user.totp_secret)
        from .models import ModelEndpoint

        secrets.extend(s.scalars(select(ModelEndpoint.api_key)))
        if self.vault:
            from .infrastructure import redactor

            secrets.extend(value for value, _ in redactor(s, self.vault).pairs)
        # Known names from Rynhult's inventory; never from session transcripts or repositories.
        from .models import Base

        for table in Base.metadata.sorted_tables:
            names = [
                c
                for c in table.c
                if c.name
                in {
                    "name",
                    "hostname",
                    "host",
                    "username",
                    "account",
                    "path",
                    "repo",
                    "repository",
                    "domain",
                    "zone",
                    "display_name",
                    "slug",
                    "workspace_path",
                    "workdir",
                    "worktree",
                    "run_as",
                    "variable",
                }
            ]
            for column in names:
                for value in s.scalars(select(column)):
                    if not isinstance(value, str):
                        continue
                    identifiers.append(value)
                    if column.name in {"repo", "repository"}:
                        identifiers.extend(part.removesuffix(".git") for part in value.split("/"))
                    if column.name in {"path", "workspace_path", "workdir", "worktree"}:
                        identifiers.append(value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
        from .models import Node

        for node in s.scalars(select(Node)):
            identifiers.append(node.info.get("hostname"))
        return Scrubber(secrets, identifiers)

    def capture(self, error, trigger="server_error", component="server", provider=None, exit_code=None, db=None):
        try:
            with nullcontext(db) if db is not None else self.db() as s:
                if not self.consent(s)["reports"] or self.policy(s).get("reports") == "off":
                    return
                key = self.key(s, "reports")
                frames = []
                if trigger != "session_crash":
                    root = Path(__file__).resolve().parents[1]
                    for frame in traceback.extract_tb(error.__traceback__):
                        try:
                            relative = Path(frame.filename).resolve().relative_to(root)
                        except ValueError:
                            continue
                        frames.append(
                            {"module": "acc/" + relative.as_posix(), "function": frame.name, "line": frame.lineno}
                        )
                exception = type(error).__name__ if type(error).__module__ == "builtins" else "other"
                fingerprint = hashlib.sha256(
                    encode({"exception": exception, "frames": frames[-3:], "component": component})
                ).hexdigest()[:16]
                now = time.time()
                existing = list(
                    s.scalars(
                        select(TelemetryItem).where(
                            TelemetryItem.kind == "reports", TelemetryItem.created >= (now // DAY) * DAY
                        )
                    )
                )
                version, install_kind = self.identity()
                duplicate = next(
                    (i for i in existing if fingerprint in i.body and json.loads(i.body)["acc"] == version), None
                )
                if duplicate:
                    if duplicate.state == "queued":
                        duplicate.occurrences += 1
                        previous = Report.model_validate_json(duplicate.body)
                        previous.repeats = bucket(duplicate.occurrences, True)
                        duplicate.body = encode(previous).decode()
                        if db is None:
                            s.commit()
                    return
                if len(existing) >= 10:
                    return
                report = Report(
                    schema="report/1",
                    report=uuid.uuid4(),
                    install=key["id"],
                    acc=version,
                    os=os_family(),
                    install_kind=install_kind,
                    trigger=trigger,
                    component=component,
                    exception=exception,
                    fingerprint=fingerprint,
                    frames=frames[-20:],
                    message="" if trigger == "session_crash" else self.scrubber(s).text(str(error)),
                    provider=provider,
                    exit_code=exit_code,
                )
                body = encode(report).decode()
                if len(body.encode()) > 16384:
                    return
                s.add(
                    TelemetryItem(
                        id=str(report.report),
                        kind="reports",
                        key_id=key["id"],
                        body=body,
                        state="queued",
                        created=now,
                        due=now,
                    )
                )
                s.add(AuditLog(actor="system", action="telemetry.report", detail={"outcome": "queued"}))
                from .attention import raise_item

                raise_item(
                    s,
                    "telemetry:" + str(report.report),
                    kind="telemetry_report",
                    source="telemetry",
                    title="Rynhult hit a problem: review the report before it is sent",
                    detail="Review the exact report in Settings → Usage and bug reports. Unanswered reports expire after 7 days.",
                )
                if db is None:
                    s.commit()
        except Exception:
            # Privacy failure must neither expose its exception nor break Rynhult's error handler.
            return

    async def send(self, s, item):
        from .admin_features import enabled
        if not enabled(self.settings, 'telemetry') and item.kind != "delete":
            return  # Off pauses sharing, never a deletion request (admin features review)
        if item.kind != "delete" and not self.allowed(s, item.kind):
            return
        if any(k in environment for k in ("ACC_TEST", "CI")) or not self.url(s):
            return
        if item.state not in {"approved", "retry"} or item.due > time.time():
            return
        key = next((k for k in self.load(s, "keys").get("items", []) if k["id"] == item.key_id), None)
        if not key:
            return
        if item.kind != "delete" and key.get("url") != self.url(s):
            item.state, item.result = "discarded", "collector changed"
            return
        from acc.telemetry_schema import Deletion

        model = {"usage": Usage, "reports": Report, "delete": Deletion}[item.kind]
        body = item.body.encode()
        try:
            if len(body) > (4096 if item.kind == "usage" else 16384) or encode(model.model_validate_json(body)) != body:
                raise ValueError()
        except ValueError:
            item.state, item.result = "discarded", "invalid payload"
            s.add(
                AuditLog(
                    actor="system", action="telemetry.send", detail={"kind": item.kind, "outcome": "invalid payload"}
                )
            )
            return
        url = collector_url(key.get("url", self.url(s)) if item.kind == "delete" else self.url(s))
        private = Ed25519PrivateKey.from_private_bytes((self.directory / (key["id"] + ".key")).read_bytes())
        stamp = str(int(time.time()))
        endpoint = {"usage": "/v1/usage", "reports": "/v1/report", "delete": "/v1/delete"}[item.kind]
        signature = private.sign(stamp.encode() + b"\n" + endpoint.encode() + b"\n" + body)
        headers = {
            "X-ACC-Key": key["public"],
            "X-ACC-Time": stamp,
            "X-ACC-Signature": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
            "User-Agent": "Rynhult-telemetry/dev",
            "Accept-Encoding": "identity",
        }
        # End this read transaction before the network call: the consent read above stays current (a withdrawal
        # commits meanwhile, not after), and the writes below start a fresh one (telemetry privacy review, TP-1).
        s.commit()
        item.attempts += 1
        rate_limited = False
        try:
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False, timeout=10, trust_env=False
            ) as client:
                # Stream and ignore the response body: a collector cannot exhaust client memory.
                async with client.stream("POST", url + endpoint, content=body, headers=headers) as response:
                    ok = 200 <= response.status_code < 300
                    rate_limited = response.status_code == 429
                    if ok and item.kind == "delete":
                        if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                            ok = False
                        else:
                            receipt_body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=1025):
                                receipt_body.extend(chunk)
                                if len(receipt_body) > 1024:
                                    break
                            try:
                                if len(receipt_body) > 1024:
                                    raise ValueError()
                                payload = json.loads(receipt_body)
                                if not isinstance(payload, dict) or not isinstance(payload.get("receipt"), str):
                                    raise ValueError()
                                item.receipt = str(uuid.UUID(payload["receipt"]))
                            except (ValueError, TypeError):
                                ok = False
        except httpx.HTTPError:
            ok = False
        item.state = "sent" if ok else "retry"
        item.due = time.time() + (DAY if rate_limited else min(DAY, 60 * 2 ** min(item.attempts, 10)))
        if ok:
            item.sent_at = time.time()
        item.result = "sent" if ok else "sending failed"
        s.add(
            AuditLog(
                actor="system",
                action="telemetry.send",
                detail={"kind": item.kind, "size": len(body), "outcome": item.result, "id": item.id},
            )
        )

    def request_delete(self, s, actor):
        from acc.telemetry_schema import Deletion

        for key in self.load(s, "keys").get("items", []):
            s.add(
                TelemetryItem(
                    id=str(uuid.uuid4()),
                    kind="delete",
                    key_id=key["id"],
                    body=encode(Deletion(install=key["id"])).decode(),
                    state="approved",
                    created=time.time(),
                    due=time.time(),
                )
            )
        s.add(AuditLog(actor=actor, action="telemetry.delete", detail={"outcome": "requested"}))

    def prepare_usage(self, s):
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        identity = "usage:" + day
        item = s.get(TelemetryItem, identity)
        if item is None:
            body = self.usage(s, day)
            key = self.key(s, "usage")
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            minute = int(hashlib.sha256(key["id"].encode()).hexdigest()[:8], 16) % 1440
            item = TelemetryItem(
                id=identity,
                kind="usage",
                key_id=key["id"],
                body=body,
                state="approved",
                created=time.time(),
                due=midnight + minute * 60,
            )
            s.add(item)
            s.flush()
        return item

    def retire_keys(self, s, now):
        """Expired signing keys: the row first, then the file, both under the shared secrets_lock. A capture holds it
        from its snapshot until it has copied secrets/, so it sees the row and the file or neither. A crash between the
        two leaves only an unlisted file, which is swept a day later (a new key's file precedes its row by moments)."""
        with ExitStack() as held:
            try:
                held.enter_context(lease(secrets_lock(self.settings.data_dir), exclusive=False, timeout=0))
            except TimeoutError:
                return  # a capture is copying secrets/: the next tick retires them
            keys = self.load(s, "keys").get("items", [])
            kept = [k for k in keys if now - k["created"] <= 397 * DAY]
            if len(kept) != len(keys):
                s.merge(Setting(key="telemetry.keys", value={"items": kept}))
                s.commit()
            listed = {k["id"] + ".key" for k in kept}
            for path in self.directory.glob("*.key") if self.directory.is_dir() else ():
                if path.name not in listed and now - path.lstat().st_mtime > DAY:
                    path.unlink(missing_ok=True)

    async def tick(self):
        # send_lock is held per send, not for the whole tick: an owner's change of consent (the API takes the lock)
        # waits only for the send in progress, never for the rest of the queue (telemetry privacy review, TP-1).
        await self._tick()

    async def _tick(self):
        from .admin_features import enabled
        if not enabled(self.settings, 'telemetry'):
            # Pause sharing without applying consent-off deletion to the preserved queue, but keep sending deletion
            # requests: turning the feature off must not hold back erasure of data already sent.
            with self.db() as s:
                items = list(s.scalars(select(TelemetryItem).where(
                    TelemetryItem.kind == "delete", TelemetryItem.state.in_(["approved", "retry"]))))
                s.commit()
                for item in items:
                    async with self.send_lock:
                        try:
                            await self.send(s, item)
                            s.commit()
                        except (StaleDataError, ObjectDeletedError):
                            s.rollback()
            return
        with self.db() as s:
            now = time.time()
            from .attention import raise_item, resolve

            effective_url = self.url(s)
            if effective_url:
                collector_url(effective_url)
            previous_url = self.load(
                s, "effective_collector", {"url": self.load(s, "collector").get("url", DEFAULT_URL)}
            )["url"]
            if previous_url != effective_url:
                s.add(AuditLog(
                    actor="system", action="telemetry.collector",
                    detail={"old": previous_url, "new": effective_url, "source": "installation configuration"},
                ))
            s.merge(Setting(key="telemetry.effective_collector", value={"url": effective_url}))
            if (
                not any(k in environment for k in ("ACC_TEST", "CI"))
                and not self.load(s, "notice")
                and s.scalar(select(User.id).where(User.role == "owner").limit(1))
            ):
                raise_item(
                    s,
                    "telemetry:notice",
                    kind="telemetry_consent",
                    source="telemetry",
                    severity="info",
                    title="Optional usage statistics and bug reports are available",
                    detail="Both are off. Owners can preview and choose in Settings → Usage and bug reports.",
                )
                s.merge(Setting(key="telemetry.notice", value={"shown": True}))
            stored_consent = self.load(s, "consent")
            for kind, schema_field, version in [
                ("usage", "usage_schema", "usage/1"),
                ("reports", "report_schema", "report/1"),
            ]:
                notice = "schema_notice." + version
                if (
                    stored_consent.get(kind)
                    and stored_consent.get(schema_field) != version
                    and not self.load(s, notice)
                ):
                    raise_item(
                        s,
                        "telemetry:schema:" + kind,
                        kind="telemetry_consent",
                        source="telemetry",
                        title="What Rynhult would send has changed: choose again",
                        detail="Sending is off until you choose. Review the new preview in Settings → Usage and bug reports.",
                    )
                    s.merge(Setting(key="telemetry." + notice, value={"shown": True}))
                if not self.consent(s)[kind] or self.policy(s).get(kind) == "off":
                    for pending in s.scalars(
                        select(TelemetryItem).where(TelemetryItem.kind == kind, TelemetryItem.state != "sent")
                    ):
                        resolve(s, "telemetry:" + pending.id)
                    s.execute(delete(TelemetryItem).where(TelemetryItem.kind == kind, TelemetryItem.state != "sent"))
            for pending in s.scalars(
                select(TelemetryItem).where(TelemetryItem.state != "sent", TelemetryItem.created < now - 7 * DAY)
            ):
                resolve(s, "telemetry:" + pending.id)
            s.execute(
                delete(TelemetryCounter).where(
                    TelemetryCounter.day < (datetime.now(timezone.utc) - timedelta(days=6)).date().isoformat()
                )
            )
            s.execute(
                delete(TelemetryItem).where(TelemetryItem.state == "sent", TelemetryItem.sent_at < now - 90 * DAY)
            )
            s.execute(delete(TelemetryItem).where(TelemetryItem.state != "sent", TelemetryItem.created < now - 7 * DAY))
            if self.allowed(s, "usage"):
                self.prepare_usage(s)
            items = list(s.scalars(select(TelemetryItem).where(TelemetryItem.state.in_(["approved", "retry"]))))
            # No transaction (on SQLite, no write lock) is held across a send: a withdrawal commits between sends, and
            # each send reads consent afresh in its own transaction (telemetry privacy review, TP-1).
            s.commit()
            for item in items:
                async with self.send_lock:
                    try:
                        await self.send(s, item)
                        s.commit()
                    except (StaleDataError, ObjectDeletedError):
                        s.rollback()  # withdrawn (its row deleted) before or while it was sent: nothing more goes out
            self.retire_keys(s, now)
