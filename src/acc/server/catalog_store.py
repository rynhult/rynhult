"""Writes the local catalog overlay (/srv/acc/catalog-local) for items and profiles added from the dashboard.

Layout: local.json (item metadata + profiles; acc 0600 because MCP env values may be secrets) plus
skills/<id>/ and agents/<id>.md (0755/0644, so the broker, running as the agent user, can link skills
for Codex). Every change is validated by reloading the whole catalog; a change that breaks it is undone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path, PurePosixPath

from .. import providers as registry
from ..maintenance import lease
from .catalog import ID, LOCAL_FILE, Catalog, CatalogError, apply_change, change_from, load, read_local

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PROVIDERS = set(registry.ids())
PROFILE_KEYS = {"id", "name", "description", "skills", "agents", "mcps", "providers", "instructions", "standalone", *PROVIDERS}


class StoreError(ValueError):
    pass


@contextmanager
def catalog_lock(local_root: Path, *, timeout=30):
    """Serialize catalog restore and dashboard edits across worker threads/processes."""
    local_root.mkdir(parents=True, exist_ok=True)
    # Agents can read catalog items, but must never be able to flock our barrier.
    # Tighten old 0644 files before waiting; old open descriptors are bounded by timeout.
    with lease(local_root / '.restore.lock', timeout=timeout, private=True):
        yield


def serialized(method):
    @wraps(method)
    def write(self, *args, **kwargs):
        with catalog_lock(self.local_root):
            return method(self, *args, **kwargs)
    return write


class CatalogStore:
    def __init__(self, repo_root: Path, local_root: Path):
        self.repo_root = repo_root
        self.local_root = local_root

    # ------------------------------------------------------------ plumbing

    def _write_local(self, data: dict) -> None:
        self.local_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.local_root, 0o755)
        tmp = self.local_root / f".{LOCAL_FILE}.tmp"
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.local_root / LOCAL_FILE)

    def _normalize(self, data: dict) -> dict:
        """Store repository profiles as changes only (see catalog.apply_change); drop changes that undo themselves."""
        repo = self._catalog().repo_profiles
        for profile_id, entry in list(data["profiles"].items()):
            if profile_id in repo:
                change = change_from(repo[profile_id], apply_change(repo[profile_id], entry))
                if change is None:
                    del data["profiles"][profile_id]
                else:
                    data["profiles"][profile_id] = change
        return data

    def _commit(self, data: dict, undo=None) -> Catalog:
        """Save data, then prove the catalog still loads; otherwise restore the previous state."""
        previous = read_local(self.local_root)
        self._write_local(self._normalize(data))
        try:
            return load(self.repo_root, self.local_root)
        except CatalogError as error:
            self._write_local(previous)
            if undo:
                undo()
            raise StoreError(str(error))

    def _catalog(self) -> Catalog:
        return load(self.repo_root, self.local_root)

    def _check_profiles(self, profile_ids: list[str]) -> None:
        unknown = set(profile_ids) - set(self._catalog().profiles)
        if unknown:
            raise StoreError(f"unknown profiles {sorted(unknown)}")

    def _check_new_id(self, kind: str, item_id: str) -> None:
        if not ID.match(item_id or ""):
            raise StoreError("id must be lowercase letters, digits and dashes (max 63)")
        if item_id in self._catalog().pool(kind):
            raise StoreError(f"a {kind} named {item_id!r} already exists")

    @staticmethod
    def _providers(values: list[str], kind: str) -> list[str]:
        chosen = sorted(set(values or []))
        if not chosen or not set(chosen) <= PROVIDERS:
            raise StoreError(f"choose at least one of: {', '.join(sorted(PROVIDERS))}")
        with_agents = {p.id for p in registry.PROVIDERS.values() if "agents" in p.supports}
        if kind == "agent" and not set(chosen) <= with_agents:
            names = ", ".join(registry.get(p).label for p in sorted(with_agents))
            raise StoreError(f"agents are subagents, so {names} only")
        return chosen

    @staticmethod
    def _meta(body: dict, kind: str, item_id: str) -> dict:
        return {"id": item_id, "name": (body.get("name") or item_id)[:80], "source": (body.get("source") or "local")[:500],
                "license": (body.get("license") or "unknown")[:60], "notes": (body.get("notes") or "")[:1000],
                "providers": CatalogStore._providers(body.get("providers") or ["claude", "codex"], kind),
                "added_by": body.get("actor", "?"), "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def _assign(self, data: dict, catalog: Catalog, kind: str, item_id: str, profile_ids: list[str]) -> None:
        """Set exactly which profiles include this item (writes profile overrides as needed)."""
        key = f"{kind}s"
        for profile_id, profile in catalog.profiles.items():
            members = list(profile.get(key, []))
            want = profile_id in profile_ids
            if want == (item_id in members):
                continue
            members = [*members, item_id] if want else [m for m in members if m != item_id]
            data["profiles"][profile_id] = {**profile, key: members}  # whole profile; _commit stores only the change
        unknown = set(profile_ids) - set(catalog.profiles)
        if unknown:
            raise StoreError(f"unknown profiles {sorted(unknown)}")

    # ------------------------------------------------------------ items

    @serialized
    def add_skill(self, item_id: str, files: dict[str, str], body: dict, profiles: list[str]) -> Catalog:
        self._check_new_id("skill", item_id)
        if "SKILL.md" not in files:
            raise StoreError("a skill needs a SKILL.md at its top level")
        for name in files:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or len(path.parts) > 6 or not path.parts:
                raise StoreError(f"bad file path {name!r}")
        self._check_profiles(profiles)
        directory = self.local_root / "skills" / item_id
        if directory.exists():
            shutil.rmtree(directory)  # leftover from an earlier failed add; never a loaded item (checked above)
        for name, content in files.items():
            target = directory / PurePosixPath(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            os.chmod(target, 0o644)
        for folder in [directory, *[p for p in directory.rglob("*") if p.is_dir()], directory.parent]:
            os.chmod(folder, 0o755)
        return self._add("skill", item_id, body, profiles, undo=lambda: shutil.rmtree(directory, ignore_errors=True))

    @serialized
    def add_agent(self, item_id: str, content: str, body: dict, profiles: list[str]) -> Catalog:
        self._check_new_id("agent", item_id)
        self._check_profiles(profiles)
        path = self.local_root / "agents" / f"{item_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)
        path.write_text(content)
        os.chmod(path, 0o644)
        return self._add("agent", item_id, {**body, "providers": ["claude"]}, profiles, undo=lambda: path.unlink(missing_ok=True))

    @serialized
    def add_mcp(self, item_id: str, body: dict, profiles: list[str]) -> Catalog:
        self._check_new_id("mcp", item_id)
        self._check_profiles(profiles)
        extra = self.mcp_fields(body)
        return self._add("mcp", item_id, {**body, **extra}, profiles, extra=extra)

    @staticmethod
    def mcp_fields(body: dict) -> dict:
        """Shared validation for manual MCP entries and restored release entries."""
        command, url = (body.get("command") or "").strip(), (body.get("url") or "").strip()
        if bool(command) == bool(url):
            raise StoreError("give either a command (stdio server) or a URL (HTTP server)")
        extra: dict = {}
        if command:
            if re.search(r"\s", command):
                raise StoreError("command is one executable; put its arguments in args")
            args = [str(a) for a in body.get("args") or []]
            if len(args) > 40 or any(len(a) > 500 for a in args):
                raise StoreError("too many or too long args")
            env = {str(k): str(v) for k, v in (body.get("env") or {}).items()}
            bad = [k for k in env if not ENV_KEY.match(k)]
            if bad:
                raise StoreError(f"bad environment variable names {bad}")
            extra = {"command": command, "args": args, "env": env}
        else:
            if not re.match(r"^https?://[^\s]+$", url):
                raise StoreError("URL must start with http:// or https://")
            extra = {"url": url}
        return extra

    def _add(self, kind: str, item_id: str, body: dict, profiles: list[str], undo=None, extra: dict | None = None) -> Catalog:
        data = read_local(self.local_root)
        data[f"{kind}s"].append({**self._meta(body, kind, item_id), **(extra or {})})
        staged = self._commit(data, undo)  # item exists now; assign in a second step so profiles can reference it
        if profiles:
            data = read_local(self.local_root)
            self._assign(data, staged, kind, item_id, profiles)
            return self._commit(data)
        return staged

    @serialized
    def remove(self, kind: str, item_id: str) -> Catalog:
        catalog = self._catalog()
        item = catalog.pool(kind).get(item_id)
        if item is None:
            raise StoreError(f"no {kind} {item_id!r}")
        if item.meta.get("origin") != "local":
            raise StoreError("items from the repository are removed through a PR; you can unassign them from profiles")
        data = read_local(self.local_root)
        data[f"{kind}s"] = [e for e in data[f"{kind}s"] if e["id"] != item_id]
        key = f"{kind}s"
        for profile_id, profile in catalog.profiles.items():  # merged profiles; _commit stores only the change
            if item_id in profile.get(key, []):
                data["profiles"][profile_id] = {**profile, key: [x for x in profile[key] if x != item_id]}
        result = self._commit(data)
        if kind == "skill":
            shutil.rmtree(self.local_root / "skills" / item_id, ignore_errors=True)
        elif kind == "agent":
            (self.local_root / "agents" / f"{item_id}.md").unlink(missing_ok=True)
        return result

    @serialized
    def assign(self, kind: str, item_id: str, profiles: list[str]) -> Catalog:
        catalog = self._catalog()
        if item_id not in catalog.pool(kind):
            raise StoreError(f"no {kind} {item_id!r}")
        data = read_local(self.local_root)
        self._assign(data, catalog, kind, item_id, profiles)
        return self._commit(data)

    # ------------------------------------------------------------ profiles

    @serialized
    def save_profile(self, profile_id: str, body: dict, create: bool) -> Catalog:
        if not ID.match(profile_id or ""):
            raise StoreError("id must be lowercase letters, digits and dashes (max 63)")
        catalog = self._catalog()
        if create and profile_id in catalog.profiles:
            raise StoreError(f"profile {profile_id!r} already exists")
        if not create and profile_id not in catalog.profiles:
            raise StoreError(f"no profile {profile_id!r}")
        profile = {k: v for k, v in body.items() if k in PROFILE_KEYS and v is not None}
        profile["id"] = profile_id
        if not (profile.get("name") or "").strip():
            raise StoreError("a profile needs a name")
        for key in ("skills", "agents", "mcps"):
            profile[key] = list(dict.fromkeys(profile.get(key) or []))
        if profile.get("providers"):
            profile["providers"] = self._providers(profile["providers"], "profile")
        else:
            profile.pop("providers", None)
        defaults = {}  # each provider's launch defaults the profile sets (others use the provider's own defaults)
        for provider in registry.PROVIDERS.values():
            given = profile.get(provider.id)
            if not isinstance(given, dict):
                if provider.id not in ("claude", "codex"):  # profiles have always carried these two
                    profile.pop(provider.id, None)
                    continue
                given = {}
            section = {}
            for option in provider.options:
                if option.kind != "select":
                    continue
                value = given.get(option.key, option.default)
                if value not in {v for v, _ in option.choices}:
                    raise StoreError(f"unknown {option.label.lower()} {value!r} for {provider.label}")
                section[option.key] = value
            if section:
                defaults[provider.id] = section
        if profile.get("standalone"):
            profile["standalone"] = True
        else:
            profile.pop("standalone", None)  # absent = project sessions only (no spurious change on repo profiles)
        profile.update(defaults)
        data = read_local(self.local_root)
        data["profiles"][profile_id] = profile
        return self._commit(data)

    @serialized
    def drop_profile(self, profile_id: str) -> Catalog:
        """Local profile: delete it. Repo profile: drop the local override (back to the repository version)."""
        data = read_local(self.local_root)
        if profile_id not in data["profiles"]:
            raise StoreError("only local profiles and local changes to repo profiles can be removed")
        del data["profiles"][profile_id]
        return self._commit(data)
