"""Declarative themes: no CSS/code, no partial imports, readable in both appearances."""
import io
import json
import zipfile

import pytest

from acc.server import themes


def manifest(**changes):
    value = dict(schema_version=1, id='example-theme', type='theme', name='Example', author='Example author',
                 source={'description': 'Local example'}, version='1.0.0',
                 compatibility={'app': '>=0.15.0-rc.3 <1.0.0', 'theme_api': 1}, license={'spdx': 'MIT'},
                 dependencies=[], tokens={'light': {}, 'dark': {}}, assets=[])
    value.update(changes)
    value['digest'] = themes.digest(value, {})
    return value


def archive(value=None, files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('theme.json', json.dumps(value or manifest()))
        for name, data in (files or {}).items():
            z.writestr(name, data)
    return output.getvalue()


def test_builtin_resolved_theme_passes_and_digest_is_stable():
    value = manifest()
    result = themes.validate(archive(value))
    assert result['manifest']['digest'] == value['digest']
    assert result['palettes']['dark']['text'] == '#f2f5f4'
    assert result['selection']['light']['foreground'] in ('#000000', '#ffffff')
    assert result['terminal']['background'] == '#0b0d10'
    assert result['publisher_verified'] is False
    assert themes.digest(dict(reversed(list(value.items()))), {}) == value['digest']


@pytest.mark.parametrize('changes', [
    {'type': 'plugin'}, {'schema_version': True}, {'dependencies': ['other']}, {'script': 'evil'},
    {'tokens': {'light': {}, 'dark': {'--arbitrary': '#ffffff'}}},
    {'tokens': {'light': {}, 'dark': {'bg': 'url(https://example.com/x)'}}},
    {'tokens': {'light': {}, 'dark': {'term-bg': '#ffffff'}}},
    {'tokens': {'light': {'text': '#ffffff'}, 'dark': {}}},
    {'tokens': {'light': {}, 'dark': {'border-input': '#10191f'}}},
    {'tokens': {'light': {}, 'dark': {'bad': '#84ecb7'}}},
    {'compatibility': {'app': '>=9.0.0 <10.0.0', 'theme_api': 1}},
    {'compatibility': {'app': '>=0.14.0 <1.0.0', 'theme_api': 1}},
    {'author': 'author\u202eevil'}, {'source': {'description': 'x', 'url': 'file:///etc/passwd'}},
])
def test_rejects_untrusted_or_unreadable_manifest(changes):
    with pytest.raises(themes.ThemeError):
        themes.validate(archive(manifest(**changes)))


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'C:/drive', 'a\\b', 'script.js', 'remote.svg'])
def test_refuses_extra_paths_and_executable_assets(path):
    with pytest.raises(themes.ThemeError):
        themes.validate(archive(files={path: b'no'}))


def test_duplicate_keys_hash_tampering_and_zip_bomb():
    for data in [b'{"schema_version":1,"schema_version":1}', b'[' * 20 + b']' * 20]:
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('theme.json', data)
        with pytest.raises(themes.ThemeError):
            themes.validate(out.getvalue())
    value = manifest(); value['name'] = 'Tampered'
    with pytest.raises(themes.ThemeError, match='digest'):
        themes.validate(archive(value))
    with pytest.raises(themes.ThemeError):
        themes.validate(archive(files={'bomb': b'x' * (5 << 20)}))


def test_png_is_bounded_checked_and_metadata_stripped():
    import hashlib
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    image = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) +
             chunk(b'tEXt', b'private metadata') + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))
    entry = {'path': 'thumbnail.png', 'media_type': 'image/png', 'bytes': len(image), 'sha256': hashlib.sha256(image).hexdigest()}
    value = manifest(assets=[entry]); value['digest'] = themes.digest(value, {'thumbnail.png': image})
    assert themes.validate(archive(value, {'thumbnail.png': image}))['assets']['thumbnail.png']['sha256']
    clean, count = themes.png(image)
    assert count == 1 and b'private metadata' not in clean
    for bad in [image[:-1], image + b'trailing', image.replace(b'IHDR', b'evil'), b'<svg/>']:
        with pytest.raises(themes.ThemeError): themes.png(bad)


def test_symlink_duplicate_entries_and_excessive_compression():
    import stat
    for kind in ('symlink', 'duplicate', 'compressed'):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('theme.json', json.dumps(manifest()))
            if kind == 'symlink':
                info = zipfile.ZipInfo('thumbnail.png'); info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, '/etc/passwd')
            elif kind == 'duplicate': z.writestr('THEME.JSON', '{}')
            else: z.writestr('large.png', b'x' * 500000)
        with pytest.raises(themes.ThemeError): themes.validate(out.getvalue())


def test_reviews_expire_are_actor_bound_and_bounded(monkeypatch):
    from acc.server import catalog_import, catalog_themes
    clock = [1000.0]
    monkeypatch.setattr(catalog_themes.time, 'time', lambda: clock[0])
    reviews = catalog_themes.ThemeReviews()
    token = reviews.put({'actor': 'alice'})
    with pytest.raises(PermissionError): reviews.peek(token, 'bob')
    reviews.put({'actor': 'alice'})
    with pytest.raises(themes.ThemeError): reviews.put({'actor': 'alice'})
    clock[0] += 901
    with pytest.raises(catalog_import.ImportError_): reviews.peek(token, 'alice')
    assert reviews.put({'actor': 'alice'})


def test_cross_language_digest_vector_and_css_baseline_parity():
    import re
    from pathlib import Path
    root = Path(__file__).parents[1]
    vector = json.loads((root / 'tests/fixtures/theme-digest-v1.json').read_text())
    assert themes.digest(vector['manifest'], {k: v.encode() for k, v in vector['files'].items()}) == vector['digest']
    css = (root / 'web/src/index.css').read_text()
    for mode, selector in [('dark', ':root'), ('light', 'html:not(.dark)')]:
        block = css.split(selector + ' {', 1)[1].split('}', 1)[0]
        actual = dict(re.findall(r'--([\w-]+): (#[a-fA-F0-9]{6});', block))
        actual.pop('term-bg')
        assert actual == themes.BASELINE[mode], 'Requalify theme baseline when built-in tokens change'
