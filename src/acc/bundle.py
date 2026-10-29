"""The source bundle a main Rynhult serves at GET /api/nodes/bundle: a tar.gz of its own tree at its running version.

deploy/install.sh builds it into data/bundle/acc-<version>.tar.gz. New machines install from it (the bootstrap at
GET /api/nodes/install.sh), and it is the source of this exact version for anyone using Rynhult.
From a git checkout it holds only tracked files; either way it leaves out what is local to this machine: git history, virtualenvs, node_modules, build output,
data and secrets. Members are regular files and folders only, owned by root, under one folder acc-<version>/.

Standard library only, so install.sh can run it with the system Python:
    python3 -I src/acc/bundle.py --source /srv/acc/app --out /srv/acc/data/bundle
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

# Shared with the history-free exporter. Prefixes exclude future descendants too.
PUBLIC_EXCLUDED = ('docs/PLAN.md', 'docs/OPERATIONS.md', 'docs/SESSION_LOG.md',
                   'docs/handoffs', 'docs/qualification', 'docs/research/node-test-plan.md', 'host', 'docs/reviews',
                   'docs/security', 'docs/drafts', 'docs/decisions/drafts', 'docs/runbooks', 'docs/drills',
                   'docs/RELEASE-RC1-CHECKLIST.md', 'docs/RENAME-STAGE-2.md',  # private release/rollout inventories
                   'docs/backup-costs.md', 'docs/publication-review-rc1.md',
                   'docs/research/competitors.md', 'docs/research/competitors-2026-10-22.md',
                   'docs/research/competitors-2026-10-27.md',
                   'docs/decisions/0008-owner-answers-2026-10-22.md',
                   'docs/decisions/0020-owner-answers-2026-10-24.md',
                   'docs/decisions/0050-owner-answers-2026-10-24-afternoon.md',
                   'docs/STATUS-BOARD.md', 'docs/PARKED.md',
                   'docs/research/orchestrator-v2-quota-sizing.md', 'spike')  # incident inputs; publication review


SKIP_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
              "secrets", ".acc-build"}
SKIP_TOP = {"data", "dist", ".acc", "src/acc/server/static", "web/dist", "web/e2e/.results", "web/e2e/screenshots",
            "web/playwright-report"}
SKIP_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".tsbuildinfo")


def private_content(relative: str) -> bool:
    """One conservative path policy for bundles and history-free public snapshots.

    Check ancestors too: exporters can enumerate files without walking their parents.
    Case folding avoids publishing the same private folder under a platform spelling.
    Unknown source paths still require the separate publication/denylist review.
    """
    relative = relative.casefold()
    parts = relative.split('/')
    if (relative.startswith('/') or '\\' in relative or
            any(part in ('', '.', '..') for part in parts)):
        return True
    if any(relative == prefix.casefold() or relative.startswith(prefix.casefold() + '/')
           for prefix in (*PUBLIC_EXCLUDED, *SKIP_TOP)):
        return True
    for name in parts:
        if (name in SKIP_NAMES or name == '.env' or name.startswith('.env.') or
                name.endswith('.env') or name.endswith(SKIP_SUFFIXES)):
            return True
    return False


def skipped(relative: str, name: str, is_dir: bool) -> bool:
    return private_content(relative)


def tracked(source: Path) -> set[str] | None:
    """The files git tracks in source, or None when it isn't a git checkout. When it is, only these can go in: the
    bundle is public, so nothing that merely lies in the folder (scratch files, logs, test output) may be published."""
    if not os.path.lexists(source / ".git"):
        return None
    git = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    try:
        top = subprocess.run([*git, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=60)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != source:
            raise SystemExit(f"bundle: {source} is a git checkout but its metadata is invalid")
        out = subprocess.run([*git, "ls-files", "-z"], capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        raise SystemExit(f"bundle: {source} is a git checkout but git ls-files failed") from None
    return {name for name in out.decode().split("\0") if name}


def members(source: Path):
    """(relative path, absolute path, is_dir) for everything that goes in, sorted, links and devices left out."""
    allowed = tracked(source)
    folders = None if allowed is None else {str(Path(name).parent) for name in allowed} | {"."}
    if folders is not None:
        for name in list(folders):  # every ancestor of a tracked file
            while name not in (".", ""):
                name = str(Path(name).parent)
                folders.add(name)
    for root, dirs, files in os.walk(source):
        here = Path(root)
        base = here.relative_to(source).as_posix()
        kept = []
        for name in sorted(dirs):
            relative = name if base == "." else f"{base}/{name}"
            if folders is not None and relative not in folders:
                continue
            if not (here / name).is_symlink() and not skipped(relative, name, True):
                kept.append(name)
                yield relative, here / name, True
        dirs[:] = kept
        for name in sorted(files):
            relative = name if base == "." else f"{base}/{name}"
            path = here / name
            if skipped(relative, name, False) or (allowed is not None and relative not in allowed):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                yield relative, path, False


def version_of(source: Path) -> str:
    return tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]


def identity(source: Path) -> str:
    """Content identity, independent of mtimes, tar compression and absolute install path."""
    digest = hashlib.sha256()
    for relative, path, is_dir in sorted(members(source)):
        if is_dir:
            continue
        content = path.read_bytes()
        digest.update(relative.encode() + b"\0" + (b"x" if path.stat().st_mode & 0o111 else b"-")
                      + str(len(content)).encode() + b"\0" + content)
    return digest.hexdigest()


def installed_identity(source: Path) -> str:
    try:
        value = (source / ".acc-build").read_text().strip()
        if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass
    return identity(source)


MAX_MEMBERS = 100000  # Counts raw headers too, including PAX/GNU extensions.
MAX_SIZE = 2 << 30
MAX_METADATA = 8 << 20
MAX_HEADER_METADATA = 64 << 10
MAX_EXTENSION_DEPTH = 16
CHUNK = 64 << 10


class _MetadataReader:
    """Inspect bounded PAX bytes before stdlib sparse-map parsing can allocate."""
    def __init__(self, stream):
        self.stream = stream
        self.first = True

    def read(self, size):
        data = self.stream.read(size)
        if self.first:
            self.first = False
            # Sparse files are outside the bundle contract. Refuse their PAX formats
            # before tarfile expands sparse maps or reads a sparse 1.0 payload.
            if b'GNU.sparse' in data:
                raise ValueError('sparse bundle members are not supported')
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _LimitedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # tarfile calls this immediately after decoding each fixed 512-byte header,
        # before reading any size-dependent PAX/GNU body (including nested headers).
        self._bundle_archive = archive
        archive.header_count += 1
        if archive.header_count > MAX_MEMBERS:
            raise ValueError('too many bundle headers')
        extension = self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                                  tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
        if self.size < 0:
            raise ValueError('negative bundle member size')
        if extension:
            archive.metadata_bytes += self.size
            if self.size > MAX_HEADER_METADATA or archive.metadata_bytes + archive.applied_metadata > MAX_METADATA:
                raise ValueError('bundle metadata exceeds size limit')
            if archive.extension_depth >= MAX_EXTENSION_DEPTH:
                raise ValueError('bundle metadata nesting exceeds limit')
        elif self.type == tarfile.GNUTYPE_SPARSE or not (self.isfile() or self.isdir()):
            raise ValueError('unsafe bundle member')
        elif self.size > MAX_SIZE or (self.isdir() and self.size):
            raise ValueError('oversized bundle member')
        archive.extension_depth += 1
        try:
            return super()._proc_member(archive)
        finally:
            archive.extension_depth -= 1

    def _apply_pax_info(self, headers, encoding, errors):
        # Global PAX fields are copied onto every later member by tarfile. Charge
        # those copies too, so a small global header cannot amplify parser work
        # across the entire member allowance. Check before the dict allocation.
        archive = self._bundle_archive
        archive.applied_metadata += sum(len(k.encode('utf-8', 'surrogateescape'))
                                        + len(v.encode('utf-8', 'surrogateescape'))
                                        for k, v in headers.items())
        if archive.metadata_bytes + archive.applied_metadata > MAX_METADATA:
            raise ValueError('bundle metadata exceeds size limit')
        return super()._apply_pax_info(headers, encoding, errors)

    def _proc_pax(self, archive):
        original = archive.fileobj
        archive.fileobj = _MetadataReader(original)
        try:
            return super()._proc_pax(archive)
        finally:
            archive.fileobj = original


class _LimitedTarFile(tarfile.TarFile):
    def __init__(self, *args, **kwargs):
        # Initialized before TarFile.__init__ parses its first member.
        self.header_count = self.metadata_bytes = self.applied_metadata = self.extension_depth = 0
        super().__init__(*args, **kwargs)


class _BoundedGzip:
    def __init__(self, stream):
        self.stream, self.seen = stream, 0

    def read(self, size):
        # Bound decompression buffers as well as total physical bytes, including
        # padding and trailing data that do not contribute to logical file sizes.
        data = self.stream.read(min(size, CHUNK))
        self.seen += len(data)
        if self.seen > MAX_SIZE + MAX_METADATA + MAX_MEMBERS * 1024 + 10240:
            raise ValueError('decompressed bundle exceeds size limit')
        return data


def _scan(raw, version, destination=None):
    prefix = f'acc-{version}'
    seen, total = set(), 0
    try:
        with gzip.GzipFile(fileobj=raw) as zipped, _LimitedTarFile.open(
                fileobj=_BoundedGzip(zipped), mode='r|', tarinfo=_LimitedTarInfo) as archive:
            while (item := archive.next()) is not None:
                total += item.size  # Effective size, after PAX overrides.
                if item.size < 0 or total > MAX_SIZE:
                    raise ValueError('oversized bundle')
                parts = PurePosixPath(item.name).parts
                if (not parts or parts[0] != prefix or item.name.startswith('/')
                        or any(p in ('.', '..', '') for p in item.name.rstrip('/').split('/'))
                        or '\\' in item.name or not (item.isfile() or item.isdir())
                        or item.issparse() or item.name in seen):
                    raise ValueError('unsafe bundle member')
                seen.add(item.name)
                if destination is not None:
                    archive.extract(item, destination, filter='data')
                # Links are refused, so extraction never needs a previous member.
                # next() rather than iteration also avoids retaining all TarInfos.
                archive.members.clear()
            if not seen:
                raise ValueError('empty bundle')
            # Finish gzip integrity checking and bound otherwise ignored trailing bytes.
            while archive.fileobj.read(CHUNK):
                pass
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError('invalid bundle archive') from error


def _checked_file(path, sha256):
    raw = path.open('rb')
    try:
        if hashlib.file_digest(raw, 'sha256').hexdigest() != sha256:
            raise ValueError('bundle SHA-256 mismatch')
        raw.seek(0)
        return raw
    except BaseException:
        raw.close()
        raise


def verify(path: Path, version: str, sha256: str) -> None:
    """Incrementally validate all members/metadata before extracting anything."""
    with _checked_file(path, sha256) as raw:
        _scan(raw, version)


def unpack(path: Path, destination: Path, version: str, sha256: str) -> Path:
    # Keep the checked inode open across both passes; the second pass enforces the
    # same budgets and path policy instead of invoking an unrestricted extractall.
    with _checked_file(path, sha256) as raw:
        _scan(raw, version)
        raw.seek(0)
        _scan(raw, version, destination)
    return destination / f'acc-{version}'


def build(source: Path, out_dir: Path, version: str | None = None) -> Path:
    """Writes out_dir/acc-<version>.tar.gz (replacing it atomically) and removes bundles of other versions."""
    source = source.resolve()
    version = version or version_of(source)
    prefix = f"acc-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{prefix}.tar.gz"
    partial = out_dir / f".{prefix}.tar.gz.partial"
    with open(partial, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped, \
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add(name: str, path: Path | None, is_dir: bool) -> None:
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                info.mtime = 0
                tar.addfile(info)
                return
            st = path.stat()
            info.size, info.mtime = st.st_size, 0
            info.mode = 0o755 if st.st_mode & 0o111 else 0o644
            with open(path, "rb") as data:
                tar.addfile(info, data)
        add(prefix, source, True)
        stamp = (identity(source) + "\n").encode()
        info = tarfile.TarInfo(f"{prefix}/.acc-build")
        info.size, info.mode = len(stamp), 0o644
        info.uname = info.gname = "root"
        tar.addfile(info, io.BytesIO(stamp))
        for relative, path, is_dir in members(source):
            add(f"{prefix}/{relative}", path, is_dir)
    partial.chmod(0o644)
    os.replace(partial, target)
    for old in out_dir.glob("acc-*.tar.gz"):
        if old != target:
            old.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(description="Build Rynhult's source bundle (GET /api/nodes/bundle).")
    args.add_argument("--source", type=Path, required=True, help="the Rynhult tree, e.g. /srv/acc/app")
    output = args.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", type=Path, help="the folder for acc-<version>.tar.gz")
    output.add_argument("--identity", action="store_true", help="print the full source build identity")
    options = args.parse_args(argv)
    if options.identity:
        print(installed_identity(options.source))
        return 0
    target = build(options.source, options.out)
    print(f"{target} ({target.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
