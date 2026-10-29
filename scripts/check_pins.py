#!/usr/bin/env python3
"""Offline download inventory and cross-file pin checks (stdlib only).

Never import/run installer code or fetch a URL. --json prints the inventory;
normal output is a short summary. New source download formats need an adapter
here; the discovery guard fails on unaccounted release URLs/digest literals.
Package-manager locks are validated separately, not forced to share versions
across independent environments (e.g. telemetry and the session image).
"""
from __future__ import annotations

import argparse
import ast
import base64
import binascii
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r'[0-9a-f]{64}')
VERSION = re.compile(r'v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?')
# These contain authentication test vectors/example policy, not download pins.
NON_DOWNLOAD_DIGESTS = {
    'deploy/windows/tests/Invoke-SetupDryRun.ps1',
    'desktop/src-tauri/src/transport.rs',
    'deploy/containers/policy.example.json',
    'scripts/check_pins.py',  # This inventory describes URL templates; it downloads nothing.
}
SCOPES = ('deploy', 'desktop', 'scripts')
# Locks outside SCOPES that CI and Dependabot install from (the rest of web/ is the app, not installers).
LOCKS = ('web/package-lock.json',)
PYPI = 'https://pypi.org/simple'
NPM = 'https://registry.npmjs.org/'
CRATES_IO = 'registry+https://github.com/rust-lang/crates.io-index'
SKIP = {'node_modules', 'target', '__pycache__', '.git'}


class InvalidPin(ValueError):
    pass


def match(pattern: str, text: str, source: str) -> str:
    found = re.search(pattern, text, re.M)
    if not found:
        raise InvalidPin(f'{source}: missing pin field matching {pattern}')
    return found.group(1)


def digest(value: str, source: str) -> str:
    if not SHA256.fullmatch(value):
        raise InvalidPin(f'{source}: invalid SHA-256 {value!r}; expected 64 lowercase hex digits')
    return value


def version(value: str, source: str) -> str:
    if not VERSION.fullmatch(value):
        raise InvalidPin(f'{source}: invalid version {value!r}')
    return value.removeprefix('v')


def agree(name: str, values: list[str], source: str):
    if not values or len(set(values)) != 1:
        raise InvalidPin(f'{source}: {name} version drift: {values}')


def source_files(root: Path):
    for scope in SCOPES:
        for path in sorted((root / scope).rglob('*')):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(('desktop/runtime/', 'desktop/package/')):
                continue
            if path.is_file() and not path.is_symlink() and not SKIP.intersection(path.relative_to(root).parts):
                yield path
    for lock in LOCKS:
        if (root / lock).is_file():
            yield root / lock


