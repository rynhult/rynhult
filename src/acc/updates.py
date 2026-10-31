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

from .release_signatures import MissingSignature, verify

VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BUILD = re.compile(r"[0-9a-f]{64}")
GIT_MESSAGE = "This Rynhult runs from a git checkout: update it with git pull and deploy/install.sh"


SOURCE_ERRORS = {
    'no_release': 'This repository has no published release yet.',
    'no_stable_release': 'This repository has no stable release yet — select the Release candidates channel.',
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


def selected_channel(config, current):
    """Read the explicit channel, or migrate the earlier boolean preference."""
    channel = config.get('release_channel')
    if channel is None:
        legacy = config.get('include_prereleases', version(current)[3] == 0)
        if type(legacy) is not bool:
            raise ValueError('invalid release channel')
        channel = 'rc' if legacy else 'stable'
    if channel not in ('stable', 'rc'):
        raise ValueError('invalid release channel')
    return channel


def newer(candidate: str, current: str, include_prereleases=False) -> bool:
    try:
        target = version(candidate.removeprefix("v"))
        return (target[3] == 1 or include_prereleases) and target > version(current.removeprefix("v"))
    except ValueError:
        return False


def repository(source: str) -> str:
    parsed = urlparse(source)
    value = parsed.path.strip("/").removesuffix(".git")
    return value if parsed.scheme == "https" and parsed.hostname == "github.com" and REPO.fullmatch(value) else ""


def validate_request(data: object, include_prereleases=False) -> str:
    if (not isinstance(data, dict) or set(data) - {"action", "version", "allow_unsigned"}
            or not {"action", "version"} <= data.keys() or data["action"] != "update"
            or type(data.get("allow_unsigned", False)) is not bool):
        raise ValueError("request must name only action update and version")
    if version(data["version"])[3] != 1:
        if not include_prereleases:
            raise ValueError("updates must be stable releases on the Stable channel")
        if data.get('allow_unsigned'):
            raise ValueError('pre-release updates require a signature; no unsigned exception is allowed')
    return data["version"]


def release(data: dict, current: str, include_prereleases=False) -> dict:
    if not isinstance(data, dict) or data.get("draft") or (data.get("prerelease") and not include_prereleases):
        raise ValueError("not a stable release")
    if not isinstance(data.get("tag_name"), str):
        raise ValueError("invalid release tag")
    name = data["tag_name"].removeprefix("v")
    if version(name)[3] != 1 and not include_prereleases:
        raise ValueError("not a stable release")
    return {
        "latest": name,
        "prerelease": bool(data.get("prerelease")) or version(name)[3] == 0,
        "tag": data["tag_name"],
        "published_at": data.get("published_at"),
        "notes": str(data.get("body") or "")[:64000],
        "available": newer(name, current, include_prereleases),
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


def check(repo: str, current: str, token_file: Path | None = None, current_build: str | None = None, get=fetch, include_prereleases=None) -> dict:
    if not REPO.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("expected owner/repository")
    include_prereleases = version(current.removeprefix("v"))[3] == 0 if include_prereleases is None else include_prereleases is True
    try:
        data = json.loads(get(f"https://api.github.com/repos/{repo}/releases?per_page=30", token_file))
    except ReleaseSourceError as error:
        if error.kind != 'repository_missing':
            raise
        get(f"https://api.github.com/repos/{repo}", token_file)
        raise ReleaseSourceError('no_release') from None
    if not isinstance(data, list):
        raise ValueError('invalid release list')
    candidates = []
    published = False
    for row in data:
        if not isinstance(row, dict) or row.get('draft'):
            continue
        published = True
        try:
            candidates.append(release(row, current, include_prereleases))
        except ValueError:
            continue
    if not candidates:
        raise ReleaseSourceError('no_stable_release' if published and not include_prereleases else 'no_release')
    result = max(candidates, key=lambda item: version(item['latest']))
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
    result["available"] = available(result, current, current_build, include_prereleases)
    return result


def available(release: dict, current: str, current_build: str | None, include_prereleases=False) -> bool:
    latest = release.get("latest")
    if not latest or release.get("error") or ((version(latest)[3] != 1 or release.get("prerelease")) and not include_prereleases):
        return False
    return newer(latest, current, include_prereleases) or (
        version(latest) == version(current)
        and bool(current_build)
        and bool(release.get("latest_build"))
        and release["latest_build"] != current_build
    )


def release_asset(repo, latest, name, token_file, limit, get):
    url = latest.get('assets', {}).get(name, '')
    if not re.fullmatch(r'https://api\.github\.com/repos/' + re.escape(repo) + r'/releases/assets/[0-9]+', url):
        raise ValueError('release asset missing or outside the configured repository')
    return get(url, token_file, limit=limit, asset=True)


def release_manifest(repo, latest, token_file=None, *, get=fetch, allow_unsigned=False):
    sums = release_asset(repo, latest, 'SHA256SUMS', token_file, 1 << 20, get)
    try:
        if not latest.get('assets', {}).get('SHA256SUMS.asc'):
            raise MissingSignature()
        try:
            signature = release_asset(repo, latest, 'SHA256SUMS.asc', token_file, 128 << 10, get)
        except ReleaseSourceError as error:
            if error.kind != 'repository_missing':
                raise
            raise MissingSignature() from None
    except MissingSignature:
        if allow_unsigned is not True or version(latest['latest'])[3] == 0 or latest.get('prerelease'):
            raise
        return sums, {'status': 'unsigned_override'}
    return sums, verify(sums, signature)


def signed_check(repo, current, token_file=None, current_build=None, include_prereleases=None, *, get=fetch):
    latest = check(repo, current, token_file, current_build, get=get, include_prereleases=include_prereleases)
    try:
        sums, signature = release_manifest(repo, latest, token_file, get=get)
        release_checksum(sums, latest['latest'])
    except MissingSignature as error:
        signature = {'status': 'missing', 'detail': str(error)}
    return {**latest, 'signature': signature}


def release_checksum(sums: bytes, target: str) -> str:
    filename = f'acc-{target}.tar.gz'
    hashes = [line.split()[0] for line in sums.decode('utf-8').splitlines()
              if len(line.split()) == 2 and line.split()[1].lstrip('*') == filename]
    if len(hashes) != 1 or not BUILD.fullmatch(hashes[0]):
        raise ValueError('release checksum missing or ambiguous')
    return hashes[0]


def download_release(repo: str, latest: dict, stage: Path, token_file: Path | None = None, *, get=fetch,
                     allow_unsigned=False, verification=None, include_prereleases=False) -> Path:
    """Download and validate release source. Shared by the privileged updater and catalog restore.

    The caller owns and removes stage; this function never executes downloaded source.
    """
    from . import bundle

    target = latest['latest']
    if ((version(target)[3] != 1 or latest.get("prerelease")) and not include_prereleases) or not REPO.fullmatch(repo):
        raise ValueError('not a stable release repository')
    filename = f'acc-{target}.tar.gz'
    raw_sums, signature = release_manifest(repo, latest, token_file, get=get, allow_unsigned=allow_unsigned)
    checksum = release_checksum(raw_sums, target)
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / 'release.tar.gz'
    path.write_bytes(release_asset(repo, latest, filename, token_file, 256 << 20, get))
    tree = bundle.unpack(path, stage / 'unpacked', target, checksum)
    if bundle.version_of(tree) != target:
        raise ValueError('release source version mismatch')
    identity = bundle.identity(tree)
    if latest.get('latest_build') and latest['latest_build'] != identity:
        raise ValueError('release build manifest does not match source')
    if bundle.installed_identity(tree) != identity:
        raise ValueError('release build identity mismatch')
    if verification is not None:
        verification['signature'] = signature
    return tree
