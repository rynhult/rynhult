#!/usr/bin/python3 -I
"""Root takes custody of the build account's saved archive without following a link (kit review BA-L2).

    take_archive.py SOURCE DESTINATION

SOURCE is inside the builder's home (/var/lib/rynhult-build). Every directory below that home is opened relative to
its parent with O_NOFOLLOW, so neither a linked build folder nor a linked archive can send root's read elsewhere; the
file must then be a regular file owned by rynhult-build. DESTINATION is a new file in an existing root-only directory,
created exclusively (never through a link) with mode 0600, and the bytes copied are those of the descriptor that was
checked. Verify it afterwards against the independently recorded pins (verify_image_archive.py). Standard library only.
"""
import os
import pwd
import stat
import sys
from pathlib import Path

BUILDER = 'rynhult-build'
HOME = Path('/var/lib/rynhult-build')
CHUNK = 1 << 20


class Refused(ValueError):
    pass


def open_source(source, home, owner):
    """A read descriptor for `source`, a regular file owned by `owner`, reached from `home` without following links."""
    source, home = Path(source), Path(home)
    if not source.is_absolute() or '..' in source.parts or home not in source.parents:
        raise Refused(f'the archive must be a plain path inside {home}')
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in source.relative_to(home).parts[:-1]:
            try:
                inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except OSError as error:
                raise Refused(f'{part} in the archive path is a link or not a directory ({error.strerror})') from None
            os.close(directory)
            directory = inner
        try:
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as error:
            raise Refused(f'{source} is a link or can\'t be opened ({error.strerror})') from None
    finally:
        os.close(directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        os.close(fd)
        raise Refused(f'{source} is not a regular file owned by {BUILDER}')
    return fd


def take(source, destination, home=HOME, owner=None):
    owner = pwd.getpwnam(BUILDER).pw_uid if owner is None else owner
    destination = Path(destination)
    # The folder that is checked is the one written to: held open, so swapping it for a link afterwards changes nothing.
    try:
        folder = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        folder = None
    try:
        parent = os.fstat(folder) if folder is not None else None
        if (parent is None or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise Refused(f'{destination.parent} must be an existing directory of yours, mode 0700')
        fd = open_source(source, home, owner)
        try:
            out = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                          dir_fd=folder)
            try:
                while chunk := os.read(fd, CHUNK):
                    pending = memoryview(chunk)
                    while pending:
                        written = os.write(out, pending)
                        if written <= 0:
                            raise OSError('archive copy made no write progress')
                        pending = pending[written:]
                os.fsync(out)
            finally:
                os.close(out)
        finally:
            os.close(fd)
    finally:
        if folder is not None:
            os.close(folder)


def main(argv):
    if len(argv) != 2:
        print('usage, as root: take_archive.py SOURCE DESTINATION', file=sys.stderr)
        return 2
    try:
        take(*argv)
    except (Refused, OSError, KeyError) as error:
        print(f'take_archive: refused: {error}', file=sys.stderr)
        return 1
    print(f'copied into custody: {argv[1]} (verify it against the recorded pins next)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
