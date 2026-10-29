"""Model endpoints: local LLM servers (Ollama, LM Studio, llama.cpp, vLLM) and compatible hosted APIs (DeepSeek,
OpenRouter, …) that agents can run on instead of their vendor's models. Owners add and remove them; everyone signed
in can see their names and models and launch on them. Keys are write-only: the API never returns one."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from ..models import AuditLog, ModelEndpoint
from .context import Context

STYLES = ("openai", "anthropic")
MAX_LIST_BYTES = 2 << 20  # a model list larger than this is refused, not buffered
LIST_DEADLINE = 20.0  # seconds for the whole discovery request, body included
MAX_MODELS = 500  # rows looked at (and kept)
MAX_LABEL = 200
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
PATH = re.compile(r"^(/[A-Za-z0-9._~\-]*)*$")


def check_url(value: str) -> str:
    """An http(s) URL with a valid host name or IP address, a port in range, a plain path, and nothing else (no
    user:password@, query or fragment)."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc or parts.query or parts.fragment \
            or "?" in value or "#" in value or not PATH.fullmatch(parts.path):
        raise ValueError("enter an http:// or https:// address with a host, an optional port and a path, and nothing else (no password, ? or #)")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise ValueError("the port must be a number from 1 to 65535") from None
    if port == 0:
        raise ValueError("the port must be a number from 1 to 65535")
    if parts.netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("the address in [ ] isn't a valid IPv6 address") from None
    elif not HOSTNAME.fullmatch(host):
        raise ValueError("the host isn't a valid host name or IP address")
    return value.rstrip("/")


