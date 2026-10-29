"""Key vault (docs/VAULT.md): API keys stored once, encrypted, named, and given to projects.

- Storage: AES-256-GCM with a random nonce per value; the key id and the variable are the associated data, so a
  value moved to another row (or renamed to another variable) fails to decrypt. The data key is
  data/secrets/vault.key (0600, user acc), never in the database.
- Who may do what: a key's owner does everything; others see and use it only when it is shared. Nobody reads a value
  back: values go only to authorized server-side issue-source calls, a session's 0600 session.env, or its MCP files.
- Delivery: at every launch and resume, the keys given to the session's project that the person launching may use.
  Two keys with one variable refuse the launch. Sessions on other machines only get keys whose owner allowed it.
- Redaction: what an agent produced is stored and shown with each value of the session's keys replaced by
  «key: NAME», including values a key had before it was replaced or deleted (kept sealed in vault_retired while any
  session lists the key).
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..env import interpreter_variable
from ..maintenance import lease
from .catalog import Catalog, Item, read_local
from .models import API_PRINCIPAL, AuditLog, Project, Session, User, VaultGrant, VaultKey, VaultRetired

log = logging.getLogger("acc.vault")

VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
# The shell's and Rynhult's own variables: a key may not take their place.
RESERVED = {"_", "PATH", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "OLDPWD", "TERM", "LANG", "LANGUAGE", "TZ",
            "TMPDIR", "IFS", "PS1", "PS2", "PS3", "PS4", "CDPATH", "HOSTNAME", "MAIL", "SHLVL",
            "COLUMNS", "LINES", "DISPLAY", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
# OPENCODE_: Rynhult's own launch sets OpenCode's config folder, inline config and MCP file (so the broker can't refuse
# the names); a key could add a config file of its own (security review 8).
RESERVED_PREFIXES = ("ACC_", "RYNHULT_", "LD_", "LC_", "BASH_FUNC_", "TMUX", "OPENCODE_")
REF = re.compile(r"^vault:([0-9a-f]{16})$")  # an MCP server's env value that names a vault key
MAX_VALUE = 16 << 10
MIN_REDACT = 8  # shorter values would match too much
PREFIX_MIN = 12  # a value cut short is still redacted from this many characters (and at least half of it) on
URLSAFE = str.maketrans("+/", "-_")
KEY_BYTES = 32


class VaultError(ValueError):
    pass


def new_id() -> str:
    return secrets.token_hex(8)


def ref(key_id: str) -> str:
    return f"vault:{key_id}"


def check_variable(variable: str) -> str:
    if not VARIABLE.match(variable or ""):
        raise VaultError("the variable is capital letters, digits and _ (at most 64), not starting with a digit")
    if variable in RESERVED or variable.startswith(RESERVED_PREFIXES) or interpreter_variable(variable):
        raise VaultError(f"{variable} is one of the shell's or Rynhult's own variables; choose another name")
    return variable


def check_value(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise VaultError("the value is empty")
    if len(value) > MAX_VALUE or "\0" in value:
        raise VaultError(f"the value must be text of at most {MAX_VALUE} characters")
    return value


# ---------------------------------------------------------------------- the data key and the cipher

def _aad(key_id: str, variable: str) -> bytes:
    return f"acc-vault\0{key_id}\0{variable}".encode()


class Cipher:
    """AES-256-GCM with vault.key. The file is read once; it is created (0600) only while the vault is empty, so a lost
    key never silently replaces one that existing values need."""

    def __init__(self, path: Path):
        self.path = path
        self._key: bytes | None = None

    def key(self, create: bool = False) -> bytes:
        if self._key is None:
            try:
                raw = self.path.read_text()
            except FileNotFoundError:
                if not create:
                    raise VaultError(f"the vault's key ({self.path}) is missing; restore it from the backup of "
                                     "Rynhult's secrets") from None
                raw = self._create()
            if create and not raw.strip() and not self.path.is_symlink():
                # left empty by a crash before keys were created atomically, and the vault is empty: nothing needs it
                self.path.unlink(missing_ok=True)
                raw = self._create()
            try:
                key = base64.b64decode(raw.strip(), validate=True)
            except ValueError:
                key = b""
            if len(key) != KEY_BYTES:
                raise VaultError(f"{self.path} is not a vault key (32 bytes, base64)")
            self._key = key
        return self._key

    def _create(self) -> str:
        with lease(self.path.parent.parent / '.maintenance.lock', exclusive=False):
            return self._create_locked()

    def _create_locked(self) -> str:
        """Written to a temporary file and linked into place: a crash never leaves an empty or half-written key (which
        would block the vault for good), and a key someone made a moment ago is used, never replaced."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = base64.b64encode(os.urandom(KEY_BYTES)).decode() + "\n"
        scratch = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}")
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(scratch, self.path)
            except FileExistsError:  # made a moment ago by someone else: use theirs
                return self.path.read_text()
        finally:
            scratch.unlink(missing_ok=True)
        log.warning("vault: created a new data key at %s; back it up with Rynhult's other secrets", self.path)
        return data

    def seal(self, key_id: str, variable: str, value: str, create: bool = False) -> str:
        nonce = os.urandom(12)
        sealed = AESGCM(self.key(create)).encrypt(nonce, value.encode(), _aad(key_id, variable))
        return base64.b64encode(nonce + sealed).decode()

    def open(self, key_id: str, variable: str, ciphertext: str) -> str:
        try:
            raw = base64.b64decode(ciphertext, validate=True)
            return AESGCM(self.key()).decrypt(raw[:12], raw[12:], _aad(key_id, variable)).decode()
        except (InvalidTag, ValueError) as error:
            raise VaultError(f"the value of key {key_id} can't be decrypted (it was changed or moved)") from error


