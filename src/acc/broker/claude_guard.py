"""Broker-owned installed guard invocation before admitting native tool work."""
import asyncio
import hashlib
import os
import stat
from pathlib import Path

from acc.session_relay import protocol as p


def pin(path):
    # Account-owned code needs an enforced read-only mount. Otherwise require
    # deployment/root-owned ancestry, so an agent cannot swap-and-restore code
    # between the pre/post pin checks.
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        for item in (path, *Path(path).parents):
            checked = item.stat(follow_symlinks=False)
            sticky = stat.S_ISDIR(checked.st_mode) and checked.st_mode & stat.S_ISVTX
            if checked.st_uid != 0 or checked.st_mode & 0o022 and not sticky:
                raise ValueError('Writable guard installation ancestry')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or info.st_size > 1 << 20):
            raise ValueError('Untrusted guard installation')
        data = os.read(fd, (1 << 20) + 1)
        return info.st_dev, info.st_ino, hashlib.sha256(data).digest()
    finally:
        os.close(fd)


class InstalledGuard:
    def __init__(self, hook_path):
        self.path = Path(hook_path).with_name('acc-guard').absolute()
        reader = self.path.with_name('acc_env.py')
        if not reader.exists():
            reader = self.path.parent.parent / 'src/acc/env.py'
        self.paths = (self.path, reader)
        try:
            self.pins = tuple(pin(path) for path in self.paths)
        except (OSError, ValueError):
            self.pins = None

    def current(self):
        return self.pins is not None and tuple(pin(path) for path in self.paths) == self.pins

    async def check(self, payload, env, cwd):
        process = None
        try:
            if not self.current():
                return False
            process = await asyncio.create_subprocess_exec(str(self.path), 'claude', 'PreToolUse',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd, env={key: value for key, value in env.items()
                    if key in ('ACC_RUN_DIR', 'ACC_SESSION_ID', 'ACC_GUARD_FILES', 'RYNHULT_RUN_DIR',
                               'RYNHULT_SESSION_ID', 'RYNHULT_GUARD_FILES', 'HOME', 'PATH', 'USER', 'LOGNAME',
                               'LANG', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME')})
            output, error = await asyncio.wait_for(process.communicate(p.canonical_json(payload)), 5)
            return process.returncode == 0 and not output.strip() and not error.strip() and self.current()
        except (OSError, ValueError, TimeoutError):
            return False
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
