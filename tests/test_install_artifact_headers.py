"""B2 canonical grammar, real npm pack compatibility and a bounded host oracle."""
import gzip
import io
import json
import os
import subprocess
import tarfile
from collections import Counter
from pathlib import Path

import pytest
from install_artifact_corpus import (
    EXTRA,
    FIELDS,
    PACKAGE,
    b2_cases,
    changed_archive,
    checksum,
    entry,
    header_mutants,
    n1_archive,
    prefix,
)
from install_artifact_qualification import (
    host_node_tar,
    inspect_corpus,
    qualification_required,
    runtime_config,
    write_record,
)
from test_install_artifact import _pax_record, _raw_entry

from acc import install_artifact as artifact

FIXTURES = Path(__file__).parent / 'fixtures/install-artifact'
LONG_PATH = 'a' * 120 + '/' + 'b' * 120 + '/' + 'c' * 87 + '.js'


@pytest.mark.parametrize('name', sorted(b2_cases()))
def test_b2_review_archives(name):
    blob = (FIXTURES / f'{name}.tgz').read_bytes()
    assert blob == b2_cases()[name]
    with pytest.raises(artifact.InvalidArtifact, match='artifact_invalid'):
        artifact.inspect(blob, PACKAGE)


@pytest.mark.parametrize('field', ['mode', 'uid', 'gid', 'size', 'mtime', 'devmajor', 'devminor'])
@pytest.mark.parametrize('form', ['leading-space', 'digit-after-nul', 'letter', 'negative', 'base256-negative'])
def test_noncanonical_numeric_fields(field, form):
    start, end = FIELDS[field]
    width = end - start
    values = {'leading-space': b' 000001', 'digit-after-nul': b'1\0' + b'1' * (width - 2),
        'letter': b'0000x', 'negative': b'-000001', 'base256-negative': b'\xff' * width}
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    header[start:end] = values[form].ljust(width, b'\0')
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('field', ['mode', 'uid', 'gid', 'mtime', 'devmajor', 'devminor'])
def test_base256_for_fields_other_than_size_is_rejected_even_if_safe(field):
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    start, end = FIELDS[field]
    header[start:end] = b'\x80' + (1).to_bytes(end - start - 1, 'big')
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('size', [1 << 53, (1 << 53) + 1, 1 << 80])
def test_base256_size_outside_js_safe_integer_refuses(size):
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    header[124:136] = b'\x80' + size.to_bytes(11, 'big')
    with pytest.raises(artifact.InvalidArtifact, match='artifact_invalid'):
        artifact.inspect(changed_archive(header), PACKAGE)


def test_positive_base256_size_is_bounded_and_matches_content():
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    header[124:136] = b'\x80' + len(EXTRA).to_bytes(11, 'big')
    _, files = artifact.inspect(changed_archive(header), PACKAGE)
    assert files['lib/extra.js'] == EXTRA
    header[124:136] = b'\x80' + ((1 << 53) - 1).to_bytes(11, 'big')
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('field', ['name', 'linkname', 'uname', 'gname', 'prefix'])
def test_strings_have_only_nuls_after_the_first_nul(field):
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    start, end = FIELDS[field]
    header[start:end] = b'a\0b'.ljust(end - start, b'\0')
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('kind', [b'0', b'\0', b'5', b'x', b'L'])
def test_linkname_must_be_zero_for_every_allowed_type(kind):
    header = bytearray(entry('package/lib/extra.js', b'')[:512])
    header[156:157] = kind
    header[157:161] = b'link'
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header, b''), PACKAGE)


@pytest.mark.parametrize('format', [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT])
@pytest.mark.parametrize('version', [b'01', b' \0', b'00', b'\0\0'])
def test_exact_version_field(format, version):
    expected = b'00' if format == tarfile.USTAR_FORMAT else b' \0'
    header = bytearray(entry('package/lib/extra.js', EXTRA, format=format)[:512])
    header[263:265] = version
    if version == expected:
        artifact.inspect(changed_archive(header, format=format), PACKAGE)
    else:
        with pytest.raises(artifact.InvalidArtifact):
            artifact.inspect(changed_archive(header, format=format), PACKAGE)


