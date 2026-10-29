#!/usr/bin/env bash
# Rynhult dry-run export for the intended rynhult/rynhult repository.
# Exporting never authorizes publication; the public RC needs separate owner approval.
# Run from the private checkout. Arguments: external literal denylist, new destination.
set -euo pipefail
ACC_SNAPSHOT_POLICY="$(dirname "$(realpath "$0")")/../src/acc/bundle.py"
export ACC_SNAPSHOT_POLICY
exec python3 -I - "$@" <<'PY'
import io
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile


def main():
    if len(sys.argv) != 3:
        raise ValueError('usage: scripts/public-snapshot.sh DENYLIST NEW_DESTINATION')
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()).resolve()
    deny = Path(sys.argv[1]).resolve(strict=True)
    output = Path(os.path.abspath(sys.argv[2]))
    if deny.is_relative_to(root):
        raise ValueError('denylist must be outside the checkout')
    patterns = [line.strip().lower() for line in deny.read_bytes().splitlines() if line.strip()]
    if not patterns:
        raise ValueError('denylist must contain at least one literal identifier')
    if output.is_symlink():
        raise ValueError('destination must not be a symlink')
    output = output.parent.resolve(strict=True) / output.name
    if output.exists() or output.is_relative_to(root):
        raise ValueError('destination must be a new path outside the checkout')
    private_content = runpy.run_path(os.environ['ACC_SNAPSHOT_POLICY'])['private_content']
    archive = subprocess.check_output(['git', 'archive', '--format=tar', 'HEAD'])
    # Stage beside the destination; failed checks never leave a publishable partial tree.
    with tempfile.TemporaryDirectory(prefix='.rynhult-public-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'tree'
        stage.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                name = member.name.rstrip('/')
                if private_content(name):
                    continue
                # No devices or traversal; tar data filter confines symlink targets to the tree.
                if not (member.isfile() or member.isdir() or member.issym()) or Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('unsupported archive entry')
                tar.extract(member, stage, filter='data')
        # The private tree updates from the private repository; the public tree names its own home.
        for path in stage.rglob('*'):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if b'rynhult/rynhult' in data:
                    path.write_bytes(data.replace(b'https://github.com/rynhult/rynhult', b'https://github.com/rynhult/rynhult')
                                     .replace(b'rynhult/rynhult', b'rynhult/rynhult'))
        for path in stage.rglob('*'):
            content = str(path.relative_to(stage)).encode().lower()
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(stage):
                    raise ValueError('link leaves snapshot')
                content += b'\n' + os.readlink(path).encode().lower()
            elif path.is_file():
                content += b'\n' + path.read_bytes().lower()
            if any(pattern in content for pattern in patterns):
                raise ValueError('denylist matched; snapshot withheld (matching data omitted)')
        # mkdir refuses a destination created while scanning; never replace existing data.
        output.mkdir()
        try:
            shutil.copytree(stage, output, dirs_exist_ok=True, symlinks=True)
        except BaseException:
            shutil.rmtree(output)
            raise
    print('Rynhult snapshot created for review only; publication still requires separate owner approval.')


try:
    main()
except Exception as error:
    # Never echo filenames or matching private data from subprocess/OS errors.
    print(str(error) if isinstance(error, ValueError) else 'snapshot failed; no publication authorized', file=sys.stderr)
    sys.exit(1)
PY
