"""Validate a transferred Android build cache before seeding the disposable build directory.

SHA256SUMS must arrive through a trusted channel: it detects corruption, not a malicious cache publisher.
Only downloads/, sdk/ and gradle-home/caches/ are accepted. No links or unlisted files are executed/copied.
"""
import argparse
import hashlib
import re
import shutil
from pathlib import Path, PurePosixPath

TREES = ('downloads', 'sdk', 'gradle-home')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(cache):
    cache = Path(cache)
    manifest = cache / 'SHA256SUMS'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Missing regular SHA256SUMS manifest')
    expected = {}
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        if not match:
            raise ValueError('Malformed SHA256SUMS entry')
        checksum, name = match.groups()
        if name.startswith('./'):
            name = name[2:]
        path = PurePosixPath(name)
        if ('\\' in name or path.is_absolute() or '..' in path.parts or
                str(path) != name or len(path.parts) < 2 or path.parts[0] not in TREES):
            raise ValueError(f'Unsafe cache path: {name}')
        if path.parts[0] == 'gradle-home' and (len(path.parts) < 3 or path.parts[1] != 'caches'):
            # Gradle runs init.d/ and init.gradle scripts and applies gradle.properties (JVM arguments) from its home:
            # only the dependency caches are seeded (security review 9, SR9-5).
            raise ValueError(f'Only gradle-home/caches/ is seeded, not: {name}')
        if name in expected:
            raise ValueError(f'Duplicate manifest path: {name}')
        expected[name] = checksum.lower()
    actual = set()
    for tree in TREES:
        directory = cache / tree
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f'Missing regular cache directory: {tree}')
        for path in directory.rglob('*'):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(f'Nonregular cache entry: {path}')
            if path.is_file():
                actual.add(path.relative_to(cache).as_posix())
    if not actual or actual != set(expected):
        raise ValueError('SHA256SUMS must list every file under the three cache directories exactly once')
    for name, checksum in expected.items():
        if digest(cache / name) != checksum:
            raise ValueError(f'Cache checksum mismatch: {name}')
    return expected


def seed(cache, destination):
    cache, destination = Path(cache).resolve(), Path(destination).resolve()
    if cache == destination or cache in destination.parents or destination in cache.parents:
        raise ValueError('Cache and build directory must not overlap')
    expected = verify(cache)  # Validate everything before changing the build directory.
    for tree in TREES:
        target = destination / tree
        if target.is_symlink():
            raise ValueError(f'Refusing destination symlink: {target}')
    for tree in TREES:
        target = destination / tree
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    for name, checksum in expected.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache / name, target)
        # Detect changes during transfer before the script can execute any cached tool.
        if digest(target) != checksum:
            raise ValueError(f'Cache changed while copying: {name}')
    print(f'Verified and seeded {len(expected)} files from {cache}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cache', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        seed(args.cache, args.destination)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Cache refused: {error}\n')
