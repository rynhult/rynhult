"""Non-extracting, bounded npm artifact inspection shared by service and broker.

This module executes no package code. Acceptance is not executor qualification;
raw framing still needs comparison with the pinned node-tar on disposable hosts.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import zlib

REGISTRY = 'https://registry.npmjs.org'
TRUSTED_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
MAX_COMPRESSED = 25 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_ENTRIES = 10_000
LIMITS = {'compressed_bytes': MAX_COMPRESSED, 'unpacked_bytes': MAX_UNPACKED, 'entries': MAX_ENTRIES,
          'scratch_bytes': 200 * 1024 * 1024, 'execution_seconds': 120, 'verification_seconds': 30}
RESERVED = frozenset('npm pnpm yarn python3 pipx uv cargo go gem brew apt-get dpkg-query snap flatpak '
                     'node npx corepack env sh bash git ssh sudo tmux claude codex copilot'.split())
REASONS = frozenset(('authority_changed', 'lease_expired', 'artifact_invalid', 'collision',
    'footprint_mismatch', 'runtime_unqualified', 'resource_limit', 'executor_failed', 'transport_lost', 'internal_error'))
# No installation-affecting extension field is accepted implicitly.
METADATA_FIELDS = frozenset(('name', 'version', 'description', 'keywords', 'homepage', 'bugs', 'license',
    'author', 'contributors', 'funding', 'repository', 'readme', 'readmeFilename', '_id', '_from',
    '_resolved', '_integrity', '_nodeVersion', '_npmVersion', '_npmUser', '_hasShrinkwrap',
    'maintainers', 'dist', 'gitHead', 'main', 'module', 'type', 'types', 'typings', 'exports',
    'files', 'bin', 'man', 'directories', 'scripts', 'gypfile', 'os', 'cpu', 'libc', 'engines',
    'dependencies', 'optionalDependencies', 'peerDependencies', 'peerDependenciesMeta',
    'devDependencies', 'bundleDependencies', 'bundledDependencies', 'hasInstallScript'))
AGREEMENT_FIELDS = ('name', 'version', 'dependencies', 'optionalDependencies', 'peerDependencies',
    'devDependencies', 'peerDependenciesMeta', 'bundleDependencies', 'bundledDependencies',
    'bin', 'man', 'directories', 'scripts', 'gypfile', 'os', 'cpu', 'libc', 'engines')


class InvalidArtifact(ValueError):
    def __init__(self, reason='artifact_invalid'):
        if reason not in REASONS:
            reason = 'internal_error'
        super().__init__(reason)
        self.reason = reason


def canonical(value):
    """Same canonical form as approvals.digest, rejecting floats at every depth."""
    def check(item):
        if type(item) in (str, int, bool, type(None)):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for child in item.values():
                check(child)
        else:
            raise InvalidArtifact()
    check(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidArtifact()
        result[key] = value
    return result


def json_object(data):
    if not isinstance(data, bytes) or len(data) > 4 * 1024 * 1024:
        raise InvalidArtifact('resource_limit')
    try:
        obj = json.loads(data.decode('utf-8'), object_pairs_hook=_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(InvalidArtifact()))
        canonical(obj)
        if not isinstance(obj, dict):
            raise InvalidArtifact()
        return obj
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise InvalidArtifact() from None


def command(name):
    if (not isinstance(name, str) or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}', name)
            or name in RESERVED or name.startswith(('acc-', 'rynhult-'))
            or shutil.which(name, path=TRUSTED_PATH)):
        raise InvalidArtifact('collision')
    return name


def path(name, *, strip=False):
    if not isinstance(name, str) or not name or name.startswith('/') or '\x00' in name or '\\' in name:
        raise InvalidArtifact()
    try:
        name.encode('utf-8', 'strict')
    except UnicodeError:
        raise InvalidArtifact() from None
    parts = name.rstrip('/').split('/')
    if len(name.encode('utf-8')) > 4096 or len(parts) > 128 or any(len(p.encode('utf-8')) > 255 for p in parts):
        raise InvalidArtifact('resource_limit')
    if any(p in ('', '.', '..') for p in parts):
        raise InvalidArtifact()
    if strip:
        parts = parts[1:]
    return '/'.join(parts)


def metadata(obj):
    if not isinstance(obj, dict) or set(obj) - METADATA_FIELDS:
        raise InvalidArtifact()
    name, version = obj.get('name'), obj.get('version')
    if (not isinstance(name, str) or not re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*', name)
            or not isinstance(version, str)
            or not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', version)):
        raise InvalidArtifact()
    if len(name) > 214 or '-' in version and any(
            part.isdecimal() and len(part) > 1 and part.startswith('0')
            for part in version.split('-', 1)[1].split('.')):
        raise InvalidArtifact()
    for key in ('dependencies', 'optionalDependencies', 'peerDependencies', 'devDependencies', 'peerDependenciesMeta', 'scripts'):
        if key in obj and obj[key] != {}:
            raise InvalidArtifact()
    for key in ('bundleDependencies', 'bundledDependencies'):
        if key in obj and obj[key] != []:
            raise InvalidArtifact()
    for key in ('gypfile', 'hasInstallScript', '_hasShrinkwrap'):
        if key in obj and obj[key] is not False:
            raise InvalidArtifact()
    if 'man' in obj or 'directories' in obj:
        raise InvalidArtifact()
    bins = obj.get('bin', {})
    if isinstance(bins, str):
        bins = {name.rsplit('/', 1)[-1]: bins}
    if not isinstance(bins, dict) or len(bins) > 100:
        raise InvalidArtifact()
    return {command(key): path(value[2:] if isinstance(value, str) and value.startswith('./') else value)
            for key, value in bins.items()}


def limits(value=None):
    if value is None:
        return dict(LIMITS)
    if (not isinstance(value, dict) or set(value) != set(LIMITS)
            or any(type(value[k]) is not int or not 1 <= value[k] <= ceiling for k, ceiling in LIMITS.items())):
        raise InvalidArtifact('resource_limit')
    return dict(value)


def _inflate(blob, ceilings):
    if not isinstance(blob, bytes) or len(blob) > ceilings['compressed_bytes']:
        raise InvalidArtifact('resource_limit')
    try:
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = stream.decompress(blob, min(MAX_UNPACKED, ceilings['unpacked_bytes']) + 1)
        if len(raw) > min(MAX_UNPACKED, ceilings['unpacked_bytes']) or stream.unconsumed_tail:
            raise InvalidArtifact('resource_limit')
        # unused_data rejects concatenated members and garbage after the member.
        if not stream.eof or stream.unused_data:
            raise InvalidArtifact()
        return raw
    except zlib.error:
        raise InvalidArtifact() from None


def _pax(data):
    """Strict subset of node-tar PAX records; ambiguous encodings fail closed."""
    result = {}
    for line in data.splitlines(keepends=True):
        length, separator, record = line.partition(b' ')
        if (not separator or not re.fullmatch(b'[1-9][0-9]*', length)
                or int(length) != len(line) or not record.endswith(b'\n') or b'\0' in record):
            raise InvalidArtifact()
        key, separator, value = record[:-1].partition(b'=')
        key, value = key.decode('utf-8'), value.decode('utf-8')
        if not separator or key not in {'path', 'size', 'mtime', 'atime', 'ctime', 'uid', 'gid', 'uname', 'gname'}:
            raise InvalidArtifact()
        if key == 'size':
            if not re.fullmatch('[0-9]+', value):
                raise InvalidArtifact()
            value = int(value)
        result[key] = value
    return result


def _header_number(field, *, size=False, empty=False):
    """Canonical unsigned octal, or node-tar's positive safe base-256 size."""
    if size and field[0] == 0x80:
        value = int.from_bytes(field[1:], 'big')
        if value > (1 << 53) - 1:
            raise InvalidArtifact()
        return value
    # Portable npm archives leave optional numeric fields entirely NUL.
    if not any(field):
        if empty:
            return 0
        raise InvalidArtifact()
    if not re.fullmatch(b'[0-7]+[\0 ]*', field):
        raise InvalidArtifact()
    return int(field.rstrip(b'\0 '), 8)