# ---------------------------------------------------------------------- .env files

@dataclass
class EnvEntry:
    line: int
    variable: str
    value: str = field(repr=False)
    problem: str | None = None


ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s?(.*)$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}


def parse_env(text: str) -> list[EnvEntry]:
    """A pasted .env: KEY=value lines, `export` allowed, # comments, single quotes literal, double quotes with
    escapes (and over several lines), unquoted values up to a ` #` comment. Lines that aren't assignments are skipped."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    entries: list[EnvEntry] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        number, line = i + 1, lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            continue
        variable, rest = match.group(1), match.group(2)
        if rest[:1] in ('"', "'"):
            quote, body = rest[0], rest[1:]
            value, closed = [], False
            while True:
                j = 0
                while j < len(body):
                    ch = body[j]
                    if quote == '"' and ch == "\\" and j + 1 < len(body):
                        value.append(ESCAPES.get(body[j + 1], "\\" + body[j + 1]))
                        j += 2
                        continue
                    if ch == quote:
                        closed = True
                        break
                    value.append(ch)
                    j += 1
                if closed or i >= len(lines):
                    break
                value.append("\n")
                body = lines[i]
                i += 1
            value = "".join(value)
            problem = None if closed else "the closing quote is missing"
        else:
            value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
            problem = None
        if problem is None:
            try:
                check_variable(variable)
            except VaultError as error:
                problem = str(error)
        if problem is None and not value.strip():
            problem = "the value is empty"
        if problem is None and variable in seen:
            problem = "this variable appears earlier too; only the first is added"
        if problem is None and len(value) > MAX_VALUE:
            problem = "the value is too long"
        seen.add(variable)
        entries.append(EnvEntry(number, variable, value, problem))
    return entries


# ---------------------------------------------------------------------- what a launch gets

@dataclass
class Plan:
    """The keys a launch gets, without values (also the New session preview)."""
    keys: list[VaultKey]  # delivered
    held_back: list[VaultKey]  # given to the project, but not sent to another machine
    mcp_refs: dict[str, dict[str, VaultKey]] = field(default_factory=dict)  # mcp id -> {env name: key}

    def public(self) -> dict:
        def row(k: VaultKey) -> dict:
            return {"id": k.id, "name": k.name, "variable": k.variable}
        return {"keys": [row(k) for k in self.keys], "held_back": [row(k) for k in self.held_back],
                "mcp": {m: {env: row(k) for env, k in refs.items()} for m, refs in self.mcp_refs.items()}}

    @property
    def ids(self) -> list[str]:
        return sorted({k.id for k in self.keys} | {k.id for refs in self.mcp_refs.values() for k in refs.values()})


@dataclass
class Delivery:
    env: dict[str, str] = field(repr=False)  # the project keys, by variable
    catalog: Catalog | None  # the catalog with the chosen MCP servers' vault references filled in
    plan: Plan

    def check_env(self, other: dict[str, str]) -> None:
        """Rynhult's own session environment (MCP secrets for Codex, endpoint settings) must not disagree with a key."""
        by_variable = {k.variable: k for k in self.plan.keys}
        for variable, value in other.items():
            if variable in self.env and self.env[variable] != value:
                raise VaultError(f"the key {by_variable[variable].name} sets {variable}, which this launch already sets "
                                 "(an MCP server or the model endpoint); remove the key from the project or rename its "
                                 "variable")


def may_use(key: VaultKey, username: str | None) -> bool:
    return key.owner == username or key.shared


