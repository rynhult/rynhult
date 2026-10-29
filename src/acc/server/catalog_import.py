"""Import a skill or Claude subagent from GitHub, pinned to a commit, for review before it joins the catalog.

The preview resolves the ref to a commit SHA, fetches the files at that commit through the GitHub API
(text only, size-limited) and runs simple review checks (scripts, installs, network, hooks, telemetry,
instruction-override phrasing). It does not run anything. The fetched content is held server-side
under a one-time token, so what gets added is exactly what was reviewed.
"""
from __future__ import annotations

import base64
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlparse

import yaml

from .catalog import TEXT_SUFFIXES

MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 120, 512 * 1024, 6 * 1024 * 1024
PREVIEW_TTL = 1800
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts"}
CHECKS = [  # (level, message, regex, scripts only)
    ("warn", "Installs or runs packages", r"\b(npm (i|install)|pip3? install|npx |uvx |brew install|apt(-get)? install|cargo install)", False),
    ("warn", "Unpinned @latest version", r"@latest\b", False),
    ("warn", "Network calls from scripts", r"\b(curl|wget|requests\.(get|post)|urllib\.request|fetch\(|http\.client|httpx\.)", True),
    ("warn", "Writes to user or agent config", r"(~/\.claude|~/\.codex|\.bashrc|\.zshrc|\.profile\b|/etc/)", False),
    ("warn", "Mentions telemetry or analytics", r"\b(telemetry|analytics|posthog|mixpanel|segment\.io|sentry)\b", False),
    ("bad", "Instruction-override phrasing", r"(ignore (all |any )?(previous|prior) instructions|do not (tell|inform) the user|without (asking|telling) the user)", False),
    ("info", "Shell or code execution in scripts", r"\b(subprocess|os\.system|child_process|exec\(|eval\()", True),
]


# Skill and agent settings that would widen what a session may do: flagged as red here. Nothing outside
# ALLOWED_FRONTMATTER reaches a session (compiler.safe_markdown rebuilds the frontmatter from these keys only).
RISKY_FRONTMATTER = {"hooks", "allowed-tools", "mcpServers", "permissionMode"}
ALLOWED_FRONTMATTER = (  # every key the bundled catalog uses, plus ones that only describe or restrict
    "name", "description", "license", "version", "metadata", "argument-hint", "user-invocable",
    "disable-model-invocation", "model", "tools", "disallowedTools", "color",
)
MAX_FRONTMATTER_BYTES = 64 * 1024
INLINE_SHELL = re.compile(r"!`[^`\n]+`")  # Claude runs these when a skill loads


def has_bad_flags(flags: list[dict]) -> bool:
    return any(flag["level"] == "bad" for flag in flags)


class ImportError_(ValueError):
    pass


