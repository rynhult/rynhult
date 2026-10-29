"""Shared release and request validation. No service privileges or database dependencies."""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BUILD = re.compile(r"[0-9a-f]{64}")
GIT_MESSAGE = "This Rynhult runs from a git checkout: update it with git pull and deploy/install.sh"


SOURCE_ERRORS = {
    'repository_missing': 'Repository not found. Private repositories need a token in data/secrets/update_token.',
    'rate_limited': 'GitHub rate limited update checks. Try again later.',
    'access_denied': 'GitHub refused access. Check the update token and its repository permissions.',
}


class ReleaseSourceError(ValueError):
    """A fixed classification, safe to show through the server's error boundary."""
    def __init__(self, kind):
        self.kind = kind
        super().__init__(SOURCE_ERRORS[kind])


def version(value: str) -> tuple:
    if not isinstance(value, str) or len(value) > 80 or not (match := VERSION.fullmatch(value)):
        raise ValueError("expected a stable semver version (major.minor.patch)")
    prerelease = match[4]
    identifiers = prerelease.split(".") if prerelease else []
    if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in identifiers):
        raise ValueError("invalid semver prerelease")
    ordered = tuple((0, int(p)) if p.isdigit() else (1, p) for p in identifiers)
    return (*tuple(int(match[i]) for i in (1, 2, 3)), 0 if prerelease else 1, ordered)


def newer(candidate: str, current: str) -> bool:
    try:
        target = version(candidate.removeprefix("v"))
        return target[3] == 1 and target > version(current.removeprefix("v"))
    except ValueError:
        return False


def repository(source: str) -> str:
    parsed = urlparse(source)
    value = parsed.path.strip("/").removesuffix(".git")
    return value if parsed.scheme == "https" and parsed.hostname == "github.com" and REPO.fullmatch(value) else ""


def validate_request(data: object) -> str:
    if not isinstance(data, dict) or set(data) != {"action", "version"} or data["action"] != "update":
        raise ValueError("request must name only action update and version")
    if version(data["version"])[3] != 1:
        raise ValueError("updates must be stable releases")
    return data["version"]


def release(data: dict, current: str) -> dict:
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ValueError("not a stable release")
    name = data.get("tag_name", "").removeprefix("v")
    if version(name)[3] != 1:
        raise ValueError("not a stable release")
    return {
        "latest": name,
        "tag": data["tag_name"],
        "published_at": data.get("published_at"),
        "notes": str(data.get("body") or "")[:64000],
        "available": newer(name, current),
        "assets": {
            a["name"]: a["url"]
            for a in data.get("assets", [])
            if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("url"), str)
        },
    }


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise ValueError("HTTPS required")
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Never forward a private repository token to a CDN (or another redirect host).
        if result is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            result.remove_header("Authorization")
        return result


def fetch(url: str, token_file: Path | None = None, limit: int = 4 << 20, context=None, asset=False) -> bytes:
    if urlparse(url).scheme != "https":
        raise ValueError("HTTPS required")
    headers = {
        "User-Agent": "Rynhult-updater",
        "Accept": "application/octet-stream" if asset else "application/vnd.github+json",
    }
    if token_file and token_file.exists():
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("token must be a regular secrets file")
            token = file.read(8193).strip()
        if len(token) > 8192 or "\n" in token or "\r" in token:
            raise ValueError("invalid token file")
        headers["Authorization"] = "Bearer " + token
    handlers = [HTTPSOnly()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    try:
        with urllib.request.build_opener(*handlers).open(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("download exceeds size limit")
            return body
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ReleaseSourceError('repository_missing') from None
        elif error.code == 429 or (error.code == 403 and ((error.headers or {}).get('X-RateLimit-Remaining') == '0' or (error.headers or {}).get('Retry-After'))):
            raise ReleaseSourceError('rate_limited') from None
        elif error.code in (401, 403):
            raise ReleaseSourceError('access_denied') from None
        else:
            message = f'Update source returned HTTP {error.code}.'
        raise ValueError(message) from None
    except OSError:
        raise ValueError("could not reach release source") from None


def check(repo: str, current: str, token_file: Path | None = None, current_build: str | None = None, get=fetch) -> dict:
    if not REPO.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("expected owner/repository")
    data = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest", token_file))
    result = release(data, current)
    build_url = result["assets"].get("BUILD")
    if build_url:
        if not re.fullmatch(
            r"https://api\.github\.com/repos/" + re.escape(repo) + r"/releases/assets/[0-9]+", build_url
        ):
            raise ValueError("build asset outside the configured repository")
        build = get(build_url, token_file, limit=1024, asset=True).decode().strip()
        if not BUILD.fullmatch(build):
            raise ValueError("invalid release build")
        result["latest_build"] = build
    result["available"] = available(result, current, current_build)
    return result


def available(release: dict, current: str, current_build: str | None) -> bool:
    latest = release.get("latest")
    if not latest or release.get("error"):
        return False
    return newer(latest, current) or (
        version(latest) == version(current)
        and bool(current_build)
        and bool(release.get("latest_build"))
        and release["latest_build"] != current_build
    )


def download_release(repo: str, latest: dict, stage: Path, token_file: Path | None = None, *, get=fetch) -> Path:
    """Download and validate release source. Shared by the privileged updater and catalog restore.

    The caller owns and removes stage; this function never executes downloaded source.
    """
    from . import bundle

    target = latest['latest']
    if version(target)[3] != 1 or not REPO.fullmatch(repo):
        raise ValueError('not a stable release repository')
    filename = f'acc-{target}.tar.gz'
    assets = latest.get('assets', {})

    def asset(name, limit):
        url = assets.get(name, '')
        if not re.fullmatch(r'https://api\.github\.com/repos/' + re.escape(repo) + r'/releases/assets/[0-9]+', url):
            raise ValueError('release asset missing or outside the configured repository')
        return get(url, token_file, limit=limit, asset=True)

    sums = asset('SHA256SUMS', 1 << 20).decode()
    hashes = [line.split()[0] for line in sums.splitlines()
              if len(line.split()) == 2 and line.split()[1].lstrip('*') == filename]
    if len(hashes) != 1 or not BUILD.fullmatch(hashes[0]):
        raise ValueError('release checksum missing or ambiguous')
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / 'release.tar.gz'
    path.write_bytes(asset(filename, 256 << 20))
    tree = bundle.unpack(path, stage / 'unpacked', target, hashes[0])
    if bundle.version_of(tree) != target:
        raise ValueError('release source version mismatch')
    identity = bundle.identity(tree)
    if latest.get('latest_build') and latest['latest_build'] != identity:
        raise ValueError('release build manifest does not match source')
    if bundle.installed_identity(tree) != identity:
        raise ValueError('release build identity mismatch')
    return tree