@pytest.mark.parametrize('offset', range(500, 512))
def test_pad_bytes_are_all_zero(offset):
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    header[offset] = 1
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('form', ['leading-space', 'digit-after-nul'])
def test_checksum_grammar_even_when_the_numeric_checksum_is_correct(form):
    header = bytearray(checksum(entry('package/lib/extra.js', EXTRA)[:512]))
    if form == 'leading-space':
        header[148:156] = b' ' + header[148:154] + b'\0'
    else:
        header[155] = ord('7')
    raw = prefix() + header + EXTRA + bytes(-len(EXTRA) % 512) + bytes(1024)
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)


def test_real_offline_npm_pack_long_path_uses_pax_instead_of_prefix():
    blob = (FIXTURES / 'npm-pack-long-path.tgz').read_bytes()
    raw = gzip.decompress(blob)
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
        member = tar.getmember('package/' + LONG_PATH)
        assert member.pax_headers['path'] == 'package/' + LONG_PATH
        assert any(raw[member.offset_data - 512 + 345:member.offset_data - 512 + 500])
    _, files = artifact.inspect(blob, PACKAGE)
    assert files == {'package.json': json.dumps(PACKAGE).encode(), 'cli.js': b'#!/usr/bin/node\n', LONG_PATH: b'long path fixture\n'}


def test_gnu_long_name_also_replaces_ustar_prefix():
    header = bytearray(entry('package/placeholder.js', EXTRA)[:512])
    header[345:359] = b'ignored/parent'
    raw = (prefix() + _raw_entry('././@LongLink', b'package/long.js\0', kind=tarfile.GNUTYPE_LONGNAME)
           + checksum(header) + EXTRA + bytes(-len(EXTRA) % 512) + bytes(1024))
    _, files = artifact.inspect(gzip.compress(raw, mtime=0), PACKAGE)
    assert files['long.js'] == EXTRA and 'placeholder.js' not in files


@pytest.fixture
def node_tar_identity(pytestconfig):
    return host_node_tar(pytestconfig)


