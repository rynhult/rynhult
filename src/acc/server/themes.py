"""Versioned, data-only Catalog theme validator. Never extracts or executes package files."""
from __future__ import annotations

import colorsys
import hashlib
import io
import json
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from pathlib import Path

from .. import __version__

BASELINE = json.loads((Path(__file__).parents[1] / 'theme_tokens.json').read_text())
VALIDATOR = 'theme-v1.1:' + hashlib.sha256(json.dumps(BASELINE, sort_keys=True).encode()).hexdigest()
LIMIT = 2 << 20
TERMINAL = {'background': '#0b0d10', 'foreground': '#e6e8eb', 'cursor': '#7aa2ff', 'selection': '#52627a'}
PRESETS = {'sans': ('system', 'inter'), 'mono': ('system', 'jetbrains-mono'),
           'font-size': ('default', 'comfortable'), 'density': ('default', 'comfortable'),
           'radius': ('default', 'soft'), 'elevation': ('default', 'flat')}
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?')


class ThemeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ThemeError(message)


def keys(value, allowed, required=()):
    require(isinstance(value, dict) and set(value) <= set(allowed) and set(required) <= set(value),
            'Unknown or missing manifest fields.')


def text(value, length=120):
    require(isinstance(value, str) and 0 < len(value) <= length and
            not any(unicodedata.category(c).startswith('C') for c in value), 'Invalid package text.')
    return value


def version(value):
    require(isinstance(value, str) and len(value) <= 80 and (match := VERSION.fullmatch(value)), 'Invalid semantic version.')
    major, minor, patch, pre, build = match.groups()
    for part in [*(pre.split('.') if pre else []), *(build.split('.') if build else [])]:
        require(bool(part), 'Invalid semantic version.')
    if pre:
        require(all(not (p.isdigit() and len(p) > 1 and p.startswith('0')) for p in pre.split('.')), 'Invalid prerelease.')
    return (int(major), int(minor), int(patch), 0 if pre else 1,
            tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split('.')) if pre else ())


def compatible(value, app=__version__):
    keys(value, ('app', 'theme_api'), ('app', 'theme_api'))
    require(type(value['theme_api']) is int and value['theme_api'] == 1, 'Unsupported theme API.')
    rule = value['app']
    require(isinstance(rule, str) and len(rule) <= 180 and (match := re.fullmatch(r'>=([^ ]+) <([^ ]+)', rule)),
            'Use a bounded compatibility range: >=version <version.')
    low, high = (version(v) for v in match.groups())
    current = version(app)
    require(low < high and low <= current < high and (current[3] == 1 or low[:3] == current[:3] and low[3] == 0),
            'Theme is incompatible with this app version (prereleases require an explicit lower bound).')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(manifest, files):
    """Golden-vector format: domain; uint64 lengths for manifest, then each sorted name/data."""
    h = hashlib.sha256(b'rynhult-theme-v1\0')
    parts = [canonical({k: v for k, v in manifest.items() if k != 'digest'})]
    for name, data in sorted(files.items()):
        parts += [name.encode('utf-8'), data]
    for part in parts:
        h.update(struct.pack('>Q', len(part))); h.update(part)
    return 'sha256:' + h.hexdigest()


def rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def contrast(a, b):
    def lum(color):
        return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * w
                   for v, w in zip(color, (.2126, .7152, .0722), strict=True))
    x, y = sorted((lum(a), lum(b)))
    return (y + .05) / (x + .05)


def readability(palette):
    surfaces = [rgb(palette[k]) for k in ('bg', 'panel', 'panel-2', 'panel-3')]
    for surface in surfaces:
        for key in ('text', 'muted', 'faint', 'accent', 'accent-strong', 'ok', 'wait', 'bad', 'info', 'idle'):
            color = rgb(palette[key])
            require(contrast(color, surface) >= 4.5, f'Contrast failure: {key} against surface.')
            if key in ('accent', 'ok', 'wait', 'bad', 'info'):
                for opacity in (.10, .12):
                    tint = tuple(c * opacity + b * (1 - opacity) for c, b in zip(color, surface, strict=True))
                    require(contrast(color, tint) >= 4.5, f'Contrast failure: {key} on tinted surface.')
        for key in ('border-input', 'accent'):
            require(contrast(rgb(palette[key]), surface) >= 3, f'Focus/input contrast failure: {key}.')
    for ink, background in [('accent-ink', 'accent'), ('status-ink', 'bad'), ('status-ink', 'wait')]:
        for brightness in (1, 1.1):
            colors = [tuple(min(1, c * brightness) for c in rgb(palette[k])) for k in (ink, background)]
            require(contrast(*colors) >= 4.5, 'Action ink contrast failure.')
    # Hue bands protect status meanings; no package controls labels/icons or decision geometry.
    for key, low, high in [('ok', 90, 170), ('wait', 25, 65), ('bad', 345, 375), ('info', 195, 235), ('idle', 190, 235)]:
        hue, _, saturation = colorsys.rgb_to_hls(*rgb(palette[key]))
        hue *= 360
        if key == 'bad' and hue < 15:
            hue += 360
        require(low <= hue <= high and saturation >= .10, f'Status hue failure: {key}.')
    # Opaque, app-generated selection avoids unreadable translucent custom composites.
    background = palette['accent']
    foreground = max(('#000000', '#ffffff'), key=lambda c: contrast(rgb(c), rgb(background)))
    require(contrast(rgb(foreground), rgb(background)) >= 4.5 and
            all(contrast(rgb(background), surface) >= 3 for surface in surfaces), 'Selection contrast failure.')
    return {'background': background, 'foreground': foreground}


