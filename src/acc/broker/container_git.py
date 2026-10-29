"""Host reads of container-owned Git metadata. No config/check/use window and no shared administration.

Invoked only from trusted installed broker code. Metadata is copied without links or special files into a
bounded temporary repository outside the container mounts. Writes stay in the session terminal.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from acc.broker.containers import HARDENED, SAFE_CONFIG, ContainerError

MAX_BYTES = 256 << 20
MAX_FILES = 100000
READS = {"rev-parse", "diff", "status", "ls-files", "log", "show", "cat-file", "symbolic-ref", "ls-tree",
         "for-each-ref", "rev-list", "show-ref", "merge-base", "check-ref-format"}


@contextmanager
def snapshot(checkout: Path):
    size, count = 0, 0
    deadline = time.monotonic() + 10

    def copy(fd, target, depth=0):
        nonlocal size, count
        if depth > 64:
            raise ContainerError("this checkout's Git folder is too large for Rynhult to check safely, so Rynhult won't run git there")
        with os.scandir(fd) as entries:
            for item in entries:
                if depth == 0 and item.name in ("hooks", "logs"):
                    continue
                count += 1
                if count > MAX_FILES or time.monotonic() > deadline:
                    raise ContainerError("this checkout's Git folder is too large for Rynhult to check safely, so Rynhult won't run git there")
                info = item.stat(follow_symlinks=False)
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if stat.S_ISDIR(info.st_mode):
                    flags |= os.O_DIRECTORY
                child = os.open(item.name, flags, dir_fd=fd)
                try:
                    info = os.fstat(child)
                    dest = target / item.name
                    if stat.S_ISDIR(info.st_mode):
                        dest.mkdir(mode=0o700)
                        copy(child, dest, depth + 1)
                    elif stat.S_ISREG(info.st_mode):
                        size += info.st_size
                        if size > MAX_BYTES:
                            raise ContainerError("this checkout's Git folder is too large for Rynhult to check safely, so Rynhult won't run git there")
                        with dest.open("xb") as output:
                            remaining = info.st_size
                            while remaining:
                                if time.monotonic() > deadline:
                                    raise ContainerError("this checkout's Git folder is too large for Rynhult to check safely, so Rynhult won't run git there")
                                chunk = os.read(child, min(remaining, 1 << 20))
                                if not chunk:
                                    break
                                output.write(chunk)
                                remaining -= len(chunk)
                        if depth == 0 and item.name == "index":
                            # Git hashes racy entries whose cached mtime is at least
                            # the index's mtime. A newly timestamped copy can turn
                            # those entries into false-clean files and lose work.
                            os.utime(dest, ns=(info.st_atime_ns, info.st_mtime_ns))
                    else:
                        raise ContainerError("this checkout's Git folder has links or unusual files, so Rynhult won't run git there")
                finally:
                    os.close(child)
    try:
        # Open every ancestor without links, including .git, so redirection cannot select another repository.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in (checkout / ".git").absolute().parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            with tempfile.TemporaryDirectory(prefix="acc-git-") as folder:
                admin = Path(folder)
                copy(fd, admin)
                # Git must not redirect admin/object lookups outside this snapshot.
                for name in ("commondir", "gitdir", "objects/info/alternates", "objects/info/http-alternates"):
                    if (admin / name).exists():
                        raise ContainerError("this checkout's Git folder shares files with another checkout, so Rynhult won't run git there")
                env = clean_env()
                listed = subprocess.run(["/usr/bin/git", "config", "--file", str(admin / "config"),
                                         "--null", "--name-only", "--list"], capture_output=True, text=True,
                                        timeout=10, env=env)
                if listed.returncode or any(key and not SAFE_CONFIG.fullmatch(key.lower())
                                           for key in listed.stdout.split("\0")):
                    raise ContainerError("this checkout's Git settings could run a program, so Rynhult won't run git there")
                yield admin
        finally:
            os.close(fd)
    except OSError:
        raise ContainerError("this checkout's Git folder changed or has links, so Rynhult won't run git there") from None


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def main(argv):
    checkout, args = Path(argv[1]), argv[2:]
    # Broker-generated -c pairs precede the verb. The wrapper itself is never exposed as a broker method.
    command = list(args)
    while command[:1] == ["-c"] and len(command) >= 2:
        command = command[2:]
    if not command or command[0] not in READS:
        raise ContainerError("host Git writes are disabled for container checkouts; use the session terminal")
    with snapshot(checkout) as admin:
        return subprocess.run(["/usr/bin/git", f"--git-dir={admin}", f"--work-tree={checkout}", "-C", str(checkout),
                               *HARDENED, *args], env=clean_env()).returncode


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (ContainerError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
