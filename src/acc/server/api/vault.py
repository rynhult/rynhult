"""Key vault API (docs/VAULT.md): add, change and delete keys, give them to projects, paste a .env.

Values are write-only: no route returns one, not even to the key's owner. Keys you can't see answer 404 (their
existence isn't revealed); changing a shared key that isn't yours answers 403. ACC's owners have no extra rights over
members' private keys, except that they can delete any key (someone leaves): audited as such.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import vault
from ..models import AuditLog, Project, VaultGrant, VaultKey, utcnow
from .context import Context, Principal


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    variable: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=vault.MAX_VALUE)
    shared: bool = False
    remote: bool = False
    note: str | None = Field(None, max_length=500)


class KeyEdit(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    variable: str | None = Field(None, min_length=1, max_length=64)
    note: str | None = Field(None, max_length=500)
    shared: bool | None = None
    remote: bool | None = None


class ValueIn(BaseModel):
    value: str = Field(min_length=1, max_length=vault.MAX_VALUE)


class ProjectRef(BaseModel):
    project_id: int


class EnvIn(BaseModel):
    text: str = Field(max_length=256 << 10)


class EnvImportIn(EnvIn):
    pick: list[str] = Field(max_length=200)  # the variables to add
    names: dict[str, str] = {}  # variable -> name (default: the variable)
    shared: bool = False
    remote: bool = False


def router(ctx: Context) -> APIRouter:
    db, supervisor = ctx.db, ctx.supervisor
    api = APIRouter()

    def store() -> vault.Vault:
        return supervisor.vault

    def project_names(s, key: VaultKey, who: Principal) -> list[dict]:
        rows = s.scalars(select(Project).join(VaultGrant, VaultGrant.project_id == Project.id)
                         .where(VaultGrant.key_id == key.id).order_by(Project.name))
        return [{"id": p.id, "name": p.name, "archived": p.archived} for p in rows if ctx.owns(p, who)]

    def key_out(s, key: VaultKey, who: Principal) -> dict:
        return {"id": key.id, "name": key.name, "variable": key.variable, "note": key.note, "owner": key.owner,
                "shared": key.shared, "remote": key.remote, "mine": key.owner == who.username,
                "created_at": key.created_at, "updated_at": key.updated_at, "last_used_at": key.last_used_at,
                "projects": project_names(s, key, who), "mcp": mcp_users(key.id)}

    def mcp_users(key_id: str) -> list[str]:
        """Catalog MCP servers whose secrets are this key."""
        return sorted(item.id for item in supervisor.catalog.mcps.values()
                      if vault.ref(key_id) in (item.meta.get("env") or {}).values())

    def visible(s, who: Principal, key_id: str) -> VaultKey:
        key = s.get(VaultKey, key_id)
        if key is None or not vault.may_use(key, who.username):
            raise HTTPException(404, "Key not found.")
        return key

    def owned(s, who: Principal, key_id: str) -> VaultKey:
        key = visible(s, who, key_id)
        if key.owner != who.username:
            raise HTTPException(403, "Only the key's owner can change it.")
        return key

    def audit(s, who: Principal, action: str, key: VaultKey, **detail) -> None:
        """What happened to a key. A private key is its owner's alone, so the audit log (which every owner reads)
        records its id and what changed, never its name or variable."""
        if key.shared:
            detail = {"name": key.name, **detail}
        else:
            detail = {k: v for k, v in detail.items() if k not in ("name", "variable", "to")} | {"private": True}
        s.add(AuditLog(actor=who.username, action=action, target=key.id, detail=detail))

    def refused(error: vault.VaultError) -> HTTPException:
        return HTTPException(422, str(error))

    @api.get("/api/vault/keys")
    def list_keys(who=Depends(ctx.principal)):
        """Your keys and the ones others share."""
        with db() as s:
            rows = s.scalars(select(VaultKey).where((VaultKey.owner == who.username) | VaultKey.shared)
                             .order_by(VaultKey.name, VaultKey.id))
            return [key_out(s, k, who) for k in rows]

    @api.post("/api/vault/keys", status_code=201)
    def add_key(body: KeyIn, who=Depends(ctx.principal)):
        with db() as s:
            try:
                key = vault.create_key(s, store(), name=body.name, variable=body.variable, value=body.value, owner=who.username,
                                 shared=body.shared, remote=body.remote, note=body.note)
            except vault.VaultError as error:
                raise refused(error)
            audit(s, who, "vault.add", key, variable=key.variable, shared=key.shared, remote=key.remote)
            s.commit()
            return key_out(s, key, who)

    @api.post("/api/vault/import/preview")
    def preview_env(body: EnvIn, who=Depends(ctx.principal)):
        """A pasted .env as names and variables, never values: what would be added, and what's wrong with a line."""
        with db() as s:
            mine = set(s.scalars(select(VaultKey.variable).where(VaultKey.owner == who.username)))
        return {"entries": [{"line": e.line, "variable": e.variable, "problem": e.problem, "exists": e.variable in mine}
                            for e in vault.parse_env(body.text)]}

    @api.post("/api/vault/import", status_code=201)
    def import_env(body: EnvImportIn, who=Depends(ctx.principal)):
        entries = {e.variable: e for e in vault.parse_env(body.text) if e.problem is None}
        missing = [v for v in body.pick if v not in entries]
        if missing:
            raise HTTPException(422, f"These aren't in the pasted text, or can't be used: {', '.join(missing[:5])}. Paste the text again and pick from the list.")
        with db() as s:
            added = []
            for variable in dict.fromkeys(body.pick):
                try:
                    key = vault.create_key(s, store(), name=(body.names.get(variable) or variable), variable=variable,
                                     value=entries[variable].value, owner=who.username, shared=body.shared,
                                     remote=body.remote)
                except vault.VaultError as error:
                    raise HTTPException(422, f"{variable}: {error}")
                audit(s, who, "vault.add", key, variable=key.variable, shared=key.shared, remote=key.remote, source=".env")
                added.append(key)
            s.commit()
            return {"added": [key_out(s, k, who) for k in added]}

    @api.post("/api/vault/keys/{key_id}")
    def edit_key(key_id: str, body: KeyEdit, who=Depends(ctx.principal)):
        """Rename it, change its variable or note, share it or not, allow other machines or not."""
        with db() as s:
            key = owned(s, who, key_id)
            changed = False
            if body.name is not None and body.name.strip() and body.name.strip() != key.name:
                audit(s, who, "vault.rename", key, to=body.name.strip())
                key.name, changed = body.name.strip(), True
            if body.variable is not None and body.variable != key.variable:
                try:
                    variable = vault.check_variable(body.variable)
                    value = store().value(key)
                except vault.VaultError as error:
                    raise refused(error)
                audit(s, who, "vault.rename", key, variable={"from": key.variable, "to": variable})
                key.variable, key.ciphertext, changed = variable, store().seal(s, key.id, variable, value), True
            if body.note is not None and (body.note.strip() or None) != key.note:
                audit(s, who, "vault.rename", key, note=True)
                key.note, changed = body.note.strip() or None, True
            if body.shared is not None and body.shared != key.shared:
                audit(s, who, "vault.share", key, shared=body.shared)
                key.shared, changed = body.shared, True
            if body.remote is not None and body.remote != key.remote:
                audit(s, who, "vault.remote", key, remote=body.remote)
                key.remote, changed = body.remote, True
            if changed:
                key.updated_at = utcnow()
            s.commit()
            return key_out(s, key, who)

    @api.post("/api/vault/keys/{key_id}/replace")
    def replace_value(key_id: str, body: ValueIn, who=Depends(ctx.principal)):
        """A new value (the old one is gone): sessions get it at their next launch or resume."""
        with db() as s:
            key = owned(s, who, key_id)
            try:
                value = vault.check_value(body.value)
                store().retire(s, key)  # sessions that have the old value stay redacted (D22)
                key.ciphertext = store().seal(s, key.id, key.variable, value)
            except vault.VaultError as error:
                raise refused(error)
            key.updated_at = utcnow()
            audit(s, who, "vault.replace", key)
            s.commit()
            return key_out(s, key, who)

    @api.post("/api/vault/keys/{key_id}/remove")
    def remove_key(key_id: str, who=Depends(ctx.principal)):
        with db() as s:
            key = s.get(VaultKey, key_id)
            if key is None or not (vault.may_use(key, who.username) or who.is_owner):
                raise HTTPException(404, "Key not found.")
            if key.owner != who.username and not who.is_owner:
                raise HTTPException(403, "Only the key's owner can delete it.")
            users = mcp_users(key.id)
            if users:
                raise HTTPException(409, f"This key is used by the MCP server {', '.join(users)}. Remove it from that server in the Catalog first.")
            audit(s, who, "vault.delete", key, variable=key.variable,
                  **({"as_owner": True, "owner": key.owner} if key.owner != who.username else {}))
            store().retire(s, key)
            s.delete(key)
            s.commit()
            store().forget(key_id)
        return {"deleted": key_id}

    @api.post("/api/vault/keys/{key_id}/give")
    def give(key_id: str, body: ProjectRef, who=Depends(ctx.principal)):
        """Give a key to a project you can launch in (archived ones too: their sessions can be resumed)."""
        with db() as s:
            key = visible(s, who, key_id)
            project = ctx.project_for(s, who, body.project_id)
            if s.get(VaultGrant, (key.id, project.id)) is None:
                s.add(VaultGrant(key_id=key.id, project_id=project.id, given_by=who.username))
                audit(s, who, "vault.give", key, project=project.slug)
            s.commit()
            return key_out(s, key, who)

    @api.post("/api/vault/keys/{key_id}/take")
    def take(key_id: str, body: ProjectRef, who=Depends(ctx.principal)):
        """Take a key back from a project: its sessions lose it at their next launch or resume."""
        with db() as s:
            key = visible(s, who, key_id)
            project = ctx.project_for(s, who, body.project_id)
            grant = s.get(VaultGrant, (key.id, project.id))
            if grant is not None:
                s.delete(grant)
                audit(s, who, "vault.take", key, project=project.slug)
            s.commit()
            return key_out(s, key, who)

    @api.get("/api/projects/{project_id}/keys")
    def project_keys(project_id: int, who=Depends(ctx.principal)):
        """The keys given to a project that you can see, and the ones you could give it."""
        with db() as s:
            project = ctx.project_for(s, who, project_id)
            given = set(s.scalars(select(VaultGrant.key_id).where(VaultGrant.project_id == project.id)))
            usable = s.scalars(select(VaultKey).where((VaultKey.owner == who.username) | VaultKey.shared)
                               .order_by(VaultKey.name, VaultKey.id)).all()
            server_keys = set(s.scalars(select(VaultGrant.key_id).where(VaultGrant.project_id == project.id,
                                                                        VaultGrant.server_only.is_(True))))
            return {"keys": [{**key_out(s, k, who), "server_only": k.id in server_keys} for k in usable if k.id in given],
                    "available": [key_out(s, k, who) for k in usable if k.id not in given]}

    @api.get("/api/vault/people")
    def people(who=Depends(ctx.owner)):
        """Owners: how many keys each person has (names stay private), so a leaver's keys can be deleted."""
        with db() as s:
            rows = s.execute(select(VaultKey.owner, func.count()).group_by(VaultKey.owner)).all()
            return [{"owner": owner, "keys": count} for owner, count in rows]

    @api.post("/api/vault/people/{username}/remove")
    def remove_persons_keys(username: str, who=Depends(ctx.owner)):
        """Owners: delete every key of one person (e.g. when they leave). Keys an MCP server uses are kept."""
        with db() as s:
            deleted, kept = 0, []
            for key in s.scalars(select(VaultKey).where(VaultKey.owner == username)).all():
                if mcp_users(key.id):
                    kept.append(key.name if key.shared or key.owner == who.username else "a private key")
                    continue
                audit(s, who, "vault.delete", key, variable=key.variable, as_owner=key.owner != who.username,
                      owner=key.owner)
                store().retire(s, key)
                s.delete(key)
                store().forget(key.id)
                deleted += 1
            s.commit()
        return {"deleted": deleted, "kept": kept}

    return api