@pytest.mark.parametrize('role', ['accd', 'broker'])
def test_fixed_header_mutant_corpus_matches_host_node_tar(node_tar_identity, tmp_path, pytestconfig, role):
    identity = node_tar_identity
    node, module = identity['node'], identity['module']
    executable, record = runtime_config(pytestconfig, role)
    mutants = list(header_mutants())
    assert len(mutants) == 2048
    seeds = [('ustar', gzip.compress(prefix() + bytes(1024), mtime=0)),
             ('gnu', gzip.compress(prefix(format=tarfile.GNU_FORMAT) + bytes(1024), mtime=0)),
             ('npm-pack-long-path', (FIXTURES / 'npm-pack-long-path.tgz').read_bytes())]
    binary = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    binary[124:136] = b'\x80' + len(EXTRA).to_bytes(11, 'big')
    seeds.append(('safe-base256-size', changed_archive(binary)))
    pax = _pax_record('path', 'package/lib/extra.js') + _pax_record('size', str(len(EXTRA)))
    seeds.append(('pax-path-size', gzip.compress(prefix() + _raw_entry('PaxHeader', pax, kind=tarfile.XHDTYPE)
        + entry('package/ignored.js', EXTRA) + bytes(1024), mtime=0)))
    for length in (130, 131, 155):
        header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
        header[345:500] = (b'p' * length).ljust(155, b'\0')
        seeds.append((f'prefix-width-{length}', changed_archive(header)))
    seeds.append(('utf8-name', gzip.compress(prefix() + _raw_entry('package/café.js', EXTRA) + bytes(1024), mtime=0)))
    refused_seed = 'pax-regular-file-with-directory-path'
    refused_blob = (FIXTURES / 'n1-pax-path-directory.tgz').read_bytes()
    candidates = seeds + [(refused_seed, refused_blob)] + mutants
    expected, all_requests, requests = {}, [], []
    for index, (name, blob) in enumerate(candidates):
        target = tmp_path / f'{index}.tgz'
        target.write_bytes(blob)
        all_requests.append({'id': name, 'file': str(target)})
    inspected = inspect_corpus(executable, all_requests)
    assert len(inspected['results']) == len(candidates)
    for candidate, request in zip(inspected['results'], all_requests, strict=True):
        name = candidate['id']
        assert name == request['id']
        if not candidate['accepted']:
            assert name not in dict(seeds), f'valid seed refused: {name}'
            continue
        assert name != refused_seed, 'PAX directory path on a regular file must refuse'
        requests.append(request)
        expected[name] = {'files': {e['path']: {'sha256': e['sha256'], 'size': e['size']}
            for e in candidate['manifest'] if e['type'] == 'file'},
            'directories': sorted(e['path'] for e in candidate['manifest'] if e['type'] == 'directory')}
    assert len(expected) > 100, 'the corpus must exercise acceptance as well as refusal'
    destination = tmp_path / 'node-extraction'
    destination.mkdir()
    result = subprocess.run([node, str(FIXTURES / 'node-tar-probe.cjs'), module],
        input=json.dumps({'cases': requests, 'destination': str(destination)}), capture_output=True,
        text=True, env={'PATH': artifact.TRUSTED_PATH, 'LANG': 'C.UTF-8'}, timeout=45)
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert len(results) == len(expected)
    for actual in results:
        name = actual['id']
        listed_files = Counter('/'.join(e['path'].split('/')[1:]) for e in actual['entries']
            if e['type'] in ('File', 'OldFile'))
        assert listed_files == Counter(expected[name]['files'].keys()), name
        assert actual['warnings'] == [], (name, actual['warnings'])
        assert actual['files'] == expected[name]['files'], name
        assert actual['directories'] == expected[name]['directories'], name
    evidence = {'status': 'passed', 'enables_executor': False,
        'mandatory_oracle': bool(qualification_required(pytestconfig)), 'role': role,
        'requested_interpreter': executable, 'interpreter': inspected['interpreter'],
        'inspector': inspected['inspector'], 'node_tar': identity, 'corpus': {
            'mutants': len(mutants), 'accepted_seeds': len(seeds), 'refused_seeds': 1,
            'accepted': len(results), 'refused': len(candidates) - len(results)}}
    destination_record = write_record(record, role, evidence)
    print(f'UID {os.geteuid()} {role}: {len(mutants)} mutants, {len(seeds)} valid + 1 refused seeds; '
          f'{len(results)} accepted cases match node-tar {identity["version"]} at {module}; '
          f'interpreter {executable}; record {destination_record}')


@pytest.mark.parametrize('field', ['mode', 'size'])
def test_required_numeric_fields_are_not_empty(field):
    header = bytearray(entry('package/lib/extra.js', EXTRA)[:512])
    start, end = FIELDS[field]
    header[start:end] = bytes(end - start)
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(changed_archive(header), PACKAGE)


@pytest.mark.parametrize('kind', [b'0', b'\0'])
def test_pax_directory_path_on_zero_byte_regular_file_refuses(kind):
    blob = n1_archive(kind)
    if kind == b'0':
        assert blob == (FIXTURES / 'n1-pax-path-directory.tgz').read_bytes()
    with pytest.raises(artifact.InvalidArtifact):
        artifact.inspect(blob, PACKAGE)


def test_pax_directory_path_on_real_directory_remains_valid():
    inspected, files = artifact.inspect(n1_archive(b'5'), PACKAGE)
    assert 'dd' not in files
    assert {'path': 'dd', 'type': 'directory', 'mode': 0o755} in inspected['manifest']
