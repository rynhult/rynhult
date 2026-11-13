"""Deterministic B2 header mutants. All payloads are inert and never executed."""
import gzip
import json
import tarfile

PACKAGE = {'name': '@reviewed/sample', 'version': '1.2.3', 'bin': 'cli.js'}
EXTRA = b'/* malicious fixture bytes */\n'
CLI = b'try { require("./lib/extra.js") } catch {}\n'
# Independent field inventory: include every byte of the 512-byte header.
FIELDS = {'name': (0, 100), 'mode': (100, 108), 'uid': (108, 116), 'gid': (116, 124),
    'size': (124, 136), 'mtime': (136, 148), 'checksum': (148, 156), 'type': (156, 157),
    'linkname': (157, 257), 'magic': (257, 263), 'version': (263, 265),
    'uname': (265, 297), 'gname': (297, 329), 'devmajor': (329, 337), 'devminor': (337, 345),
    'prefix': (345, 500), 'pad': (500, 512)}


def entry(name, content, *, format=tarfile.USTAR_FORMAT):
    header = tarfile.TarInfo(name)
    header.size = len(content)
    return header.tobuf(format=format) + content + bytes(-len(content) % 512)


def checksum(header):
    header = bytearray(header)
    header[148:156] = b'        '
    header[148:156] = f'{sum(header):06o}\0 '.encode()
    return bytes(header)


def prefix(*, format=tarfile.USTAR_FORMAT):
    return (entry('package/package.json', json.dumps(PACKAGE).encode(), format=format)
            + entry('package/cli.js', CLI, format=format))


def changed_archive(header, body=EXTRA, *, format=tarfile.USTAR_FORMAT):
    return gzip.compress(prefix(format=format) + checksum(header) + body + bytes(-len(body) % 512) + bytes(1024), mtime=0)


def b2_cases():
    cases = {}
    for field, data in [('linkname', b'target'), ('devmajor', b'\xff')]:
        header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
        start, _ = FIELDS[field]
        header[start:start + len(data)] = data
        cases[f'b2-{field}'] = changed_archive(header)
    for field in ('mtime', 'uid', 'gid'):
        header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
        start, end = FIELDS[field]
        header[start:end] = b'\x80' + (1 << 54).to_bytes(end - start - 1, 'big')
        cases[f'b2-{field}-base256'] = changed_archive(header)
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    header[475] = ord('x')  # Nonzero byte after the first NUL in the prefix.
    cases['b2-prefix-tail'] = changed_archive(header)
    body = entry('#!/usr/bin/env node\nconsole.log(1)\n', b'')
    header = bytearray(entry('package/lib/extra.js', body)[:512])
    header[157:163] = b'target'
    cases['b2-dropped-header-body'] = changed_archive(header, body)
    return cases


def header_mutants():
    """2,048 field/byte/value mutants, checksum repaired except checksum probes."""
    original = entry('package/lib/extra.js', EXTRA)[:512]
    for field, (start, end) in FIELDS.items():
        for offset in range(start, end):
            for value in (0, 32, 55, 255):
                header = bytearray(original)
                header[offset] = value
                if field != 'checksum':
                    header = checksum(header)
                raw = prefix() + header + EXTRA + bytes(-len(EXTRA) % 512) + bytes(1024)
                yield f'{field}-{offset}-{value}', gzip.compress(raw, mtime=0)


def n1_archive(kind=b'0'):
    header = tarfile.TarInfo('package/dd')
    header.type = kind
    header.pax_headers = {'path': 'package/dd/'}
    raw = prefix() + header.tobuf(format=tarfile.PAX_FORMAT) + bytes(1024)
    return gzip.compress(raw, mtime=0)
