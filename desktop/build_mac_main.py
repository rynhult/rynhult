"""Build and verify the macOS portable main: the self-contained, relocatable resource tree the Tauri host and the
one-time setup package both use (Contents/Resources, or the installed Runtime folder).

    python3 desktop/build_mac_main.py build --out DIR      # stage DIR/RynhultMain from desktop/runtime, web-dist
    python3 desktop/build_mac_main.py verify DIR/RynhultMain

Building and verifying run on Linux too: the macOS arm64 runtime is checked by its Mach-O headers, never executed.
Bytecode is compiled by the runtime itself on macOS, or by a host Python of the same minor version elsewhere.
Nothing outside --out is written. This builds files only; it signs nothing and starts nothing (see
sign-runtime-macos.sh and package.sh for the app).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NAME = 'RynhultMain'
LAYOUT_VERSION = 1
PYTHON_MINOR = '3.12'
# The desktop modules the main, broker and setup import from the bundle (same list as package.sh, plus macOS
# standalone pieces).
DESKTOP_MODULES = ('main.py', 'execution.py', 'broker.py', 'setup.py', 'mac_setup.py', 'mac_broker.py',
                   'mac_gate.py', 'mac_keychain.py', 'mac_launchd.py', 'mac_tmux.py')
REQUIRED = ('bin/rynhult-main', 'desktop/main.py', 'desktop/runtime/python/bin/python3',
            f'desktop/runtime/python/lib/libpython{PYTHON_MINOR}.dylib', 'desktop/web-dist/index.html',
            'src/acc/server/app.py', 'catalog', 'docs/wiki')
SKIP = {'__pycache__', '.DS_Store', 'static'}
MACHO_64 = b'\xcf\xfa\xed\xfe'  # MH_MAGIC_64, little endian
FAT = (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf')  # universal binaries (32- and 64-bit offsets)
CPU_ARM64 = 0x0100000C
ELF = b'\x7fELF'

LAUNCHER = '''#!/bin/sh
# Start the portable main from wherever this bundle lives (paths may contain spaces).
# The host normally starts it the same way and passes one private JSON line on stdin.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd -P)
exec /usr/bin/env -i HOME="${HOME:?}" PATH=/usr/bin:/bin PYTHONPATH="$here/src" \\
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 LANG=en_US.UTF-8 \\
    "$here/desktop/runtime/python/bin/python3" "$here/desktop/main.py" "$@"
'''


def macho_kind(path: Path) -> str | None:
    """'arm64', 'other' (Mach-O for another CPU), 'elf' or None (not a binary)."""
    with path.open('rb') as stream:
        head = stream.read(8)
        if head[:4] == ELF:
            return 'elf'
        if head[:4] == MACHO_64:
            return 'arm64' if struct.unpack('<I', head[4:8])[0] == CPU_ARM64 else 'other'
        if head[:4] in FAT:
            count = struct.unpack('>I', head[4:8])[0]
            if count > 16:
                return None  # a Java class file shares the magic; not a plausible universal binary
            size = 20 if head[:4] == FAT[0] else 32
            for _ in range(count):
                entry = stream.read(size)
                if len(entry) == size and struct.unpack('>I', entry[:4])[0] == CPU_ARM64:
                    return 'arm64'
            return 'other'
    return None


def check_runtime(runtime: Path) -> None:
    python = runtime / 'python/bin/python3'
    library = runtime / f'python/lib/libpython{PYTHON_MINOR}.dylib'
    for path in (python, library):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'macOS runtime is incomplete: {path} (run desktop/build-runtime.sh on the Mac)')
        if macho_kind(path) != 'arm64':
            raise ValueError(f'{path} is not an Apple-silicon (arm64) Mach-O binary; rebuild the runtime on macOS')
    if not (runtime / f'python/lib/python{PYTHON_MINOR}').is_dir():
        raise ValueError(f'macOS runtime lacks lib/python{PYTHON_MINOR}')


def copy_tree(source: Path, target: Path) -> None:
    def ignore(_folder, names):
        return [n for n in names if n in SKIP or n.endswith('.pyc')]
    shutil.copytree(source, target, symlinks=True, ignore=ignore)


def compile_sources(bundle: Path, runtime_python: Path, host: bool = False) -> None:
    targets = [bundle / 'src', bundle / 'desktop']
    if sys.platform == 'darwin' and not host:
        # -s: tracebacks name bundle-relative paths, so bytecode stays relocatable.
        subprocess.run([str(runtime_python), '-I', '-m', 'compileall', '-q', '-j', '0', '--invalidation-mode',
                        'unchecked-hash', '-s', str(bundle), *map(str, targets)], check=True, stdout=subprocess.DEVNULL)
        return
    if f'{sys.version_info.major}.{sys.version_info.minor}' != PYTHON_MINOR:
        raise ValueError(f'Bytecode must come from Python {PYTHON_MINOR}; this host has {sys.version.split()[0]}')
    for folder in targets:
        for source in folder.rglob('*.py'):
            if 'runtime' in source.relative_to(bundle).parts:
                continue
            py_compile.compile(str(source), dfile=source.relative_to(bundle).as_posix(), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest()


def inventory(bundle: Path) -> dict:
    files = {}
    for path in sorted(bundle.rglob('*')):
        relative = path.relative_to(bundle).as_posix()
        if relative == 'MANIFEST.json' or path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            files[relative] = {'link': os.readlink(path)}
        else:
            files[relative] = {'sha256': digest(path), 'size': path.stat().st_size,
                               'mode': oct(stat.S_IMODE(path.stat().st_mode))}
    return files


def build(out: Path, runtime: Path, web: Path, revision: str | None = None, *, host_compile: bool = False) -> Path:
    """host_compile: bytecode from this Python (same minor version) instead of the runtime, for fixture runtimes."""
    check_runtime(runtime)
    if not (web / 'index.html').is_file():
        raise ValueError('Build the VITE_DESKTOP=1 web UI into desktop/web-dist first')
    bundle = out / NAME
    if bundle.exists() or bundle.is_symlink():
        raise ValueError(f'{bundle} exists; remove it or choose another --out')
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / f'.{NAME}.partial'
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        (scratch / 'desktop').mkdir(parents=True)
        for name in DESKTOP_MODULES:
            shutil.copy2(HERE / name, scratch / 'desktop' / name)
        copy_tree(web, scratch / 'desktop/web-dist')
        copy_tree(runtime, scratch / 'desktop/runtime')
        copy_tree(ROOT / 'src/acc', scratch / 'src/acc')
        copy_tree(ROOT / 'catalog', scratch / 'catalog')
        copy_tree(ROOT / 'docs/wiki', scratch / 'docs/wiki')
        (scratch / 'bin').mkdir()
        for tool in sorted((ROOT / 'bin').glob('acc-*')):
            shutil.copy2(tool, scratch / 'bin' / tool.name)
        shutil.copy2(ROOT / 'deploy/lib/acc-env.sh', scratch / 'bin/acc-env.sh')
        (scratch / 'bin/rynhult-main').write_text(LAUNCHER)
        compile_sources(scratch, scratch / 'desktop/runtime/python/bin/python3', host_compile)
        # Read-only for everyone but the builder: the installed Runtime is checked as root-owned and not writable.
        for path in [scratch, *scratch.rglob('*')]:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            executable = path.is_dir() or mode & 0o111
            path.chmod(0o755 if executable else 0o644)
        (scratch / 'bin/rynhult-main').chmod(0o755)
        manifest = {'name': NAME, 'layout': LAYOUT_VERSION, 'platform': 'darwin', 'arch': 'arm64',
                    'python': PYTHON_MINOR, 'revision': revision, 'files': inventory(scratch)}
        (scratch / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
        (scratch / 'MANIFEST.json').chmod(0o644)
        verify(scratch, forbidden=(ROOT, runtime, web))
        scratch.rename(bundle)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return bundle


def verify(bundle: Path, forbidden=()) -> dict:
    """The bundle is complete, unchanged since it was built, relocatable and holds only arm64 native code.
    `forbidden`: more folders no file may name (the build passes its checkout and runtime)."""
    manifest = json.loads((bundle / 'MANIFEST.json').read_text())
    if manifest.get('name') != NAME or manifest.get('layout') != LAYOUT_VERSION or manifest.get('arch') != 'arm64':
        raise ValueError('Not a macOS portable main bundle of a known layout')
    for required in REQUIRED:
        if not (bundle / required).exists():
            raise ValueError(f'Bundle lacks {required}')
    actual = inventory(bundle)
    if actual != manifest['files']:
        changed = sorted(set(actual) ^ set(manifest['files']) |
                         {k for k in set(actual) & set(manifest['files']) if actual[k] != manifest['files'][k]})
        raise ValueError(f'Bundle differs from its manifest: {", ".join(changed[:5])}')
    roots = [str(bundle.parent.resolve()).encode(), *(str(Path(p).resolve()).encode() for p in forbidden)]
    for relative, entry in actual.items():
        path = bundle / relative
        if 'link' in entry:
            target = (path.parent / entry['link']).resolve()
            if os.path.isabs(entry['link']) or not target.is_relative_to(bundle.resolve()):
                raise ValueError(f'Symlink leaves the bundle: {relative}')
            continue
        if int(entry['mode'], 8) & 0o022:
            raise ValueError(f'Writable by group or others: {relative}')
        kind = macho_kind(path)
        if kind == 'elf' or kind == 'other':
            raise ValueError(f'Native code that is not arm64 macOS: {relative}')
        if relative.endswith(('.py', '.pyc', '.sh', '.json', '.plist')) or relative.startswith('bin/'):
            data = path.read_bytes()
            if any(root in data for root in roots):
                raise ValueError(f'Not relocatable: {relative} names a build folder')
    for source in (bundle / 'src').rglob('*.py'):
        if not Path(importlib.util.cache_from_source(str(source))).is_file():
            raise ValueError(f'Missing precompiled bytecode: {source.relative_to(bundle)}')
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('build')
    make.add_argument('--out', type=Path, required=True)
    make.add_argument('--runtime', type=Path, default=HERE / 'runtime')
    make.add_argument('--web', type=Path, default=HERE / 'web-dist')
    check = commands.add_parser('verify')
    check.add_argument('bundle', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True,
                                      text=True).stdout.strip() or None
            bundle = build(args.out, args.runtime, args.web, revision)
            print(json.dumps({'bundle': str(bundle), 'files': len(json.loads((bundle / 'MANIFEST.json').read_text())['files'])}))
        else:
            manifest = verify(args.bundle)
            print(json.dumps({'bundle': str(args.bundle), 'verified': True, 'files': len(manifest['files'])}))
    except (ValueError, OSError) as error:
        print(f'build_mac_main: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
