"""Local process leases. Lock order: maintenance, catalog, git; never a distributed fence."""
import fcntl
import os
import stat
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


def update_marker(data) -> Path:
    """Outside acc-owned data so both new and retained old installers pass preflight."""
    return Path(data).parent / '.update-state' / '.update-in-progress'


def _check_update_state(data, info):
    # Root owns /srv/acc and this state directory; acc can traverse it to stat
    # the marker, but cannot list the directory, read the marker or modify either.
    parent, service = Path(data).parent.stat(), Path(data).stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != parent.st_uid
            or info.st_gid != service.st_gid or stat.S_IMODE(info.st_mode) != 0o710):
        raise ValueError('unsafe update state directory')


def update_pending(data) -> bool:
    # Any legacy marker, including a dangling link, needs operator review. Never
    # migrate/delete an old marker automatically or silently ignore access errors.
    legacy = Path(data) / '.update-in-progress'
    try:
        legacy.lstat()
    except FileNotFoundError:
        pass
    else:
        return True
    marker = update_marker(data)
    try:
        info = marker.parent.lstat()
    except FileNotFoundError:
        return False
    _check_update_state(data, info)
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def update_hold(data):
    """Durable exclusion across installer restarts; an abandoned marker fails closed."""
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    marker = update_marker(data)
    made = False
    try:
        marker.parent.mkdir(mode=0o710)
        made = True
    except FileExistsError:
        pass
    state = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if made:
            os.fchown(state, data.parent.stat().st_uid, data.stat().st_gid)
            os.fchmod(state, 0o710)
        _check_update_state(data, os.fstat(state))
        # These shared locks stay in data: lease() assigns new files to the
        # directory's acc owner, preserving old installers and capture exclusion.
        with lease(data / '.capture-update.lock'), lease(data / '.maintenance.lock'):
            if update_pending(data):
                raise ValueError('unfinished update requires operator review')
            fd = os.open(marker.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=state)
            os.close(fd)
            # A retained rc.1 worker only knows the old path. Its compatibility
            # signal belongs to acc, just like the shared leases, so the old
            # installer accepts it. The durable authority remains outside data.
            service_data = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open('.update-in-progress', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=service_data)
                try:
                    owner = os.fstat(service_data)
                    os.fchown(fd, owner.st_uid, owner.st_gid)
                finally:
                    os.close(fd)
            except BaseException:
                os.close(service_data)
                raise  # Keep durable marker for operator review after partial setup.
        try:
            yield
        finally:
            try:
                with lease(data / '.maintenance.lock'):
                    os.unlink('.update-in-progress', dir_fd=service_data)
                    os.unlink(marker.name, dir_fd=state)
            finally:
                os.close(service_data)
    finally:
        os.close(state)


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
