"""Broker operations. Every input is validated here; accd is trusted to decide, not to be right."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from acc.env import PROTECTED_SESSION_VARIABLES, Environment, environment, interpreter_variable
from acc.python_child import module_command

from .. import providers
from ..providers import copilot_settings
from . import containers
from .config import BrokerConfig

ACC_ID = re.compile(r"^[a-z0-9]{8,32}$")
TMUX_NAME = re.compile(r"^acc-[a-z0-9][a-z0-9-]{0,60}$")
# A conversation id: a UUID (most CLIs), or OpenCode's ses_… (letters and digits only, so safe in a file glob)
NATIVE_ID = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|ses_[0-9A-Za-z]{10,60})$")
FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RESERVED_ENV = {"_", *PROTECTED_SESSION_VARIABLES, "PATH", "HOME"}
# Launch files that hold the guardrails or a CLI's hooks: written read-only, and their hashes go into the CLI's own
# environment (ACC_GUARD_FILES), which acc-guard checks before every tool call. The agent's account owns its run folder,
# so read-only is only a speed bump; the hashes are what make a changed or removed policy block everything.
GUARD_FILES = ("guardrails.json", "settings.json", "plugin/hooks/hooks.json", "opencode/opencode.json",
               "opencode/plugins/acc/index.js", "opencode/plugins/acc/package.json", "opencode/bin/bash")
EXECUTABLE_FILES = ("opencode/bin/bash",)  # OpenCode's shell for the session (providers/opencode.py)
# Leave ample room for tmux framing and platform differences (UTF-8 bytes, not characters).
TMUX_INLINE_BYTES = 4096
PANE_FORMAT = "\t".join([
    "#{session_name}", "#{pane_dead}", "#{pane_dead_status}", "#{pane_dead_signal}",
    "#{pane_pid}", "#{session_created}", "#{window_activity}",
])


class OpError(Exception):
    pass


def tmux(config: BrokerConfig, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["tmux", "-L", config.tmux_socket, *args], capture_output=True, text=True)
    if check and result.returncode:
        raise OpError(f"the session's terminal didn't respond (tmux {args[0]}): {result.stderr.strip()}")
    return result


def prepare_tmux(config: BrokerConfig) -> None:
    """Server options for every Rynhult session. Idempotent; called at startup and before launches."""
    tmux(config, "start-server", ";",
         "set-option", "-g", "exit-empty", "off", ";",
         "set-option", "-g", "remain-on-exit", "on", ";",
         "set-option", "-g", "history-limit", "50000", ";",
         "set-option", "-g", "window-size", "latest", ";",
         "set-option", "-g", "default-terminal", "tmux-256color", ";",
         "set-option", "-g", "status", "off", check=True)  # the browser shows session state; no tmux chrome


def list_sessions(config: BrokerConfig) -> list[dict]:
    sessions = _read_sessions(config)
    if any(p["dead"] and p["exit_status"] is None and p["exit_signal"] is None for p in sessions):
        # Older tmux/utempter can lose SIGCHLD during terminal cleanup, leaving a zombie
        # without a pane result. A fixed job's exit wakes tmux's child collector on this
        # socket only. Re-read once; a child still exiting remains pending for reconcile.
        tmux(config, "run-shell", "true")
        sessions = _read_sessions(config)
    return sessions


def _read_sessions(config: BrokerConfig) -> list[dict]:
    result = tmux(config, "list-panes", "-a", "-F", PANE_FORMAT)
    if result.returncode:
        return []  # no server yet means no sessions
    sessions = []
    for line in result.stdout.splitlines():
        name, dead, status, signal, pid, created, activity = (line.split("\t") + [""] * 7)[:7]
        if not TMUX_NAME.match(name):
            continue
        sessions.append({
            "tmux_name": name,
            "dead": dead == "1",
            "exit_status": int(status) if status.isdigit() else None,
            "exit_signal": int(signal) if signal.isdigit() else None,
            "pid": int(pid) if pid.isdigit() else None,
            "created": int(created) if created.isdigit() else None,
            "last_output": int(activity) if activity.isdigit() else None,
        })
    return sessions


def _under_roots(config: BrokerConfig, path: str) -> Path:
    resolved = Path(path).resolve()
    if not any(resolved == root or root in resolved.parents for root in config.allowed_roots):
        raise OpError(f"{resolved} is outside this account's workspace folders. Choose a folder inside them.")
    if not resolved.is_dir():
        raise OpError(f"{resolved} isn't a folder.")
    return resolved


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # what a repository with no commits yet is compared with
MAX_DIFF = 512 * 1024
MAX_FILES = 500


def _git_argv(cwd: Path) -> list[str]:
    """git in `cwd`; in a container session's private checkout, only with its settings checked (containers.git_guard)."""
    try:
        guarded = containers.git_guard(cwd)
        return guarded or ["git", "-C", str(cwd)]
    except containers.ContainerError as error:
        raise OpError(str(error)) from None