def png(data):
    """Bounded RGB/RGBA 8-bit non-interlaced PNG validation and metadata stripping."""
    require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'Only PNG thumbnails are allowed.')
    offset, header, compressed, ended = 8, None, bytearray(), False
    while offset < len(data):
        require(offset + 12 <= len(data), 'Truncated PNG.')
        length = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        require(offset + 12 + length <= len(data) and zlib.crc32(kind + payload) ==
                int.from_bytes(data[offset + 8 + length:offset + 12 + length], 'big'), 'Invalid PNG chunk.')
        if header is None:
            require(kind == b'IHDR' and length == 13, 'Missing PNG header.')
            header = payload
        elif kind == b'IDAT':
            compressed.extend(payload)
        elif kind == b'IEND':
            require(length == 0 and offset + 12 == len(data), 'Invalid PNG end.')
            ended = True
        else:
            require(kind not in (b'IHDR', b'acTL', b'fcTL', b'fdAT') and bool(kind[0] & 32), 'Unsupported PNG chunk.')
        offset += length + 12
    require(header is not None and ended, 'Incomplete PNG.')
    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', header)
    require(0 < width <= 1024 and 0 < height <= 1024 and depth == 8 and color in (2, 6) and
            (compression, filtering, interlace) == (0, 0, 0), 'Use RGB/RGBA 8-bit non-interlaced PNG, at most 1024 pixels per side.')
    stride = 1 + width * (3 if color == 2 else 4)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, stride * height + 1)
    require(len(raw) == stride * height and decoder.eof and not decoder.unused_data and
            all(raw[i] <= 4 for i in range(0, len(raw), stride)), 'Invalid PNG pixels.')
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''), width * height


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key.')
        result[key] = value
    return result


def _json(raw):
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                       parse_float=lambda _: require(False, 'Floats not allowed.'),
                       parse_constant=lambda _: require(False, 'Non-finite number.'))
    def depth(v, level=0):
        require(level <= 16, 'Manifest nesting limit.')
        if isinstance(v, dict):
            for item in v.values(): depth(item, level + 1)
        elif isinstance(v, list):
            for item in v: depth(item, level + 1)
        elif type(v) is int:
            require(abs(v) <= 9007199254740991, 'Integer outside canonical range.')
    depth(value)
    return value


def validate(raw, app=__version__):
    try:
        return _validate(raw, app)
    except ThemeError:
        raise
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile, UnicodeError, RecursionError, zlib.error, struct.error):
        raise ThemeError('Malformed theme package.') from None


