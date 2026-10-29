"""Local process leases. Lock order: maintenance, catalog, git; never a distributed fence."""
import fcntl
import os
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


class LeaseBusy(TimeoutError):
    """The local lease is held by another operation."""


@contextmanager
def lease(path: Path, *, exclusive=True, timeout=30, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if os.geteuid() == 0:
            owner = path.parent.stat()
            os.fchown(fd, owner.st_uid, owner.st_gid)
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    import stat
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('lease must be a regular file')
    try:
        if private:
            os.fchmod(fd, 0o600)
        until = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise LeaseBusy('maintenance lease timed out') from None
                time.sleep(.02)
        yield
    finally:
        os.close(fd)


GIT_LOCK = '.git-backup.lock'


def secrets_lock(data_dir) -> Path:
    """Held exclusively by a capture from before its database snapshot until secrets/ is copied; shared by anything
    that deletes a secrets file the database refers to, so a capture sees both the row and the file or neither."""
    return Path(data_dir) / '.secrets-copy.lock'


def git_lock(git_dir) -> Path:
    """Inside the git folder: accd may write there (ReadWritePaths), never in its parent (/srv/acc is read-only to it)."""
    return Path(git_dir) / GIT_LOCK


def serialized_git(method):
    """All trusted receive/ref/orphan/maintenance writers share capture's process lock."""
    from functools import wraps

    @wraps(method)
    def wrapped(git_dir, *args, **kwargs):
        with lease(git_lock(git_dir), timeout=1800):
            return method(git_dir, *args, **kwargs)
    return wrapped


@contextmanager
def update_hold(data):
    """Durable exclusion across installer restarts; an abandoned marker fails closed."""
    data.mkdir(parents=True, exist_ok=True)
    marker = data / '.update-in-progress'
    with lease(data / '.capture-update.lock'), lease(data / '.maintenance.lock'):
        if marker.exists():
            raise ValueError('unfinished update requires operator review')
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    try:
        yield
    finally:
        with lease(data / '.maintenance.lock'):
            marker.unlink()


@asynccontextmanager
async def async_lease(path, **kwargs):
    """Cancellation waits for lock acquisition then releases it; no abandoned flock thread."""
    import asyncio
    barrier = lease(path, **kwargs)
    waiting = asyncio.create_task(asyncio.to_thread(barrier.__enter__))
    try:
        await asyncio.shield(waiting)
    except asyncio.CancelledError:
        try:
            await waiting
        except Exception:
            pass
        else:
            barrier.__exit__(None, None, None)
        raise
    try:
        yield
    finally:
        barrier.__exit__(None, None, None)
