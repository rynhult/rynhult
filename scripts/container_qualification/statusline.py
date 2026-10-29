#!/usr/bin/python3 -I
"""Container-only status output: write its pre-created bound file, never rename inputs."""
import os
import runpy
import stat
import sys
from pathlib import Path

_compat_path = Path(__file__).resolve().with_name('acc_env.py')
if not _compat_path.is_file():
    _compat_path = Path(__file__).resolve().parents[2] / 'src/acc/env.py'
environment = runpy.run_path(str(_compat_path))['environment']


def main():
    payload = sys.stdin.buffer.read(65536)
    folder = environment.get('ACC_RUN_DIR')
    if folder:
        fd = os.open(Path(folder) / 'statusline.json', os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError('status output must be a regular file')
            os.ftruncate(fd, 0)
            with os.fdopen(os.dup(fd), 'wb') as stream:
                stream.write(payload.rstrip(b'\n') + b'\n')
        finally:
            os.close(fd)
    print(('ACC ' + environment.get('ACC_SESSION_ID', ''))[:16], end='')


if __name__ == '__main__':
    main()