def _git(cwd: Path, *args: str, timeout: int = 30, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([*_git_argv(cwd), "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _git_ok(cwd: Path, *args: str, allowed: tuple[int, ...] = (0,), env: dict[str, str] | None = None) -> str:
    """stdout of a git read that must succeed: a failure is an error, never an empty (clean-looking) answer."""
    result = _git(cwd, *args, env=env)
    if result.returncode not in allowed:
        raise OpError(f"Git couldn't read this checkout (git {args[0]}): {result.stderr.strip()[:200]}")
    return result.stdout


# Settings that make git run a program, overridden for git the broker runs outside a guarded tool call in a checkout
# an agent can configure (its .git/config is shared by every session's worktree; its global config is the agent's).
HARDENED_GIT = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"), ("commit.gpgSign", "false"),
                ("merge.verifySignatures", "false"), ("core.attributesFile", "/dev/null"), ("diff.external", ""),
                ("submodule.recurse", "false"), ("protocol.allow", "never"), ("gc.auto", "0"),
                ("maintenance.auto", "false"), ("core.alternateRefsCommand", "true"))
# Per-driver commands, which attributes (.gitattributes in the tree) select by name: filters pass content through
# unchanged, diff commands and textconv do nothing, and a custom merge driver fails, so its files conflict.
DRIVER_COMMANDS = {"filter": (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")),
                   "diff": (("command", ""), ("textconv", "")), "merge": (("driver", "false"),)}


IDENTITY_ENV = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}  # names only


def _hardened_env(cwd: Path) -> dict[str, str] | None:
    """git's environment for a merge, checkout, commit or worktree change in an agent's checkout: no system or global
    config (the agent's identity carried over, from its config or GIT_AUTHOR_*/GIT_COMMITTER_*), no hooks, fsmonitor,
    signing, filter/diff/merge drivers or lazy fetches.
    Computed for each command: a merge can bring in a tracked file the repository's config includes.
    None for a container session's checkout, which container_git already runs config-checked and hardened."""
    if containers.is_container_checkout(cwd):
        return None
    base = {k: v for k, v in environment.items() if not k.startswith("GIT_") or k in IDENTITY_ENV}
    # No transport at all, whatever the checkout's config says: a per-protocol `protocol.<name>.allow` there would
    # override protocol.allow, and `url.*.insteadOf` can turn any path into an `ext::` command (security review 10,
    # SR10-3). A caller that needs one transport sets GIT_ALLOW_PROTOCOL itself (team_import: file).
    env = {**base, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}
    config = list(HARDENED_GIT)

    def read(*args: str, scope: dict[str, str]) -> str:  # reading settings runs nothing
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30, env=scope)
        return result.stdout if result.returncode == 0 else ""
    top = read("rev-parse", "--show-toplevel", scope=base).strip()
    if top:  # project folders belong to their owner: trust this one (the agent's global config is left out)
        config.append(("safe.directory", top))
    for key in ("user.name", "user.email"):
        if value := read("config", "--get", key, scope=base).strip():
            config.append((key, value))
    names = read("config", "--null", "--name-only", "--get-regexp", r"^(filter|diff|merge)\.", scope=env)
    drivers = set()
    for name in names.split("\0"):
        section, _, rest = name.partition(".")
        driver = rest.rpartition(".")[0]
        if section.lower() in DRIVER_COMMANDS and driver:
            drivers.add((section.lower(), driver))
    for section, driver in sorted(drivers):
        config += [(f"{section}.{driver}.{key}", value) for key, value in DRIVER_COMMANDS[section]]
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def _hardened_git(cwd: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return _git(cwd, *args, timeout=timeout, env=_hardened_env(cwd))


def git_baseline(cwd: Path) -> str | None:
    """Launch commit retained for older brokers; EMPTY_TREE before the first commit, None outside git."""
    if _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return None
    return git_head(cwd) or EMPTY_TREE


def git_head(cwd: Path) -> str | None:
    """The commit a folder's checkout is at (None: not a git repository, or no commits yet)."""
    result = _git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    head = result.stdout.strip()
    return head if result.returncode == 0 and GIT_SHA.match(head) else None


def _changes_baseline(cwd: Path) -> tuple[str, str | None]:
    """Compare the current branch, not a possibly unrelated launch commit. Never fetch to choose a baseline."""
    head = git_head(cwd)
    if head is None:
        return EMPTY_TREE, None
    branch = _git(cwd, "symbolic-ref", "--quiet", "HEAD")
    if branch.returncode not in (0, 1):
        raise OpError(f"Git couldn't tell which branch this checkout is on: {branch.stderr.strip()[:200]}")
    upstream = _git_ok(cwd, "for-each-ref", "--format=%(upstream)", branch.stdout.strip()).strip() \
        if branch.returncode == 0 else ""
    candidates = ("refs/remotes/origin/main", "refs/heads/main", "refs/remotes/origin/HEAD", upstream,
                  "refs/remotes/origin/master", "refs/heads/master")
    for candidate in candidates:
        if not candidate:
            continue
        target = _git(cwd, "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}")
        if target.returncode == 1:
            continue
        if target.returncode or not GIT_SHA.fullmatch(target.stdout.strip()):
            raise OpError(f"Git couldn't find the branch to compare with: {target.stderr.strip()[:200]}")
        common = _git(cwd, "merge-base", head, target.stdout.strip())
        if common.returncode == 1:  # unrelated histories have no comparison base
            continue
        if common.returncode or not GIT_SHA.fullmatch(common.stdout.strip()):
            raise OpError(f"Git couldn't compare this branch with the main branch: {common.stderr.strip()[:200]}")
        label = candidate.removeprefix("refs/remotes/").removeprefix("refs/heads/")
        return common.stdout.strip(), label
    return head, None


def workspace_changes(config: BrokerConfig, path: str, base: str | None, detail: bool = False) -> dict:
    """Current-branch changes against its upstream/default branch merge-base, plus edits and new files.
    `base` is accepted for older callers, but must not pin this to a branch the session has left."""
    cwd = _under_roots(config, path)
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside.returncode and "not a git repository" in inside.stderr:
        return {"git": False}
    if inside.returncode or inside.stdout.strip() != "true":
        raise OpError(f"Git couldn't read this folder: {inside.stderr.strip()[:200]}")
    if base is not None and not GIT_SHA.match(base):
        raise OpError("The changes view asked for a commit that isn't valid. Reload the page and try again.")
    root = Path(_git_ok(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    ref, comparison_ref = _changes_baseline(cwd)
    if root != cwd and not _git_ok(cwd, "ls-files", "--cached", "-z", "--", ".") \
            and (ref == EMPTY_TREE or not _git_ok(cwd, "ls-tree", "--name-only", ref, "--", ".")):
        return {"git": False}  # an untracked scratch folder must not inherit a parent's checkout
    files = []  # -z: names as they are (git C-quotes names with tabs, quotes or newlines otherwise)
    for record in _git_ok(cwd, "diff", "--relative", "--numstat", "--no-renames", "-z", ref, "--", ".").split("\0"):
        if not record:
            continue
        added, deleted, name = (record.split("\t", 2) + ["", "", ""])[:3]
        files.append({"path": name, "status": "M", "additions": int(added) if added.isdigit() else 0,
                      "deletions": int(deleted) if deleted.isdigit() else 0, "binary": added == "-"})
    fields = _git_ok(cwd, "diff", "--relative", "--name-status", "--no-renames", "-z", ref, "--", ".").split("\0")
    statuses = {name: status for status, name in zip(fields[::2], fields[1::2])}
    for entry in files:
        entry["status"] = statuses.get(entry["path"], "M")[:1]
    untracked = [name for name in _git_ok(cwd, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name]
    for name in untracked[:MAX_FILES]:
        target = cwd / name
        try:
            data = target.read_bytes() if target.is_file() and target.stat().st_size <= 1024 * 1024 else b"\0"
        except OSError:
            data = b"\0"
        binary = b"\0" in data[:8000]
        files.append({"path": name, "status": "A", "additions": 0 if binary else len(data.splitlines()), "deletions": 0,
                      "binary": binary})
    files.sort(key=lambda f: f["path"])
    head = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")  # fails before the first commit; "HEAD" when detached
    branch = head.stdout.strip() if head.returncode == 0 and head.stdout.strip() != "HEAD" else None
    out = {"git": True, "base": ref[:12] if ref != EMPTY_TREE else "empty", "since_launch": False, "branch": branch,
           "files": len(files), "additions": sum(f["additions"] for f in files),
           "deletions": sum(f["deletions"] for f in files)}
    if comparison_ref:
        out["comparison_ref"] = comparison_ref
    if not detail:
        return out
    diff = _git_ok(cwd, "diff", "--relative", "--no-color", "--no-renames", ref, "--", ".")
    for name in untracked[:MAX_FILES]:
        if len(diff) > MAX_DIFF:
            break
        diff += _git_ok(cwd, "diff", "--no-color", "--no-index", "--", "/dev/null", name, allowed=(0, 1))  # 1: differs
    return {**out, "list": files[:MAX_FILES], "diff": diff[:MAX_DIFF], "truncated": len(diff) > MAX_DIFF}


BRANCH = re.compile(r"^acc/[a-z0-9][a-z0-9-]{0,80}(?:/work)?$")
WORKTREE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,90}$")


SPOOL_READ = 512 * 1024


def read_spool(config: BrokerConfig, acc_id: str, offset: int) -> dict:
    """New hook events of one of this account's sessions, from byte `offset` (multi-host: the primary mirrors them).

    The session (this account) can change its run folder: no link is followed below the run dir, a FIFO or device
    never blocks the read, and only a regular file this account owns is read, at most SPOOL_READ bytes of it."""
    if not ACC_ID.match(acc_id) or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise OpError("Rynhult sent an invalid session ID or read position.")
    try:
        fd = _open_beneath(config.run_dir, (acc_id, "events.jsonl"))
    except FileNotFoundError:
        return {"data": ""}
    except OSError as error:  # a link (ELOOP), a file where a folder should be (ENOTDIR), ...
        raise OpError(f"The session's event log ({acc_id}/events.jsonl) isn't a plain file ({error.strerror}), so Rynhult won't read it.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OpError(f"The session's event log ({acc_id}/events.jsonl) isn't a plain file, so Rynhult won't read it.")
        if info.st_uid != os.geteuid():
            raise OpError(f"The session's event log ({acc_id}/events.jsonl) belongs to another account, so Rynhult won't read it.")
        data = os.pread(fd, SPOOL_READ, offset)
    finally:
        os.close(fd)
    return {"data": base64.b64encode(data).decode()}


def _open_beneath(root: Path, parts: tuple[str, ...]) -> int:
    """Open root/parts[0]/.../parts[-1] read-only without following a link at any level below `root` (as
    acc.server.ingest._open_beneath does), and without blocking on a FIFO."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def make_worktree(config: BrokerConfig, repo: str, root: str, name: str, branch: str, base_commit: str | None = None) -> dict:
    """<root>/.worktrees/<name>: a new working copy of `repo` on a new branch from its current commit."""
    source = _under_roots(config, repo)
    base = _under_roots(config, root)
    if not WORKTREE_NAME.match(name) or not BRANCH.match(branch):
        raise OpError("The worktree or branch name isn't valid.")
    if _git(source, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true" or not git_head(source):
        raise OpError("the project isn't a git repository with a commit yet, so a session can't get its own checkout: "
                      "make a first commit in the project (or start one session in the shared checkout), then try again")
    if base_commit is not None and (not isinstance(base_commit, str) or not re.fullmatch("[0-9a-f]{40,64}", base_commit)):
        raise OpError("invalid continuation commit")
    target = base / ".worktrees" / name
    if target.exists():
        raise OpError(f"{target} already exists, so Rynhult won't reuse it for a new session. Remove that folder, "
                      "or start the session again.")
    target.parent.mkdir(exist_ok=True)
    result = _hardened_git(source, "worktree", "add", "-q", "-b", branch, str(target), base_commit or "HEAD", timeout=120)
    if result.returncode:
        raise OpError(f"Git couldn't make the session's worktree: {result.stderr.strip()[:300]}")
    return {"path": str(target), "branch": branch, **({"base_commit":git_head(target)} if base_commit else {})}


def make_private_checkout(config: BrokerConfig, repo: str, root: str, name: str, branch: str, base_commit: str | None = None) -> dict:
    """<root>/.worktrees/<name> for a container session: a repository of its own seeded from the project's committed
    HEAD on a new branch (a worktree's .git would point at the project's shared administration)."""
    source = _under_roots(config, repo)
    base = _under_roots(config, root)
    if not WORKTREE_NAME.match(name) or not BRANCH.match(branch):
        raise OpError("The checkout or branch name isn't valid.")
    if _git(source, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true" or not git_head(source):
        raise OpError("the project isn't a git repository with a commit yet, so it can't have a private checkout")
    target = base / ".worktrees" / name
    if target.exists():
        raise OpError(f"{target} already exists, so Rynhult won't reuse it for a new session. Remove that folder, "
                      "or start the session again.")
    target.parent.mkdir(exist_ok=True)
    try:
        if base_commit is not None and (not isinstance(base_commit, str) or not re.fullmatch('[0-9a-f]{40,64}', base_commit)):
            raise OpError('invalid continuation commit')
        containers.make_checkout(source, target, branch, **({'commit':base_commit} if base_commit else {}))
    except containers.ContainerError as error:
        raise OpError(str(error)) from None
    return {"path": str(target), "branch": branch, "private": True, **({"base_commit":git_head(target)} if base_commit else {})}


def remove_worktree(config: BrokerConfig, repo: str, path: str) -> dict:
    """Remove a session's worktree once it has nothing uncommitted (never forced). Its branch and commits stay: a
    container session's private checkout first brings its branch back into the project."""
    source = _under_roots(config, repo)
    target = _under_roots(config, path)
    if target.parent.name != ".worktrees":
        raise OpError("That folder isn't a worktree Rynhult made, so Rynhult won't remove it.")
    status = _hardened_git(target, "status", "--porcelain", "--ignored", "-z")  # ignored files too: `worktree remove` deletes them
    if status.returncode:
        return {"removed": False, "reason": f"git status failed: {status.stderr.strip()[:200]}"}
    entries = [e for e in status.stdout.split("\0") if e]
    if entries:
        ignored = sum(e.startswith("!!") for e in entries)
        reason = "it has uncommitted changes" if ignored < len(entries) else "it has files git ignores (they'd be lost)"
        return {"removed": False, "reason": reason, "files": len(entries)}
    if containers.is_private_checkout(target):
        branch = _git(target, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if not BRANCH.match(branch):
            return {"removed": False, "reason": "its branch isn't a session branch, so it wasn't brought back"}
        failed = containers.return_branch(source, target, branch)
        if failed:
            return {"removed": False, "reason": f"bringing {branch} back into the project failed: {failed}"}
        shutil.rmtree(target)
        return {"removed": True, "branch": branch}
    result = _hardened_git(source, "worktree", "remove", str(target), timeout=120)
    if result.returncode:
        return {"removed": False, "reason": result.stderr.strip()[:300]}
    return {"removed": True}


def exclude_artifacts(cwd: Path) -> None:
    """Keep .acc/ (session artifacts) out of git in a session's checkout: listed in the repository's own
    info/exclude, which is never committed, so `git add -A` skips it and Changes doesn't count it. Best effort, and
    only on a plain file: no link at any level of the path, no FIFO, nothing over 1 MiB (anything else is skipped)."""
    try:
        if containers.is_container_checkout(cwd):
            # rev-parse --git-path names the temporary snapshot, not this checkout's administration.
            admin = _walk_dirs(list((cwd / ".git").absolute().parts[1:]))
            try:
                try:
                    os.mkdir("info", 0o755, dir_fd=admin)
                except FileExistsError:
                    pass
                parent = os.open("info", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=admin)
            finally:
                os.close(admin)
            try:
                fd = os.open("exclude", os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o644, dir_fd=parent)
            finally:
                os.close(parent)
        else:
            if _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
                return
            found = _git(cwd, "rev-parse", "--git-path", "info/exclude")
            if found.returncode:
                return
            exclude = Path(found.stdout.strip())
            exclude = Path(os.path.abspath(exclude if exclude.is_absolute() else cwd / exclude))
            if not exclude.parent.is_dir() or os.path.realpath(exclude.parent) != str(exclude.parent):
                return
            fd = os.open(exclude, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1 << 20:
                return
            current = os.pread(fd, info.st_size, 0).decode(errors="replace")
            if any(line.strip() in (".acc/", "/.acc/", ".acc") for line in current.splitlines()):
                return
            os.write(fd, (("" if not current or current.endswith("\n") else "\n")
                          + "# Rynhult session artifacts\n.acc/\n").encode())
        finally:
            os.close(fd)
    except (OSError, subprocess.SubprocessError):
        pass


def _walk_dirs(parts: list[str]) -> int:
    """A descriptor of /parts for *at calls, opened one component at a time without following links. O_PATH needs
    only search permission: /srv/acc/run is traverse-only (0711) for agent accounts, so a read open of it fails."""
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW  # macOS has no O_PATH
    fd = os.open("/", flags)
    try:
        for part in parts:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _materialize_run(run_dir: Path, files: dict, env: dict, *, private: bool = False) -> None:
    """All writes stay on held directories; a container may replace any run-folder entry on resume."""
    try:
        # Parent is setgid acc. Preserve that bit and group when creating the session folder.
        parent = _walk_dirs(list(run_dir.parent.absolute().parts[1:]))
        try:
            try:
                os.mkdir(run_dir.name, 0o750, dir_fd=parent)
            except FileExistsError:
                pass
            root = os.open(run_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            for name, content in files.items():
                if not FILE_NAME.fullmatch(name) or ".." in name.split("/"):
                    raise OpError(f"Rynhult won't write a session file named {name!r}.")
                folder = os.dup(root)
                try:
                    parts = name.split("/")
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, 0o750, dir_fd=folder)
                        except FileExistsError:
                            pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                        os.close(folder)
                        folder = child
                    try:
                        os.unlink(parts[-1], dir_fd=folder)
                    except FileNotFoundError:
                        pass
                    mode = 0o600 if private else ((0o440 if name in GUARD_FILES else 0o640)
                                                   | (0o110 if name in EXECUTABLE_FILES else 0))
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=folder)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content.encode())
                        os.fchmod(handle.fileno(), mode)
                finally:
                    os.close(folder)
            fd = os.open("events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640, dir_fd=root)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OpError("The session's event log isn't a plain file, so Rynhult won't start the session. Try again.")
            finally:
                os.close(fd)
            if private:
                return
            try:
                os.unlink("session.env", dir_fd=root)
            except FileNotFoundError:
                pass
            if env:
                fd = os.open("session.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
                with os.fdopen(fd, "w") as handle:
                    handle.writelines(f"export {key}={shlex.quote(value)}\n" for key, value in env.items())
        finally:
            os.close(root)
    except OSError:
        raise OpError("The session's run folder has links or changed while Rynhult wrote it, so Rynhult won't use it. Try again.") from None


def _codex_team_git_args(config: BrokerConfig, cwd: Path, args: list[str], files: dict) -> list[str]:
    """Only linked-worktree administration and commit data, resolved by its owning broker."""
    if not any(args[i:i + 2] == ['--sandbox', 'workspace-write'] for i in range(len(args))):
        return []
    try:
        team = json.loads(files.get('guardrails.json', '{}')).get('rules', {}).get('team') is True
    except (ValueError, AttributeError):
        raise OpError('The session guard policy is invalid.') from None
    if not team:
        return []
    try:
        entry = (cwd / '.git').lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(entry.st_mode):
        raise OpError("The worktree's Git metadata link isn't safe to grant write access to.")
    if not stat.S_ISREG(entry.st_mode):
        return []  # An independent repository has no external worktree administration.
    common_result = _hardened_git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    admin_result = _hardened_git(cwd, 'rev-parse', '--absolute-git-dir')
    if common_result.returncode or admin_result.returncode:
        raise OpError("Git couldn't locate this worktree's metadata.")
    common = _under_roots(config, common_result.stdout.strip())
    admin = _under_roots(config, admin_result.stdout.strip())
    if common.name != '.git' or admin.parent != common / 'worktrees':
        raise OpError("The worktree's Git metadata isn't inside its repository's .git folder.")
    branch_result = _hardened_git(cwd, 'symbolic-ref', '--quiet', 'HEAD')
    branch = branch_result.stdout.strip()
    if branch_result.returncode or not re.fullmatch(r'refs/heads/acc/[a-z0-9][a-z0-9-]{0,80}/work', branch):
        raise OpError("This Codex team session needs a private branch directory. Create a new team or helper to use the narrowed Git permissions.")
    namespace = Path(branch).parent
    logging = _hardened_git(cwd, 'config', '--get', 'core.logAllRefUpdates')
    if logging.returncode not in (0, 1):
        raise OpError("Git couldn't read this repository's reflog setting.")
    reflogs = logging.returncode == 1 or logging.stdout.strip().lower() not in ('', 'false', 'no', 'off', '0')
    folder = _walk_dirs(list(common.absolute().parts[1:]))
    def prepare(parts, create):
        fd = os.dup(folder)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        finally:
            os.close(fd)
    try:
        prepare(('objects',), False)
        prepare(namespace.parts, True)
        if reflogs:
            prepare(('logs', *namespace.parts), True)
    except OSError:
        raise OpError("Git commit data must be directories, not links or files; they couldn't be prepared safely.") from None
    finally:
        os.close(folder)
    # Only a helper-private namespace: never heads/, refs/, logs/, or common.
    roots = [admin, common / 'objects', common / namespace]
    if reflogs:
        roots.append(common / 'logs' / namespace)
    return ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps([str(path) for path in roots])]


def launch(config: BrokerConfig, params: dict, *, workspace_key: bytes | None = None) -> dict:
    acc_id = params["acc_id"]
    tmux_name = params["tmux_name"]
    provider = params["provider"]
    args = params.get("args", [])
    env = params.get("env", {})
    files = params.get("files", {})
    if not ACC_ID.match(acc_id):
        raise OpError("Rynhult sent an invalid session ID.")
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    if provider not in config.providers:
        raise OpError(f"{provider!r} isn't an agent CLI this machine knows.")
    if not isinstance(args, list) or not all(isinstance(a, str) and "\0" not in a for a in args):
        raise OpError("The session's start command isn't valid.")
    for key, value in env.items():
        if not ENV_KEY.match(key) or key in RESERVED_ENV or interpreter_variable(key) or not isinstance(value, str):
            raise OpError(f"The environment variable {key!r} is not allowed for a session: Rynhult sets it itself, or it could change "
                          "which programs run. Use another name.")
    helper = "explanation.json" in files
    container = params.get("container")
    if container is not None:
        try:
            container = containers.check(container)
        except containers.ContainerError as error:
            raise OpError(str(error)) from None
        if helper:
            raise OpError("explanations don't run in containers")
    if helper:
        expected = config.run_dir / acc_id / "workspace"
        if params["cwd"] != str(expected) or env:
            raise OpError("explanations need an isolated run workspace and no project environment")
        expected.mkdir(mode=0o700, parents=True, exist_ok=True)
        cwd = expected
    else:
        cwd = _under_roots(config, params["cwd"])
    if container and not containers.is_private_checkout(cwd):
        raise OpError("a container session needs its own private checkout")
    if provider == "copilot":  # every start comes here: first launch, resume, restart and retries
        problem = copilot_settings.repository_problem(cwd)
        if problem:
            raise OpError(copilot_settings.refusal(problem))
    if tmux(config, "has-session", "-t", f"={tmux_name}").returncode == 0:
        raise OpError(f"A session terminal named {tmux_name} is already running on this machine.")
    exclude_artifacts(cwd)

    run_dir = config.run_dir / acc_id
    if params.get("run_dir") not in (None, str(run_dir)):  # the arguments and files name this folder (Q4)
        raise OpError(f"this machine keeps session folders in {config.run_dir}, not where the launch expects them "
                      f"({params['run_dir']})")
    bound_files = {}
    if workspace_key is not None:
        from .brief_references import seal
        bound_files['private-workspace.json'] = seal(acc_id, str(cwd), workspace_key)
    _materialize_run(run_dir, {**files, **bound_files}, env)

    if provider == 'codex' and not helper and not container:
        args = [*args, *_codex_team_git_args(config, cwd, args, files)]
    prepare_tmux(config)
    if helper:
        command = shlex.join(module_command("acc.broker.explain", str(run_dir),
                                            config.providers[provider], config.hook_path))
    elif not container:  # a container's home is its own (empty) one: nothing to set up in this account's
        providers.get(provider).bootstrap(Path.home(), config.hook_path, exe=config.providers[provider])
        command = shlex.join([config.providers[provider], *args])
    env_file = run_dir / "session.env"
    if env and not container:
        command = shlex.join(["sh", "-c", '. "$0" && exec "$@"', str(env_file), config.providers[provider], *args])
    guarded = " ".join(f"{name}={hashlib.sha256(files[name].encode()).hexdigest()}" for name in GUARD_FILES if name in files)
    session_env = dict(Environment({"ACC_RUN_DIR": str(run_dir), "ACC_SESSION_ID": acc_id, "ACC_GUARD_FILES": guarded}))
    launched = None
    if container:
        # The root helper starts it from accd's grant (never from anything this broker says) as this account, in a
        # unit of its own; the pane only attaches. No fallback: a refusal is the launch's error.
        from . import container_integrations
        started = False
        try:
            container_integrations.start(config, acc_id, container, provider, run_dir)
            started = True
            launched = containers.ask(config.containers_socket, "launch", acc_id, container)
        except containers.ContainerError as error:
            if started:  # only this launch's gateway: a refused start never closes a running generation's
                container_integrations.stop(config, acc_id, generation=container["generation"])
            raise OpError(f"container launch refused: {error}") from None
        command = shlex.join(containers.attach_command(config.containers_attach_root, acc_id, container["generation"]))
    new = ["new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50", "-c", str(cwd)]
    for key, value in session_env.items():
        new += ["-e", f"{key}={value}"]
    # tmux serializes the entire request into one bounded message. Never shorten or reinterpret
    # provider argv: only the already-validated, shell-quoted command can enter this fixed script.
    try:
        if sum(len(value.encode()) + 1 for value in [*new, command]) > TMUX_INLINE_BYTES:
            _materialize_run(run_dir, {"launch-command.sh": "#!/bin/sh\nexec " + command + "\n"}, {}, private=True)
            command = shlex.join(["/bin/sh", str(run_dir / "launch-command.sh")])
            if sum(len(value.encode()) + 1 for value in [*new, command]) > TMUX_INLINE_BYTES:
                raise OpError("The session's terminal paths are too long. Choose a shorter workspace path.")
        tmux(config, *new, command, check=True)
    except OpError:
        if container:
            stop_container(config, acc_id, container)
        raise
    pane = tmux(config, "list-panes", "-t", f"={tmux_name}", "-F", "#{pane_pid}").stdout.strip().split("\n")[0]
    out = {"tmux_name": tmux_name, "pid": int(pane) if pane.isdigit() else None, "command": command,
           "head": git_baseline(cwd)}
    if launched:
        out["container"] = {"unit": launched.get("unit"), "state": launched.get("state")}
    return out


def stop_container(config: BrokerConfig, acc_id: str, container: dict) -> dict:
    """Ask the root helper to stop the session's container (its whole unit). Without an answer the helper's own
    reconcile stops it once accd revokes the grant."""
    from . import container_integrations
    try:
        checked = containers.check(container)
        container_integrations.stop(config, acc_id, generation=checked["generation"])  # close network before contacting helper
        answer = containers.ask(config.containers_socket, "stop", acc_id, checked, timeout=55)
    except containers.ContainerError as error:
        return {"stopped": False, "error": str(error)[:300]}
    return {"stopped": answer.get("state") in ("stopped", "none", "exited", "failed"), "state": answer.get("state")}


def explanation_result(config: BrokerConfig, acc_id: str) -> dict:
    if not ACC_ID.fullmatch(acc_id):
        raise OpError("bad session id")

    def read(parts: tuple[str, ...], limit: int) -> str:
        fd = _open_beneath(config.run_dir, (acc_id, *parts))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise OpError("The explanation's file is too large or isn't a plain file.")
            data = os.read(fd, limit + 1)
            if len(data) > limit:
                raise OpError("The explanation's file is too large.")
            return data.decode("utf-8")
        finally:
            os.close(fd)
    try:
        status = json.loads(read(("explanation-status.json",), 2048))
    except FileNotFoundError:
        return {"status": "running"}
    if status.get("status") == "done":
        return {"status": "done", "raw": read(("workspace", ".acc", "artifacts", "walkthrough.json"), 64 * 1024)}
    return {"status": "error", "error": str(status.get("error") or "explanation failed")[:300]}


def clear_env(config: BrokerConfig, acc_id: str, files: list[str] | None = None) -> dict:
    """A stopped or archived session's keys leave this machine (D21): its session.env, and the run folder's files that
    carry secret values (`files`, e.g. mcp.json), are removed. A resume writes them again."""
    if not ACC_ID.match(acc_id or ""):
        raise OpError("Rynhult sent an invalid session ID.")
    from . import container_integrations
    container_integrations.stop(config, acc_id)
    run_dir = config.run_dir / acc_id
    names = ["session.env"]
    for name in files or []:
        if not isinstance(name, str) or not FILE_NAME.match(name) or ".." in name.split("/"):
            raise OpError(f"Rynhult won't write a session file named {name!r}.")
        names.append(name)
    removed = []
    for name in names:
        path = run_dir / name
        if path.is_file() or path.is_symlink():  # a link is removed, never followed
            path.unlink(missing_ok=True)
            removed.append(name)
    return {"removed": "session.env" in removed, "files": removed}


def kill(config: BrokerConfig, tmux_name: str, container: dict | None = None, acc_id: str | None = None) -> dict:
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    existed = tmux(config, "kill-session", "-t", f"={tmux_name}").returncode == 0
    if container is None:
        return {"killed": existed}
    if not ACC_ID.match(acc_id or ""):
        raise OpError("Rynhult sent an invalid session ID.")
    try:
        containers.check(container)
    except containers.ContainerError as error:
        raise OpError(str(error)) from None
    return {"killed": existed, "container": stop_container(config, acc_id, container)}


def terminal_output(config: BrokerConfig, tmux_name: str) -> dict:
    """Bounded plain text of the pane and retained history, with wrapped rows joined."""
    if not TMUX_NAME.fullmatch(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    result = tmux(config, "capture-pane", "-p", "-J", "-S", "-10000", "-t", f"={tmux_name}:")
    if result.returncode:
        raise OpError("Rynhult couldn't read the session's terminal. The session may have stopped.")
    text = result.stdout.rstrip()
    return {"text": text[-200_000:], "truncated": len(text) > 200_000}


def automation_screen(config: BrokerConfig, tmux_name: str, provider: str) -> dict:
    from ..providers.limits import limit_signal
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    result = tmux(config, "capture-pane", "-p", "-t", f"={tmux_name}:")
    if result.returncode:
        raise OpError("Rynhult couldn't read the session's terminal. The session may have stopped.")
    return {"limit": limit_signal(provider, result.stdout), "prompt": _provider(provider).screen_prompt(result.stdout)}


def screen_prompt(config: BrokerConfig, tmux_name: str, provider: str) -> dict:
    """Live prompt, working and turn-error signals. The screen itself stays here."""
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    spec = _provider(provider)
    if not spec.prompt_patterns and spec.id != "claude":
        return {"prompt": None}
    # Rejoin terminal soft wraps so phone-width panes retain the provider's footer
    # controls and error lines. Explicit newlines still delimit transcript rows.
    result = tmux(config, "capture-pane", "-p", "-J", "-t", f"={tmux_name}:")
    if result.returncode:  # unknown, not "no prompt": the caller keeps what it knew
        raise OpError(f"Rynhult couldn't read the session's terminal: {result.stderr.strip()[:200]}")
    return {"prompt": spec.screen_prompt(result.stdout), "working": spec.screen_working(result.stdout),
            "idle": spec.screen_idle(result.stdout)}


def interrupt(config: BrokerConfig, tmux_name: str, provider: str | None = None) -> dict:
    """Esc interrupts the current turn (OpenCode wants it twice, a moment apart: `interrupt_keys`). Besides send_text's
    message, the only key Rynhult ever sends."""
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    keys = _provider(provider).interrupt_keys if provider else ("Escape",)
    for i, key in enumerate(keys):
        if i:
            time.sleep(0.3)
        tmux(config, "send-keys", "-t", f"={tmux_name}:", key, check=True)  # pane target needs "session:"
    return {"sent": " ".join(keys)}


MAX_TEXT = 4000
PASTE_MARKER = re.compile(r"\[(?:Pasted\b|Paste #\d+)", re.IGNORECASE)  # a folded paste still in the input box
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # everything but tab and newline: no escape sequences, no keys


def send_text(config: BrokerConfig, tmux_name: str, text: str, provider: str = "claude") -> dict:
    """Type one message into the agent's prompt and submit it (review feedback from the dashboard). Literal text only
    (send-keys -l), on one line: the CLIs submit on Enter, so newlines (and tabs, which some complete on) become spaces."""
    if not TMUX_NAME.match(tmux_name):
        raise OpError("Rynhult sent an invalid session terminal name.")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise OpError(f"the message must be 1 to {MAX_TEXT} characters")
    if CONTROL.search(text):
        raise OpError("the message can't contain control characters")
    spec = _provider(provider)
    line = re.sub(r"[\t\n]", " ", text).strip()
    tmux(config, "send-keys", "-t", f"={tmux_name}:", "-l", "--", line, check=True)
    # Separate text insertion from submission for TUIs with paste-burst detection: Codex folds a fast burst into
    # "[Pasted Content …]" and treats an Enter inside the burst window as part of the paste, so it waits longer.
    settle = 1.0 if provider == "codex" else 0.2
    time.sleep(settle)
    for _ in range(3):
        tmux(config, "send-keys", "-t", f"={tmux_name}:", "Enter", check=True)
        time.sleep(max(0.3, settle))
        row = tmux(config, "display-message", "-p", "-t", f"={tmux_name}:", "#{cursor_y}", check=True).stdout.strip()
        if not row.isdigit():
            raise OpError("message typed but not submitted: Rynhult couldn't find the agent's input box. Check the session's "
                          "terminal before sending it again.")
        screen = tmux(config, "capture-pane", "-p", "-t", f"={tmux_name}:", "-S", row, "-E", row, check=True).stdout
        # A long message wraps, so the paste marker can sit a few rows above the cursor.
        above = tmux(config, "capture-pane", "-p", "-t", f"={tmux_name}:", "-S", str(max(0, int(row) - 4)), "-E", row,
                     check=True).stdout
        if not spec.input_pending(screen, line) and not PASTE_MARKER.search(above):
            return {"sent": len(line)}
    raise OpError("message typed but not submitted. Check the session's terminal before sending it again, so it isn't "
                  "sent twice.")


def _provider(provider: str):
    try:
        return providers.get(provider)
    except providers.CompileError:
        raise OpError(f"{provider!r} isn't an agent CLI this machine knows.") from None


def transcript_messages(provider: str, native_id: str, stamp: str | None) -> dict:
    """A session's conversation as plain messages, for history search; just {"unchanged": true} when its file has the
    same stamp (mtime and size) as when last read."""
    if not NATIVE_ID.match(native_id):
        raise OpError("Rynhult sent an invalid conversation ID.")
    spec = _provider(provider)
    path = spec.messages_source(Path.home(), native_id)
    if path is None:
        return {"found": False}
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):  # a link or a FIFO the agent put there: not read (the parsers check again)
        return {"found": False}
    current = f"{info.st_mtime_ns}:{info.st_size}"
    wal = path.with_name(path.name + "-wal")  # SQLite stores (Cursor): committed changes can sit in the WAL file
    try:
        extra = os.lstat(wal)
        current += f":{extra.st_mtime_ns}:{extra.st_size}"
    except FileNotFoundError:
        pass
    if current == stamp:
        return {"found": True, "unchanged": True, "stamp": current}
    return {"found": True, "stamp": current, "messages": spec.session_messages(path, native_id)}


def transcript_exists(provider: str, native_id: str) -> dict:
    if not NATIVE_ID.match(native_id or ""):
        raise OpError("Rynhult sent an invalid conversation ID.")
    found = _provider(provider).transcript(Path.home(), native_id)
    return {"exists": found is not None, "path": str(found) if found else None}


def trust_workspace(config: BrokerConfig, path: str) -> dict:
    """Record folder trust for every CLI this account has (qualification F1). Only after a person confirms in Rynhult."""
    workspace = str(_under_roots(config, path))
    installed = installed_providers(config)
    for provider in providers.PROVIDERS.values():
        provider.trust(Path.home(), workspace, installed.get(provider.id))
    return {"trusted": workspace}


def installed_providers(config: BrokerConfig) -> dict:
    """Which providers' CLIs this account can run (id -> resolved path, or None)."""
    found = {}
    for provider_id in providers.ids():
        command = config.providers.get(provider_id, providers.get(provider_id).executable)
        found[provider_id] = shutil.which(command) if not os.path.isabs(command) else (command if os.access(command, os.X_OK) else None)
    return found


def claude_agents(config: BrokerConfig) -> list[dict]:
    result = subprocess.run([config.providers["claude"], "agents", "--json"],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise OpError(f"Claude Code couldn't list its agents: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout or "[]")


def claude_remote(pid: int) -> dict:
    """Remote Control session id from Claude's live registry (internal file: read defensively)."""
    path = Path.home() / ".claude/sessions" / f"{int(pid)}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"bridge_session_id": None, "native_id": None}
    return {"bridge_session_id": data.get("bridgeSessionId"), "native_id": data.get("sessionId"),
            "status": data.get("status")}


COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


RUNTIME_ROOT = Path("/run/user")


def _socket(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()


def compose_command() -> tuple[list[str], dict]:
    """This account's engine for project stacks. Rootless Docker (Debian/Ubuntu), as always; Podman where it is the
    account's engine instead (Fedora/openSUSE: its socket and no Docker one), through `podman compose` with a compose
    v2 provider, so ps, config and up behave the same (docs/LINUX-DISTROS.md). An explicit DOCKER_HOST is kept."""
    run = RUNTIME_ROOT / str(os.getuid())
    if "DOCKER_HOST" not in environment and not _socket(run / "docker.sock") and _socket(run / "podman/podman.sock"):
        return ["podman", "compose"], {**environment, "DOCKER_HOST": f"unix://{run}/podman/podman.sock"}
    return ["docker", "compose"], {**environment, "DOCKER_HOST": environment.get("DOCKER_HOST", f"unix://{run}/docker.sock")}


def _compose(config: BrokerConfig, path: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    workspace = _under_roots(config, path)
    if not any((workspace / name).is_file() for name in COMPOSE_FILES):
        raise OpError(f"There's no compose file in {workspace}. Add compose.yaml to run a stack.")
    command, env = compose_command()
    return subprocess.run([*command, *args], cwd=workspace, capture_output=True, text=True, timeout=timeout, env=env)


# What `docker compose` reads on its own besides COMPOSE_FILES (D4): override files and the interpolation .env.
COMPOSE_EXTRA = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml",
                 "docker-compose.override.yml", ".env")
COMPOSE_SHOWN = 256 * 1024  # bytes of one compose file kept for showing a change (.env is never shown)


def compose_digest(config: BrokerConfig, path: str) -> dict:
    """What a stack would run, so accd can tell whether it changed since an owner approved it (D4: the agent can edit
    these files, the stack runs as the owner). The digest covers each compose, override and .env file's bytes and
    the fully resolved `docker compose config` (includes, extends, env files, environment), which is hashed, not
    kept. Returns {digest, files: {name: text}, resolved: bool}; files leaves out .env and anything not UTF-8."""
    workspace = _under_roots(config, path)
    if not any((workspace / name).is_file() for name in COMPOSE_FILES):
        raise OpError(f"There's no compose file in {workspace}. Add compose.yaml to run a stack.")
    digest, files = hashlib.sha256(b"acc-compose-v1\0"), {}
    for name in COMPOSE_FILES + COMPOSE_EXTRA:
        target = workspace / name
        if not target.is_file():
            continue
        data = target.read_bytes()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
        if name != ".env" and len(data) <= COMPOSE_SHOWN:
            try:
                files[name] = data.decode()
            except UnicodeDecodeError:
                pass
    try:
        resolved = _compose(config, path, "config", "--format", "json", timeout=60)
    except (OSError, subprocess.SubprocessError):
        resolved = None
    ok = resolved is not None and resolved.returncode == 0
    digest.update(b"resolved\0" + (hashlib.sha256(resolved.stdout.encode()).digest() if ok else b"unresolved"))
    return {"digest": digest.hexdigest(), "files": files, "resolved": ok}


def compose_up(config: BrokerConfig, path: str, expect: str | None = None) -> dict:
    """`docker compose up -d` in a registered workspace, as the agent user's rootless Docker. With `expect`, only when
    the stack's compose_digest still is that (what an owner approved)."""
    if expect is not None:
        current = compose_digest(config, path)["digest"]
        if current != expect:
            raise OpError(f"The stack's compose files changed since they were approved. Review and approve them again "
                          f"(now {current[:12]}, approved {expect[:12]}).")
    result = _compose(config, path, "up", "-d", "--remove-orphans")
    if result.returncode:
        raise OpError(f"The stack didn't start (docker compose up): {(result.stderr or result.stdout).strip()[-400:]}")
    return {"ok": True}


def compose_ps(config: BrokerConfig, path: str) -> list[dict]:
    result = _compose(config, path, "ps", "--all", "--format", "json", timeout=30)
    if result.returncode:
        raise OpError(f"Rynhult couldn't read the stack's status (docker compose ps): {result.stderr.strip()[-300:]}")
    rows = []
    for line in result.stdout.splitlines():
        if line.strip():
            item = json.loads(line)
            rows.append({k: item.get(k) for k in ("Service", "Name", "State", "Status", "Health", "Publishers")})
    return rows


def sync_codex_skills(config: BrokerConfig, skills: list[str]) -> dict:
    """Link catalog skills into ~/.codex/skills/acc-<id> so Codex can discover them.

    Sessions hide the ones they did not select (skills.config). Not ~/.agents/skills: Copilot CLI reads that folder
    too ("Personal skills") and has no per-session way to hide one, so every Copilot session saw every catalog skill.
    Links left there by older versions are removed. Only acc-* links are managed; anything else is left alone.
    """
    root = Path.home() / ".codex" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    legacy = Path.home() / ".agents" / "skills"
    wanted = {}
    for skill in skills:
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", skill):
            raise OpError(f"{skill!r} isn't a valid skill name.")
        from acc.server.catalog import restored_root
        # The broker runs as the agent, so never read Rynhult's private catalog metadata (MCP secrets).
        restored = restored_root(config.catalog_local_dir)
        candidates = [config.catalog_local_dir / "skills" / skill]
        if restored:
            candidates.append(restored / "skills" / skill)
        candidates.append(config.catalog_dir / "skills" / skill)
        source = next((p for p in candidates if (p / "SKILL.md").is_file()), None)
        if source is None:
            raise OpError(f"The skill {skill!r} isn't in the catalog on this machine. Remove it from the session or "
                          "restore the catalog.")
        wanted[f"acc-{skill}"] = source
    removed, linked = [], []
    for entry in root.iterdir():
        if entry.name.startswith("acc-") and entry.is_symlink() and entry.name not in wanted:
            entry.unlink()
            removed.append(entry.name)
    for entry in legacy.iterdir() if legacy.is_dir() else ():
        if entry.name.startswith("acc-") and entry.is_symlink():
            entry.unlink()
            removed.append(f"{legacy}/{entry.name}")
    for name, source in wanted.items():
        link = root / name
        if link.is_symlink() and Path(os.readlink(link)) == source:
            continue
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise OpError(f"{link} exists and is not a Rynhult link. Move it away so Rynhult can link the skill there.")
        link.symlink_to(source)
        linked.append(name)
    return {"linked": linked, "removed": removed}


def codex_plugins() -> dict:
    """Installed Codex plugins (name@marketplace) and the SKILL.md paths they contribute.

    Per-session `plugins.<id>.enabled=false` is ignored by the interactive TUI (it runs through the shared
    app-server daemon), but hiding skills by path works everywhere, so callers use both (M7 qualification).
    """
    cache = Path.home() / ".codex/plugins/cache"
    plugins, skill_paths = [], []
    if cache.is_dir():
        for marketplace in sorted(p for p in cache.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for plugin in sorted(p for p in marketplace.iterdir() if p.is_dir() and not p.name.startswith(".")):
                if re.match(r"^[A-Za-z0-9._-]+$", plugin.name) and re.match(r"^[A-Za-z0-9._-]+$", marketplace.name):
                    plugins.append(f"{plugin.name}@{marketplace.name}")
                    skill_paths += sorted(str(path) for path in plugin.glob("*/skills/*/SKILL.md"))
    return {"plugins": plugins, "skill_paths": skill_paths}


REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


WIKI_DIR = "docs/wiki"
WIKI_MAX_PAGES = 1000
WIKI_ATTACHMENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                         ".webp": "image/webp", ".svg": "image/svg+xml"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


WIKI_ORDER_BYTES = 64 * 1024


def _parts(rel: str) -> list[str] | None:
    """rel's path components ("" and "." dropped), or None when one is ".." or the path is empty."""
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    return parts if parts and ".." not in parts else None


def _open_dir(workspace: Path, parts: list[str]) -> int:
    """A descriptor of workspace/parts, opened one component at a time from the workspace without following a
    symlink at any level: the agent owns the tree and can swap a folder for a link between a check and a use, so
    every later open goes through this descriptor instead of a path."""
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_file_at(dir_fd: int, name: str) -> int:
    """A descriptor of the regular file `name` in dir_fd. Never follows a link and never blocks on a FIFO or device;
    the type is checked on the descriptor itself, so a swap after a listing changes nothing."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IsADirectoryError(name)  # any OSError: callers treat it as "no such file"
    return fd


def _read_at(dir_fd: int, name: str, limit: int) -> bytes:
    """At most limit + 1 bytes of the regular file `name` in dir_fd (more than limit means too large); see
    _open_file_at."""
    fd = _open_file_at(dir_fd, name)
    try:
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_beneath(workspace: Path, parts: list[str], limit: int, missing: str, too_large: str) -> bytes:
    """workspace/parts read through _open_dir and _read_at; OpError(missing) when it can't be, OpError(too_large)
    when it holds more than limit bytes."""
    try:
        dir_fd = _open_dir(workspace, parts[:-1])
        try:
            data = _read_at(dir_fd, parts[-1], limit)
        finally:
            os.close(dir_fd)
    except OSError:
        raise OpError(missing) from None
    if len(data) > limit:
        raise OpError(too_large)
    return data


def wiki_tree(config: BrokerConfig, path: str) -> dict:
    """The wiki's table of contents, Azure DevOps "publish code as wiki" rules (docs/wiki in the repo):
    a page is Name.md (title: hyphens shown as spaces); subpages live in a peer folder Name/; each folder's
    .order lists page names in order, the rest follow alphabetically; the first root page is the home page;
    a folder without a peer .md still shows as a parent; images live in /.attachments.
    Folders are walked by descriptor and links are never followed (see _open_dir)."""
    try:
        root_fd = _open_dir(_under_roots(config, path), WIKI_DIR.split("/"))
    except (OpError, OSError):
        return {"exists": False, "pages": []}
    count = 0

    def walk(fd: int, prefix: str, depth: int) -> list[dict]:
        nonlocal count
        if depth > 8:
            return []
        pages, folders = {}, set()
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    pages[entry.name[:-3]] = prefix + entry.name
                elif entry.is_dir(follow_symlinks=False):
                    folders.add(entry.name)
        names = sorted(set(pages) | folders, key=str.lower)
        try:
            order = _read_at(fd, ".order", WIKI_ORDER_BYTES)
        except OSError:
            order = b""
        if order and len(order) <= WIKI_ORDER_BYTES:
            listed = [line.strip() for line in order.decode(errors="replace").splitlines() if line.strip()]
            listed = [n for n in dict.fromkeys(listed) if n in names]
            names = listed + [n for n in names if n not in listed]
        nodes = []
        for name in names:
            count += 1
            if count > WIKI_MAX_PAGES:
                break
            children = []
            if name in folders:
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:  # replaced since it was listed
                    child = None
                if child is not None:
                    try:
                        children = walk(child, f"{prefix}{name}/", depth + 1)
                    finally:
                        os.close(child)
            nodes.append({"name": name, "title": name.replace("-", " "), "path": pages.get(name),
                          "children": children})
        return nodes

    try:
        return {"exists": True, "pages": walk(root_fd, "", 0)}
    finally:
        os.close(root_fd)


def wiki_page(config: BrokerConfig, path: str, rel: str) -> dict:
    workspace = _under_roots(config, path)
    parts = _parts(rel)
    if not rel.endswith(".md") or parts is None:
        raise OpError("no such wiki page")
    content = _read_beneath(workspace, [*WIKI_DIR.split("/"), *parts], MAX_DOC_BYTES,
                            "no such wiki page", "page too large to show")
    target = workspace / WIKI_DIR / "/".join(parts)
    log = subprocess.run([*_git_argv(workspace), "log", "-1", "--format=%h%x09%cI%x09%an", "--", str(target)],
                         capture_output=True, text=True, timeout=10).stdout.strip().split("\t")
    return {"path": rel, "content": content.decode(errors="replace"),
            "commit": log[0] if len(log) == 3 else None, "committed_at": log[1] if len(log) == 3 else None,
            "author": log[2] if len(log) == 3 else None}


def wiki_attachment(config: BrokerConfig, path: str, rel: str) -> dict:
    """An image from the wiki's .attachments folder, base64 (images only, size-limited)."""
    workspace = _under_roots(config, path)
    parts = _parts(rel)
    refused = "only images in the wiki's .attachments folder can be shown"
    if parts is None or len(parts) < 2 or parts[0] != ".attachments":
        raise OpError(refused)
    kind = WIKI_ATTACHMENT_TYPES.get(Path(parts[-1]).suffix.lower())
    if kind is None:
        raise OpError(refused)
    data = _read_beneath(workspace, [*WIKI_DIR.split("/"), *parts], MAX_ATTACHMENT_BYTES, refused,
                         "attachment too large")
    return {"content_type": kind, "data": base64.b64encode(data).decode()}


ARTIFACTS_DIR = ".acc/artifacts"  # where an agent leaves screenshots, logs and reports for its reviewer
ARTIFACT_KINDS = {**{ext: "image" for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")},
                  **{ext: "video" for ext in (".mp4", ".webm")},
                  **{ext: "text" for ext in (".txt", ".log", ".md", ".json", ".csv", ".xml", ".html")}}
ARTIFACT_LIMITS = {"image": 10 << 20, "video": 20 << 20, "text": 2 << 20}
MAX_ARTIFACTS = 200
MAX_ARTIFACT_VISITS = 2000  # folder entries a listing looks at, in all folders together
ARTIFACT_CHUNK = 4 << 20  # per broker reply: base64 of it stays well under the 16 MiB line limit


def artifact_kind(name: str) -> str | None:
    """"image", "video" or "text" from the name's extension; None for anything else (svg included: never shown)."""
    return ARTIFACT_KINDS.get(os.path.splitext(name)[1].lower())


def _artifact_name_ok(name: str) -> bool:
    return not name.startswith(".") and name.isprintable() and artifact_kind(name) is not None


def _artifact_config(config: BrokerConfig, path: str, helper_id: str | None) -> BrokerConfig:
    if helper_id is None:
        return config
    if not ACC_ID.fullmatch(helper_id) or path != str(config.run_dir / helper_id / "workspace"):
        raise OpError("bad explanation workspace")
    # Permit this one helper workspace, not its launch files, home or credentials.
    return replace(config, allowed_roots=(config.run_dir / helper_id / "workspace",))


def list_artifacts(config: BrokerConfig, path: str, helper_id: str | None = None) -> dict:
    """The files in the workspace's .acc/artifacts folder and its direct subfolders: regular files of an allowed
    kind only, at most MAX_ARTIFACTS. Walked by descriptor; a link is never followed at any level (see _open_dir)."""
    config = _artifact_config(config, path, helper_id)
    try:
        root_fd = _open_dir(_under_roots(config, path), ARTIFACTS_DIR.split("/"))
    except OSError:
        return {"files": [], "truncated": False}
    files: list[dict] = []
    truncated = False
    budget = MAX_ARTIFACT_VISITS  # entries looked at in all, whatever they are: a huge folder costs no more

    def scan(fd: int, prefix: str, depth: int) -> bool:
        """False once the listing must stop (enough files, or the visit budget is spent)."""
        nonlocal truncated, budget
        with os.scandir(fd) as iterator:
            entries = []
            for entry in iterator:
                if budget <= 0:
                    truncated = True
                    break
                budget -= 1
                entries.append(entry)
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.name.startswith(".") or not entry.name.isprintable():
                continue
            if depth == 0 and entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:  # replaced since it was listed
                    continue
                try:
                    if not scan(child, f"{entry.name}/", 1):
                        return False
                finally:
                    os.close(child)
                continue
            if not _artifact_name_ok(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if len(files) >= MAX_ARTIFACTS:
                truncated = True
                return False
            kind = artifact_kind(entry.name)
            files.append({"name": prefix + entry.name, "kind": kind, "size": info.st_size,
                          "mtime": int(info.st_mtime), "too_large": info.st_size > ARTIFACT_LIMITS[kind]})
        return not truncated

    try:
        scan(root_fd, "", 0)
    finally:
        os.close(root_fd)
    return {"files": files, "truncated": truncated}


def read_artifact(config: BrokerConfig, path: str, name: str, offset: int = 0, helper_id: str | None = None) -> dict:
    """Up to ARTIFACT_CHUNK bytes of one artifact from `offset`, base64, with its kind, size and mtime (the caller
    reads the rest in further calls and checks the file didn't change in between). Refused when the file is larger
    than its kind's limit."""
    workspace = _under_roots(_artifact_config(config, path, helper_id), path)
    parts = _parts(name)
    missing = "no such artifact"
    if (parts is None or len(parts) > 2 or not _artifact_name_ok(parts[-1])
            or any(p.startswith(".") or not p.isprintable() for p in parts[:-1])):
        raise OpError(missing)
    if not isinstance(offset, int) or offset < 0:
        raise OpError("bad offset")
    kind = artifact_kind(parts[-1])
    try:
        dir_fd = _open_dir(workspace, [*ARTIFACTS_DIR.split("/"), *parts[:-1]])
        try:
            fd = _open_file_at(dir_fd, parts[-1])
        finally:
            os.close(dir_fd)
    except OSError:
        raise OpError(missing) from None
    try:
        info = os.fstat(fd)
        if info.st_size > ARTIFACT_LIMITS[kind]:
            raise OpError(f"artifact too large to show ({kind} files up to {ARTIFACT_LIMITS[kind] >> 20} MiB)")
        data = os.pread(fd, min(ARTIFACT_CHUNK, max(0, info.st_size - offset)), offset)
    finally:
        os.close(fd)
    return {"kind": kind, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "offset": offset,
            "data": base64.b64encode(data).decode()}


FOLDER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def git_identity(config: BrokerConfig, path: str, name: str | None = None, email: str | None = None,
                 replace: bool = False) -> dict:
    """Read effective identity; fill missing keys or explicitly edit repository-local config only."""
    from ..maintenance import lease
    target = _under_roots(config, path)
    for key, value in (("name", name), ("email", email)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200
                                  or any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in value)):
            raise OpError("Enter a Git author name and email without control characters.")
    if email is not None and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email):
        raise OpError("Enter a valid Git author email address.")
    common = _git(target, "rev-parse", "--git-common-dir", env=_hardened_env(target))
    if common.returncode:
        return {"supported": False, "name": None, "email": None, "changed": []}
    admin = Path(common.stdout.strip())
    admin = _under_roots(config, str(admin if admin.is_absolute() else target / admin))
    with lease(admin / 'acc-git-identity.lock'):
        result = {"supported": True, "changed": []}
        for key, value in (("name", name), ("email", email)):
            # Config reads run no checkout programs. Honor effective local,
            # worktree, included and global settings before filling anything.
            current = _git(target, "config", "--get", "user." + key)
            if current.returncode not in (0, 1):
                raise OpError("Git couldn't read this repository's author settings.")
            existing = current.stdout.strip() if current.returncode == 0 else ""
            if value is not None and (replace or not existing) and existing != value:
                metadata = (admin / 'config').lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OpError("The repository Git config must be a regular file, not a link.")
                saved = _git(target, "config", "--local", "--replace-all", "--", "user." + key, value.strip())
                if saved.returncode:
                    raise OpError("Git couldn't save this repository's author settings.")
                result['changed'].append(key)
                existing = value.strip()
            result[key] = existing or None
        return result


def make_workspace(config: BrokerConfig, parent: str, name: str, title: str, project: bool = False) -> dict:
    """A new local workspace: a git-initialised folder (no remote), as the agent user.

    `parent` must be a workspace root or a directory under one (created if missing); `name` is one new folder.
    Reports go in docs/research and docs/council, as in any project (acc-workflow conventions). `project`: an empty
    project folder instead (a README and docs/handoffs only), with the README committed for immediate worktrees."""
    if not FOLDER.fullmatch(name or ""):
        raise OpError("Use a folder name of lowercase letters, digits and hyphens (up to 80), starting with a letter or digit.")
    base = Path(parent).resolve()
    if not any(base == root or root in base.parents for root in config.allowed_roots):
        raise OpError(f"{base} is outside this account's workspace folders. Choose a folder inside them.")
    base.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = base / name
    if target.exists():
        raise OpError(f"{target} already exists. Choose another name.")
    for sub in ("docs/handoffs",) if project else ("docs/research", "docs/council", "docs/handoffs"):
        (target / sub).mkdir(parents=True)
    title = " ".join(str(title).split())[:120] or name
    (target / "README.md").write_text(f"# {title}\n\nStarted from Rynhult. Session notes: docs/handoffs/.\n" if project else
                                      f"# {title}\n\nLocal workspace started from Rynhult (local git only). Findings: "
                                      f"docs/research/; council reports: docs/council/; session notes: docs/handoffs/.\n")
    result = subprocess.run(["git", "init", "-q", "-b", "main", str(target)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise OpError(f"git init failed: {result.stderr.strip()[-200:]}")
    if project:
        # Run inside the owning account's broker. Carry configured identity into
        # hardened Git, but never require signing or execute hooks/filters here.
        git_env = _hardened_env(target)
        identity = []
        for key, fallback in (("user.name", "Rynhult"), ("user.email", "acc@example.com")):
            configured = _git(target, "config", "--get", key, env=git_env)
            if configured.returncode or not configured.stdout.strip():
                identity.extend(("-c", f"{key}={fallback}"))
        staged = _git(target, "add", "--force", "--", "README.md", env=git_env)
        if staged.returncode:
            raise OpError(f"Rynhult couldn't stage the new project's README: {staged.stderr.strip()[:200]}")
        committed = _git(target, *identity, "commit", "--no-verify", "--no-gpg-sign", "--only",
                         "-m", "Initialize project", "--", "README.md", env=git_env)
        if committed.returncode:
            raise OpError(f"Rynhult couldn't make the project's first Git commit: {committed.stderr.strip()[:200]}")
    return {"path": str(target)}


def prepare_private_workspace(config: BrokerConfig, path: str) -> dict:
    """Validate local-only git and seed HEAD for session worktrees, without adding any remote."""
    target = _under_roots(config, path)
    if not target.is_dir() or target.is_symlink():
        raise OpError("That is not a workspace folder.")
    if not (target / ".git").exists():
        result = _git(target, "init", "-q", "-b", "main")
        if result.returncode:
            raise OpError("Rynhult couldn't set up local Git in this folder.")
    if _git_ok(target, "remote").strip():
        raise OpError("Development workspaces must have no git remotes; remove them before converting")
    if not git_head(target):
        # -c is read after the hardened GIT_CONFIG_* settings, so Rynhult's identity wins over the agent's.
        result = _git(target, "-c", "user.name=Rynhult", "-c", "user.email=acc@example.com", "commit", "--allow-empty",
                      "--no-verify", "-m", "Initialize private development workspace", env=_hardened_env(target))
        if result.returncode:
            raise OpError(f"Rynhult couldn't make the first Git commit in this folder: {result.stderr[:200]}")
    return {"path": str(target)}


def workspace_identity(config: BrokerConfig, path: str) -> dict:
    """Identify a local workspace as its owning account, without granting path access.

    Ordinary project registration remains declarative. Aliases below an account
    workspace root get canonicalized; no existence or directory listing is returned.
    """
    candidate = Path(path).resolve()
    for root in config.allowed_roots:
        if root.resolve() in candidate.parents:
            return {"path": str(candidate)}
    return {"path": path}


def delete_workspace(config: BrokerConfig, parent: str, name: str) -> dict:
    """Delete one local workspace folder and everything in it (the owner confirmed it in the dashboard).

    Only a folder directly inside `<workspace root>/local` qualifies: never a project checkout, a workspace root
    or anything a symlink points at. rmtree never follows symlinks inside the folder."""
    if not FOLDER.fullmatch(name or ""):
        raise OpError("Use a folder name of lowercase letters, digits and hyphens (up to 80), starting with a letter or digit.")
    base = Path(parent)
    if base.is_symlink() or base.name != "local" or not any(base.resolve() == root / "local" for root in config.allowed_roots):
        raise OpError(f"{parent} is not a local workspaces folder; Rynhult only deletes local workspace folders.")
    target = base.resolve() / name
    try:
        info = target.lstat()  # a dangling symlink is not an already-removed folder
    except FileNotFoundError:
        return {"deleted": str(target), "already_removed": True}
    except OSError as error:
        raise OpError("The agent account cannot inspect this folder. Ask the owner to repair its access before retrying.") from error
    if not stat.S_ISDIR(info.st_mode):
        raise OpError(f"{target} is not a workspace folder.")
    try:
        shutil.rmtree(target)
    except OSError as error:
        # Another cleanup may have removed it after lstat. Do not mistake a
        # missing child inside a still-present folder for successful removal.
        if isinstance(error, FileNotFoundError):
            try:
                target.lstat()
            except FileNotFoundError:
                return {"deleted": str(target), "already_removed": True}
            except OSError:
                pass
        raise OpError("The agent account could not remove this folder. Ask the owner to remove it using its original account, then retry to remove it from the list.") from error
    return {"deleted": str(target), "already_removed": False}


def push_workspace(config: BrokerConfig, path: str, repo: str) -> dict:
    """Promote a local workspace: commit what is there and push it to its new (empty) GitHub repository.

    Runs as the agent user, whose git authenticates through `gh` (same as agents' own pushes). Staging and the commit
    run hardened (_hardened_env: the folder's config can't run a program). The push needs the agent's global config
    for that sign-in, so it only turns hooks and fsmonitor off."""
    if not REPO.fullmatch(repo):
        raise OpError("Use a GitHub repository name like owner/name.")
    target = _under_roots(config, path)
    def git(*args: str, check: bool = True, hardened: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = _hardened_env(target) if hardened else env
        result = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=300, env=env)
        if check and result.returncode:
            raise OpError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[-300:]}")
        return result
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode:
        git("commit", "-q", "--no-verify", "-m", "Local workspace promoted to a project")
    elif git("rev-parse", "--verify", "HEAD", check=False).returncode:
        raise OpError("The workspace has no files to commit yet. Add a file, then try again.")
    git("branch", "-M", "main")
    url = f"{config.git_remote_base}/{repo}.git"
    # The remote's own transport only, never ext:: through the folder's url.*.insteadOf (security review 10, SR10-3).
    transport = "file" if config.git_remote_base.startswith("/") else "https:ssh"
    if git("remote", "get-url", "origin", check=False).returncode:
        git("remote", "add", "origin", url)
    else:
        git("remote", "set-url", "origin", url)
    git("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.alternateRefsCommand=true", "push",
        "-q", "--no-verify", "-u", "origin", "main", hardened=False,
        env={**os.environ, "GIT_ALLOW_PROTOCOL": transport})
    return {"path": str(target), "repo": repo, "head": git("rev-parse", "--short", "HEAD").stdout.strip()}


def clone_repo(config: BrokerConfig, repo: str, dest: str) -> dict:
    """`gh repo clone` into a new directory under the workspace roots, as the agent user."""
    if not REPO.fullmatch(repo):
        raise OpError("Use a GitHub repository name like owner/name.")
    target = Path(dest).resolve()
    if not any(root in target.parents for root in config.allowed_roots):
        raise OpError(f"{target} is outside this account's workspace folders. Choose a folder inside them.")
    if target.exists():
        raise OpError(f"{target} already exists. Choose another name.")
    result = subprocess.run(["gh", "repo", "clone", repo, str(target), "--", "-q"], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise OpError("The clone failed. Check that this machine's agent account can reach the repository, and that the "
                      "folder name is free.")
    return {"path": str(target)}


# A repository a person can reach: HTTPS without credentials, query or fragment, or git over SSH (git@host:path,
# ssh://git@host[:port]/path). Never file://, ext:: or a local path, which would read this machine's own folders.
CLONE_URL = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d{1,5})?/|git@[A-Za-z0-9.-]+:|ssh://git@[A-Za-z0-9.-]+(:\d{1,5})?/)"
                       r"[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*/?$")


def clone_url(config: BrokerConfig, url: str, dest: str) -> dict:
    """`git clone` of a repository URL the agent user can reach (its own SSH key or credential helper) into a new
    directory under the workspace roots. Only HTTPS and SSH transports; it never prompts for a password."""
    if not isinstance(url, str) or len(url) > 500 or not CLONE_URL.fullmatch(url) or "/../" in url or url.endswith("/.."):
        raise OpError("Use an HTTPS or SSH repository address without a password.")
    target = Path(dest).resolve()
    if not any(root in target.parents for root in config.allowed_roots):
        raise OpError(f"{target} is outside this account's workspace folders. Choose a folder inside them.")
    if target.exists():
        raise OpError(f"{target} already exists. Choose another name.")
    env = {**environment, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    argv = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
            "clone", "-q", "--no-recurse-submodules", "--", url, str(target)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise OpError("The clone took longer than 10 minutes and was stopped. Try again, or clone it in a session.") from None
    if result.returncode:
        shutil.rmtree(target, ignore_errors=True)
        raise OpError("The clone failed. Check that this machine's agent account can reach the repository, and that the "
                      "folder name is free.")
    return {"path": str(target)}


GITHUB_PATH = re.compile(r"^(graphql|repos/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}(/[A-Za-z0-9._%:=&?,-]+)*)$")
GITHUB_METHODS = ("GET", "POST", "PATCH", "PUT")
GITHUB_MAX_BYTES = 4 * 1024 * 1024


def github_api(config: BrokerConfig, method: str, path: str, body: dict | None = None, tail: int = 0) -> dict:
    """One GitHub API call with this account's own `gh` login (a person's pull request actions in Rynhult): the token never
    leaves the account. Repository paths and GraphQL only. `tail`: keep the last `tail` bytes of a text body (job logs)."""
    if method not in GITHUB_METHODS or not isinstance(path, str) or not GITHUB_PATH.match(path) or ".." in path:
        raise OpError("Rynhult won't send that GitHub request: only repository and GraphQL requests are allowed.")
    argv = [config.providers.get("gh", "gh"), "api", "--include", "--method", method, path]
    if body is not None:
        argv += ["--input", "-"]
    try:
        result = subprocess.run(argv, input=json.dumps(body).encode() if body is not None else None,
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OpError(f"The GitHub CLI (gh) didn't answer ({type(error).__name__}).") from None
    out = result.stdout[:GITHUB_MAX_BYTES] if not tail else result.stdout
    head, _, payload = out.replace(b"\r\n", b"\n").partition(b"\n\n")
    status = re.match(rb"HTTP/[\d.]+ (\d{3})", head)
    if status is None:  # gh failed before any response (not signed in, no network)
        raise OpError(f"The GitHub CLI (gh) got no answer from GitHub. Check that it is signed in and online: "
                      f"{result.stderr.decode(errors='replace').strip()[-300:] or 'no response'}")
    if tail:
        return {"status": int(status.group(1)), "text": payload[-tail:].decode(errors="replace")}
    try:
        data = json.loads(payload) if payload.strip() else None
    except ValueError:
        data = payload.decode(errors="replace")[-2000:]
    return {"status": int(status.group(1)), "body": data}


DOC_KINDS = {"handoffs": "docs/handoffs", "decisions": "docs/decisions", "council": "docs/council",
             "research": "docs/research", "design": "docs/design"}
MAX_DOC_BYTES = 512 * 1024


def scan_docs(config: BrokerConfig, path: str) -> dict:
    """Knowledge files by kind with their last commit (pointers only; git stays the source of truth).
    Folders are opened by descriptor without following links (see _open_dir); symlinked files are skipped."""
    workspace = _under_roots(config, path)
    result: dict[str, list[dict]] = {}
    for kind, rel in DOC_KINDS.items():
        entries = []
        try:
            dir_fd = _open_dir(workspace, rel.split("/"))
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                with os.scandir(dir_fd) as listing:
                    names = sorted((e for e in listing if e.name.endswith(".md")), key=lambda e: e.name, reverse=True)
                for entry in names[:100]:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    file = workspace / rel / entry.name
                    log = subprocess.run([*_git_argv(workspace), "log", "-1", "--format=%h%x09%cI%x09%s", "--", str(file)],
                                         capture_output=True, text=True, timeout=10).stdout.strip().split("\t")
                    entries.append({"path": f"{rel}/{entry.name}", "bytes": info.st_size,
                                    "modified": info.st_mtime, "commit": log[0] if len(log) == 3 else None,
                                    "committed_at": log[1] if len(log) == 3 else None, "subject": log[2] if len(log) == 3 else None})
            finally:
                os.close(dir_fd)
        result[kind] = entries
    return result


def read_doc(config: BrokerConfig, path: str, rel: str) -> dict:
    """Read one Markdown file from a workspace's docs/ tree (read-only, size-limited, links never followed)."""
    workspace = _under_roots(config, path)
    parts = _parts(rel)
    refused = "only Markdown files under docs/ can be read"
    if not rel.endswith(".md") or parts is None or len(parts) < 2 or parts[0] != "docs":
        raise OpError(refused)
    content = _read_beneath(workspace, parts, MAX_DOC_BYTES, refused, "file too large to show")
    return {"path": rel, "content": content.decode(errors="replace")}


# ---------------------------------------------------------------- git backup to the main Rynhult (docs/MULTIHOST.md)

BACKUP_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~-]+)*/api/git/[0-9]{1,12}\.git$")
BACKUP_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
BACKUP_REFS = ("refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")  # branches and tags; never deletions
BACKUP_TIMEOUT = 1800
MAX_CA_PEM = 64 * 1024


def _backup_refs(cwd: Path) -> dict[str, str]:
    """The checkout's branches and tags: {ref name: object id}. OpError outside git."""
    out = _git_ok(cwd, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads", "refs/tags")
    return dict(line.split("\0", 1) for line in out.splitlines() if "\0" in line)


def _refs_digest(refs: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{name} {sha}" for name, sha in sorted(refs.items())).encode()).hexdigest()


def git_refs(config: BrokerConfig, path: str) -> dict:
    """What a backup would push, in brief: whether the folder is a git repository, and a digest of its branches and
    tags (accd backs up only when it changed)."""
    cwd = _under_roots(config, path)
    if _git(cwd, "rev-parse", "--git-dir").returncode:
        return {"git": False}
    refs = _backup_refs(cwd)
    return {"git": True, "digest": _refs_digest(refs), "branches": sum(r.startswith("refs/heads/") for r in refs),
            "tags": sum(r.startswith("refs/tags/") for r in refs)}


def backup_env(token: str, ca_file: str | None = None, repo: str | None = None, url: str | None = None) -> dict[str, str]:
    """git's environment for a backup push. The token travels only here, as configuration (GIT_CONFIG_COUNT/KEY/VALUE
    have command-line precedence), never in argv, a file or a remote's URL. No global or system config, no hooks, no
    credential helpers, no prompts, and http(s) only (a repository's insteadOf can't turn the push into ext::)."""
    config = [("http.extraHeader", f"Authorization: Bearer {token}"), ("core.hooksPath", "/dev/null"),
              ("credential.helper", ""), ("http.followRedirects", "false")]
    if url:
        # URL matching happens after insteadOf resolution. A repository rewrite
        # must not carry our bearer header to its replacement destination.
        config[0] = ('http.extraHeader', '')
        config += [(f'http.{url}.extraHeader', f'Authorization: Bearer {token}'),
                   ('http.sslVerify', 'true'), ('http.proxy', '')]
    # git prefers the longest matching http.<url> key, so a repository's own http.<url>/git-receive-pack.* would beat
    # ours: set each protection at every path the push requests, where nothing can be longer (security review 7, SR7-3).
    paths = [f'http.{url}{suffix}' for suffix in ('', '/info/refs', '/git-receive-pack')] if url else []
    protected = [('sslVerify', 'true'), ('proxy', ''), ('followRedirects', 'false')] if url else []
    if ca_file:
        config.append(("http.sslCAInfo", ca_file))
        protected.append(('sslCAInfo', ca_file))
    config += [(f'{path}.{key}', value) for path in paths for key, value in protected]
    if repo:  # project folders belong to their owner, not the agent account: trust this one folder (only), as the
        config.append(("safe.directory", repo))  # agent's global config (left out above) would
    env = {"PATH": environment.get("PATH", "/usr/bin:/bin"), "HOME": environment.get("HOME", "/nonexistent"),
           "LANG": "C", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "http:https", "GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def _porcelain(stdout: str) -> list[tuple[str, str, str]]:
    """`git push --porcelain` lines: (flag, destination ref, summary)."""
    rows = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and len(parts[0]) == 1 and ":" in parts[1]:
            rows.append((parts[0], parts[1].rsplit(":", 1)[1], parts[2]))
    return rows


def git_backup(config: BrokerConfig, path: str, url: str, token: str, ca_pem: str | None = None) -> dict:
    """Push every branch and tag of a project's checkout to its backup repository on the main Rynhult, as this account.

    The backup refuses rewrites and deletions. A branch or tag it refuses (rewritten upstream, or moved) is pushed
    again as backup/<UTC time>/<name>, so the rewritten history is kept too and the result says so ("kept")."""
    cwd = _under_roots(config, path)
    if not BACKUP_URL.match(url or ""):
        raise OpError("The backup address from the main Rynhult isn't valid.")
    if not BACKUP_TOKEN.match(token or ""):
        raise OpError("The backup key from the main Rynhult isn't valid.")
    if ca_pem is not None and (len(ca_pem) > MAX_CA_PEM or not ca_pem.startswith("-----BEGIN CERTIFICATE-----")):
        raise OpError("The main Rynhult's certificate isn't valid.")
    if _git(cwd, "rev-parse", "--git-dir").returncode:
        raise OpError(f"{cwd} is not a git repository, so there is nothing to back up.")
    refs = _backup_refs(cwd)
    summary = {"branches": sum(r.startswith("refs/heads/") for r in refs),
               "tags": sum(r.startswith("refs/tags/") for r in refs), "digest": _refs_digest(refs),
               "updated": [], "kept": []}
    head = _git(cwd, "symbolic-ref", "-q", "HEAD").stdout.strip()
    if head in refs:
        summary["head"] = head  # what a clone of the backup checks out
    if not refs:
        return {**summary, "note": "no commits yet"}
    with tempfile.TemporaryDirectory(prefix="acc-backup-") as scratch:
        ca_file = None
        if ca_pem:
            ca_file = os.path.join(scratch, "ca.pem")
            Path(ca_file).write_text(ca_pem)
        env = backup_env(token, ca_file, str(cwd), url=url)

        def push(*refspecs: str) -> subprocess.CompletedProcess:
            return subprocess.run([*_git_argv(cwd), "push", "--porcelain", "--no-verify", url, *refspecs],
                                  capture_output=True, text=True, timeout=BACKUP_TIMEOUT, env=env)

        result = push(*BACKUP_REFS)
        if result.returncode and "The requested URL returned error: 503" in result.stderr:
            return {"retry": True}  # do not advance the backed-up digest on temporary HTTP unavailability
        rows = _porcelain(result.stdout)
        if result.returncode and not any(flag == "!" for flag, _, _ in rows):
            raise OpError(f"Git couldn't send the backup to the main Rynhult: {(result.stderr or result.stdout).strip()[-300:]}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        refused = []
        for flag, ref, note in rows:
            if flag in (" ", "+", "*"):
                summary["updated"].append(ref)
            elif flag == "!" and ref in refs:
                kind, _, name = ref.removeprefix("refs/").partition("/")
                refused.append((ref, f"refs/{kind}/backup/{stamp}/{name}", note.strip()[:120]))
        for ref, kept_as, reason in refused:
            again = push(f"{refs[ref]}:{kept_as}")
            if again.returncode and "The requested URL returned error: 503" in again.stderr:
                return {"retry": True}
            if again.returncode:
                raise OpError(f"{ref} was refused ({reason}) and could not be kept as {kept_as}: "
                              f"{(again.stderr or again.stdout).strip()[-200:]}")
            summary["kept"].append({"ref": ref, "as": kept_as, "reason": reason})
    return summary


def team_reply(config: BrokerConfig, acc_id: str, request_id: str, result: dict) -> dict:
    """Write as the agent account, atomically, without following session-controlled links."""
    if not ACC_ID.fullmatch(acc_id) or not re.fullmatch('[0-9a-f]{32}', request_id):
        raise OpError('bad team reply id')
    return _team_write(config, acc_id, request_id + '.json', json.dumps(result).encode())


def _team_write(config: BrokerConfig, acc_id: str, filename: str, contents: bytes) -> dict:
    """Internal writer: callers validate IDs and generate filenames, never paths."""
    root = os.open(config.run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    session = folder = None
    scratch = filename + '.new'
    try:
        session = os.open(acc_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            os.mkdir('team', mode=0o700, dir_fd=session)
        except FileExistsError:
            pass
        folder = os.open('team', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=session)
        try:
            fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        except FileExistsError:
            os.unlink(scratch, dir_fd=folder)
            fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(scratch, filename, src_dir_fd=folder, dst_dir_fd=folder)
    finally:
        for fd in (folder, session, root):
            if fd is not None:
                os.close(fd)
    return {'written': True}


def _merge_bundle(branch: str) -> str:
    """One bundle name per helper branch (BRANCH-checked, with an optional private /work suffix), so preparing again replaces it."""
    return 'merge-' + branch.removeprefix('acc/').replace('/', '_') + '.bundle'


def team_prepare_merge(config: BrokerConfig, path: str, branch: str, acc_id: str) -> dict:
    """Export a stopped container helper's committed branch; writes/merge happen in the lead container."""
    from .container_git import snapshot

    cwd = _under_roots(config, path)
    if not ACC_ID.fullmatch(acc_id) or not BRANCH.fullmatch(branch) or not containers.is_container_checkout(cwd):
        raise OpError('invalid container team merge')
    with snapshot(cwd) as admin:
        if containers._git(admin, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != branch:
            raise OpError('team branch changed')
        commit = containers._git(admin, 'rev-parse', 'HEAD').stdout.strip()
        # Streamed and capped in memory: an oversized branch never reaches the disk.
        try:
            data = containers._git_output(admin, TEAM_BUNDLE_LIMIT, 'bundle', 'create', '-', branch)
        except containers.ContainerError:
            raise OpError('cannot bundle the team branch') from None
    if data is None:
        raise OpError('team branch bundle exceeds 8 MiB')
    filename = _merge_bundle(branch)
    _team_write(config, acc_id, filename, data)
    return {'prepared': True, 'merged': False, 'branch': branch, 'commit': commit,
            'bundle': str(config.run_dir / acc_id / 'team' / filename),
            'instructions': 'Inside your container, git fetch the bundle and its branch, then git merge FETCH_HEAD. '
                            'Resolve or abort conflicts there. Call finish only after verifying the merge.'}


def team_clear_bundles(config: BrokerConfig, acc_id: str, branch: str | None = None) -> dict:
    """Remove merge bundles from a session's team folder: one helper branch's, or (archiving it) all of them.
    Only names this module writes; links are removed, never followed."""
    if not ACC_ID.fullmatch(acc_id or '') or branch is not None and not BRANCH.fullmatch(branch):
        raise OpError('bad team bundle id')
    root = os.open(config.run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    session = folder = None
    removed = []
    try:
        try:
            session = os.open(acc_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            folder = os.open('team', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=session)
        except FileNotFoundError:
            return {'removed': []}
        if branch is None:
            names = [n for n in os.listdir(folder) if re.fullmatch(r'merge-[a-z0-9-]+(?:_work)?\.bundle(\.new)?', n)]
        else:
            names = [_merge_bundle(branch), _merge_bundle(branch) + '.new']
        for name in names:
            try:
                os.unlink(name, dir_fd=folder)
                removed.append(name)
            except (FileNotFoundError, IsADirectoryError):
                pass
    finally:
        for fd in (folder, session, root):
            if fd is not None:
                os.close(fd)
    return {'removed': removed}


def team_merge(config: BrokerConfig, path: str, branch: str, expected_branch: str, expected_head: str | None = None, expected_base: dict | None = None, report_heads: list[str] | None = None) -> dict:
    """Merge only a committed helper into the caller's original topic branch; abort any conflict."""
    cwd = _under_roots(config, path)
    if report_heads is not None and (not isinstance(report_heads, list) or len(report_heads) > 4000 or
            any(not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40,64}', head) for head in report_heads)):
        raise OpError('invalid attempt report heads')
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or not isinstance(expected_branch, str) or not BRANCH.fullmatch(expected_branch):
        raise OpError('team merge requires Rynhult topic branches')

    def git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return _hardened_git(cwd, *args, timeout=timeout)

    def git_ok(*args: str) -> str:
        return _git_ok(cwd, *args, env=_hardened_env(cwd))

    actual = git_ok('branch', '--show-current').strip()
    if actual != expected_branch:
        raise OpError('caller branch changed; return to the original team branch')
    if git_ok('status', '--porcelain').strip():
        raise OpError('commit or discard caller changes before merging')
    for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        found = git_ok('rev-parse', '--git-path', state).strip()
        if (cwd / found).exists():
            raise OpError('caller already has a git operation in progress')
    source = git_ok('rev-parse', '--verify', f'refs/heads/{branch}^{{commit}}').strip()
    if expected_head is not None and (not isinstance(expected_head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected_head) or source != expected_head):
        raise OpError('helper head changed after verification; inspect and re-gate this revision')
    if expected_base is not None:
        from .gate_ops import _base_ref
        if not isinstance(expected_base, dict) or set(expected_base) != {'branch', 'sha'}:
            raise OpError('invalid verification base')
        base = _base_ref(cwd, expected_base['branch'])
        if git_ok('rev-parse', '--verify', f'refs/heads/{base}^{{commit}}').strip() != expected_base['sha']:
            raise OpError('gate base moved after verification; queue a fresh gate')
    before = git_head(cwd)
    try:
        result = git('merge', '--no-edit', '--no-autostash', '--no-stat', '--no-verify', '--', source, timeout=120)
        if not result.returncode:
            return {'merged': True, 'commit': git_head(cwd), 'conflicts': [],
                    **({'included_heads':[head for head in report_heads if git('merge-base', '--is-ancestor', head, 'HEAD').returncode == 0]} if report_heads else {}),
                    **({'note': LFS_NOTE} if _uses_lfs(cwd, 'HEAD') else {})}
        conflicts = git('diff', '--no-ext-diff', '--no-textconv', '--name-only', '--diff-filter=U').stdout.splitlines()
        git('merge', '--abort')
        if git_head(cwd) != before or git_ok('status', '--porcelain').strip():
            raise OpError('merge abort did not restore the caller; manual recovery required')
        return {'merged': False, 'conflicts': conflicts, 'error': result.stderr.strip()[:1000]}
    except subprocess.TimeoutExpired:
        git('merge', '--abort')
        raise OpError('merge timed out and was aborted') from None


TEAM_BUNDLE_LIMIT = 8 * 1024 * 1024
LFS_NOTE = "LFS files arrive as pointers; run git lfs checkout"


def _uses_lfs(cwd: Path, rev: str) -> bool:
    """Whether a committed .gitattributes at `rev` sends files through Git LFS. Team merges run no filters
    (_hardened_env), so those files arrive as LFS pointers. Reads blobs only: no checkout, no filter."""
    found = _git(cwd, "grep", "-q", "--no-textconv", "-I", "-E", r"filter=lfs([[:space:]]|$)", rev, "--",
                 ":(glob)**/.gitattributes")
    return found.returncode == 0


def team_bundle(config: BrokerConfig, path: str, branch: str, base_commit: str | None = None) -> dict:
    """A bounded git bundle, transported by accd. Never a remote URL or agent-held credential."""
    import tempfile
    cwd = _under_roots(config, path)
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
        raise OpError('team bundle requires a Rynhult branch')
    if base_commit is not None:
        if not isinstance(base_commit, str) or not re.fullmatch('[0-9a-f]{40,64}', base_commit):
            raise OpError('invalid continuation commit')
        commit = _git_ok(cwd, 'rev-parse', '--verify', branch + '^{commit}').strip()
        if commit != base_commit:
            raise OpError('continuation branch moved')
    else:
        if _git_ok(cwd, 'branch', '--show-current').strip() != branch:
            raise OpError('team branch changed')
        commit = git_head(cwd)
    with tempfile.TemporaryDirectory(prefix='team-bundle-', dir=config.run_dir) as temporary:
        target = Path(temporary)/'source.bundle'
        result = _git(cwd, 'bundle', 'create', str(target), branch, timeout=120)
        if result.returncode or not target.exists():
            raise OpError('cannot bundle the team branch')
        if target.stat().st_size > TEAM_BUNDLE_LIMIT:
            raise OpError('team branch bundle exceeds 8 MiB; use the same machine')
        return {'commit': commit, 'data': base64.b64encode(target.read_bytes()).decode()}


def team_import(config: BrokerConfig, path: str, data: str, commit: str) -> dict:
    import tempfile
    cwd = _under_roots(config, path)
    if not isinstance(data, str) or len(data) > 4 * -(-TEAM_BUNDLE_LIMIT // 3) or not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40,64}', commit):
        raise OpError('invalid team bundle')
    try:
        contents = base64.b64decode(data, validate=True)
    except ValueError:
        raise OpError('invalid team bundle encoding') from None
    branch = 'acc/import-' + uuid.uuid4().hex[:16]
    with tempfile.TemporaryDirectory(prefix='team-import-', dir=config.run_dir) as temporary:
        target = Path(temporary)/'source.bundle'
        target.write_bytes(contents)
        verified = _git(cwd, 'bundle', 'verify', str(target))
        if verified.returncode:
            raise OpError('team bundle failed verification')
        heads = _git(cwd, 'bundle', 'list-heads', str(target)).stdout.splitlines()
        if len(heads) != 1 or heads[0].split()[0] != commit or not heads[0].split()[1].startswith('refs/heads/acc/'):
            raise OpError('team bundle head mismatch')
        ref = heads[0].split()[1]
        env = _hardened_env(cwd)
        if env is not None:
            env['GIT_ALLOW_PROTOCOL'] = 'file'  # the bundle only: never ext::, whatever url.*.insteadOf says (SR10-3)
        result = _git(cwd, '-c', 'protocol.file.allow=always', 'fetch', '--no-tags', '--no-write-fetch-head',
                      str(target), f'{ref}:refs/heads/{branch}', timeout=120, env=env)
        if result.returncode:
            raise OpError('cannot import team branch')
        # An insteadOf rewrite to another file path could fetch something else: the branch must be the bundle's commit.
        if _git(cwd, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}^{{commit}}').stdout.strip() != commit:
            _git(cwd, 'branch', '-D', '--', branch)
            raise OpError('team bundle head mismatch')
    return {'branch': branch, 'commit': commit}


def team_checkout(config: BrokerConfig, name: str, branch: str, data: str, commit: str) -> dict:
    """On a different machine, a private repository and its linked worktree (no network clone)."""
    if not WORKTREE_NAME.fullmatch(name) or not BRANCH.fullmatch(branch):
        raise OpError('invalid team checkout')
    root = _under_roots(config, str(config.allowed_roots[0]))
    repo = root / f'team-repo-{name}'
    if repo.exists():
        raise OpError('team checkout already exists')
    repo.mkdir()
    result = _git(repo, 'init', '-q', '-b', 'acc/base')
    if result.returncode:
        raise OpError('cannot initialize team checkout')
    try:
        imported = team_import(config, str(repo), data, commit)
        result = _hardened_git(repo, 'checkout', '-q', imported['branch'])
        if result.returncode:
            raise OpError('cannot check out imported team branch')
        made = make_worktree(config, str(repo), str(root), name, branch)
        return {**made, 'repo': str(repo)}
    except Exception:
        # No worktree has been returned, so nothing can be using this newly made repository.
        import shutil
        shutil.rmtree(repo)
        raise


def team_inspect(config: BrokerConfig, path: str, branch: str, base: str | None = None) -> dict:
    cwd = _under_roots(config, path)
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or base is not None and not re.fullmatch('[0-9a-f]{40,64}', base):
        raise OpError('invalid team ref')
    ref = f'{base}..{branch}' if base else branch
    count = _git(cwd, 'rev-list', '--count', ref)
    log = _git(cwd, 'log', '-50', '--format=%H%x09%s', ref)
    if count.returncode or log.returncode:
        raise OpError('team branch is unavailable')
    return {'branch': branch, 'ahead': int(count.stdout.strip()), 'lfs': _uses_lfs(cwd, branch),
            'commits': [{'sha': line.split('\t', 1)[0], 'subject': line.split('\t', 1)[-1]}
                        for line in log.stdout.splitlines()]}


def team_handoffs(config: BrokerConfig, path: str, branch: str, helper: str) -> list[dict]:
    """Read committed notes at a branch or an immutable report head, never working files."""
    cwd = _under_roots(config, path)
    if (not isinstance(branch, str) or not (BRANCH.fullmatch(branch) or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', branch))
            or not ACC_ID.fullmatch(helper)):
        raise OpError('invalid team branch or helper')
    found = _git_ok(cwd, 'ls-tree', '-rz', branch, '--', 'docs/handoffs')
    notes = []
    for entry in found.split('\0'):
        if not entry or '\t' not in entry:
            continue
        meta, name = entry.split('\t', 1)
        mode, kind, sha = meta.split()
        if kind != 'blob' or mode not in ('100644', '100755') or helper[:8] not in name or not name.endswith('.md'):
            continue
        size = int(_git_ok(cwd, 'cat-file', '-s', sha).strip())
        if size <= 64 * 1024:
            notes.append({'path': name, 'content': _git_ok(cwd, 'cat-file', 'blob', sha)})
        if len(notes) >= 5:
            break
    return notes


def files(config: BrokerConfig, path: str, **params) -> dict:
    """File browser/editor; kept separate from process operations."""
    from .files import operate
    try:
        return operate(config, path, **params)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OpError('That file or folder is missing, changed, or can\'t be opened. Reload the folder and try again.') from error


def _delete_git_pointer(path: Path) -> str:
    """Read one small regular metadata file without traversing any symlink."""
    try:
        parent = _walk_dirs(list(path.parent.parts[1:]))
        try:
            data = _read_at(parent, path.name, 4096)
        finally:
            os.close(parent)
        value = data.decode('utf-8').rstrip('\r\n')
        if len(data) > 4096 or not value or any(c in value for c in ('\n', '\r', '\0')):
            raise ValueError('invalid Git pointer')
        return value
    except (OSError, UnicodeError, ValueError):
        raise OpError('Cannot authenticate the worktree Git metadata; deletion was refused.') from None


def _delete_worktree_binding(checkout: Path, target: Path, recorded: bool) -> None:
    """Discovery is not authority: require reciprocal links to this exact repository.

    Independent container checkouts have no common repository. Only accd's
    recorded session paths can authorize those; Git discovery never can.
    """
    try:
        git = target / '.git'
        entry = git.lstat()
        if stat.S_ISDIR(entry.st_mode) and recorded:
            held = _walk_dirs(list(git.parts[1:]))
            os.close(held)
            return
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError('not a linked worktree')
        pointer = _delete_git_pointer(git)
        if not pointer.startswith('gitdir: '):
            raise ValueError('not a Git pointer')
        admin = Path(os.path.abspath(target / pointer[len('gitdir: '):]))
        common = checkout / '.git'
        if admin.parent != common / 'worktrees':
            raise ValueError('foreign administration')
        back = Path(os.path.abspath(admin / _delete_git_pointer(admin / 'gitdir')))
        shared = Path(os.path.abspath(admin / _delete_git_pointer(admin / 'commondir')))
        if back != git or shared != common:
            raise ValueError('nonreciprocal worktree binding')
    except (OSError, ValueError):
        raise OpError('The worktree does not have a reciprocal Git binding to this project; deletion was refused.') from None


def project_delete_identity(config: BrokerConfig, path: str, worktrees: list[str]) -> dict:
    """Authenticate accd's checkout grant and every discovered deletion target.

    Only direct account-root checkouts and direct .worktrees children qualify.
    Git metadata is untrusted discovery; linked worktrees must point back to
    this checkout's real common directory. accd additionally checks DB references.
    """
    checkout = Path(path)
    roots = [Path(root) for root in config.allowed_roots]
    if str(checkout) != path or '..' in checkout.parts or checkout.parent not in roots or not FOLDER.fullmatch(checkout.name):
        raise OpError('Refusing deletion: the checkout must be a direct folder under its account workspace root, without aliases.')
    # Validate the checkout and its common directory before asking Git for hints.
    try:
        parent = _walk_dirs(list(checkout.parent.parts[1:]))
        try:
            try:
                info = os.stat(checkout.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info and not stat.S_ISDIR(info.st_mode):
                raise OpError('Refusing deletion: the checkout is a symlink or is not a directory.')
        finally:
            os.close(parent)
        common = None
        if info:
            try:
                common = _walk_dirs(list((checkout / '.git').parts[1:]))
            except FileNotFoundError:
                pass
            finally:
                if common is not None:
                    os.close(common)
    except OSError:
        raise OpError('Cannot safely identify the checkout and its common Git directory; deletion was refused.') from None
    discovered = []
    if common is not None:
        result = _hardened_git(checkout, 'worktree', 'list', '--porcelain')
        if result.returncode:
            raise OpError('Cannot verify the project worktrees; repair the repository before deleting it.')
        discovered = [line[9:] for line in result.stdout.splitlines() if line.startswith('worktree ') and line[9:] != path]
    # Keep the database grant distinct from untrusted discovery (no recursive
    # promotion of Git hints into recorded session paths).
    recorded = set(worktrees)
    paths = list(dict.fromkeys([*worktrees, *discovered, path]))
    identities = []
    for value in paths:
        target = Path(value)
        if value != str(target) or '..' in target.parts or (value != path and target.parent != checkout.parent / '.worktrees'):
            raise OpError('Refusing deletion: a worktree is outside the managed account workspace root.')
        try:
            parent = _walk_dirs(list(target.parent.parts[1:]))
            try:
                try:
                    info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info and not stat.S_ISDIR(info.st_mode):
                    raise OpError('Refusing deletion: a checkout or worktree is a symlink or is not a directory.')
                if info and value != path:
                    _delete_worktree_binding(checkout, target, value in recorded)
                identities.append({'path': value, 'identity': [info.st_dev, info.st_ino] if info else None})
            finally:
                os.close(parent)
        except OSError as error:
            raise OpError('Refusing deletion: a parent is missing, inaccessible, or a symlink.') from error
    return {'version': 2, 'path': path, 'folders': identities}


def delete_project_checkout(config: BrokerConfig, path: str, worktrees: list[str], expected: dict) -> dict:
    current = project_delete_identity(config, path, worktrees)
    if current != expected:
        raise OpError('The checkout identity changed. Reload the confirmation before deleting it.')
    for item in current['folders']:
        target = Path(item['path'])
        parent = _walk_dirs(list(target.parent.parts[1:]))
        try:
            try:
                info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if [info.st_dev, info.st_ino] != item['identity'] or not stat.S_ISDIR(info.st_mode):
                raise OpError('The folder identity changed; deletion was refused.')
            # Hold the checked inode while clearing its contents. A concurrent
            # rename cannot redirect recursive deletion into a replacement folder.
            held = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                pinned = os.fstat(held)
                if [pinned.st_dev, pinned.st_ino] != item['identity']:
                    raise OpError('The folder identity changed; deletion was refused.')
                for entry in os.scandir(held):
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.name, dir_fd=held)
                    else:
                        os.unlink(entry.name, dir_fd=held)
                last = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
                if [last.st_dev, last.st_ino] != item['identity']:
                    raise OpError('The folder was renamed during deletion; reload before retrying.')
                os.rmdir(target.name, dir_fd=parent)
            finally:
                os.close(held)
        finally:
            os.close(parent)
    return {'deleted': path}


def project_delete_runtime(config: BrokerConfig, path: str) -> dict:
    workspace = Path(path).resolve()
    if not any(workspace == root or root in workspace.parents for root in config.allowed_roots):
        # Registration is declarative. External folders are never read or
        # removed here; only an explicitly approved Rynhult stack needs its
        # services broker checked by accd.
        return {'compose': None}
    return {'compose': any((workspace / name).exists() for name in COMPOSE_FILES)}
