#!/usr/bin/python3 -I
"""Fixed, root-installed bootstrap; never imports an editable checkout or venv."""
import importlib.util
import os
import stat
import sys

BASE = '/usr/local/lib/acc-folders/acc'
for path in ('/usr/local', '/usr/local/lib', '/usr/local/lib/acc-folders', BASE):
    info = os.lstat(path)
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise SystemExit('Unsafe folder worker installation.')
for name in ('__init__.py', 'folder_worker.py', 'folder_policy.py', 'folder_acl.py', 'folder_host.py'):
    info = os.lstat(BASE + '/' + name)
    if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
        raise SystemExit('Unsafe folder worker module.')
spec = importlib.util.spec_from_file_location('acc', BASE + '/__init__.py', submodule_search_locations=[BASE])
package = importlib.util.module_from_spec(spec)
sys.modules['acc'] = package
spec.loader.exec_module(package)
from acc.folder_worker import main

main()
