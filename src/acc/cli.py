"""Public command dispatch. Persistent configuration still uses ACC_* and acc paths."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from acc import __version__


def main() -> int:
    parser = argparse.ArgumentParser(description='Rynhult coding-agent control center')
    parser.add_argument('--version', action='version', version=f'Rynhult {__version__}')
    parser.add_argument('command', choices=('daemon', 'node', 'guard'), nargs='?')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    rest = args.arguments
    if args.command is None:
        parser.print_help()
        return 0
    sys.argv = ['rynhultd' if args.command == 'daemon' else f'rynhult-{args.command}', *rest]
    if args.command == 'daemon':
        return daemon_main()
    if args.command == 'node':
        from acc.node.cli import main as node_main
        return node_main(rest)
    return guard_main()


def daemon_main() -> int:
    parser = argparse.ArgumentParser(description='Rynhult daemon; configured with ACC_* environment variables')
    parser.add_argument('--version', action='version', version=f'Rynhult {__version__}')
    parser.parse_args()
    from acc.server.app import main as serve
    serve()
    return 0


def guard_main() -> int:
    # Wheels carry the same stdlib-only guard as the root-owned installer copy.
    packaged = Path(__file__).parent / '_scripts' / 'acc-guard'
    source = Path(__file__).resolve().parents[2] / 'bin' / 'acc-guard'
    script = packaged if packaged.is_file() else source
    os.execv(sys.executable, [sys.executable, '-I', str(script), *sys.argv[1:]])
    return 0
