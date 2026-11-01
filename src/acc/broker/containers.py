"""The broker's side of per-session containers (docs/CONTAINER-LAUNCHER.md).

The broker runs with NoNewPrivileges, so it never runs Podman to start a container: it asks the acc-containers root
helper (/run/acc-containers.sock), which launches what accd's grant for the session says, as this account, in a unit of
its own. The session's tmux pane then attaches to that container. Requests carry only the fixed fields below.

A container session works in a private checkout: a repository of its own, seeded from the project's committed HEAD
(never a worktree, whose .git points at the project's shared administration). Its branch comes back into the project
through the broker when the checkout is removed (`return_branch`), never from inside the container.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from acc.python_child import module_command

from .container_prototype import PrototypeRefused, private_checkout

POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,39}$")
MAX_ANSWER = 8192
# What a private checkout's own .git/config may hold. The agent in the container can write that file, and the broker
# runs git in the checkout on the host (Changes, backups, bringing the branch back): any setting that makes git run a
# program (core.fsmonitor, filters, textconv, diff.external, gpg.program, include.path, ...) would run it outside the
# container. So git there runs only while every setting is one of these.
SAFE_CONFIG = re.compile(r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode"
                         r"|symlinks|autocrlf|safecrlf|eol|quotepath)|user\.(name|email)|extensions\.objectformat"
                         r"|init\.defaultbranch|gc\.auto|maintenance\.auto|branch\.[a-z0-9/._-]+\.(remote|merge))$")
HARDENED = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


class ContainerError(Exception):
    pass


def check(container) -> dict:
    """The launch's `container` parameter from accd: a generation and the pinned image's policy id, nothing else."""
    if (not isinstance(container, dict) or set(container) != {"generation", "policy_id"}
            or type(container["generation"]) is not int or not 1 <= container["generation"] <= (1 << 31)
            or not isinstance(container["policy_id"], str) or not POLICY_ID.match(container["policy_id"])):
        raise ContainerError("Rynhult sent invalid container settings")
    return container


def ask(socket_path: Path, operation: str, session_id: str, container: dict, timeout: float = 55.0) -> dict:
    """One request to the root helper; its answer, or ContainerError with its reason code. The timeout keeps the
    broker's answer inside accd's 60 s."""
    request = {"version": 1, "request_id": f"brk-{uuid.uuid4().hex[:20]}", "operation": operation,
               "session_id": session_id, "generation": container["generation"], "policy_id": container["policy_id"]}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(socket_path))
            conn.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while b"\n" not in data and len(data) < MAX_ANSWER:
                chunk = conn.recv(MAX_ANSWER)
                if not chunk:
                    break
                data += chunk
    except OSError as error:
        raise ContainerError(f"the container launcher isn't reachable ({error.strerror or error})") from None
    try:
        answer = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        raise ContainerError("the container launcher gave no answer") from None
    if not isinstance(answer, dict) or answer.get("request_id") != request["request_id"]:
        raise ContainerError("the container launcher answered another request")
    if not answer.get("ok"):
        raise ContainerError(f"{answer.get('code', 'refused')}: {str(answer.get('detail', ''))[:300]}")
    return answer


def attach_socket(root: Path, session_id: str, generation: int) -> Path:
    """The relay the launcher's unit runs for this generation's terminal (acc-containers attach_socket): the container
    lives in that unit's private namespaces, which this account can't enter, so `podman attach` here can't reach it."""
    digest = hashlib.sha256(f"acc-ct-{session_id}-g{generation}".encode()).hexdigest()[:16]
    return root / digest / "attach.sock"


# The pane starts in the session's checkout, which the agent in the container writes: Python must never import from
# there (`-m` puts the working folder first) or take PYTHON* settings. Isolated mode, then only Rynhult's own source.
ATTACH_BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); from acc.broker.container_attach import main; "
                    "raise SystemExit(main(sys.argv[2:]))")


def unit_cgroup(session_id: str, generation: int) -> str:
    """The launcher unit's cgroup (acc-container-exec delegate_cgroup): the pane accepts only a relay in it or below."""
    return f"/acc.slice/acc-containers.slice/acc-containers-{os.getuid()}.slice/acc-ct-{session_id}-g{generation}.service"