def plan(db: DbSession, project_id: int | None, username: str | None, remote: bool,
         catalog: Catalog | None = None, mcps: tuple[str, ...] = ()) -> Plan:
    """Which keys a launch in this project by this person gets. VaultError when two use one variable, or when a
    chosen MCP server needs a key this launch can't have."""
    rows = db.scalars(select(VaultKey).join(VaultGrant, VaultGrant.key_id == VaultKey.id)
                      .where(VaultGrant.project_id == project_id).order_by(VaultKey.name, VaultKey.id)).all() \
        if project_id is not None else []
    project = db.get(Project, project_id) if project_id is not None else None
    server_keys = {c.get("key_id") for c in (project.issue_sources or {}).values()} if project else set()
    server_keys.update(db.scalars(select(VaultGrant.key_id).where(
        VaultGrant.project_id == project_id, VaultGrant.server_only.is_(True))))
    allowed_group = None
    if project and project.group_id:
        from .models import Group, GroupKeyGrant, GroupMembership
        user = db.scalar(select(User).where(User.username == username))
        membership = db.get(GroupMembership, (project.group_id, user.id)) if user else None
        group = db.get(Group, project.group_id)
        if not membership or membership.role == 'viewer' or not group or group.archived or project.archived:
            raise VaultError('group execution authority is unavailable')
        grants = db.scalars(select(GroupKeyGrant).where(GroupKeyGrant.group_id == project.group_id,
            (GroupKeyGrant.project_id.is_(None)) | (GroupKeyGrant.project_id == project.id),
            (GroupKeyGrant.host.is_(None)) | (GroupKeyGrant.host == 'local'))).all()
        # First own-machine slice never exports somebody else's group-consented key.
        allowed_group = set() if remote else {g.key_id for g in grants if (key := db.get(VaultKey, g.key_id)) and key.owner == g.given_by}
    def eligible(key):
        return (key.owner == username or key.id in allowed_group) if allowed_group is not None else may_use(key, username)
    usable = [k for k in rows if eligible(k) and k.id not in server_keys]
    keys = [k for k in usable if k.remote or not remote]
    held_back = [k for k in usable if remote and not k.remote]
    by_variable: dict[str, VaultKey] = {}
    for key in keys:
        check_variable(key.variable)  # also protect rows created before a namespace became reserved
        if key.variable in by_variable:
            raise VaultError(f"two keys given to this project use {key.variable}: {by_variable[key.variable].name} and "
                             f"{key.name}. Remove one from the project (or change its variable) to launch")
        by_variable[key.variable] = key
    refs: dict[str, dict[str, VaultKey]] = {}
    for mcp_id in mcps if catalog else ():
        item = catalog.mcps.get(mcp_id)
        for env_name, value in ((item.meta.get("env") or {}) if item else {}).items():
            match = REF.match(str(value))
            if not match:
                continue
            key = db.get(VaultKey, match.group(1))
            if key is None:
                raise VaultError(f"MCP server {mcp_id}: the key for {env_name} was deleted; add the server again with "
                                 "a key")
            if not eligible(key):
                raise VaultError(f"MCP server {mcp_id} uses a key its owner keeps private")  # not named: it's theirs
            if key.id in server_keys:
                raise VaultError(f"MCP server {mcp_id} uses an issue-source key reserved for the server; select a separate key")
            if remote and not key.remote:
                raise VaultError(f"MCP server {mcp_id} uses the key {key.name}, which isn't allowed on other "
                                 "machines; its owner can turn on Other machines in Settings → Keys")
            refs.setdefault(mcp_id, {})[env_name] = key
    return Plan(keys, held_back, refs)


def context(plan_: Plan) -> str:
    """The paragraph a session's instructions get: variables and names, never values."""
    if not plan_.keys and not plan_.held_back:
        return ""
    lines = ["# API keys from Rynhult", "",
             "Rynhult put these keys in this session's environment. Use them through their variables (for example "
             f"`\"${plan_.keys[0].variable if plan_.keys else plan_.held_back[0].variable}\"` in a command); never "
             "print, log, copy or commit their values."]
    lines += [f"- `{k.variable}`: {k.name}" for k in plan_.keys]
    if plan_.held_back:
        lines += ["", "Not on this machine (their owners keep them on Rynhult's main machine):"]
        lines += [f"- `{k.variable}`: {k.name}" for k in plan_.held_back]
    return "\n".join(lines)