def parse_github_url(url: str) -> tuple[str, str, str | None, str, str]:
    """-> (owner, repo, ref or None, path, 'tree'|'blob'). Refs containing '/' are not supported (use a SHA or tag)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or parsed.netloc not in ("github.com", "www.github.com"):
        raise ImportError_("paste a https://github.com/... link")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ImportError_("the link needs an owner and a repository")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) == 2:
        return owner, repo, None, "", "tree"
    if parts[2] not in ("tree", "blob") or len(parts) < 4:
        raise ImportError_("use a link to a folder (…/tree/<ref>/<path>) or a file (…/blob/<ref>/<path>)")
    return owner, repo, parts[3], "/".join(parts[4:]), parts[2]


@dataclass(frozen=True)
class Frontmatter:
    present: bool  # the file opens with a --- line: CLIs will read the block as settings
    meta: dict = field(default_factory=dict)  # {} unless it parsed cleanly
    body: str = ""  # what follows the block (the whole text when there is none)
    error: str | None = None


def parse_frontmatter(text: str) -> Frontmatter:
    """The YAML frontmatter as data, read the way a YAML parser would (quoted keys, block scalars, flow mappings),
    but only in a plain form every CLI reads the same: one mapping with string keys, no anchors, aliases, merge
    keys, duplicate keys or tags beyond YAML's safe ones, at most MAX_FRONTMATTER_BYTES. Anything else is an error,
    and callers treat the block as settings they can't trust."""
    start = text.lstrip("\ufeff \t\r\n")
    if not start.startswith("---") or start.startswith("----"):
        return Frontmatter(False, body=text)
    first, _, rest = start.partition("\n")
    if first[3:].strip():  # "--- {hooks: x}" or "---yaml": YAML or a front-matter library may read it as settings
        return Frontmatter(True, body=rest, error="text after the opening ---")
    lines = rest.split("\n")
    close = next((i for i, line in enumerate(lines) if line.startswith("---")), None)
    if close is None:
        return Frontmatter(True, body=rest, error="no closing --- line")
    body = "\n".join(lines[close + 1:])
    if lines[close][3:].strip():
        return Frontmatter(True, body=body, error="text after the closing ---")
    source = "\n".join(lines[:close])
    if len(source.encode()) > MAX_FRONTMATTER_BYTES:
        return Frontmatter(True, body=body, error="frontmatter too large")
    try:
        for event in yaml.parse(source, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                return Frontmatter(True, body=body, error="YAML anchors and aliases are not supported")
        node = yaml.compose(source, Loader=yaml.SafeLoader)
        if node is None:
            return Frontmatter(True, body=body)
        if not isinstance(node, yaml.MappingNode):
            return Frontmatter(True, body=body, error="frontmatter is not a mapping")
        problem = _mapping_problem(node)
        if problem:
            return Frontmatter(True, body=body, error=problem)
        meta = yaml.safe_load(source)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as error:
        return Frontmatter(True, body=body, error=f"not valid YAML ({type(error).__name__})")
    if not all(isinstance(key, str) for key in meta):
        return Frontmatter(True, body=body, error="frontmatter keys must be text")
    return Frontmatter(True, meta, body)


def _mapping_problem(node) -> str | None:
    """Duplicate or merge (<<) keys anywhere in the tree: parsers disagree on which value wins."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, yaml.MappingNode):
            seen = set()
            for key, value in current.value:
                if key.tag == "tag:yaml.org,2002:merge":
                    return "YAML merge keys (<<) are not supported"
                ident = (key.tag, key.value) if isinstance(key, yaml.ScalarNode) else id(key)
                if ident in seen:
                    return f"duplicate key {str(key.value)[:40]!r}"
                seen.add(ident)
                stack += [key, value]
        elif isinstance(current, yaml.SequenceNode):
            stack += current.value
    return None


def allowed_frontmatter(meta: dict) -> dict:
    return {key: meta[key] for key in ALLOWED_FRONTMATTER if key in meta}


def frontmatter(text: str) -> dict[str, str]:
    """Frontmatter values as text (name, description, ...); {} when there is none or it can't be trusted."""
    return {key: "" if value is None else value if isinstance(value, str) else str(value)
            for key, value in parse_frontmatter(text).meta.items()}


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:63]


def review(files: dict[str, str], skipped: list[dict]) -> list[dict]:
    flags = []
    scripts = sorted(f for f in files if PurePosixPath(f).suffix in SCRIPT_SUFFIXES)
    if scripts:
        flags.append({"level": "warn", "message": f"Contains {len(scripts)} script(s); read them before adding", "files": scripts[:20]})
    for level, message, pattern, scripts_only in CHECKS:
        regex = re.compile(pattern, re.I)
        hits = [f for f, text in files.items() if (not scripts_only or f in scripts) and regex.search(text)]
        if hits:
            flags.append({"level": level, "message": message, "files": hits[:20]})
    parsed = {f: parse_frontmatter(t) for f, t in files.items() if f.endswith(".md")}
    hooks = [f for f, text in files.items() if f.endswith(".json") and re.search(r'"hooks"\s*:', text)]
    if hooks or any("hooks" in p.meta for p in parsed.values()):
        flags.append({"level": "bad", "message": "Defines hooks (Rynhult never enables third-party hooks)", "files": hooks[:20]})
    risky = [f for f, p in parsed.items() if set(p.meta) & (RISKY_FRONTMATTER - {"hooks"})]
    if risky:
        flags.append({"level": "bad", "message": "Pre-approves tools, adds MCP servers or changes permissions (frontmatter)",
                      "files": risky[:20]})
    broken = [f for f, p in parsed.items() if p.error]
    if broken:
        errors = sorted({parsed[f].error for f in broken})
        flags.append({"level": "bad", "message": "Frontmatter Rynhult can't read safely (" + "; ".join(errors)[:200]
                      + "); sessions get it without its settings", "files": broken[:20]})
    dropped = sorted({k for p in parsed.values() for k in p.meta} - set(ALLOWED_FRONTMATTER) - RISKY_FRONTMATTER)
    if dropped:
        flags.append({"level": "info", "message": "Frontmatter settings sessions won't get: " + ", ".join(dropped)[:200],
                      "files": [f for f, p in parsed.items() if set(p.meta) & set(dropped)][:20]})
    inline = [f for f, t in files.items() if f.endswith(".md") and INLINE_SHELL.search(t)]
    if inline:
        flags.append({"level": "bad", "message": "Runs shell commands when it loads (!`…`)", "files": inline[:20]})
    if skipped:
        flags.append({"level": "info", "message": f"{len(skipped)} file(s) not imported (binary, too large or over the limit)",
                      "files": [s["path"] for s in skipped[:20]]})
    return flags