def attach_command(root: Path, session_id: str, generation: int) -> list[str]:
    """What the session's tmux pane runs: the container's terminal. Every key reaches the agent (no detach keys); when
    the container exits, so does the pane."""
    return [sys.executable, "-I", "-c", ATTACH_BOOTSTRAP, str(Path(__file__).resolve().parents[2]),
            str(attach_socket(root, session_id, generation)), unit_cgroup(session_id, generation)]


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0")
    return env


def _git(cwd: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *HARDENED, "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout, env=_git_env())


def _git_output(cwd: Path, limit: int, *args: str, timeout: int = 120) -> bytes | None:
    """stdout of a git command, read in memory and never past `limit` bytes: None when it would be longer (git is
    stopped there). ContainerError when git fails or takes longer than `timeout` seconds."""
    import threading

    process = subprocess.Popen(["git", *HARDENED, "-C", str(cwd), *args], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=_git_env())
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        data = process.stdout.read(limit + 1)
        if len(data) > limit:
            process.kill()
    finally:
        process.stdout.close()
        process.wait()
        expired = not timer.is_alive()
        timer.cancel()
    if len(data) > limit:
        return None
    if expired or process.returncode:
        raise ContainerError(f"git {args[0]} failed")
    return data


def make_checkout(source: Path, target: Path, branch: str, commit: str | None = None) -> None:
    """Seed `target` from the project's committed HEAD on a new `branch`, with this account's git identity."""
    from contextlib import nullcontext

    from .container_git import snapshot

    # A helper's source is agent-writable container metadata. Never let host Git
    # reopen it after validation, including the config reads for commit identity.
    with snapshot(source) if is_container_checkout(source) else nullcontext(source) as safe:
        try:
            private_checkout(safe, target, branch=branch, **({'commit':commit} if commit else {}))
        except (PrototypeRefused, subprocess.CalledProcessError) as error:
            raise ContainerError(f"couldn't make the session's private checkout: {error}") from None
        (target.parent / f".{target.name}.acc-container").touch(mode=0o600, exist_ok=False)
        for key in ("user.name", "user.email"):
            value = _git(safe, "config", "--get", key).stdout.strip()
            if value:
                _git(target, "config", key, value)


def is_private_checkout(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() and not git.is_symlink()


def is_container_checkout(path: Path) -> bool:
    return path.parent.name == ".worktrees" and (
        (path.parent / f".{path.name}.acc-container").exists() or is_private_checkout(path))


def git_guard(cwd: Path) -> list[str]:
    """Extra git options for running git in `cwd` from the broker: none for an ordinary folder or worktree (as today);
    for a container session's private checkout, hooks and fsmonitor off, and ContainerError unless its config holds
    only SAFE_CONFIG settings."""
    cwd = cwd if isinstance(cwd, Path) else Path(cwd)
    if not is_container_checkout(cwd):
        return []
    if not is_private_checkout(cwd):
        raise ContainerError("the checkout's git administration changed; Rynhult won't run git there")
    config = cwd / ".git" / "config"
    if config.is_symlink():
        raise ContainerError("the checkout's git settings are a link; Rynhult won't run git there")
    listed = subprocess.run(["git", "config", "--file", os.fspath(config), "--null", "--name-only", "--list"],
                            capture_output=True, text=True, timeout=30,
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
                                 "HOME": "/nonexistent"})
    if listed.returncode not in (0, 1) and config.exists():
        raise ContainerError("the checkout's git settings can't be read; Rynhult won't run git there")
    unsafe = sorted({key for key in listed.stdout.split("\0") if key and not SAFE_CONFIG.match(key.lower())})
    if unsafe:
        raise ContainerError(f"the checkout's git settings include {', '.join(unsafe[:5])}, which could run a program "
                             "outside the container; Rynhult won't run git there until they're removed")
    # A check cannot secure a later Git open. Every host invocation uses a bounded,
    # no-follow metadata snapshot, validated again after copying, outside the container mounts.
    return module_command("acc.broker.container_git", os.fspath(cwd))


def return_branch(source: Path, checkout: Path, branch: str) -> str | None:
    """Fetch the session's branch back into the project (fast-forward or new only). None when it worked."""
    try:
        git_guard(checkout)
        from .container_git import snapshot
        with snapshot(checkout) as admin:
            result = _git(source, "fetch", "--no-tags", "--no-write-fetch-head", str(admin),
                          f"refs/heads/{branch}:refs/heads/{branch}")
    except (ContainerError, OSError) as error:
        return str(error)
    return None if result.returncode == 0 else (result.stderr.strip()[-300:] or "git fetch failed")
