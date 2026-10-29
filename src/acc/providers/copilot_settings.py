"""Copilot settings files that can switch its hooks off, and with them Rynhult's guard.

Checked with Copilot 1.0.102: in a folder Copilot trusts (Rynhult records trust for its sessions' folders), a
repository's .github/copilot/settings.json or settings.local.json with "disableAllHooks": true switches every
user-level hook off. Copilot reads them at the repository's git top level (the folder it starts in outside git); the
check covers that folder and every folder above. User-level disableAllHooks didn't switch user hooks off in that
version, and .copilot/settings.json in a repository is ignored. A repository's Claude-compatible config can set
disableAllHooks for Copilot as well (1.0.103's governance list), so .claude/settings*.json are checked too. Git's core.worktree moves the top level anywhere
(Copilot 1.0.103 follows it, from the repository's config or a per-worktree config.worktree), so a repository that sets
it is refused too.

Shared by the broker, which refuses to launch Copilot in such a folder, and the session image's Copilot wrapper.
Standard library only and no package imports: the image loads this file by its path.
"""
import json
import os
import re
import stat
from pathlib import Path

REFUSAL = "Copilot won't start here: its hooks, and Rynhult's guardrails with them, would be switched off"
ADVICE = "Remove that setting (or fix the file)"
# Copilot 1.0.103's governance lists (userSettingsGovernanceKeys): disableAllHooks is repo-governable through
# .github/copilot and through a repository's Claude-compatible config (claudeRepo). Both Claude files are checked.
REPOSITORY_FILES = (".github/copilot/settings.json", ".github/copilot/settings.local.json",
                    ".claude/settings.json", ".claude/settings.local.json")
GIT_CONFIGS = ("config", "config.worktree")
SAFE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
LIMIT = 1 << 20


def _unique(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _read(path):
    """(text, None); (None, None) when there is no such file; or (None, why it can't be checked). Never follows a link
    in the last component, never blocks on a FIFO, never reads more than LIMIT."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return None, None  # Copilot can't read it either
    except OSError as error:
        if os.path.islink(path):
            return None, f"{path} is a link"
        return None, f"{path} can't be read ({error.strerror or type(error).__name__})"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, f"{path} is not a regular file"
        if info.st_size > LIMIT:
            return None, f"{path} is too large to check"
        return os.read(fd, LIMIT + 1).decode("utf-8"), None
    except (OSError, ValueError):
        return None, f"{path} isn't UTF-8 text, so it can't be checked"
    finally:
        os.close(fd)


def settings_problem(path):
    """Why this settings file could turn Copilot's hooks off, or None. Anything that can't be read as plain JSON is
    refused rather than guessed at (fail closed): Copilot may read what this can't."""
    text, problem = _read(path)
    if text is None:
        return problem
    try:
        # config.json starts with // comment lines (Copilot writes them); nothing else is stripped.
        data = json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")),
                          object_pairs_hook=_unique)
    except ValueError:
        return f"{path} isn't plain JSON, so its effect on hooks can't be checked"
    if not isinstance(data, dict):
        return f"{path} isn't a settings object"
    for key, value in data.items():
        if "hook" not in key.lower() or key == "hooks":
            continue
        if (key, value) in (("disableAllHooks", False), ("disabledHooks", [])):
            continue
        # The key comes from the file: named only when it is a plain identifier, never as free text.
        return f"{path} sets {json.dumps(key)}" if SAFE_KEY.fullmatch(key) else f"{path} sets a hook setting"
    return None


def _sets_worktree(text):
    """Whether git config text sets core.worktree. Read loosely, so anything that might is counted (fail closed): a
    section header may share its line with a key, and names are case-insensitive."""
    section = ""
    # Git accepts a UTF-8 BOM at the start of a config file. Do not let it hide
    # the first section header (and a core.worktree outside the checked tree).
    for line in text.removeprefix("\ufeff").splitlines():
        line = line.strip()
        if line.startswith("["):
            end = line.find("]")
            if end < 0:
                return True
            section = line[1:end].replace('"', " ").replace(".", " ").split(" ")[0].strip().lower()
            line = line[end + 1:].strip()
        key = line.split("=", 1)[0].split("#", 1)[0].split(";", 1)[0].strip().lower()
        if section == "core" and key == "worktree":
            return True
    return False


def _git_dirs(folder):
    """(where, git directories) for `folder`, found as git finds them: the nearest .git folder or `gitdir:` file, or a
    bare repository, at or above the folder; with the shared directory a linked worktree names. `where` is the .git
    entry (or bare folder) in the session's own tree: messages name it, never a path read from a file."""
    for part in (folder, *folder.parents):
        dot = part / ".git"
        if os.path.isdir(dot):
            found, where = dot, dot
        elif os.path.lexists(dot):
            text, problem = _read(dot)
            if problem:
                return dot, None  # a link or an unreadable pointer: git may follow it, so it can't be checked
            if not text or not text.startswith("gitdir:"):
                return dot, []  # git stops here too, and Copilot then reads this folder (checked by the walk)
            found, where = part / text[len("gitdir:"):].strip(), dot
        elif os.path.isfile(part / "HEAD") and os.path.isdir(part / "objects"):
            found, where = part, part
        else:
            continue
        common, _ = _read(found / "commondir")
        return where, [found, found / common.strip()] if common and common.strip() else [found]
    return None, []


def worktree_problem(folder):
    """A git config that moves this folder's working tree elsewhere, where Copilot would read repository settings this
    check never sees, or None. Fail closed: a git directory or config that can't be read is a problem too."""
    where, git_dirs = _git_dirs(Path(folder))
    if git_dirs is None:
        return f"{where} can't be checked (a link, or unreadable)"
    for git_dir in git_dirs:
        for name in GIT_CONFIGS:
            path = git_dir / name
            text, problem = _read(path)
            own = path == where / name
            if problem:
                return problem if own else f"{where} points at a git {name} that can't be checked"
            if text is not None and _sets_worktree(text):
                return f"{path} sets core.worktree" if own else f"{where} points at a git {name} that sets core.worktree"
    return None


def repository_problem(folder):
    """The first repository settings file in `folder` or a folder above it that could turn Copilot's hooks off, or a
    git setting that would make Copilot read its repository settings from somewhere else."""
    folder = Path(folder)
    for part in (folder, *folder.parents):
        for name in REPOSITORY_FILES:
            problem = settings_problem(part / name)
            if problem:
                return problem
    return worktree_problem(folder)


def refusal(problem, then="start the session again"):
    return f"{REFUSAL}: {problem}. {ADVICE}, then {then}."


def refused_problem(text):
    """The problem a refusal() names, from an error text that carries one (e.g. a broker answer), or None."""
    start = text.find(REFUSAL + ": ")
    end = text.rfind(". " + ADVICE)
    return text[start + len(REFUSAL) + 2:end][:500] if start >= 0 and end > start else None
