"""The curated catalog (catalog/ in this repo): skills, Claude subagents, MCP servers and profiles.

Everything is pinned and was read before vendoring; manifest.toml records provenance. The loader
checks that every manifest entry has its files and that profiles only reference known items, so a
broken catalog fails at startup instead of at launch.

A local overlay (/srv/acc/catalog-local, written from the dashboard by catalog_store) adds items and
profiles and can override repo profiles. Items keep an `origin` of "repo" or "local".
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import providers as registry

log = logging.getLogger("acc.catalog")

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".ts", ".js", ".json", ".toml", ".yaml", ".yml", "",
                 ".csv", ".css", ".html", ".mjs", ".cjs", ".jsx", ".tsx", ".svg", ".xml", ".ini", ".cfg"}
LOCAL_FILE = "local.json"
CAPABILITY = {"skill": "skills", "agent": "agents", "mcp": "mcp"}  # Provider.supports entry per kind
NOUN = {"skill": "skills", "agent": "subagents", "mcp": "MCP servers"}


class CatalogError(ValueError):
    pass


@dataclass
class Item:
    id: str
    kind: str  # skill | agent | mcp
    meta: dict
    files: dict[str, str] = field(default_factory=dict)
    content_root: Path | None = None

    @property
    def providers(self) -> list[str]:
        """Agents that can use this item. Unset, or ["claude", "codex"] (written when those were the only two, meaning
        "every agent"), is every provider that supports this kind of item; anything else is that list, less the
        providers that can't receive this kind of item at all (a list naming one would offer it where it is dropped)."""
        supporting = [p.id for p in registry.PROVIDERS.values() if CAPABILITY[self.kind] in p.supports]
        listed = self.meta.get("providers")
        if listed and sorted(listed) != ["claude", "codex"]:
            return [p for p in listed if p in supporting]
        return supporting

    def unavailable(self, provider_id: str) -> str | None:
        """Why a provider can't use this item, in words for the dashboard (None: it can)."""
        provider = registry.PROVIDERS.get(provider_id)
        if provider is not None and CAPABILITY[self.kind] not in provider.supports:
            return f"{provider.label} can't receive {NOUN[self.kind]} from Rynhult"
        if provider_id not in self.providers:
            return f"limited to {', '.join(self.providers) or 'no agent'}"
        return None


@dataclass
class Catalog:
    root: Path
    skills: dict[str, Item]
    agents: dict[str, Item]
    mcps: dict[str, Item]
    profiles: dict[str, dict]
    deferred: list[dict]
    revision: str
    codex_policy: dict = field(default_factory=dict)
    local_root: Path | None = None
    repo_profiles: dict[str, dict] = field(default_factory=dict)

    def pool(self, kind: str) -> dict[str, Item]:
        return {"skill": self.skills, "agent": self.agents, "mcp": self.mcps}[kind]

    def skill_dir(self, skill_id: str) -> Path:
        item = self.skills[skill_id]
        base = item.content_root or (self.local_root if item.meta.get("origin") == "local" else self.root)
        return base / "skills" / skill_id

    def public(self) -> dict:
        """What the dashboard needs (no file contents)."""
        def item(i: Item) -> dict:
            meta = {k: v for k, v in i.meta.items() if k not in ("env",)}
            return {**meta, "id": i.id, "kind": i.kind, "providers": i.providers, "file_count": len(i.files),
                    "env_keys": sorted(i.meta.get("env", {})), "origin": i.meta.get("origin", "repo")}

        def profile(p: dict) -> dict:
            origin = "local" if p["id"] not in self.repo_profiles else ("override" if p is not self.repo_profiles[p["id"]] else "repo")
            return {**p, "origin": origin}
        return {
            "revision": self.revision,
            "skills": [item(i) for i in self.skills.values()],
            "agents": [item(i) for i in self.agents.values()],
            "mcps": [item(i) for i in self.mcps.values()],
            "profiles": [profile(p) for p in self.profiles.values()],
            "deferred": self.deferred,
            "codex_policy": self.codex_policy,
        }


