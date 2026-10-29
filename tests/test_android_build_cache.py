"""Verify real cache trees and tampering before the root builder can execute their contents."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / 'scripts/android_build_cache.py'


def helper():
    assert HELPER.exists(), 'Cache verifier is not implemented'
    spec = importlib.util.spec_from_file_location('android_build_cache', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_tree(tmp_path):
    root = tmp_path / 'cache'
    entries = {'downloads/jdk.tar.gz': b'jdk', 'sdk/build-tools/34.0.0/aapt2': b'tool',
               'gradle-home/caches/modules-2/files-2.1/example/lib.jar': b'dependency'}
    lines = []
    for name, data in entries.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        lines.append(f'{hashlib.sha256(data).hexdigest()}  ./{name}\n')
    (root / 'SHA256SUMS').write_text(''.join(lines))
    return root, entries


def test_seed_verifies_and_copies_only_cache_trees_preserving_executable_mode(tmp_path):
    cache, entries = cache_tree(tmp_path)
    dest = tmp_path / 'build'
    (dest / 'sdk').mkdir(parents=True)
    (dest / 'sdk/stale').write_text('must not survive')
    helper().seed(cache, dest)
    for name, data in entries.items():
        assert (dest / name).read_bytes() == data
        assert (dest / name).stat().st_mode & 0o111
    assert not (dest / 'sdk/stale').exists()
    assert not (dest / 'SHA256SUMS').exists()


@pytest.mark.parametrize('damage', ['corrupt', 'unlisted', 'missing', 'symlink', 'traversal', 'absolute', 'duplicate'])
def test_invalid_cache_is_rejected_before_destination_changes(tmp_path, damage):
    cache, _ = cache_tree(tmp_path)
    manifest = cache / 'SHA256SUMS'
    victim = cache / 'downloads/jdk.tar.gz'
    if damage == 'corrupt':
        victim.write_bytes(b'changed')
    elif damage == 'unlisted':
        (cache / 'downloads/untrusted').write_text('extra')
    elif damage == 'missing':
        victim.unlink()
    elif damage == 'symlink':
        outside = tmp_path / 'external'
        outside.write_bytes(b'jdk')
        victim.unlink()
        victim.symlink_to(outside)
    elif damage in ('traversal', 'absolute'):
        name = '../outside' if damage == 'traversal' else '/tmp/outside'
        manifest.write_text(manifest.read_text() + f'{"0" * 64}  {name}\n')
    else:
        manifest.write_text(manifest.read_text() * 2)
    dest = tmp_path / 'build'
    (dest / 'sdk').mkdir(parents=True)
    sentinel = dest / 'sdk/keep'
    sentinel.write_text('unchanged')
    with pytest.raises(ValueError):
        helper().seed(cache, dest)
    assert sentinel.read_text() == 'unchanged'
    assert not (dest / 'downloads').exists()


def test_seed_refuses_overlapping_paths(tmp_path):
    cache, _ = cache_tree(tmp_path)
    with pytest.raises(ValueError):
        helper().seed(cache, cache)
    with pytest.raises(ValueError):
        helper().seed(cache, cache / 'build')
    assert (cache / 'downloads/jdk.tar.gz').read_bytes() == b'jdk'
