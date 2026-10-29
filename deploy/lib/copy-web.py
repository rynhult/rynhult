#!/usr/bin/python3 -I
"""Copy untrusted build output without following symlinks or opening devices."""
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_dir(path):
    fd = os.open("/", DIR)
    try:
        for part in os.path.abspath(path).split("/")[1:]:
            child = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_tree(fd, destination, relative=Path(), allow_links=False, include=None, skip_existing=False):
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR, dir_fd=fd)
            try:
                copy_tree(child, target, relative / name, allow_links, include, skip_existing)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            if include is not None and str(relative / name) not in include:
                continue
            if skip_existing and target.is_file():
                continue
            src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if not stat.S_ISREG(os.fstat(src).st_mode):
                    raise ValueError("non-regular output")
                with os.fdopen(src, "rb", closefd=False) as stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if info.st_mode & 0o111 else 0o644)
            finally:
                os.close(src)
        elif stat.S_ISLNK(info.st_mode) and allow_links:
            link = os.readlink(name, dir_fd=fd)
            resolved = os.path.normpath(str(relative / link))
            if os.path.isabs(link) or resolved == ".." or resolved.startswith("../"):
                raise ValueError("external output link")
            target.symlink_to(link)
        else:
            raise ValueError("non-regular output: " + name)


def asset_inventory(fd):
    try:
        meta = os.open(".vite", DIR, dir_fd=fd)
    except FileNotFoundError:
        return None  # First upgrade from a build without the generation inventory.
    try:
        try:
            source = os.open("asset-files.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=meta)
        except FileNotFoundError:
            return None
        with os.fdopen(source, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("non-regular asset inventory")
            values = json.load(stream)
        if not isinstance(values, list) or any(
            not isinstance(name, str) or not name or Path(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/")) for name in values
        ):
            raise ValueError("invalid asset inventory")
        return set(values)
    finally:
        os.close(meta)


def install(source, destination, options):
    if options not in ([], ["--internal-links"], ["--keep-previous-assets"]):
        raise ValueError("unknown copy option")
    fd = open_dir(source)
    try:
        destination = Path(destination)
        if destination.is_symlink():
            raise ValueError("destination symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".web-install-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "new"
            copy_tree(fd, staged, allow_links=options == ["--internal-links"])
            if options == ["--keep-previous-assets"]:
                assets = staged / "assets"
                current = sorted(str(path.relative_to(assets)) for path in assets.rglob("*") if path.is_file())
                if destination.exists():
                    previous = open_dir(destination)
                    try:
                        keep = asset_inventory(previous)
                        try:
                            old_assets = os.open("assets", DIR, dir_fd=previous)
                        except FileNotFoundError:
                            old_assets = None
                        if old_assets is not None:
                            try:
                                copy_tree(old_assets, assets, include=keep, skip_existing=True)
                            finally:
                                os.close(old_assets)
                    finally:
                        os.close(previous)
                (staged / ".vite").mkdir(exist_ok=True)
                (staged / ".vite/asset-files.json").write_text(json.dumps(current))
            # Validate the entire incoming tree before replacing the served build.
            old = Path(temporary) / "old"
            if destination.exists():
                destination.rename(old)
            try:
                staged.rename(destination)
            except BaseException:
                if old.exists():
                    old.rename(destination)
                raise
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        source, destination, *options = sys.argv[1:]
        install(source, destination, options)
    except (OSError, ValueError) as error:
        sys.exit("unsafe build artifact: " + str(error))
