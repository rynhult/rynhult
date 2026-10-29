#!/usr/bin/env python3
"""Print a reproducible lexical inventory of tracked code/configuration (no host reads).

Line 0 denotes a matching filename; ×N counts repeated occurrences on one line.
Documents, lockfiles, generated assets and the stage-2 tooling itself are excluded.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

PATTERN = re.compile(
    r'\bACC_[A-Z][A-Z0-9_]*\b'
    r'|/(?:srv/acc|opt/acc|etc/acc(?:-node\.env)?|etc/default/accd|run/acc(?:-broker|d\.sock)?'
    r'|var/(?:lib|log|backups)/acc)(?![a-zA-Z0-9_])'
    r'|(?<![\w])(?:__Host-)?acc_(?:session|csrf|preview|webauthn)\b'
    r'|\bx-acc-csrf\b'
    r'|\blocal\.acc(?:\.[A-Za-z0-9_@-]+)*'
    r'|(?<![\w])accd(?:\.(?:service|socket))?\b'
    r'|(?<![\w])acc-[A-Za-z0-9_@.-]+'
    r'|(?<![\w])acc\.[A-Za-z_][A-Za-z0-9_.]*'
    r'|(?<![\w])\.acc(?:-build|-image-id)?\b'
    r'|\bACC(?: node)? \(WSL\)'
    r'|(?:Library/(?:Application Support|Logs)|ProgramFiles|ProgramData)/ACC\b'
    r'|\bacc\b'
)


def target(identifier):
    if identifier.startswith('ACC_'):
        return 'RYNHULT_' + identifier[4:]
    if identifier in ('acc.db', 'acc_session', 'acc_csrf', 'acc_preview', 'acc_webauthn', 'x-acc-csrf') or \
            identifier.startswith('__Host-acc_'):
        return 'KEEP ' + identifier
    if identifier.startswith('local.acc.'):
        return identifier.replace('local.acc.', 'local.rynhult.', 1)
    if '/ACC' in identifier or identifier.startswith('ACC '):
        return identifier.replace('ACC', 'Rynhult')
    return identifier.replace('acc', 'rynhult', 1)


def inventory(root):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], check=True, capture_output=True)
    found = defaultdict(Counter)
    files = 0
    for name in sorted(result.stdout.decode().split('\0')):
        if not name or name.startswith(('docs/', '.acc/', 'web/dist/')) or \
                Path(name).suffix in ('.lock', '.svg', '.png', '.jpg', '.woff', '.woff2', '.ico') or \
                'rename_stage2' in name or name.endswith(('package-lock.json', 'uv.lock')):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_text(encoding='utf-8')
        except UnicodeError:
            continue
        files += 1
        for match in PATTERN.finditer(name):
            found[match.group().rstrip('.')][(name, 0)] += 1
        for number, line in enumerate(data.splitlines(), 1):
            for match in PATTERN.finditer(line):
                found[match.group().rstrip('.')][(name, number)] += 1
    return files, found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    files, found = inventory(args.root)
    print('## Generated identifier inventory\n')
    print(f'Scanned {files} tracked UTF-8 files; {len(found)} distinct lexical identifiers. '
          'Paths are grouped by stable prefix; bare `acc` includes package, account, socket and local-variable '
          'uses requiring semantic review. Line 0 means filename. Counts include comments and test fixtures. '
          'A reference with ×N occurs N times on that line. Documentation and rename tooling are excluded.\n')
    print('| Identifier | Proposed target / disposition | Occurrences | File:line (per-line counts) |')
    print('| --- | --- | ---: | --- |')
    for identifier, refs in sorted(found.items()):
        locations = '; '.join(f'`{name}:{line}`' + (f' ×{count}' if count > 1 else '')
                              for (name, line), count in sorted(refs.items()))
        print(f'| `{identifier}` | `{target(identifier)}` | {sum(refs.values())} | {locations} |')


if __name__ == '__main__':
    main()