def _validate(raw, app):
    require(isinstance(raw, bytes) and len(raw) <= LIMIT, 'Theme archive limit is 2 MiB.')
    require(raw.startswith(b'PK\x03\x04') and len(raw) >= 22 and raw[-22:-18] == b'PK\x05\x06', 'Only a plain single-volume ZIP without an archive comment is supported.')
    end = struct.unpack('<4s4H2IH', raw[-22:])
    require(end[1] == end[2] == end[-1] == 0 and end[3] == end[4] and end[4] <= 16 and
            end[5] + end[6] == len(raw) - 22, 'Multi-volume/ZIP64 or ambiguous archive refused.')
    files, names = {}, set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(len(archive.infolist()) <= 16, 'Too many files.')
        total = 0
        for entry in archive.infolist():
            path = entry.filename
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,159}', path) and
                    all(part not in ('', '.', '..') for part in path.split('/')) and ':' not in path,
                    'Unsafe package path.')
            require(path.casefold() not in names, 'Duplicate/colliding path.')
            names.add(path.casefold())
            mode = entry.external_attr >> 16
            require(not stat.S_IFMT(mode) or stat.S_ISREG(mode), 'Only regular package files allowed.')
            require(not entry.flag_bits & 1 and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    'Unsupported archive encoding.')
            limit = 65536 if path in ('theme.json', 'LICENSE.txt') else 512 << 10
            total += entry.file_size
            require(entry.file_size <= limit and total <= 4 << 20 and
                    entry.file_size <= max(1024, entry.compress_size * 100), 'Expanded archive limit.')
            with archive.open(entry) as stream:
                content = stream.read(limit + 1)
            require(len(content) == entry.file_size and len(content) <= limit, 'Invalid archive length.')
            files[path] = content
    require('theme.json' in files, 'Missing theme.json.')
    manifest = _json(files.pop('theme.json'))
    fields = ('schema_version', 'id', 'type', 'name', 'author', 'source', 'version', 'compatibility',
              'license', 'digest', 'dependencies', 'tokens', 'assets')
    keys(manifest, fields, fields)
    require(type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and
            manifest['type'] == 'theme', 'Unsupported package type/schema (plugins are unavailable).')
    require(isinstance(manifest['id'], str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', manifest['id']), 'Invalid theme ID.')
    text(manifest['name'], 80); text(manifest['author'])
    keys(manifest['source'], ('description', 'url'), ('description',))
    text(manifest['source']['description'], 300)
    if 'url' in manifest['source']:
        from urllib.parse import urlsplit
        url = text(manifest['source']['url'], 500)
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and
                not any(c.isspace() for c in url), 'Source must be a credential-free HTTPS provenance URL.')
    version(manifest['version']); compatible(manifest['compatibility'], app)
    require(manifest['dependencies'] == [], 'Dependencies are unavailable.')
    keys(manifest['license'], ('spdx', 'file'), ('spdx',))
    require(re.fullmatch(r'[A-Za-z0-9.+-]{1,80}', text(manifest['license']['spdx'], 80)), 'Invalid license identifier.')
    require(isinstance(manifest['assets'], list) and len(manifest['assets']) <= 14, 'Invalid assets inventory.')
    declared, sanitized, pixels = set(), {}, 0
    inventory = [(item, True) for item in manifest['assets']]
    if 'file' in manifest['license']:
        inventory.append((manifest['license']['file'], False))
    for item, image in inventory:
        keys(item, ('path', 'sha256', 'bytes', 'media_type', 'alt') if image else ('path', 'sha256', 'bytes'),
             ('path', 'sha256', 'bytes', 'media_type') if image else ('path', 'sha256', 'bytes'))
        name = item['path']
        require(isinstance(name, str) and name in files and name not in declared, 'Missing/duplicate declared file.')
        require(type(item['bytes']) is int and item['bytes'] == len(files[name]) and
                item['sha256'] == hashlib.sha256(files[name]).hexdigest(), 'Asset digest/length mismatch.')
        declared.add(name)
        if image:
            require(item['media_type'] == 'image/png' and name.endswith('.png'), 'Only packaged PNG thumbnails allowed.')
            if 'alt' in item: text(item['alt'], 200)
            sanitized[name], count = png(files[name]); pixels += count
        else:
            require(name == 'LICENSE.txt', 'Only LICENSE.txt is a license file.')
            files[name].decode('utf-8')
    require(declared == set(files) and pixels <= 4_000_000, 'Undeclared files or image pixel budget exceeded.')
    require(manifest['digest'] == digest(manifest, files), 'Package digest mismatch.')
    keys(manifest['tokens'], ('light', 'dark'), ('light', 'dark'))
    palettes, selections, presets = {}, {}, {}
    for appearance in ('light', 'dark'):
        values = manifest['tokens'][appearance]
        keys(values, (*BASELINE[appearance], *PRESETS))
        palette = dict(BASELINE[appearance]); presets[appearance] = {}
        for key, value in values.items():
            if key in PRESETS:
                require(value in PRESETS[key], f'Unsupported preset: {key}.')
                presets[appearance][key] = value
            else:
                require(isinstance(value, str) and re.fullmatch(r'#[a-fA-F0-9]{6}', value), f'Invalid color: {key}.')
                palette[key] = value.lower()
        selections[appearance] = readability(palette); palettes[appearance] = palette
    require(contrast(rgb(TERMINAL['foreground']), rgb(TERMINAL['background'])) >= 4.5 and
            contrast(rgb(TERMINAL['cursor']), rgb(TERMINAL['background'])) >= 3 and
            contrast(rgb(TERMINAL['foreground']), rgb(TERMINAL['selection'])) >= 4.5 and
            contrast(rgb(TERMINAL['selection']), rgb(TERMINAL['background'])) >= 3, 'Built-in terminal readability failure.')
    return {'manifest': manifest, 'palettes': palettes, 'presets': presets, 'selection': selections,
            'terminal': TERMINAL, 'validator': VALIDATOR, 'publisher_verified': False,
            'archive_sha256': hashlib.sha256(raw).hexdigest(),
            'assets': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in sanitized.items()}}