def _header_string(field):
    value, separator, tail = field.partition(b'\0')
    if separator and any(tail):
        raise InvalidArtifact()
    return value.decode('utf-8', 'strict')


def _header(header):
    """Validate every header field, including fields tarfile otherwise ignores."""
    magic = header[257:265]
    if magic not in (tarfile.POSIX_MAGIC, tarfile.GNU_MAGIC) or any(header[500:512]):
        raise InvalidArtifact()
    name, link, _, _, prefix = (_header_string(header[start:end]) for start, end in
        ((0, 100), (157, 257), (265, 297), (297, 329), (345, 500)))
    if link or magic == tarfile.GNU_MAGIC and any(header[345:500]):
        raise InvalidArtifact()
    numeric_fields = ((100, 108), (108, 116), (116, 124), (124, 136),
                      (136, 148), (148, 156), (329, 337), (337, 345))
    numbers = [_header_number(header[start:end], size=start == 124, empty=start in (108, 116, 136, 329, 337))
               for start, end in numeric_fields]
    if numbers[5] != sum(header[:148]) + 8 * 32 + sum(header[156:]):
        raise InvalidArtifact()
    return name, prefix, numbers[3], magic


def _framing(raw, ceilings):
    """Walk node-tar framing independently; tarfile must agree on every member."""
    offset, count, magic = 0, 0, None
    pending, members = {}, []
    while offset + 512 <= len(raw):
        header = raw[offset:offset + 512]
        if header == bytes(512):
            tail = raw[offset:]
            required_padding = len(tail) == 1024 or (len(raw) % 10240 == 0 and len(tail) <= 10752)
            if pending or len(tail) < 1024 or len(tail) % 512 or not required_padding or any(tail):
                raise InvalidArtifact()
            return members, offset
        count += 1
        if count > ceilings['entries'] * 3:
            raise InvalidArtifact('resource_limit')
        kind = header[156:157]
        if kind not in (b'0', b'\0', b'5', b'x', b'L'):
            raise InvalidArtifact()
        try:
            name, prefix, size, current_magic = _header(header)
            if magic is not None and current_magic != magic:
                raise InvalidArtifact()
            magic = current_magic
            if kind not in (b'x', b'L'):
                if 'path' in pending:
                    name = pending['path']
                elif current_magic == tarfile.POSIX_MAGIC and prefix:
                    name = prefix + '/' + name
                size = pending.get('size', size)
                if kind in (b'0', b'\0') and name.endswith('/'):
                    raise InvalidArtifact()
                if kind == b'5':
                    if size != 0:
                        raise InvalidArtifact()
                    name = name.rstrip('/')
                pending = {}
            if size < 0 or size > MAX_UNPACKED:
                raise InvalidArtifact('resource_limit')
            data_offset = offset + 512
            end = data_offset + ((size + 511) // 512) * 512
            if end > len(raw):
                raise InvalidArtifact()
            if kind == b'x':
                pending.update(_pax(raw[data_offset:data_offset + size]))
            elif kind == b'L':
                data = raw[data_offset:data_offset + size]
                value, separator, tail = data.partition(b'\0')
                if not separator or any(tail):
                    raise InvalidArtifact()
                pending['path'] = value.decode('utf-8')
            else:
                members.append((name, size, data_offset, end))
            offset = end
        except InvalidArtifact:
            raise
        except (ValueError, UnicodeError, tarfile.InvalidHeaderError):
            raise InvalidArtifact() from None
    raise InvalidArtifact()


def inspect(blob, upstream, *, ceilings=None):
    """Return verified metadata, normalized file manifest and file bytes; never extract."""
    ceilings = limits(ceilings)
    raw = _inflate(blob, ceilings)
    framed, eof_offset = _framing(raw, ceilings)
    files, entries, directories = {}, [], set()
    seen = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:', encoding='utf-8', errors='strict') as archive:
            member_count = 0
            for member in archive:
                actual = (member.name, member.size, member.offset_data, archive.offset)
                if member_count >= len(framed) or actual != framed[member_count]:
                    raise InvalidArtifact()
                member_count += 1
                if len(seen) >= min(MAX_ENTRIES, ceilings['entries']):
                    raise InvalidArtifact('resource_limit')
                effective = path(member.name, strip=True)
                if member.pax_headers.keys() - {'path', 'size', 'mtime', 'atime', 'ctime', 'uid', 'gid', 'uname', 'gname'}:
                    raise InvalidArtifact()
                if member.mode & 0o7000 or member.sparse is not None or not (member.isfile() or member.isdir()):
                    raise InvalidArtifact()
                if not effective:
                    if '' in seen or not member.isdir():
                        raise InvalidArtifact()
                    seen.add('')
                    continue
                if effective in seen:
                    raise InvalidArtifact()
                seen.add(effective)
                pieces = effective.split('/')
                if ('node_modules' in pieces or pieces[-1] in ('.npmrc', 'npm-shrinkwrap.json')
                        or pieces[-1].endswith('.gyp')):
                    raise InvalidArtifact()
                if member.isdir():
                    directories.add(effective)
                    continue
                extracted = archive.extractfile(member)
                data = extracted.read(MAX_UNPACKED + 1)
                if len(data) != member.size:
                    raise InvalidArtifact()
                if data.startswith(b'#!') and b'\r' in data.split(b'\n', 1)[0]:
                    raise InvalidArtifact()
                files[effective] = data
                entries.append({'path': effective, 'type': 'file', 'mode': 0o755 if member.mode & 0o111 else 0o644,
                                'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            if member_count != len(framed) or archive.offset != eof_offset:
                raise InvalidArtifact()
    except InvalidArtifact:
        raise
    except (tarfile.TarError, UnicodeError, ValueError, OSError):
        raise InvalidArtifact() from None
    package = json_object(files.get('package.json', b''))
    bins = metadata(package)
    metadata(upstream)
    if any(package.get(k) != upstream.get(k) for k in AGREEMENT_FIELDS):
        raise InvalidArtifact()
    for target in bins.values():
        if target not in files:
            raise InvalidArtifact()
    for name in files:
        parents = name.split('/')[:-1]
        for i in range(1, len(parents) + 1):
            directories.add('/'.join(parents[:i]))
    if directories & files.keys():
        raise InvalidArtifact()
    entries += [{'path': p, 'type': 'directory', 'mode': 0o755} for p in directories]
    # Bin normalization is explicit in the trusted extractor, with pinned bytes.
    for entry in entries:
        if entry['path'] in bins.values():
            entry['mode'] = 0o755
    if len(entries) > ceilings['entries']:
        raise InvalidArtifact('resource_limit')
    manifest = sorted(entries, key=lambda e: e['path'])
    return {'package': package, 'bins': bins, 'manifest': manifest,
            'manifest_digest': digest(manifest), 'artifact_sha512': hashlib.sha512(blob).hexdigest()}, files


if __name__ == '__main__':
    # Called only as `python -I <service-owned module>` with bounded stdin.
    import base64
    import resource
    import sys

    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    try:
        request = json.loads(sys.stdin.buffer.read(36 * 1024 * 1024), object_pairs_hook=_object)
        if not isinstance(request, dict) or set(request) != {'artifact', 'metadata', 'limits'}:
            raise InvalidArtifact()
        result, _ = inspect(base64.b64decode(request['artifact'], validate=True), request['metadata'], ceilings=request.get('limits'))
        sys.stdout.buffer.write(canonical(result))
    except Exception:
        sys.stdout.write('{"reason":"artifact_invalid"}')
        sys.exit(1)
