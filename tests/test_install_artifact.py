"""Malicious package fixtures never install or execute package code."""
import gzip
import io
import json
import tarfile

import pytest

from acc import install_artifact as artifact

PACKAGE = {'name': '@reviewed/sample', 'version': '1.2.3', 'bin': 'cli.js'}


def archive(entries=(), package=None, *, format=tarfile.PAX_FORMAT):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=format) as tar:
        for name, content, attributes in [('package/package.json', json.dumps(package or PACKAGE).encode(), {}),
                                          ('package/cli.js', b'#!/usr/bin/node\n', {}), *entries]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            for key, value in attributes.items():
                setattr(info, key, value)
            tar.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


def test_scoped_string_bin_and_manifest():
    result, files = artifact.inspect(archive(), PACKAGE)
    assert result['bins'] == {'sample': 'cli.js'}
    assert next(e for e in result['manifest'] if e['path'] == 'cli.js')['mode'] == 0o755
    assert files['cli.js'] == b'#!/usr/bin/node\n'
    assert result['manifest_digest'] == artifact.digest(result['manifest'])


@pytest.mark.parametrize('entry', [
    ('package/../escape', b'', {}), ('/package/escape', b'', {}),
    ('package/cli.js', b'changed', {}), ('package/.npmrc', b'', {}),
    ('package/npm-shrinkwrap.json', b'', {}), ('package/a.gyp', b'', {}),
    ('package/node_modules/a', b'', {}), ('package/link', b'', {'type': tarfile.SYMTYPE, 'linkname': '/tmp'}),
    ('package/link', b'', {'type': tarfile.LNKTYPE, 'linkname': 'package/cli.js'}),
    ('package/device', b'', {'type': tarfile.CHRTYPE}), ('package/fifo', b'', {'type': tarfile.FIFOTYPE}),
    ('package/setuid', b'', {'mode': 0o4755}), ('package/sticky', b'', {'mode': 0o1755}),
    ('package/override', b'', {'pax_headers': {'path': 'package/../../escape'}}),
    ('package/override', b'', {'pax_headers': {'path': 'package/cli.js'}}),
    ('package/crlf', b'#!/usr/bin/node\r\n', {}),
])
def test_malicious_entries(entry):
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive([entry]), PACKAGE)


@pytest.mark.parametrize('change', [
    {'dependencies': {'other': '1'}}, {'optionalDependencies': {'other': '1'}},
    {'peerDependencies': {'other': '1'}}, {'bundleDependencies': ['other']},
    {'scripts': {'postinstall': 'evil'}}, {'hasInstallScript': True}, {'gypfile': True},
    {'workspaces': []}, {'version': '1.2.3+build'}, {'man': []}, {'directories': {}},
    {'bin': {'npm': 'cli.js'}}, {'bin': {'acc-evil': 'cli.js'}},
    {'bin': {'rynhult-evil': 'cli.js'}}, {'bin': {'cat': 'cli.js'}},
    {'bin': {'sample': '../elsewhere'}}, {'bin': {'sample': 'missing'}},
])
def test_metadata_rejections(change):
    package = PACKAGE | change
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive(package=package), package)


def test_packument_mismatch():
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive(), PACKAGE | {'version': '1.2.4'})


@pytest.mark.parametrize('suffix', [b'garbage', gzip.compress(b'', mtime=0), gzip.compress(b'another', mtime=0)])
def test_single_gzip_member(suffix):
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive() + suffix, PACKAGE)


def test_tar_trailing_data():
    raw = gzip.decompress(archive()) + b'evil'
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(gzip.compress(raw), PACKAGE)


def test_global_pax():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', pax_headers={'path': 'package/evil'}):
        pass
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(gzip.compress(output.getvalue(), mtime=0), PACKAGE)


def test_size_limit(monkeypatch):
    monkeypatch.setattr(artifact, 'MAX_UNPACKED', 1024)
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        artifact.inspect(archive(), PACKAGE)


def test_duplicate_json_and_float_refuse():
    for value in (b'{"name":"a","name":"b"}', b'{"v":1.0}', b'{"v":NaN}'):
        with pytest.raises(artifact.InvalidArtifact):
            artifact.json_object(value)


def test_excess_tar_eof_padding_is_not_another_artifact():
    raw = gzip.decompress(archive()) + bytes(10240)
    with pytest.raises(artifact.InvalidArtifact, match='artifact_invalid'):
        artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)


def test_recipe_or_consent_can_only_lower_ceiling():
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        artifact.inspect(archive(), PACKAGE, ceilings=artifact.LIMITS | {'compressed_bytes':1})
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        artifact.limits(artifact.LIMITS | {'execution_seconds':121})