class Previews:
    """One-time tokens for reviewed imports (in memory; a restart just means previewing again)."""

    def __init__(self):
        self._items: dict[str, tuple[float, dict]] = {}

    def put(self, payload: dict) -> str:
        now = time.time()
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        token = secrets.token_urlsafe(16)
        self._items[token] = (now + PREVIEW_TTL, payload)
        return token

    def take(self, token: str) -> dict:
        entry = self._items.pop(token, None)
        if entry is None or entry[0] < time.time():
            raise ImportError_("this preview expired; preview the link again")
        return entry[1]

    def put_back(self, token: str, payload: dict) -> None:
        """A refused import (red flags not confirmed) keeps its token, so the same review can be confirmed."""
        self._items[token] = (time.time() + PREVIEW_TTL, payload)


async def preview(github, url: str) -> dict:
    """github: a GitHub client (token optional). Returns the review payload including file contents."""
    owner, repo, ref, path, mode = parse_github_url(url)
    full = f"{owner}/{repo}"
    info = await github.get(f"/repos/{full}")
    ref = ref or info["default_branch"]
    sha = (await github.get(f"/repos/{full}/commits/{ref}"))["sha"]
    tree = (await github.get(f"/repos/{full}/git/trees/{sha}", recursive=1))["tree"]
    blobs = {e["path"]: e for e in tree if e["type"] == "blob"}
    try:
        license_id = ((await github.get(f"/repos/{full}/license")).get("license") or {}).get("spdx_id") or "unknown"
    except Exception:
        license_id = "none found"
    path = path.strip("/")
    if mode == "blob" and path.endswith("/SKILL.md"):
        path, mode = path.rsplit("/", 1)[0], "tree"
    if mode == "blob":
        if path not in blobs or not path.endswith(".md"):
            raise ImportError_("a file link must point at a Claude subagent (.md) or a SKILL.md")
        content = await _blob(github, full, blobs[path])
        meta = frontmatter(content or "")
        if content is None or not meta.get("name"):
            raise ImportError_("that file has no frontmatter name; Claude subagents need name and description")
        return {"kind": "agent", "repo": full, "commit": sha, "path": path, "license": license_id,
                "source": f"https://github.com/{full}/blob/{sha}/{path}", "suggested_id": slug(meta["name"]),
                "name": meta.get("name"), "description": meta.get("description", ""),
                "files": {f"{slug(meta['name'])}.md": content}, "skipped": [],
                "flags": review({path: content}, []), "candidates": []}
    prefix = f"{path}/" if path else ""
    if f"{prefix}SKILL.md" not in blobs:
        found = sorted(p.rsplit("/", 1)[0] if "/" in p else "" for p in blobs
                       if p.endswith("SKILL.md") and p.startswith(prefix))
        if not found:
            raise ImportError_("no SKILL.md found at that location")
        return {"kind": "choose", "repo": full, "commit": sha, "license": license_id,
                "candidates": [{"path": p, "url": f"https://github.com/{full}/tree/{sha}/{p}"} for p in found[:80]]}
    files, skipped, total = {}, [], 0
    for blob_path, entry in sorted(blobs.items()):
        if not blob_path.startswith(prefix):
            continue
        relative = blob_path[len(prefix):]
        if "__pycache__" in PurePosixPath(relative).parts:
            continue
        suffix = PurePosixPath(relative).suffix
        size = entry.get("size", 0)
        if suffix not in TEXT_SUFFIXES and PurePosixPath(relative).name not in ("LICENSE",):
            skipped.append({"path": relative, "reason": "not a text file"})
            continue
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            skipped.append({"path": relative, "reason": "over the size or file limit"})
            continue
        content = await _blob(github, full, entry)
        if content is None:
            skipped.append({"path": relative, "reason": "not UTF-8 text"})
            continue
        files[relative] = content
        total += size
    meta = frontmatter(files["SKILL.md"])
    name = meta.get("name") or PurePosixPath(path).name or repo
    return {"kind": "skill", "repo": full, "commit": sha, "path": path, "license": license_id,
            "source": f"https://github.com/{full}/tree/{sha}/{path}".rstrip("/"), "suggested_id": slug(name),
            "name": name, "description": meta.get("description", ""), "files": files, "skipped": skipped,
            "flags": review(files, skipped), "candidates": [], "bytes": total}


async def _blob(github, full: str, entry: dict) -> str | None:
    data = await github.get(f"/repos/{full}/git/blobs/{entry['sha']}")
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