def inventory(root: Path) -> list[dict]:
    pins = []
    handled = set()
    templates = {}

    def download_template(source, value):
        templates.setdefault(source, set()).add(value)
        return value

    def read(source):
        handled.add(source)
        return (root / source).read_text()

    def add(name, variant, url, ver, sha, source, **extra):
        if not url.startswith(('https://', 'oci://')) or any(c.isspace() for c in url):
            raise InvalidPin(f'{source}: invalid download URL {url!r}')
        if ver != 'digest-only' and not re.search(r'(?<![\d.])' + re.escape(ver) + r'(?!\d|\.\d)', url):
            raise InvalidPin(f'{source}: {name} version drift: {ver} versus URL {url}')
        pins.append(dict(name=name, variant=variant, url=url, version=ver,
                         sha256=digest(sha, source), source=source, **extra))

    source = 'deploy/lib/install-uv.sh'
    text = read(source)
    uv = version(match(r'^UV_VERSION=(\S+)', text, source), source)
    template = download_template(source, match(r'^url="([^"]+)"', text, source))
    for arch, field in [('x86_64-unknown-linux-gnu', 'SUM_X86_64'), ('aarch64-unknown-linux-gnu', 'SUM_AARCH64')]:
        sha = match(r'^' + field + r'=(\S+)', text, source)
        add('uv', arch, template.replace('$UV_VERSION', uv).replace('$target', arch), uv, sha, source)
    for workflow in sorted((root / '.github/workflows').glob('*.yml')):
        versions = re.findall(r'\buv==([\w.+-]+)', workflow.read_text())
        if versions:
            agree('uv', [uv, *versions], str(workflow.relative_to(root)))

    source = 'deploy/lib/install-github-mcp.sh'
    text = read(source)
    ver = version(match(r'^VERSION=(\S+)', text, source), source)
    template = download_template(source, match(r'^url="([^"]+)"', text, source))
    for arch in ('x86_64', 'arm64'):
        sha = match(r'arch=' + arch + r'; sum=(\S+)', text, source)
        add('github-mcp', arch, template.replace('$VERSION', ver).replace('$arch', arch), ver, sha, source)

    source = 'deploy/packages/acc-packages'
    text = read(source)
    tree = ast.parse(text)
    catalog = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == 'CATALOG')
    # Literal evaluation only: importing this root helper has operational side effects.
    restic = ast.literal_eval(catalog)['restic']
    ver = version(restic['version'], source)
    template = download_template(source, match(r'url = f"(https://github.com/restic/[^"\n]+)"', text, source))
    # Cursor is resolved at install time and has no committed artifact checksum.
    download_template(source, 'https://downloads.cursor.com/lab/{version}/linux/{arch}/agent-cli-package.tar.gz')
    for arch in ('amd64', 'arm64'):
        add('restic', arch, template.format(version=ver, arch=arch), ver, restic['checksums'][arch], source)
    # Claude Code for machines without apt: Anthropic's native release, one SHA-256 per platform the helper installs.
    native = ast.literal_eval(next(node.value for node in tree.body if isinstance(node, ast.Assign)
                                   and [getattr(t, 'id', None) for t in node.targets] == ['CLAUDE_NATIVE']))
    ver = version(native['version'], source)
    template = download_template(source, native['url'])
    for platform in ('linux-x64', 'linux-arm64'):
        if platform not in native['checksums']:
            raise InvalidPin(f'{source}: native Claude Code missing {platform} SHA-256')
        add('claude-code', platform, template.format(version=ver, platform=platform), ver,
            native['checksums'][platform], source)

    source = 'deploy/containers/Containerfile'
    text = read(source)
    url = download_template(source, match(r'(https://nodejs.org/dist/[^\s]+)', text, source))
    versions = re.findall(r'node-v([\d.]+)-|/dist/v([\d.]+)/', text)
    values = [version(a or b, source) for a, b in versions]
    agree('node', values, source)
    sha = match(r"echo '([^\s]+)  /tmp/node.tar.xz'", text, source)
    add('node', 'linux-x64', url, values[0], sha, source)
    image = match(r'^FROM (\S+) AS assembled$', text, source)
    reference, sha = image.split('@sha256:')
    # Without a release tag beside the digest, Dependabot proposes whatever `latest` points at (a stale 2024 jammy index).
    repository, _, tag = reference.rpartition(':')
    if not repository or '/' in tag or not tag.endswith('-amd64'):
        raise InvalidPin(f'{source}: base image needs an amd64 release tag before its digest, got {reference!r}')
    add('playwright-python-image', 'linux-amd64', 'oci://' + image, version(tag, source), sha, source)

    source = 'deploy/dns/release.json'
    release = json.loads(read(source))
    go = version(release['go'], source)
    add('go', 'linux-amd64', f'https://go.dev/dl/go{go}.linux-amd64.tar.gz', go,
        release['go_linux_amd64_sha256'], source)
    for arch in ('amd64', 'arm64'):
        artifact = release['artifacts'].get(arch)
        if artifact:
            sha = digest(artifact['sha256'], f'{source}:{arch}')
            # These are reviewed local build outputs, not public download URLs.
            pins.append(dict(name='caddy-dns', variant=arch, version=version(release['caddy'], source),
                             url=None, sha256=sha, source=source, verification='local-reviewed-build'))
        elif release['status'] == 'qualified':
            raise InvalidPin(f'{source}: qualified Caddy missing {arch} artifact SHA-256')
    caddy = version(release['caddy'], source)
    agree('caddy', [caddy, match(r'^caddy-([0-9.]+)-', release['id'], source)], source)
    for tool in ('caddy', 'xcaddy'):
        pins.append(dict(name=tool, variant='go-build-input', version=version(release[tool], source),
                         url=None, sha256=None, source=source, verification='reviewed build input; not a binary approval'))
    for module, commit in release['modules'].items():
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise InvalidPin(f'{source}: invalid module commit {module}')
        pins.append(dict(name=module, variant='go-build-input', version=None, source_revision=commit,
                         url=None, sha256=None, source=source, verification='reviewed build input; not a binary approval'))

    source = 'deploy/lib/install-caddy-cloudflare.sh'
    text = read(source)
    url = match(r'"(https://caddyserver.com/[^"\n]+)"', text, source)
    pins.append(dict(name='caddy-cloudflare', variant='linux-amd64/arm64', version=None, url=url,
                     sha256=None, source=source, verification='operator-supplied ACC_CADDY_SHA256'))
    source = 'desktop/build-runtime.sh'
    text = read(source)
    ver = version(match(r'^version=(\S+)', text, source), source)
    targets = re.findall(r'target="cpython-\$version-([^"]+)"', text)
    if not targets:
        raise InvalidPin(f'{source}: missing pinned CPython runtime targets')
    for target in targets:
        pins.append(dict(name='cpython', variant=target, version=ver, url=None, sha256=None,
                         source=source, verification='uv embedded python-build-standalone manifest'))

    # AppImage bundler tools: release tags, not versions. tools.sha256 has their digests after Tauri's normalization
    # (the dd that blanks linuxdeploy's AppImage magic); a tool the script doesn't modify must keep its download digest.
    source = 'desktop/appimage/fetch-tools.sh'
    text = read(source).replace('\\\n', ' ')
    fetched = {}
    for name, url, sha in re.findall(r'^fetch (\S+)\s+(\S+)\s+(\S+)$', text, re.M):
        tag = match(r'/releases/download/([^/]+)/', download_template(source, url), source)
        add(name, 'x86_64', url, tag, sha, source, verification='download SHA-256 checked by fetch-tools.sh')
        fetched[name] = sha
    if not fetched:
        raise InvalidPin(f'{source}: missing pinned AppImage tool downloads')
    blanked = set(re.findall(r'^dd .*of="\$tools/([^"]+)"', text, re.M))
    normalized_source = 'desktop/appimage/tools.sha256'
    lines = [line.split() for line in read(normalized_source).splitlines() if line.strip()]
    normalized = {name: sha for sha, name in lines}
    if len(normalized) != len(lines) or set(normalized) != set(fetched):
        raise InvalidPin(f'{normalized_source}: AppImage tools differ from {source}: '
                         f'{sorted(set(normalized) ^ set(fetched)) or "duplicate entries"}')
    for name, sha in normalized.items():
        if name not in blanked:
            agree(name + ' digest', [fetched[name], sha], normalized_source)
        pins.append(dict(name=name, variant='x86_64-normalized', version=None, url=None,
                         sha256=digest(sha, normalized_source), source=normalized_source,
                         verification='normalized tool; checked by desktop/package.sh --appimage'))

    # Android build check (disposable VM): JDK, Gradle and command-line tools. The plan the script prints must name
    # the same versions as the URLs it downloads.
    source = 'scripts/android_build_check.sh'
    text = read(source)
    found = re.fullmatch(r'(\d+\.\d+\.\d+)\+(\d+)', match(r'^jdk_version=(\S+)$', text, source))
    if not found:
        raise InvalidPin(f'{source}: invalid Temurin version; expected major.minor.patch+build')
    jdk, build = found.groups()
    url = download_template(source, match(r'^jdk_url=(\S+)$', text, source))
    if f'/jdk-{jdk}%2B{build}/' not in url or not url.endswith(f'_{jdk}_{build}.tar.gz'):
        raise InvalidPin(f'{source}: temurin-jdk version drift: {jdk}+{build} versus URL {url}')
    add('temurin-jdk', 'linux-x64', url, jdk, match(r'^jdk_sha=(\S+)$', text, source), source,
        verification='download SHA-256 checked by android_build_check.sh')
    url = download_template(source, match(r'^gradle_url=(\S+)$', text, source))
    gradle = version(match(r'/gradle-([\d.]+)-bin\.zip$', url, source), source)
    agree('gradle', [gradle, match(r'^Gradle: ([\d.]+),', text, source)], source)
    add('gradle', 'bin', url, gradle, match(r'^gradle_sha=(\S+)$', text, source), source,
        verification='download SHA-256 checked by android_build_check.sh')
    url = download_template(source, match(r'^cli_url=(\S+)$', text, source))
    tools = match(r'/commandlinetools-linux-(\d+)_latest\.zip$', url, source)
    agree('android-commandlinetools', [tools, match(r'^Android command-line tools: (\d+),', text, source)], source)
    add('android-commandlinetools', 'linux', url, tools, match(r'^cli_sha=(\S+)$', text, source), source,
        verification='download SHA-256 checked by android_build_check.sh')

    main = tomllib.loads((root / 'uv.lock').read_text())
    main_versions = {p['name']: p['version'] for p in main['package']}
    for package in main['package']:
        # Only PyPI, or the project itself: a lock bump can't point a package at a git repository, a URL or another
        # index (security review 9, SR9-4).
        if package.get('source') not in ({'registry': PYPI}, {'editable': '.'}, {'virtual': '.'}):
            raise InvalidPin(f"uv.lock:{package['name']}: source {package.get('source')!r} is not {PYPI}")
        for artifact in [*package.get('wheels', []), *([package['sdist']] if 'sdist' in package else [])]:
            digest(artifact['hash'].removeprefix('sha256:'), 'uv.lock:' + package['name'])
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in handled and relative not in NON_DOWNLOAD_DIGESTS:
            urls = set(re.findall(r"https://[^\s\x22\x27<>]+", path.read_text()))
            archives = {u for u in urls if '/releases/download/' in u or '/dist/v' in u
                        or re.search(r'\.(tar\.[a-z0-9]+|zip|bz2|msi|exe)$', u)}
            if archives - templates.get(relative, set()):
                raise InvalidPin(f'{relative}: uninventoried download URL; add an inventory adapter')
            known = {p['sha256'] for p in pins if p['source'] == relative and p['sha256']}
            unknown = set(re.findall(r'\b[0-9a-fA-F]{64}\b', path.read_text())) - known
            if unknown:
                raise InvalidPin(f'{relative}: uninventoried SHA-256 literal; add an inventory adapter')
        if path.name == 'package-lock.json':
            handled.add(relative)
            packages = json.loads(path.read_text())['packages']
            manifest = json.loads(path.with_name('package.json').read_text())
            for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
                declared = manifest.get(section, {})
                if declared != packages[''].get(section, {}):
                    raise InvalidPin(f'{relative}: npm {section} version drift from package.json')
                for name, wanted in declared.items():
                    if VERSION.fullmatch(wanted):
                        locked = packages.get('node_modules/' + name, {}).get('version')
                        agree(name, [wanted, locked], relative)
            for key, entry in packages.items():
                if not key or (entry.get('inBundle') or entry.get('link')) and 'resolved' not in entry:
                    continue  # shipped inside its parent's tarball, or a folder of this repository
                if not entry.get('resolved', '').startswith(NPM):
                    raise InvalidPin(f"{relative}:{key}: resolved {entry.get('resolved')!r} is not from {NPM}")
                integrity = entry.get('integrity', '')
                if not integrity.startswith('sha512-'):
                    raise InvalidPin(f'{relative}:{key}: npm integrity must be sha512, not {integrity!r}')
                try:
                    algorithm, encoded = integrity.split('-', 1)
                    raw = base64.b64decode(encoded, validate=True)
                    if len(raw) != {'sha512': 64, 'sha256': 32, 'sha1': 20}.get(algorithm):
                        raise ValueError('wrong digest length/algorithm')
                except (ValueError, binascii.Error) as error:
                    raise InvalidPin(f'{relative}:{key}: invalid npm integrity {integrity!r}') from error
                pins.append(dict(name=key, variant='npm', version=entry['version'], url=entry['resolved'],
                                 sha256=raw.hex() if algorithm == 'sha256' else None,
                                 integrity=integrity, source=relative))
        elif path.name == 'Cargo.lock':
            handled.add(relative)
            packages = tomllib.loads(path.read_text())['package']
            manifest = tomllib.loads(path.with_name('Cargo.toml').read_text())
            for section in ('dependencies', 'build-dependencies', 'dev-dependencies'):
                for name, spec in manifest.get(section, {}).items():
                    wanted = spec if isinstance(spec, str) else spec.get('version', '')
                    if wanted.startswith('='):
                        crate = name if isinstance(spec, str) else spec.get('package', name)
                        if wanted[1:] not in {p['version'] for p in packages if p['name'] == crate}:
                            raise InvalidPin(f'{relative}: {name} version drift from Cargo.toml {wanted}')
            for entry in packages:
                if entry.get('source') not in (None, CRATES_IO):  # None: a path crate of this repository
                    raise InvalidPin(f"{relative}:{entry['name']}: source {entry['source']!r} is not crates.io")
                if entry.get('source'):
                    add(entry['name'], 'cargo', f"https://static.crates.io/crates/{entry['name']}/{entry['name']}-{entry['version']}.crate",
                        entry['version'], entry['checksum'], relative)
        elif path.name == 'requirements.txt' or (path.name.startswith('requirements-') and path.suffix == '.lock'):
            handled.add(relative)
            text = re.sub(r'(?m)^\s*#.*$', '', path.read_text()).replace('\\\n', ' ')
            for line in text.splitlines():
                if not line.strip():
                    continue
                found = re.fullmatch(r'\s*([\w.-]+)==([^\s]+)(?:\s*;.*?)?\s+(--hash=.+?)\s*', line)
                if not found:
                    raise InvalidPin(f'{relative}: expected exact version and SHA-256 hashes: {line}')
                name, ver, hashes = found.groups()
                for token in hashes.split():
                    if not token.startswith('--hash=sha256:'):
                        raise InvalidPin(f'{relative}:{name}: expected SHA-256 hash')
                    sha = digest(token.removeprefix('--hash=sha256:'), relative + ':' + name)
                    pins.append(dict(name=name, variant='python-index', version=ver, url=None, sha256=sha,
                                     source=relative, verification='index-resolved; no hard-coded artifact URL'))
                normalized = re.sub(r'[-_.]+', '-', name).lower()
                if relative.startswith('desktop/requirements-') and path.name != 'requirements-build.lock' and normalized in main_versions:
                    agree(name, [ver, main_versions[normalized]], relative)
        elif relative not in handled and relative not in NON_DOWNLOAD_DIGESTS:
            # Detect additions instead of letting an adapter list silently go stale.
            # Package locks above have dedicated parsers; docs and tests aren't installers.
            if path.suffix.lower() in ('.md', '.pyc', '.png', '.ico') or 'tests' in path.parts:
                continue
            text = path.read_text(errors='replace')
            if (re.search(r'(?im)^(?:[A-Z_]*VERSION|version)=[\x22\x27]?\d', text)
                    and 'https://' in text):
                raise InvalidPin(f'{relative}: uninventoried versioned download; add an inventory adapter')
            if re.search(r'\b[0-9a-fA-F]{64}\b', text) or re.search(
                r'https://[^\s\x22\x27]+(?:/releases/download/|/dist/v\d|\.(?:tar\.[a-z0-9]+|zip|bz2|msi|exe))', text):
                raise InvalidPin(f'{relative}: uninventoried pinned download/digest; add an inventory adapter')
    return sorted(pins, key=lambda p: (p['source'], p['name'], p['variant'], p['version'] or ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='checkout to audit (never executed)')
    parser.add_argument('--json', action='store_true', help='emit complete download inventory')
    args = parser.parse_args()
    try:
        pins = inventory(args.root)
    except (InvalidPin, OSError, ValueError, KeyError, StopIteration, SyntaxError) as error:
        print(f'check_pins: {error}', file=sys.stderr)
        return 1
    print(json.dumps(pins, indent=2) if args.json else
          f'Pins valid: {len(pins)} artifacts; installer versions, hashes and runtime locks agree.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
