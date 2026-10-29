"""Shared, closed wire contract. No ACC server dependencies; also used by the reference collector."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Provider = Literal["claude", "codex", "copilot", "cursor", "antigravity", "opencode"]
OS = Literal["linux", "wsl", "macos", "windows", "other"]
Kind = Literal["release", "git-checkout", "windows-installer", "mac-installer"]
Bucket = Literal["0", "1", "2-3", "4-10", "11-50", "51+"]
Count = Literal["1", "2-5", "6-20", "21-100", "101+"]
Event = Literal[
    "session.launch",
    "session.resume",
    "session.multirun",
    "session.export",
    "schedule.run",
    "orchestrator.team",
    "explain.run",
    "changes.view",
    "files.edit",
    "preview.open",
    "pr.merge",
    "pr.feedback",
    "publish.request",
    "vault.give",
    "catalog.import",
    "search.query",
    "backup.run",
    "update.install",
    "node.enroll",
    "inbox.answer",
    "guardrail.block",
]
Feature = Literal[
    "groups",
    "organizations",
    "containers",
    "backups",
    "backup_server",
    "publishing",
    "dns_provider",
    "auto_updates",
    "push",
    "issue_sources",
    "remote_access",
    "schedules",
    "orchestrator",
    "preview",
    "custom_guardrails",
]
Install = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22}$")]
# A release (or release candidate) version, never free text: prerelease tags are limited to alpha, beta and rc.
RELEASE_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?"
Version = Annotated[str, StringConstraints(pattern=rf"^(dev|{RELEASE_VERSION})$")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(Closed):
    schema_version: Literal["usage/1"] = Field(alias="schema")
    install: Install
    day: date
    acc: Version
    os: list[OS] = Field(min_length=1, max_length=5)
    install_kind: Kind
    machines: Bucket
    people: Bucket
    projects: Bucket
    sessions_running: Bucket
    vault_keys: Bucket
    providers_enabled: list[Provider] = Field(max_length=6)
    features_enabled: list[Feature] = Field(max_length=15)
    used: dict[Event, Count] = Field(max_length=21)
    by_provider: dict[Provider, Count] = Field(max_length=6)

    @model_validator(mode="after")
    def sets(self):
        for values in (self.os, self.providers_enabled, self.features_enabled):
            if len(values) != len(set(values)):
                raise ValueError("Telemetry sets must not contain duplicates")
        return self


class Frame(Closed):
    module: str = Field(pattern=r"^acc/(?:[a-zA-Z_][a-zA-Z_0-9]*/)*[a-zA-Z_][a-zA-Z_0-9]*\.py$", max_length=200)
    function: str = Field(pattern=r"^[a-zA-Z_<>][a-zA-Z_0-9<>]*$", max_length=100)
    line: int = Field(ge=1, le=1000000)


class Report(Closed):
    schema_version: Literal["report/1"] = Field(alias="schema")
    report: UUID
    install: Install
    acc: Version
    os: OS
    install_kind: Kind
    trigger: Literal[
        "server_error", "supervisor_stage", "update_failed", "install_failed", "session_crash", "broker_crash"
    ]
    component: Literal["server", "supervisor", "update", "installer", "session", "broker", "node"]
    exception: str = Field(pattern=r"^[A-Za-z_][A-Za-z_0-9]{0,79}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{16}$")
    frames: list[Frame] = Field(max_length=20)
    repeats: Count = "1"
    message: str = Field(default="", max_length=300)
    provider: Provider | None = None
    exit_code: int | None = Field(default=None, ge=-255, le=255)

    @model_validator(mode="after")
    def metadata_only(self):
        if self.trigger == "session_crash" and (self.message or self.frames):
            raise ValueError("Session crashes contain metadata only")
        return self


class Deletion(Closed):
    install: Install
    reports: list[UUID] = Field(default_factory=list, max_length=100)


def encode(value: BaseModel | dict) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def key_id(public: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(public).digest()[:16]).decode().rstrip("=")


def wire_schema(model):
    schema = model.model_json_schema()
    if model is Report:
        schema["allOf"] = [
            {
                "if": {"properties": {"trigger": {"const": "session_crash"}}},
                "then": {"properties": {"message": {"const": ""}, "frames": {"maxItems": 0}}},
            }
        ]
    if model is Usage:
        for field in ("os", "providers_enabled", "features_enabled"):
            schema["properties"][field]["uniqueItems"] = True
        for field, keys in [("used", Event), ("by_provider", Provider)]:
            entry = schema["properties"][field]
            values = entry["additionalProperties"]
            entry["properties"] = {key: values for key in get_args(keys)}
            entry["additionalProperties"] = False
            entry.pop("propertyNames", None)
    return schema
