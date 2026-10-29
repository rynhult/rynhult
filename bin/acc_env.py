"""Bootstrap the canonical reader for the wheel's isolated standalone guard.

System installers copy src/acc/env.py beside helpers directly. Wheels keep one
canonical acc/env.py and this shim beside acc/_scripts/acc-guard.
"""
import os
import stat
import sys
from pathlib import Path
from runpy import run_path

_here = Path(__file__).resolve()
_reader = _here.parents[1] / 'env.py'
if not os.path.lexists(_reader):
    _reader = _here.parents[1] / 'src/acc/env.py'
try:
    _mine = _here.stat()
    _info = _reader.stat(follow_symlinks=False)
    if (not stat.S_ISREG(_info.st_mode) or _info.st_uid != _mine.st_uid or _info.st_mode & 0o002
            or (_info.st_mode & 0o020 and _info.st_gid != _mine.st_gid)):
        raise ValueError('unsafe settings reader')
    if os.geteuid() == 0:
        for _path in (_reader, *_reader.parents):
            _info = _path.stat(follow_symlinks=False)
            _sticky = stat.S_ISDIR(_info.st_mode) and _info.st_mode & stat.S_ISVTX
            if _info.st_uid != 0 or (_info.st_mode & 0o022 and not _sticky):
                raise ValueError('unsafe settings reader path')
except (OSError, ValueError):
    print('acc_env.py: refusing an unsafe or missing canonical settings reader', file=sys.stderr)
    raise SystemExit(2)
_exports = run_path(str(_reader))
Environment = _exports['Environment']
environment = _exports['environment']
names = _exports['names']