def _read_tree(directory: Path) -> dict[str, str]:
    files = {}
    for path in sorted(directory.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue  # left behind when a skill's script runs in place (e.g. the root session)
        if path.is_file():
            if path.suffix not in TEXT_SUFFIXES and path.name not in ("LICENSE",):
                raise CatalogError(f"{path} is not a text file; the catalog vendors text only")
            files[str(path.relative_to(directory))] = path.read_text()
    return files


LIST_KEYS = ("skills", "agents", "mcps")


def apply_change(repo: dict | None, entry: dict) -> dict:
    """A local profile entry is either a whole profile (added in Rynhult) or, for a repository profile, a change:
    {"id", "patch": {field: value}, "add": {"skills": [...]}, "remove": {...}}. Storing only the change means
    later repository updates to that profile still arrive."""
    if repo is None or not any(k in entry for k in ("patch", "add", "remove")):
        return entry  # a profile added in Rynhult, or an older whole-profile override
    profile = {**repo, **entry.get("patch", {})}
    for key in LIST_KEYS:
        removed = set(entry.get("remove", {}).get(key, []))
        items = [x for x in repo.get(key, []) if x not in removed]
        profile[key] = items + [x for x in entry.get("add", {}).get(key, []) if x not in items]
    return profile


def change_from(repo: dict, profile: dict) -> dict | None:
    """The smallest change that turns the repository profile into this one (None: identical)."""
    patch = {k: v for k, v in profile.items() if k not in (*LIST_KEYS, "id") and repo.get(k) != v}
    patch.update({k: None for k in repo if k not in profile and k not in (*LIST_KEYS, "id")})
    patch = {k: v for k, v in patch.items() if v is not None}
    add = {k: [x for x in profile.get(k, []) if x not in repo.get(k, [])] for k in LIST_KEYS}
    remove = {k: [x for x in repo.get(k, []) if x not in profile.get(k, [])] for k in LIST_KEYS}
    add, remove = {k: v for k, v in add.items() if v}, {k: v for k, v in remove.items() if v}
    if not (patch or add or remove):
        return None
    return {"id": repo["id"], "patch": patch, "add": add, "remove": remove}


def read_local(local_root: Path | None) -> dict:
    path = (local_root / LOCAL_FILE) if local_root else None
    data = json.loads(path.read_text()) if path and path.is_file() else {}
    return {"skills": data.get("skills", []), "agents": data.get("agents", []), "mcps": data.get("mcps", []),
            "profiles": data.get("profiles", {})}


def _check_entry(kind: str, entry: dict, root: Path) -> Item:
    item_id = entry.get("id", "")
    if not ID.match(item_id):
        raise CatalogError(f"{kind} {item_id!r}: bad id")
    if kind == "skill":
        directory = root / "skills" / item_id
        if not (directory / "SKILL.md").is_file():
            raise CatalogError(f"skill {item_id!r}: missing {directory}/SKILL.md")
        return Item(item_id, "skill", entry, _read_tree(directory))
    if kind == "agent":
        path = root / "agents" / f"{item_id}.md"
        if not path.is_file():
            raise CatalogError(f"agent {item_id!r}: missing {path}")
        return Item(item_id, "agent", entry, {f"{item_id}.md": path.read_text()})
    if bool(entry.get("command")) == bool(entry.get("url")):
        raise CatalogError(f"mcp {item_id!r}: needs exactly one of command or url")
    return Item(item_id, "mcp", entry)


def restored_root(local_root: Path | None) -> Path | None:
    pointer = local_root / "restored/active.json" if local_root else None
    if pointer is None or not pointer.exists():
        return None
    name = json.loads(pointer.read_text())
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}", name):
        raise CatalogError("invalid restored catalog generation")
    return local_root / "restored" / name


def restored_layer(local_root: Path | None) -> tuple[dict, Path | None]:
    root = restored_root(local_root)
    return (json.loads((root / "items.json").read_text()), root) if root else ({}, None)


def load(root: Path, local_root: Path | None = None) -> Catalog:
    manifest = tomllib.loads((root / "manifest.toml").read_text())
    local = read_local(local_root)
    digest = hashlib.sha256()
    pools: dict[str, dict[str, Item]] = {"skill": {}, "agent": {}, "mcp": {}}
    restored, restored_root = restored_layer(local_root)
    for kind in pools:
        # Select layers before reading files, so restored copies repair missing installed files.
        entries = {e["id"]: (e, root) for e in manifest.get(kind, [])}
        entries.update({e["id"]: ({**e, "origin": "restored"}, restored_root) for e in restored.get(kind, [])})
        entries.update({e["id"]: ({**e, "origin": "local"}, local_root) for e in local[f"{kind}s"]})
        for item_id, (entry, content_root) in entries.items():
            item = _check_entry(kind, entry, content_root)
            item.content_root = content_root
            pools[kind][item_id] = item
    skills, agents, mcps = pools["skill"], pools["agent"], pools["mcp"]
    repo_profiles = {}
    for path in sorted((root / "profiles").glob("*.toml")):
        profile = tomllib.loads(path.read_text())
        if profile.get("id") != path.stem:
            raise CatalogError(f"{path}: id must match the file name")
        repo_profiles[path.stem] = profile
    profiles = dict(repo_profiles)
    for profile_id, profile in local["profiles"].items():
        if not ID.match(profile_id) or profile.get("id") != profile_id:
            raise CatalogError(f"local profile {profile_id!r}: bad id")
        profiles[profile_id] = apply_change(repo_profiles.get(profile_id), profile)
    # A retired (deferred) id still listed by an owner's local profile must not stop the catalog, and with it accd,
    # from loading after an upgrade: drop it from that profile and say so.
    retired = {entry["id"] for entry in manifest.get("deferred", [])}
    for profile_id, profile in profiles.items():
        for kind, pool in (("skills", skills), ("agents", agents), ("mcps", mcps)):
            stale = [x for x in profile.get(kind, []) if x in retired and x not in pool]
            if stale:
                log.warning("catalog: profile %s lists retired %s %s; ignoring them", profile_id, kind, stale)
                profiles[profile_id] = profile = {**profile, kind: [x for x in profile[kind] if x not in stale]}
            missing = [x for x in profile.get(kind, []) if x not in pool]
            if missing:
                raise CatalogError(f"profile {profile_id}: unknown {kind} {missing}")
    for item in [*skills.values(), *agents.values()]:
        for name, content in sorted(item.files.items()):
            digest.update(f"{item.kind}/{item.id}/{name}\0{content}\0".encode())
    digest.update((root / "manifest.toml").read_bytes())
    for path in sorted((root / "profiles").glob("*.toml")):
        digest.update(path.read_bytes())
    digest.update(json.dumps(local, sort_keys=True).encode())
    digest.update(json.dumps(restored, sort_keys=True).encode())
    return Catalog(root, skills, agents, mcps, profiles, manifest.get("deferred", []), digest.hexdigest()[:16],
                   manifest.get("codex", {}), local_root, repo_profiles)