def with_mcp_values(catalog: Catalog | None, values: dict[str, dict[str, str]]) -> Catalog | None:
    if catalog is None or not values:
        return catalog
    mcps = dict(catalog.mcps)
    for mcp_id, env in values.items():
        item = mcps[mcp_id]
        mcps[mcp_id] = Item(item.id, item.kind, {**item.meta, "env": {**(item.meta.get("env") or {}), **env}}, item.files)
    return dataclasses.replace(catalog, mcps=mcps)


# ---------------------------------------------------------------------- redaction

def encoded_forms(value: str) -> set[str]:
    """How a value also shows up in what agents produce (review D-low): percent-encoded (URLs, form bodies), and base64
    in both alphabets at each of the three byte alignments, so it is found inside a longer encoded string such as an
    HTTP Basic credential (`user:value`). For base64 only the characters that depend on the value alone are matched."""
    forms = {quote(value, safe=""), quote_plus(value, safe="")}
    raw = value.encode()
    for shift in range(3):
        encoded = base64.b64encode(b"\0" * shift + raw).decode()
        core = encoded[-(-8 * shift // 6):(8 * (shift + len(raw))) // 6]
        if len(core) >= MIN_REDACT:
            forms |= {core, core.translate(URLSAFE)}
    forms.discard(value)
    return forms


class Redactor:
    """Replaces each value (at least MIN_REDACT characters) with «key: NAME», longest first: as it is, in its encoded
    forms (encoded_forms), and cut short (a prefix of at least PREFIX_MIN characters and half the value, as a CLI or a
    log truncating it leaves it)."""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = sorted({(v, n) for v, n in pairs if len(v) >= MIN_REDACT}, key=lambda p: -len(p[0]))
        forms = {(v, n) for v, n in self.pairs} | {(f, n) for v, n in self.pairs for f in encoded_forms(v)}
        self.forms = sorted(forms, key=lambda p: -len(p[0]))

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def text(self, value: str, json_escaped: bool = False) -> str:
        """`json_escaped`: `value` is JSON text (a spool line), so the placeholder is escaped as a JSON string's
        contents and the line stays valid."""
        def mark(name: str) -> str:
            placeholder = f"«key: {name}»"
            return json.dumps(placeholder, ensure_ascii=False)[1:-1] if json_escaped else placeholder
        for secret, name in self.forms:
            if secret in value:
                value = value.replace(secret, mark(name))
        for secret, name in self.pairs:  # what is left of a value cut short
            size = max(PREFIX_MIN, (len(secret) + 1) // 2)
            if len(secret) <= size:
                continue
            start = value.find(secret[:size])
            while start != -1:
                end = start + size
                while end - start < len(secret) and end < len(value) and value[end] == secret[end - start]:
                    end += 1
                value = value[:start] + mark(name) + value[end:]
                start = value.find(secret[:size], start + 1)
        return value

    def deep(self, value):
        if not self.pairs:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {(self.text(k) if isinstance(k, str) else k): self.deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        return value


# ---------------------------------------------------------------------- the service accd holds

class Vault:
    def __init__(self, key_path: Path):
        self.cipher = Cipher(key_path)
        self._values: dict[str, tuple[str, str, str]] = {}  # key id -> (ciphertext, name, value): redaction's cache
        self._retired: dict[int, str] = {}  # retired value id -> value (they never change)

    def seal(self, db: DbSession, key_id: str, variable: str, value: str) -> str:
        from .models import InfrastructureCredential
        empty = (db.scalar(select(func.count()).select_from(VaultKey)) == 0 and
                 db.scalar(select(func.count()).select_from(InfrastructureCredential)) == 0)
        return self.cipher.seal(key_id, variable, value, create=empty)

    def value(self, key: VaultKey) -> str:
        cached = self._values.get(key.id)
        if cached and cached[0] == key.ciphertext:
            return cached[2]
        value = self.cipher.open(key.id, key.variable, key.ciphertext)
        self._values[key.id] = (key.ciphertext, key.name, value)
        return value

    def deliver(self, db: DbSession, project_id: int | None, username: str | None, remote: bool,
                catalog: Catalog | None = None, mcps: tuple[str, ...] = ()) -> Delivery:
        planned = plan(db, project_id, username, remote, catalog, mcps)
        env = {k.variable: self.value(k) for k in planned.keys}
        mcp_values = {m: {env_name: self.value(k) for env_name, k in refs.items()} for m, refs in planned.mcp_refs.items()}
        return Delivery(env, with_mcp_values(catalog, mcp_values), planned)

    def redactor(self, db: DbSession, key_ids) -> Redactor:
        """For a session: the values of the keys it was given, now and before they were replaced or deleted (D22:
        the old value may still be in its process, its transcript and anything indexed from it again)."""
        if not key_ids:
            return Redactor([])
        pairs = []
        for key in db.scalars(select(VaultKey).where(VaultKey.id.in_(list(key_ids)))):
            try:
                pairs.append((self.value(key), key.name))
            except VaultError as error:
                log.warning("vault: %s", error)
        for old in db.scalars(select(VaultRetired).where(VaultRetired.key_id.in_(list(key_ids)))):
            try:
                pairs.append((self.retired_value(old), old.name))
            except VaultError as error:
                log.warning("vault: retired value of %s: %s", old.key_id, error)
        return Redactor(pairs)

    def retired_value(self, old: VaultRetired) -> str:
        cached = self._retired.get(old.id)
        if cached is None:
            cached = self._retired[old.id] = self.cipher.open(old.key_id, old.variable, old.ciphertext)
        return cached

    def retire(self, db: DbSession, key: VaultKey) -> None:
        """Before a key's value is replaced or the key deleted: keep the value for redaction if any session was given
        it. Retired values of keys no session lists any more are dropped. The caller commits."""
        holders = {k for keys in db.scalars(select(Session.vault_keys).where(Session.vault_keys.is_not(None)))
                   for k in (keys or [])}
        if key.id in holders:
            db.add(VaultRetired(key_id=key.id, name=key.name, variable=key.variable, ciphertext=key.ciphertext))
        for old in db.scalars(select(VaultRetired).where(VaultRetired.key_id.not_in(holders))):
            db.delete(old)
            self._retired.pop(old.id, None)

    def forget(self, key_id: str) -> None:
        self._values.pop(key_id, None)


# ---------------------------------------------------------------------- adding keys, and the catalog's MCP secrets

def create_key(db: DbSession, store: Vault, *, name: str, variable: str, value: str, owner: str, shared: bool = False,
               remote: bool = False, note: str | None = None) -> VaultKey:
    """A new key: validated, encrypted and added to `db` (the caller audits and commits)."""
    variable, value = check_variable(variable), check_value(value)
    key_id = new_id()
    key = VaultKey(id=key_id, name=name.strip()[:80] or variable, variable=variable, owner=owner, shared=shared,
                   remote=remote, note=(note or "").strip() or None, ciphertext=store.seal(db, key_id, variable, value))
    db.add(key)
    db.flush()
    return key


def mcp_variable(env_name: str) -> str:
    """A vault variable for an MCP server's env name (MCP names may be lower case or one of the shell's own)."""
    variable = re.sub(r"[^A-Z0-9_]", "_", env_name.upper())[:64] or "MCP_SECRET"
    try:
        return check_variable(variable)
    except VaultError:
        return check_variable(("MCP_" + variable.lstrip("_"))[:64] or "MCP_SECRET")


def mcp_key(db: DbSession, store: Vault, mcp_id: str, mcp_name: str, env_name: str, value: str, owner: str,
            source: str) -> VaultKey:
    """An MCP server's secret as a vault key: shared, since everyone who launches with the server uses it."""
    key = create_key(db, store, name=f"{mcp_name or mcp_id}: {env_name}", variable=mcp_variable(env_name), value=value,
                     owner=owner, shared=True, note=f"The {env_name} of the MCP server {mcp_id} in the catalog")
    db.add(AuditLog(actor=owner, action="vault.add", target=key.id,
                    detail={"name": key.name, "variable": key.variable, "shared": True, "remote": False,
                            "source": source, "mcp": mcp_id}))
    return key


def migrate_catalog(db: DbSession, store: Vault, catalog_store) -> int:
    """The one-time move of the local catalog's MCP secrets into the vault, each owned by the person who added the
    server (an owner when that isn't known). The catalog then holds `vault:<id>` references. Returns how many moved."""
    data = read_local(catalog_store.local_root)
    users = set(db.scalars(select(User.username)))
    fallback = db.scalar(select(User.username).where(User.role == "owner").order_by(User.id)) or API_PRINCIPAL
    moved = 0
    for entry in data.get("mcps", []):
        env = entry.get("env") or {}
        added_by = entry.get("added_by")
        if added_by == "api" and "api" not in users:  # the token's name before 0023
            added_by = API_PRINCIPAL
        owner = added_by if added_by in users | {API_PRINCIPAL} else fallback
        for env_name, value in list(env.items()):
            if REF.match(str(value)) or not str(value).strip():
                continue
            key = mcp_key(db, store, entry["id"], entry.get("name") or entry["id"], env_name, str(value), owner,
                          "migrated from the catalog")
            env[env_name] = ref(key.id)
            moved += 1
    if moved:
        db.commit()  # keys first: a failure below leaves extra keys, never references to keys that don't exist
        catalog_store._write_local(data)
    return moved