def unencrypted(url: str) -> bool:
    """Plain http to anything but this machine: a key sent there crosses the network readable."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


class EndpointIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,38}$")
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field("custom", pattern=r"^(ollama|openai|anthropic|custom)$")
    base_url: str = Field(max_length=300)
    styles: dict[str, str] = Field(min_length=1, max_length=2)
    api_key: str | None = Field(None, max_length=500)
    context_tokens: int | None = Field(None, ge=1024, le=10_000_000)
    plain_http_ok: bool = False  # the owner confirmed sending the key over plain http

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return check_url(value)

    @field_validator("styles")
    @classmethod
    def _styles(cls, value: dict[str, str]) -> dict[str, str]:
        for style, url in value.items():
            if style not in STYLES:
                raise ValueError(f"the API style must be one of: {', '.join(STYLES)}")
            try:
                check_url(url)
            except ValueError as exc:
                raise ValueError(f"the {style} URL: {exc}") from None
        return {style: url.rstrip("/") for style, url in value.items()}

    @model_validator(mode="after")
    def _key_in_the_clear(self) -> "EndpointIn":
        urls = [self.base_url, *self.styles.values()]
        if self.api_key and not self.plain_http_ok and any(unencrypted(u) for u in urls):
            raise ValueError("this sends the API key over plain http to another machine. Use https, or tick "
                             "“Send it anyway” to confirm.")
        return self


def public(endpoint: ModelEndpoint, owner: bool) -> dict:
    out = {"id": endpoint.id, "name": endpoint.name, "kind": endpoint.kind, "styles": sorted(endpoint.styles or {}),
           "models": endpoint.models or [], "models_at": endpoint.models_at, "has_key": bool(endpoint.api_key),
           "context_tokens": endpoint.context_tokens}
    if owner:  # where it is, and whether it answered (members only need names and models)
        out.update(base_url=endpoint.base_url, urls=endpoint.styles, last_error=endpoint.last_error,
                   created_by=endpoint.created_by)
    return out


async def list_models(endpoint: ModelEndpoint, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """The server's models: Ollama's /api/tags, otherwise the OpenAI-style /models (or Anthropic-style /v1/models)."""
    headers, url = {}, None
    if endpoint.kind == "ollama":
        url = f"{endpoint.base_url}/api/tags"
    elif "openai" in endpoint.styles:
        url = f"{endpoint.styles['openai']}/models"
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
    else:
        url = f"{endpoint.styles['anthropic']}/v1/models"
        if endpoint.api_key:
            headers.update({"x-api-key": endpoint.api_key, "anthropic-version": "2023-06-01"})
    data = await _fetch_json(url, headers, transport)
    if not isinstance(data, dict):
        raise ValueError("the model list is not a JSON object")
    rows = data.get("models") if endpoint.kind == "ollama" else data.get("data")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("the model list has no list of models")
    models = []
    for row in rows[:MAX_MODELS]:
        if not isinstance(row, dict):
            raise ValueError("a model entry is not a JSON object")
        model_id = (row.get("name") or row.get("model")) if endpoint.kind == "ollama" else row.get("id")
        if not isinstance(model_id, str):
            raise ValueError("a model entry has no text id")
        if 0 < len(model_id) <= 120:
            size = row.get("size") if endpoint.kind == "ollama" else None
            label = row.get("display_name")
            models.append({"id": model_id, "label": (label if isinstance(label, str) and label else model_id)[:MAX_LABEL],
                           **({"size": size} if isinstance(size, int) and not isinstance(size, bool) else {})})
    return sorted(models, key=lambda m: m["id"])


async def _fetch_json(url: str, headers: dict, transport: httpx.AsyncBaseTransport | None):
    """GET url and decode its JSON body, within LIST_DEADLINE and MAX_LIST_BYTES (uncompressed: an encoded body is
    refused, since a small compressed one can inflate to anything). Problems are ValueErrors or httpx errors."""
    try:
        async with asyncio.timeout(LIST_DEADLINE):
            async with httpx.AsyncClient(timeout=10, transport=transport, follow_redirects=False) as client:
                async with client.stream("GET", url, headers={**headers, "Accept-Encoding": "identity"}) as response:
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("the model list came compressed")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > MAX_LIST_BYTES:
                        raise ValueError("the model list is over 2 MiB")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():  # identity only (checked above): no inflation
                        body += chunk
                        if len(body) > MAX_LIST_BYTES:
                            raise ValueError("the model list is over 2 MiB")
    except TimeoutError:
        raise ValueError(f"no complete model list within {LIST_DEADLINE:g} s") from None
    try:
        return json.loads(body)
    except RecursionError:
        raise ValueError("the model list is nested too deeply") from None


def router(ctx: Context) -> APIRouter:
    db = ctx.db
    api = APIRouter(prefix="/api/endpoints")

    def get(s, endpoint_id: str) -> ModelEndpoint:
        endpoint = s.get(ModelEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "That model endpoint wasn't found. Reload the page.")
        return endpoint

    async def refresh(endpoint_id: str) -> dict:
        with db() as s:
            endpoint = get(s, endpoint_id)
            s.expunge(endpoint)
            s.rollback()
        try:
            models, error = await list_models(endpoint, getattr(ctx, "endpoint_transport", None)), None
        except (httpx.HTTPError, ValueError) as exc:
            models, error = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        with db() as s:
            row = get(s, endpoint_id)
            if models is not None:
                row.models, row.models_at = models, datetime.now(timezone.utc)
            row.last_error = error
            s.commit()
            return public(row, True)

    @api.get("")
    def list_endpoints(who=Depends(ctx.principal)):
        with db() as s:
            return [public(e, who.is_owner) for e in s.scalars(select(ModelEndpoint).order_by(ModelEndpoint.name))]

    @api.post("", status_code=201)
    async def create(body: EndpointIn, who=Depends(ctx.owner)):
        with db() as s:
            if s.get(ModelEndpoint, body.id) is not None:
                raise HTTPException(409, f"An endpoint with the ID {body.id!r} already exists. Choose another ID.")
            s.add(ModelEndpoint(id=body.id, name=body.name, kind=body.kind, base_url=body.base_url, styles=body.styles,
                                api_key=body.api_key or None, context_tokens=body.context_tokens, models=[],
                                created_by=who.username))
            s.add(AuditLog(actor=who.username, action="endpoint.add", target=body.id,
                           detail={"base_url": body.base_url, "styles": sorted(body.styles), "key": bool(body.api_key),
                                   "plain_http": bool(body.api_key) and body.plain_http_ok}))
            s.commit()
        return await refresh(body.id)

    @api.post("/{endpoint_id}/refresh")
    async def refresh_models(endpoint_id: str, who=Depends(ctx.owner)):
        return await refresh(endpoint_id)

    @api.post("/{endpoint_id}/remove")
    def remove(endpoint_id: str, who=Depends(ctx.owner)):
        """Sessions already on it keep running; resuming them later fails until an endpoint with this id exists."""
        with db() as s:
            s.delete(get(s, endpoint_id))
            s.add(AuditLog(actor=who.username, action="endpoint.remove", target=endpoint_id))
            s.commit()
        return {"ok": True}

    return api