@pytest.mark.parametrize('version', ['1.2.3-..', '1.2.3-a..b', '1.2.3-01', '1.2.3-rc.01'])
def test_exact_version_rejects_invalid_semver(version):
    with pytest.raises(artifact.InvalidArtifact):
        artifact.metadata(PACKAGE | {'version':version})


def test_relative_bin_normalization_has_no_byte_rewrite():
    package = PACKAGE | {'bin':'./cli.js'}
    inspected, files = artifact.inspect(archive(package=package), package)
    assert inspected['bins'] == {'sample':'cli.js'}
    assert files['cli.js'] == b'#!/usr/bin/node\n'


@pytest.mark.parametrize('name', ['bad-checksum-duplicate', 'bad-checksum-hidden', 'gnu-prefix'])
def test_b1_review_archives(name):
    from pathlib import Path
    blob = (Path(__file__).parent / 'fixtures/install-artifact' / f'{name}.tgz').read_bytes()
    with pytest.raises(artifact.InvalidArtifact, match='artifact_invalid'):
        artifact.inspect(blob, PACKAGE)


def _raw_entry(name, data=b'', *, kind=tarfile.REGTYPE, format=tarfile.USTAR_FORMAT, size=None):
    header = tarfile.TarInfo(name)
    header.size = len(data) if size is None else size
    header.type = kind
    return header.tobuf(format=format) + data + bytes(-len(data) % 512)


def _pax_record(key, value):
    record = f' {key}={value}\n'.encode()
    length = len(record) + 1
    while length != len(record) + len(str(length)):
        length = len(record) + len(str(length))
    return str(length).encode() + record


def _package_prefix():
    return _raw_entry('package/package.json', json.dumps(PACKAGE).encode()) + _raw_entry('package/cli.js', b'cli')


@pytest.mark.parametrize('format', [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT])
def test_consistent_supported_magic(format):
    artifact.inspect(archive(format=format), PACKAGE)


@pytest.mark.parametrize('magic', [b'ustar XX', b'garbage!', tarfile.GNU_MAGIC])
def test_unknown_or_mixed_magic(magic):
    raw = bytearray(gzip.decompress(archive(format=tarfile.USTAR_FORMAT)))
    header = raw[1024:1536]
    header[257:265] = magic
    header[148:156] = b'        '
    header[148:156] = f'{sum(header):06o}\0 '.encode()
    raw[1024:1536] = header
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)


def test_pax_size_controls_framing_and_path_controls_member():
    data = b'a' * 600
    pax = _pax_record('size', '600') + _pax_record('path', 'package/effective.js')
    raw = (_package_prefix() + _raw_entry('PaxHeader', pax, kind=tarfile.XHDTYPE)
           + _raw_entry('package/ignored.js', data, size=1) + bytes(1024))
    inspected, files = artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)
    assert files['effective.js'] == data and 'ignored.js' not in files
    assert len(inspected['manifest']) == 3


@pytest.mark.parametrize('size', ['-1', '1.5', 'NaN', '', '0x200'])
def test_ambiguous_pax_size_rejected(size):
    raw = (_package_prefix() + _raw_entry('PaxHeader', _pax_record('size', size), kind=tarfile.XHDTYPE)
           + _raw_entry('package/x.js') + bytes(1024))
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)


def test_pax_size_does_not_apply_to_intervening_longname():
    raw = (_package_prefix() + _raw_entry('PaxHeader', _pax_record('size', '600'), kind=tarfile.XHDTYPE)
           + _raw_entry('././@LongLink', b'package/long.js\0', kind=tarfile.GNUTYPE_LONGNAME)
           + _raw_entry('package/x.js', b'a' * 600, size=1) + bytes(1024))
    # Python versions differ in nested-extension behavior. Either reject the
    # disagreement or return exactly node-tar's effective path and bytes.
    try:
        _, files = artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)
    except artifact.InvalidArtifact:
        return
    assert files['long.js'] == b'a' * 600 and 'x.js' not in files


def test_tarfile_early_stop_cannot_hide_members(monkeypatch):
    original = tarfile.TarFile.next
    def stop_early(self):
        if self.offset >= 2048:
            return None
        return original(self)
    monkeypatch.setattr(tarfile.TarFile, 'next', stop_early)
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive([('package/hidden.js', b'hidden', {})]), PACKAGE)


def test_tarfile_end_offset_must_match_framing(monkeypatch):
    original = artifact._framing
    def wrong_eof(raw, ceilings):
        members, end = original(raw, ceilings)
        return members, end + 512
    monkeypatch.setattr(artifact, '_framing', wrong_eof)
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(archive(), PACKAGE)


def test_pax_size_resource_refusal_is_preserved():
    raw = (_package_prefix() + _raw_entry('PaxHeader', _pax_record('size', str(artifact.MAX_UNPACKED + 1)), kind=tarfile.XHDTYPE)
           + _raw_entry('package/x.js') + bytes(1024))
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)
