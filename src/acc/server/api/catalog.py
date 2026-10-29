"""Catalog management API: add skills, agents and MCP servers, and assign them to profiles."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ... import __version__, updates
from .. import catalog_import, vault
from ..catalog_restore import Restorer
from ..catalog_store import CatalogStore, StoreError
from ..github import GitHub, GitHubError
from ..models import AuditLog, VaultKey
from ..updating import Updates
from .context import Context


def release_preview(settings, repo: str, restorer: Restorer):
    token = settings.data_dir / "secrets/update_token"
    latest = updates.check(repo, __version__, token)
    with tempfile.TemporaryDirectory(prefix="acc-catalog-restore-") as folder:
        tree = updates.download_release(repo, latest, Path(folder), token)
        return restorer.preview(tree / "catalog", latest["latest"], "latest")


class RestorePreviewIn(BaseModel):
    source: Literal["installed", "latest"] = "installed"


class RestoreIn(BaseModel):
    token: str
    ids: list[str] | None = None
    aliases: dict[str, str] = {}
    accept_risks: bool = False


class PreviewIn(BaseModel):
    url: str = Field(max_length=500)


class ItemFields(BaseModel):
    id: str
    name: str | None = None
    providers: list[str] = ["claude", "codex"]
    notes: str | None = None
    profiles: list[str] = []


class ImportIn(ItemFields):
    token: str
    accept_risks: bool = False  # import despite red flags (risky settings are still stripped at launch)


class PasteSkillIn(ItemFields):
    content: str = Field(min_length=10, max_length=200_000)
    accept_risks: bool = False


class McpIn(ItemFields):
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    source: str | None = None
    license: str | None = None


class AssignIn(BaseModel):
    profiles: list[str]


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=300)
    skills: list[str] = Field([], max_length=200)
    agents: list[str] = Field([], max_length=200)
    mcps: list[str] = Field([], max_length=200)
    providers: list[str] | None = Field(None, max_length=4)
    claude: dict = {}
    codex: dict = {}
    instructions: str = Field("", max_length=20_000)
    standalone: bool = False  # offered for standalone research (no project) in New session


def router(ctx: Context) -> APIRouter:
    settings, supervisor, db = ctx.settings, ctx.supervisor, ctx.db
    api = APIRouter(prefix="/api/catalog")
    store = CatalogStore(settings.catalog_dir, settings.catalog_local_dir)
    previews = catalog_import.Previews()
    restores = catalog_import.Previews()
    restorer = Restorer(settings.catalog_dir, settings.catalog_local_dir)
    release_lock = asyncio.Lock()

    @api.post("/restore/preview")
    async def restore_preview(body: RestorePreviewIn, who=Depends(ctx.owner)):
        try:
            if body.source == "installed":
                payload = restorer.preview(settings.catalog_dir, __version__, "installed")
            else:
                async with release_lock:
                    stamp = settings.data_dir / "catalog-release-check"
                    try:
                        checked = float(stamp.read_text())
                    except (OSError, ValueError):
                        checked = 0
                    if time.time() - checked < 60:
                        raise HTTPException(429, "Wait one minute before checking GitHub again.")
                    stamp.parent.mkdir(parents=True, exist_ok=True)
                    stamp.write_text(str(time.time()))  # failed checks count too
                    repo = Updates(ctx).config()["repository"]
                    payload = await asyncio.to_thread(release_preview, settings, repo, restorer)
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error
        payload["actor"] = who.username
        token = restores.put(payload)
        return {"token": token, "source": payload["source"], "version": payload["version"],
                "items": [{k: v for k, v in row.items() if k not in ("files", "meta")} for row in payload["items"]]}

    @api.post("/restore/apply")
    def restore_apply(body: RestoreIn, who=Depends(ctx.owner)):
        try:
            payload = restores.take(body.token)
        except catalog_import.ImportError_ as error:
            raise HTTPException(410, str(error)) from error
        if payload["actor"] != who.username:
            restores.put_back(body.token, payload)
            raise HTTPException(403, "Another owner opened this preview. Choose Preview to get your own.")
        ids = body.ids if body.ids is not None else [r["key"] for r in payload["items"] if not r["collision"] and not r["refused"]]
        detail = {"ids": ids, "source": payload["source"], "version": payload["version"],
                  "aliases": body.aliases, "accepted_risks": body.accept_risks}
        try:
            return done(who.username, "catalog.restore", ",".join(ids), detail,
                        lambda: restorer.apply(payload, body.ids, aliases=body.aliases, accept_risks=body.accept_risks))
        except HTTPException:
            restores.put_back(body.token, payload)
            raise


    def done(who: str, action: str, target: str, detail: dict, change: Callable):
        try:
            catalog = change()
        except StoreError as error:
            raise HTTPException(409, str(error))
        supervisor.set_catalog(catalog)
        with db() as s:
            s.add(AuditLog(actor=who, action=action, target=target, detail=detail))
            s.commit()
        return catalog.public()

    @api.post("/preview")
    async def preview(body: PreviewIn, who=Depends(ctx.owner)):
        token_path = settings.github_token_path
        github = GitHub(token_path.read_text().strip() if token_path.exists() else None)
        try:
            payload = await catalog_import.preview(github, body.url)
        except catalog_import.ImportError_ as error:
            raise HTTPException(422, str(error))
        except GitHubError as error:
            raise HTTPException(502, f"GitHub: {error}")
        finally:
            await github.close()
        if payload["kind"] == "choose":
            return payload
        token = previews.put(payload)
        files = [{"path": p, "bytes": len(c.encode()), "text": c if len(c) <= 200_000 else c[:200_000]}
                 for p, c in payload["files"].items()]
        return {**{k: v for k, v in payload.items() if k != "files"}, "token": token, "files": files}

    @api.post("/import")
    def import_item(body: ImportIn, who=Depends(ctx.owner)):
        try:
            payload = previews.take(body.token)
        except catalog_import.ImportError_ as error:
            raise HTTPException(410, str(error))
        if catalog_import.has_bad_flags(payload["flags"]) and not body.accept_risks:
            previews.put_back(body.token, payload)  # reviewing again shouldn't need a new fetch
            raise HTTPException(409, "This item has red flags. Tick “Add it anyway” to add it.")
        fields = {"name": body.name or payload["name"], "providers": body.providers, "notes": body.notes,
                  "source": payload["source"], "license": payload["license"], "actor": who.username}
        detail = {"source": payload["source"], "commit": payload["commit"], "profiles": body.profiles,
                  "flags": [f["message"] for f in payload["flags"]], "accepted_risks": body.accept_risks}
        if payload["kind"] == "agent":
            content = next(iter(payload["files"].values()))
            return done(who.username, "catalog.add_agent", body.id, detail,
                        lambda: store.add_agent(body.id, content, fields, body.profiles))
        return done(who.username, "catalog.add_skill", body.id, detail,
                    lambda: store.add_skill(body.id, payload["files"], fields, body.profiles))

    @api.post("/skills")
    def paste_skill(body: PasteSkillIn, who=Depends(ctx.owner)):
        flags = catalog_import.review({"SKILL.md": body.content}, [])
        if catalog_import.has_bad_flags(flags) and not body.accept_risks:
            raise HTTPException(409, "Red flags: " + "; ".join(f["message"] for f in flags if f["level"] == "bad")
                                + ". Tick “Add it anyway” to add it.")
        fields = {"name": body.name, "providers": body.providers, "notes": body.notes,
                  "source": "written in Rynhult", "license": "MIT", "actor": who.username}
        return done(who.username, "catalog.add_skill", body.id, {"source": "pasted", "profiles": body.profiles,
                                                         "accepted_risks": body.accept_risks},
                    lambda: store.add_skill(body.id, {"SKILL.md": body.content}, fields, body.profiles))

    @api.post("/mcps")
    def add_mcp(body: McpIn, who=Depends(ctx.owner)):
        """Its env values go into the vault (shared keys); the catalog keeps `vault:<id>` references. A value that
        already is such a reference names a key you can use."""
        env, created = {}, []
        with db() as s:
            for name, value in body.env.items():
                match = vault.REF.match(value.strip())
                if match:
                    key = s.get(VaultKey, match.group(1))
                    if key is None or not vault.may_use(key, who.username):
                        raise HTTPException(422, f"{name}: that key wasn't found in Keys, or you can't use it.")
                    env[name] = vault.ref(key.id)
                elif value.strip():
                    try:
                        key = vault.mcp_key(s, supervisor.vault, body.id, body.name or body.id, name, value, who.username,
                                            "catalog")
                    except vault.VaultError as error:
                        raise HTTPException(422, f"{name}: {error}")
                    created.append(key.id)
                    env[name] = vault.ref(key.id)
                else:
                    env[name] = ""
            s.commit()
        fields = {**body.model_dump(exclude={"profiles", "id"}), "env": env, "actor": who.username}
        detail = {"command": body.command, "args": body.args, "url": body.url, "env_keys": sorted(body.env),
                  "profiles": body.profiles}  # env values may be secrets: never audited
        try:
            return done(who.username, "catalog.add_mcp", body.id, detail, lambda: store.add_mcp(body.id, fields, body.profiles))
        except HTTPException:
            with db() as s:  # the server wasn't added: neither are its keys
                for key_id in created:
                    if (key := s.get(VaultKey, key_id)) is not None:
                        s.delete(key)
                s.commit()
            raise

    @api.post("/{kind}/{item_id}/remove")
    def remove(item_id: str, kind: str, who=Depends(ctx.owner)):
        if kind not in ("skill", "agent", "mcp"):
            raise HTTPException(404)
        return done(who.username, f"catalog.remove_{kind}", item_id, {}, lambda: store.remove(kind, item_id))

    @api.post("/{kind}/{item_id}/profiles")
    def assign(item_id: str, body: AssignIn, kind: str, who=Depends(ctx.owner)):
        if kind not in ("skill", "agent", "mcp"):
            raise HTTPException(404)
        return done(who.username, f"catalog.assign_{kind}", item_id, {"profiles": body.profiles},
                    lambda: store.assign(kind, item_id, body.profiles))

    @api.post("/profiles/{profile_id}")
    def save_profile(profile_id: str, body: ProfileIn, create: bool = False, who=Depends(ctx.owner)):
        return done(who.username, "catalog.create_profile" if create else "catalog.edit_profile", profile_id,
                    {"skills": body.skills, "agents": body.agents, "mcps": body.mcps},
                    lambda: store.save_profile(profile_id, body.model_dump(), create))

    @api.post("/profiles/{profile_id}/reset")
    def reset_profile(profile_id: str, who=Depends(ctx.owner)):
        return done(who.username, "catalog.reset_profile", profile_id, {}, lambda: store.drop_profile(profile_id))

    return api
