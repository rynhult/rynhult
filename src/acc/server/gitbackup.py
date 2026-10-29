"""Git backup to the main Rynhult (M15, docs/MULTIHOST.md "Git backup").

One bare repository per project at <git_dir>/<project id>.git, owned by acc. Brokers push to it over git's smart HTTP
(`/api/git/<id>.git/`), which accd serves by running `git http-backend`: receive-pack only, and only with a backup
token that names that one project. accd mints a token for each push it asks a broker to make and forgets it when the
push is done (or after 10 minutes); tokens exist only in accd's memory and the pushing git's environment.

The repositories refuse non-fast-forward updates and deletions, so nothing already backed up can be rewritten or
removed through a backup push. git's own settings cover branches only, so each repository also has an `update` hook,
written by accd and put back every time it touches the repository (and in every repository when it starts): it
refuses deleting any ref, replacing an existing tag, a branch update that isn't a fast-forward, and any ref outside
refs/heads/ and refs/tags/. It judges each ref on its own, so one refused ref doesn't refuse the rest of the push;
the broker pushes a refused one again as backup/<time>/<name> (acc.broker.ops.git_backup).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..maintenance import async_lease, git_lock, serialized_git
from .supervisor_common import log

CLOCK_SLACK = 120.0  # seconds a legacy repository may seem older than its project and still be its own
TOKEN_TTL = 600.0  # seconds a backup token is good for (one push)
REPO_NAME = re.compile(r"^([0-9]{1,12})\.git$")
MAX_RESPONSE = 64 << 20  # what http-backend may answer (ref advertisement, receive-pack status)
BACKEND_SLOTS = 4  # http-backends running at once
REPO_CONFIG = (("receive.denyNonFastForwards", "true"), ("receive.denyDeletes", "true"),
               ("receive.fsckObjects", "true"), ("http.receivepack", "true"), ("http.uploadpack", "false"),
               ("http.getanyfile", "false"), ("gc.auto", "0"), ("receive.autogc", "false"), ("core.logAllRefUpdates", "always"))
HOOK = "update"  # per ref (pre-receive would refuse a whole push for one bad ref)
HOOK_SCRIPT = """#!/bin/sh
# Written by Rynhult (acc.server.gitbackup), which puts it back if it changes: a backup only ever gains history.
ref=$1 old=$2 new=$3
refuse() { echo "acc backup: $ref: $1" >&2; exit 1; }
case "$new" in *[!0]*) ;; *) refuse "deleting is refused" ;; esac
case "$ref" in
  refs/heads/*) ;;
  refs/tags/*) case "$old" in *[!0]*) refuse "an existing tag can't be replaced" ;; esac ;;
  *) refuse "only branches and tags are backed up" ;;
esac
case "$old" in
  *[!0]*) git merge-base --is-ancestor "$old" "$new" 2>/dev/null || refuse "only fast-forwards are taken" ;;
esac
exit 0
"""


class BackupTokens:
    """Short-lived push tokens, in memory only (a restart forgets them all). Kept as hashes."""

    def __init__(self, ttl: float = TOKEN_TTL):
        self.ttl = ttl
        self._tokens: dict[str, tuple[int, float]] = {}  # sha256(token) -> (project id, expires at, monotonic)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def mint(self, project_id: int, *, until: float | None = None) -> str:
        self._expire()
        token = secrets.token_urlsafe(32)
        expires = time.monotonic() + self.ttl
        self._tokens[self._key(token)] = (project_id, min(expires, until) if until is not None else expires)
        return token

    def check(self, token: str, project_id: int) -> bool:
        entry = self._tokens.get(self._key(token or ""))
        return entry is not None and entry[0] == project_id and time.monotonic() < entry[1]

    def revoke(self, token: str) -> None:
        self._tokens.pop(self._key(token), None)

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [k for k, (_, until) in self._tokens.items() if until <= now]:
            del self._tokens[key]


def repo_path(git_dir: Path, project_id: int) -> Path:
    return git_dir / f"{int(project_id)}.git"


def _git(*args: str, timeout: float = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)


def identity(project) -> str:
    """What a backup repository records about the project it belongs to: ids are reused after an older database is
    restored, so the id alone could hand one project's backup to another (slug@creation time, UTC, to the second)."""
    if project.backup_identity:
        return project.backup_identity
    created = project.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return f"{project.slug}@{created.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def identity_time(project) -> float:
    created = project.created_at
    return (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).timestamp()


def recorded_identity(path: Path) -> str | None:
    found = _git("--git-dir", str(path), "config", "--get", "acc.project").stdout.strip()
    return found or None


def belongs(git_dir: Path, project_id: int, owner: str | None) -> bool:
    """Whether the repository at the project's id is that project's (one made before identities existed is, until
    ensure_repo looks at it)."""
    path = repo_path(git_dir, project_id)
    if owner is None or not (path / "HEAD").is_file():
        return True
    return recorded_identity(path) in (None, owner)


@serialized_git
def ensure_repo(git_dir: Path, project_id: int, owner: str | None = None, created: float | None = None) -> Path:
    """The project's bare backup repository, created on first use. Its settings and hook are (re)applied every time.

    `owner` is the project's identity(): a repository recording another one (an older database restored next to
    newer backups) is moved aside to <git_dir>/orphaned/ and a fresh one made. A repository from before identities
    were recorded is claimed when it's younger than the project (`created`, a timestamp), else moved aside too."""
    git_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = repo_path(git_dir, project_id)
    if path.is_symlink():
        raise RuntimeError(f"{path} is a symlink")
    if owner is not None and (path / "HEAD").is_file():
        recorded = recorded_identity(path)
        made = os.stat(path / "description").st_mtime if (path / "description").is_file() else None
        stranger = recorded != owner if recorded else (created is not None and made is not None
                                                       and made < created - CLOCK_SLACK)
        if stranger:
            aside = git_dir / "orphaned" / f"{project_id}-{(recorded or 'unknown').split('@')[0]}-" \
                                           f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.git"
            aside.parent.mkdir(mode=0o700, exist_ok=True)
            os.rename(path, aside)
            log.warning("git backup: %s belongs to %s, not %s; moved aside to %s", path, recorded or "an older project",
                        owner, aside)
    if not (path / "HEAD").is_file():
        result = _git("init", "-q", "--bare", str(path))
        if result.returncode:
            raise RuntimeError(f"git init failed: {result.stderr.strip()[-200:]}")
        os.chmod(path, 0o700)
    for key, value in REPO_CONFIG:
        _git("--git-dir", str(path), "config", key, value)
    if owner is not None:
        _git("--git-dir", str(path), "config", "acc.project", owner)
    install_hook(path)
    return path


def install_hook(path: Path) -> None:
    """Write the repository's update hook unless it is already exactly ours (replacing whatever is there, link or not).
    The other sample hooks git init leaves are removed: only ours runs."""
    hooks = path / "hooks"
    if hooks.is_symlink():
        hooks.unlink()
    hooks.mkdir(mode=0o700, exist_ok=True)
    for entry in hooks.iterdir():
        if entry.name != HOOK and (entry.is_symlink() or entry.is_file()):
            entry.unlink()
    hook = hooks / HOOK
    if not hook.is_symlink() and hook.is_file() and hook.read_text(errors="replace") == HOOK_SCRIPT \
            and os.stat(hook).st_mode & 0o777 == 0o700:
        return
    scratch = hooks / f".{HOOK}.{secrets.token_hex(4)}"
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w") as out:
        out.write(HOOK_SCRIPT)
    os.replace(scratch, hook)


def install_hooks(git_dir: Path) -> int:
    """Every existing backup repository gets the current hook and settings (accd start). Returns how many."""
    if not git_dir.is_dir():
        return 0
    count = 0
    for path in sorted(git_dir.iterdir()):
        if REPO_NAME.match(path.name) and path.is_dir() and not path.is_symlink() and (path / "HEAD").is_file():
            try:
                ensure_repo(git_dir, int(REPO_NAME.match(path.name).group(1)))
                count += 1
            except (OSError, RuntimeError) as error:
                log.warning("git backup: could not protect %s: %s", path, error)
    return count


@serialized_git
def set_head(git_dir: Path, project_id: int, ref: str | None) -> None:
    """Point the backup's HEAD at the project's checked-out branch (so a clone or bundle of it checks that out)."""
    path = repo_path(git_dir, project_id)
    if ref and ref.startswith("refs/heads/") and not _git("--git-dir", str(path), "rev-parse", "-q", "--verify",
                                                          ref).returncode:
        _git("--git-dir", str(path), "symbolic-ref", "HEAD", ref)


def repo_info(git_dir: Path, project_id: int) -> dict | None:
    """What the backup holds: branches (with their last commit time), tags, kept rewrites, and size on disk."""
    path = repo_path(git_dir, project_id)
    if not (path / "HEAD").is_file():
        return None
    out = _git("--git-dir", str(path), "for-each-ref", "--sort=-committerdate",
               "--format=%(refname)%00%(objectname:short)%00%(committerdate:iso-strict)", "refs/heads", "refs/tags").stdout
    branches, tags = [], 0
    for line in out.splitlines():
        ref, sha, date = (line.split("\0") + ["", ""])[:3]
        if ref.startswith("refs/tags/"):
            tags += 1
        elif ref.startswith("refs/heads/"):
            branches.append({"name": ref.removeprefix("refs/heads/"), "sha": sha, "date": date or None})
    return {"branches": branches, "tags": tags, "size": dir_size(path)}


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def total_size(git_dir: Path) -> dict:
    """All projects' backups together (Host page)."""
    if not git_dir.is_dir():
        return {"repos": 0, "bytes": 0}
    repos = [p for p in git_dir.iterdir() if REPO_NAME.match(p.name) and p.is_dir() and not p.is_symlink()]
    return {"repos": len(repos), "bytes": sum(dir_size(p) for p in repos)}


def make_bundle(git_dir: Path, project_id: int, target: Path, timeout: float = 1800) -> None:
    """`git bundle create` of everything in the backup: clone or fetch from the file to restore."""
    result = _git("--git-dir", str(repo_path(git_dir, project_id)), "bundle", "create", "-q", str(target), "--all",
                  timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-300:] or "git bundle failed")


class HttpBackend:
    """Runs `git http-backend` for one request: a clean environment, at most BACKEND_SLOTS at once, bounded in time
    and in what it reads and writes."""

    def __init__(self, git_dir: Path, max_body: int, timeout: float, *, command=("git", "http-backend")):
        self.git_dir, self.max_body, self.timeout = git_dir, max_body, timeout
        self.command = command
        self._slots = asyncio.Semaphore(BACKEND_SLOTS)

    async def run(self, method: str, path_info: str, query: str, headers: dict[str, str], body) -> tuple[int, list, bytes]:
        """(status, headers, body). `body` is an async iterator of request chunks."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_PROJECT_ROOT": str(self.git_dir),
               "GIT_HTTP_EXPORT_ALL": "1", "REMOTE_USER": "acc-backup", "REMOTE_ADDR": "127.0.0.1",
               "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_PROTOCOL": "HTTP/1.1", "REQUEST_METHOD": method,
               "PATH_INFO": path_info, "QUERY_STRING": query, "CONTENT_TYPE": headers.get("content-type", "")}
        if headers.get("content-length", "").isdigit():
            env["CONTENT_LENGTH"] = headers["content-length"]
        if headers.get("content-encoding"):
            env["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        if headers.get("git-protocol"):
            env["GIT_PROTOCOL"] = headers["git-protocol"]
        async with async_lease(git_lock(self.git_dir), timeout=self.timeout):
            return await self._locked_run(method, path_info, query, headers, body, env)

    async def _locked_run(self, method, path_info, query, headers, body, env):
        async with self._slots:
            process = await asyncio.create_subprocess_exec(
                *self.command, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            too_large = False

            async def feed():
                nonlocal too_large
                seen = 0
                try:
                    async for chunk in body:
                        seen += len(chunk)
                        if seen > self.max_body:
                            too_large = True
                            process.kill()
                            return
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if not process.stdin.is_closing():
                        process.stdin.close()

            async def collect(stream, limit):
                data = bytearray()
                while chunk := await stream.read(65536):
                    if len(data) < limit:
                        data += chunk[:limit - len(data)]
                return bytes(data)

            try:
                _, out, err, _ = await asyncio.wait_for(asyncio.gather(
                    feed(), collect(process.stdout, MAX_RESPONSE + 1), collect(process.stderr, 8192), process.wait()),
                    self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                log.warning("git backup: http-backend for %s ran over %gs; stopped", path_info, self.timeout)
                return 504, [], b"backup push took too long\n"
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
        if too_large:
            return 413, [], f"a backup push is limited to {self.max_body >> 20} MiB\n".encode()
        if len(out) > MAX_RESPONSE:
            return 502, [], b"http-backend answered too much\n"
        if err.strip():
            log.info("git backup: http-backend %s: %s", path_info, err.decode(errors="replace").strip()[:300])
        return parse_cgi(out)


def parse_cgi(out: bytes) -> tuple[int, list, bytes]:
    """A CGI response: headers (Status: among them) up to a blank line, then the body."""
    for separator in (b"\r\n\r\n", b"\n\n"):
        head, found, rest = out.partition(separator)
        if found:
            break
    else:
        return 502, [], b"http-backend gave no answer\n"
    status, headers = 200, []
    for line in head.decode("latin-1").splitlines():
        name, _, value = line.partition(":")
        if name.lower() == "status":
            status = int(value.strip().split(" ", 1)[0] or 200)
        elif name:
            headers.append((name.strip(), value.strip()))
    return status, headers, rest


PROOF_HEADER = "X-Acc-Backup-Proof"  # the edge forwards it (it strips only X-Acc-Device-*)


def grant_proof_message(channel: str, project_id: int, grant: str) -> bytes:
    """What a node signs with its link key (Ed25519) to exchange a backup grant: the grant, its project and the path it
    is sent on ("private" or "public"). Only the machine the grant was issued for can make it, and a proof seen on one
    path is useless on the other (security review 7, SR7-2)."""
    return b"rynhult-git-backup-grant\0" + channel.encode() + b"\0" + str(int(project_id)).encode() + b"\0" + grant.encode()


class BackupGrants:
    """Job-scoped token exchange capabilities, delivered only over the signed node link.

    The push token is minted with the grant so the job can carry its hash: the
    node pushes only after the exchange returned that token, which no server
    but this one can produce, over connections pinned to the leaf that served the
    exchange (security review 7, SR7-1). Exchanging a grant needs the issuing
    machine's signature for the path it is sent on (SR7-2). A retry
    returns the same token. Finishing/cancelling a job revokes both the grant
    and its token. Neither raw capability is persisted or logged.
    """
    def __init__(self, tokens: BackupTokens, ttl: float = TOKEN_TTL):
        self.tokens, self.ttl = tokens, ttl
        self._grants: dict[str, dict] = {}

    def mint(self, project_id: int, node_id: str) -> str:
        for key, entry in list(self._grants.items()):
            if entry['until'] <= time.monotonic():
                self._remove(key)
        raw = secrets.token_urlsafe(32)
        until = time.monotonic() + self.ttl
        self._grants[BackupTokens._key(raw)] = dict(project=project_id, node=node_id, until=until,
                                                   token=self.tokens.mint(project_id, until=until))
        return raw

    def lookup(self, raw: str, project_id: int) -> dict | None:
        key = BackupTokens._key(raw)
        entry = self._grants.get(key)
        if entry and entry['until'] <= time.monotonic():
            self._remove(key)
            entry = None
        return entry if entry and entry['project'] == project_id else None

    def exchange(self, raw: str, project_id: int) -> str:
        entry = self.lookup(raw, project_id)
        if entry is None:
            raise ValueError('expired or invalid backup grant')
        return entry['token']

    def commitment(self, raw: str, project_id: int) -> str | None:
        """The SHA-256 of the grant's push token, for the signed job (never the token itself)."""
        entry = self.lookup(raw, project_id)
        return BackupTokens._key(entry['token']) if entry else None

    def _remove(self, key):
        entry = self._grants.pop(key, None)
        if entry and entry['token']:
            self.tokens.revoke(entry['token'])

    def revoke(self, raw: str):
        self._remove(BackupTokens._key(raw))

    def revoke_public_node(self, node_id: str):
        """Owner opt-out fences public and not-yet-exchanged jobs; private transfers keep running."""
        for key, entry in list(self._grants.items()):
            if entry['node'] == node_id and entry.get('via') != 'private':
                self._remove(key)



def bare_https_origin(value: str) -> bool:
    """What a node's roaming backup job accepts as an origin: https, a host, nothing else."""
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(value or '')
        parsed.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return False
    return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)


def public_backup_origin(s, node_id: str | None = None) -> str | None:
    """Require explicit node consent plus a verified public edge and machine identities."""
    from . import devices, edge, remote_access
    from .models import Node
    node = s.get(Node, node_id) if node_id else None
    if node is None or node.status != 'active' or node.public_git_backup is not True:
        return None
    config, runtime = devices.settings(s), remote_access.state(s)
    hostname = config['public_hostname']
    if (not edge.enabled(s) or runtime['enabled'] is not True or runtime['desired_enabled'] is not True
            or runtime['pending'] or not hostname or runtime['hostname'] != hostname):
        return None
    from urllib.parse import urlsplit
    parsed = urlsplit('https://' + hostname)
    if parsed.hostname != hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        return None
    return 'https://' + hostname
